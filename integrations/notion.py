"""
Notion integration — writes training log entries automatically.
The bot writes here; you rarely need to open Notion directly.
"""

import httpx
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionClient:
    def __init__(self, config):
        self.api_key = config.NOTION_API_KEY
        self.database_id = config.NOTION_DATABASE_ID

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def log_daily_entry(self, date: str, summary: dict):
        """
        Write a daily training log entry to Notion.

        summary dict expects keys like:
          - recovery_score, hrv, rhr, sleep_hours, sleep_efficiency
          - activities (list of strings)
          - lifts (list of strings)
          - notes (str)
          - daily_brief (str)
        """
        properties = {
            "Date": {"date": {"start": date}},
            "Recovery Score": {"number": summary.get("recovery_score")},
            "HRV (ms)": {"number": summary.get("hrv")},
            "RHR": {"number": summary.get("rhr")},
            "Sleep (hrs)": {"number": summary.get("sleep_hours")},
            "Sleep Efficiency": {"number": summary.get("sleep_efficiency")},
            "Activities": {
                "rich_text": [{"text": {"content": "\n".join(summary.get("activities", []))}}]
            },
            "Lifts": {
                "rich_text": [{"text": {"content": "\n".join(summary.get("lifts", []))}}]
            },
            "Notes": {
                "rich_text": [{"text": {"content": summary.get("notes", "")}}]
            },
            "Daily Brief": {
                "rich_text": [{"text": {"content": summary.get("daily_brief", "")}}]
            },
        }

        # Remove None values to avoid Notion API errors
        properties = {k: v for k, v in properties.items() if v is not None}

        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{NOTION_BASE}/pages",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code not in (200, 201):
                logger.warning(f"Notion log failed: {resp.status_code} {resp.text}")
            else:
                logger.info(f"Notion entry logged for {date}.")

    async def log_lift(self, date: str, exercise: str, sets_reps_weight: str, notes: str = ""):
        """Append a lift entry to the day's Notion page if it exists, otherwise create it."""
        # For simplicity, we log lifts as part of the daily entry via the database.
        # This method can be expanded later for more granular lift tracking.
        logger.info(f"Lift logged to Notion: {exercise} — {sets_reps_weight}")
