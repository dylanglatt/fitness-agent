"""
WHOOP integration — fetches recovery, sleep, strain, and cycle data.
Uses WHOOP API v2 (v1 was discontinued; see https://developer.whoop.com/docs/developing/v1-v2-migration/).
"""

import httpx
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# v2 keeps the version in the path — base is /developer, endpoints live under /v2/...
WHOOP_BASE = "https://api.prod.whoop.com/developer"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


def _persist_refresh_token_to_env(new_token: str) -> None:
    """
    WHOOP rotates refresh tokens — every successful refresh invalidates the old
    one and returns a new one. If we don't persist it, the bot will fail on the
    NEXT restart with a 400 Bad Request because .env still holds the consumed
    token. This rewrites WHOOP_REFRESH_TOKEN in .env in place.
    """
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    try:
        with open(env_path, "r") as f:
            content = f.read()
        pattern = r"^WHOOP_REFRESH_TOKEN=.*$"
        replacement = f"WHOOP_REFRESH_TOKEN={new_token}"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
        else:
            content += f"\n{replacement}\n"
        with open(env_path, "w") as f:
            f.write(content)
    except Exception as e:
        # Don't crash the bot if we can't write the file — just warn loudly.
        logger.error(
            f"Could not persist rotated WHOOP refresh token to .env: {e}. "
            f"Bot will work this session but will 400 on next restart."
        )


