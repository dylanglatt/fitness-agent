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

    async def get_activity_detail(self, activity_id: int) -> dict:
        """Fetch full detail for a single activity."""
        await self._ensure_token()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{STRAVA_BASE}/activities/{activity_id}",
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
            resp.raise_for_status()
            return resp.json()

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
