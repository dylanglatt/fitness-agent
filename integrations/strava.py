"""
Strava integration — fetches recent activities.
Handles OAuth token refresh automatically.
"""

import httpx
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

STRAVA_BASE = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"


class StravaClient:
    def __init__(self, config):
        self.client_id = config.STRAVA_CLIENT_ID
        self.client_secret = config.STRAVA_CLIENT_SECRET
        self.refresh_token = config.STRAVA_REFRESH_TOKEN
        self._access_token: Optional[str] = None
        self._token_expires_at: int = 0

    async def _ensure_token(self):
        """Refresh access token if expired."""
        if self._access_token and datetime.utcnow().timestamp() < self._token_expires_at - 60:
            return

        async with httpx.AsyncClient() as client:
            resp = await client.post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            })
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            self._token_expires_at = data["expires_at"]
            logger.info("Strava token refreshed.")

    async def get_recent_activities(self, days: int = 7) -> list[dict]:
        """Fetch activities from the last N days."""
        await self._ensure_token()
        after = int((datetime.utcnow() - timedelta(days=days)).timestamp())

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{STRAVA_BASE}/athlete/activities",
                headers={"Authorization": f"Bearer {self._access_token}"},
                params={"after": after, "per_page": 50},
            )
            resp.raise_for_status()
            activities = resp.json()

        logger.info(f"Fetched {len(activities)} Strava activities.")
        return activities

    async def iter_all_activities(self, after: Optional[int] = None, per_page: int = 200):
        """
        Paginate through every activity from `after` (epoch seconds) to now.
        Strava caps per_page at 200; we walk `page` until we get an empty page.
        Yields activities one at a time.
        """
        await self._ensure_token()
        page = 1
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                await self._ensure_token()
                params: dict = {"per_page": per_page, "page": page}
                if after is not None:
                    params["after"] = after
                resp = await client.get(
                    f"{STRAVA_BASE}/athlete/activities",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    params=params,
                )
                resp.raise_for_status()
                activities = resp.json()
                if not activities:
                    return
                for act in activities:
                    yield act
                # Last page is signaled by getting fewer than per_page results.
                if len(activities) < per_page:
                    return
                page += 1

    async def get_activity_detail(self, activity_id: int) -> dict:
        """Fetch full detail for a single activity.

        The Detailed activity response includes fields the list endpoint
        omits — most importantly `average_heartrate` and `max_heartrate`.
        Without this enrichment, all HR columns end up null.
        """
        await self._ensure_token()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{STRAVA_BASE}/activities/{activity_id}",
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_activity_zones(self, activity_id: int) -> list[dict] | None:
        """Fetch the per-zone time distribution for an activity.

        Returns the raw Strava response (a list of zone-type dicts, each
        with `distribution_buckets`). Returns None on any failure — zones
        are optional context, never worth crashing the caller for. Activities
        without a paired HR sensor will return an empty list (200 OK with []),
        which we also normalize to None so downstream code can treat
        "no zones" uniformly.
        """
        await self._ensure_token()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{STRAVA_BASE}/activities/{activity_id}/zones",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
        except Exception as e:
            logger.debug(f"Strava zones fetch failed for {activity_id}: {e}")
            return None
        if resp.status_code != 200:
            logger.debug(
                f"Strava zones {activity_id} returned {resp.status_code}: {resp.text[:200]}"
            )
            return None
        data = resp.json() or []
        return data or None

    async def enrich_activity(
        self, activity: dict, *, fetch_zones: bool = True
    ) -> dict:
        """Take a Summary activity dict and return an enriched copy with
        Detailed fields merged in (for HR), plus optionally the HR-zone
        distribution stored under the `_zones` key for downstream use.

        Use this in any code path that gets activities from the list endpoint
        and writes them to the database — it's the difference between Notion
        rows with an Avg HR and rows without.

        `fetch_zones=False` skips the second API call. Useful when you want
        HR but the zones data isn't critical and you want to halve API cost.
        """
        activity_id = activity.get("id")
        if not activity_id:
            return activity
        out = dict(activity)  # shallow copy so caller's dict isn't mutated
        try:
            detail = await self.get_activity_detail(int(activity_id))
            if detail:
                # Detailed is a superset of Summary; merge with detail winning.
                out = {**out, **detail}
        except Exception as e:
            logger.debug(f"Enrichment detail fetch failed for {activity_id}: {e}")
        if fetch_zones:
            zones = await self.get_activity_zones(int(activity_id))
            if zones is not None:
                # Stash under "_zones" inside the same dict that gets serialized
                # to raw_json — backfill_notion.py reads this back.
                out["_zones"] = zones
        return out

    def summarize_activity(self, activity: dict) -> str:
        """Convert a raw Strava activity dict into a readable summary string."""
        name = activity.get("name", "Activity")
        sport = activity.get("sport_type", activity.get("type", "Unknown"))
        date = activity.get("start_date_local", "")[:10]
        distance_km = round(activity.get("distance", 0) / 1000, 2)
        duration_min = round(activity.get("moving_time", 0) / 60, 1)
        avg_hr = activity.get("average_heartrate")
        max_hr = activity.get("max_heartrate")
        elevation = activity.get("total_elevation_gain", 0)

        parts = [f"{date} | {sport}: {name}"]
        if distance_km > 0:
            parts.append(f"{distance_km}km")
        parts.append(f"{duration_min} min")
        if avg_hr:
            parts.append(f"avg HR {avg_hr} bpm")
        if max_hr:
            parts.append(f"max HR {max_hr} bpm")
        if elevation > 0:
            parts.append(f"{elevation}m elevation")

        return " | ".join(parts)
