"""
WHOOP integration — fetches recovery, sleep, strain, and cycle data.
Uses WHOOP API v1.
"""

import httpx
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

WHOOP_BASE = "https://api.prod.whoop.com/developer/v1"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


class WhoopClient:
    def __init__(self, config):
        self.client_id = config.WHOOP_CLIENT_ID
        self.client_secret = config.WHOOP_CLIENT_SECRET
        self.refresh_token = config.WHOOP_REFRESH_TOKEN
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
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = int(datetime.utcnow().timestamp()) + expires_in
            logger.info("WHOOP token refreshed.")

    def _headers(self):
        return {"Authorization": f"Bearer {self._access_token}"}

    async def get_recovery(self, days: int = 7) -> list[dict]:
        """Fetch recovery records for the last N days."""
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/recovery",
                headers=self._headers(),
                params={"start": start},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_sleep(self, days: int = 7) -> list[dict]:
        """Fetch sleep records for the last N days."""
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/activity/sleep",
                headers=self._headers(),
                params={"start": start},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])

    async def get_strain(self, days: int = 7) -> list[dict]:
        """Fetch strain (cycle) records for the last N days."""
        await self._ensure_token()
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WHOOP_BASE}/cycle",
                headers=self._headers(),
                params={"start": start},
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