class WhoopClient:
    def __init__(self, config):
        self.client_id = config.WHOOP_CLIENT_ID
        self.client_secret = config.WHOOP_CLIENT_SECRET
        self.refresh_token = config.WHOOP_REFRESH_TOKEN
        self._access_token: Optional[str] = None
        self._token_expires_at: int = 0

    async def _ensure_token(self):
        """Refresh access token if expired.

        WHOOP rotates refresh tokens on every exchange, so we must capture the
        new refresh_token from the response, update our in-memory copy, AND
        persist it to .env so the next process start doesn't use a consumed
        token and hit 400 Bad Request.
        """
        if self._access_token and datetime.utcnow().timestamp() < self._token_expires_at - 60:
            return

        async with httpx.AsyncClient() as client:
            resp = await client.post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                # v2 requires the same scopes we originally authorized with;
                # omitting 'scope' here lets WHOOP return the token's existing scopes.
            })
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = int(datetime.utcnow().timestamp()) + expires_in

            # Capture the rotated refresh token so we don't burn it on restart.
            new_refresh = data.get("refresh_token")
            if new_refresh and new_refresh != self.refresh_token:
                self.refresh_token = new_refresh
                _persist_refresh_token_to_env(new_refresh)
                logger.info("WHOOP token refreshed (refresh token rotated and persisted).")
            else:
                logger.info("WHOOP token refreshed.")

    def _headers(self):
        return {"Authorization": f"Bearer {self._access_token}"}

    async def get_recovery(self, days: int = 7) -> list[dict]:
        """Fetch recovery records for the last N days."""
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/recovery",
                headers=self._headers(),
                params={"start": start, "limit": 25},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_sleep(self, days: int = 7) -> list[dict]:
        """Fetch sleep records for the last N days."""
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/activity/sleep",
                headers=self._headers(),
                params={"start": start, "limit": 25},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_strain(self, days: int = 7) -> list[dict]:
        """Fetch strain (cycle) records for the last N days."""
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/v2/cycle",
                headers=self._headers(),
                params={"start": start, "limit": 25},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_today_snapshot(self) -> dict:
        """Get today's recovery, sleep, and strain as a single dict."""
        recovery = await self.get_recovery(days=1)
        sleep = await self.get_sleep(days=1)
        strain = await self.get_strain(days=1)

        return {
            "recovery": recovery[0] if recovery else None,
            "sleep": sleep[0] if sleep else None,
            "strain": strain[0] if strain else None,
        }

    # ── Paginated history iterators ─────────────────────────────────────────
    # v2 paginates with `nextToken`. Max `limit` per request is 25.
    # Use these for backfill + multi-week trend fetches.

    async def _iter_paginated(self, path: str, start: Optional[str], end: Optional[str]):
        """Generic v2 paginator. Yields records one at a time until nextToken is None."""
        await self._ensure_token()
        next_token: Optional[str] = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                params: dict = {"limit": 25}
                if start:
                    params["start"] = start
                if end:
                    params["end"] = end
                if next_token:
                    params["nextToken"] = next_token
                # Token might expire mid-backfill; refresh if needed.
                await self._ensure_token()
                resp = await client.get(
                    f"{WHOOP_BASE}{path}", headers=self._headers(), params=params
                )
                resp.raise_for_status()
                payload = resp.json()
                records = payload.get("records", [])
                for rec in records:
                    yield rec
                next_token = payload.get("next_token") or payload.get("nextToken")
                if not next_token or not records:
                    return

    async def iter_all_recovery(self, start: Optional[str] = None, end: Optional[str] = None):
        async for rec in self._iter_paginated("/v2/recovery", start, end):
            yield rec

    async def iter_all_sleep(self, start: Optional[str] = None, end: Optional[str] = None):
        async for rec in self._iter_paginated("/v2/activity/sleep", start, end):
            yield rec

    async def iter_all_cycles(self, start: Optional[str] = None, end: Optional[str] = None):
        async for rec in self._iter_paginated("/v2/cycle", start, end):
            yield rec

    # ── Row normalizers — raw WHOOP JSON → flat dict ready to upsert ────────

    @staticmethod
    def normalize_recovery(rec: dict) -> tuple[Optional[str], dict]:
        """Return (date, normalized_row) for a v2 recovery record."""
        score = rec.get("score") or {}
        # v2 recoveries are tied to the sleep they're calculated from.
        # The date we index on is the date of the associated sleep end.
        created_at = rec.get("created_at") or rec.get("updated_at") or ""
        date = created_at[:10] if created_at else None
        return date, {
            "recovery_score": score.get("recovery_score"),
            "hrv_rmssd_ms": score.get("hrv_rmssd_milli"),
            "resting_hr": score.get("resting_heart_rate"),
            "spo2_pct": score.get("spo2_percentage"),
            "skin_temp_c": score.get("skin_temp_celsius"),
            "cycle_id": str(rec.get("cycle_id")) if rec.get("cycle_id") is not None else None,
            "sleep_id": rec.get("sleep_id"),
        }

    @staticmethod
    def normalize_sleep(rec: dict) -> tuple[Optional[str], dict]:
        score = rec.get("score") or {}
        stage = score.get("stage_summary") or {}
        # Use the end of the sleep period as the "date" so naps late at night
        # don't fight with the morning main sleep. If both end on the same day,
        # upsert keeps the most recent.
        end = rec.get("end") or rec.get("start") or ""
        date = end[:10] if end else None
        to_hours = lambda ms: round((ms or 0) / 3_600_000, 2) if ms else None
        return date, {
            "total_in_bed_hours": to_hours(stage.get("total_in_bed_time_milli")),
            "total_asleep_hours": to_hours(
                (stage.get("total_in_bed_time_milli") or 0)
                - (stage.get("total_awake_time_milli") or 0)
            ),
            "sleep_efficiency_pct": score.get("sleep_efficiency_percentage"),
            "sleep_performance_pct": score.get("sleep_performance_percentage"),
            "sleep_consistency_pct": score.get("sleep_consistency_percentage"),
            "disturbance_count": score.get("num_disturbances"),
            "rem_hours": to_hours(stage.get("total_rem_sleep_time_milli")),
            "sws_hours": to_hours(stage.get("total_slow_wave_sleep_time_milli")),
            "light_hours": to_hours(stage.get("total_light_sleep_time_milli")),
            "sleep_id": rec.get("id"),
        }

    @staticmethod
    def normalize_cycle(rec: dict) -> tuple[Optional[str], dict]:
        score = rec.get("score") or {}
        start = rec.get("start") or ""
        date = start[:10] if start else None
        return date, {
            "strain": score.get("strain"),
            "kilojoule": score.get("kilojoule"),
            "average_hr": score.get("average_heart_rate"),
            "max_hr": score.get("max_heart_rate"),
            "cycle_id": str(rec.get("id")) if rec.get("id") is not None else None,
        }

    def summarize_recovery(self, record: dict) -> str:
        """Readable summary of a recovery record."""
        if not record:
            return "No recovery data."
        score = record.get("score", {})
        recovery_score = score.get("recovery_score", "N/A")
        hrv = round(score.get("hrv_rmssd_milli", 0), 1)
        rhr = score.get("resting_heart_rate", "N/A")
        return f"Recovery: {recovery_score}% | HRV: {hrv}ms | RHR: {rhr} bpm"

    def summarize_sleep(self, record: dict) -> str:
        """Readable summary of a sleep record."""
        if not record:
            return "No sleep data."
        score = record.get("score", {})
        efficiency = score.get("sleep_efficiency_percentage", "N/A")
        stage_summary = record.get("score", {}).get("stage_summary", {})
        total_ms = stage_summary.get("total_in_bed_time_milli", 0)
        total_hours = round(total_ms / 3_600_000, 1)
        disturbances = score.get("num_disturbances", "N/A")
        return f"Sleep: {total_hours}h | Efficiency: {efficiency}% | Disturbances: {disturbances}"
