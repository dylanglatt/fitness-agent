"""
Delete the current Strava push subscription.

Strava only allows ONE subscription per app, so you have to delete before
re-creating (e.g. when rotating the callback URL or the verify token).
This script lists the existing subscription, prints it, and deletes it.

Usage:
  python scripts/strava_unsubscribe.py           # lists + deletes all
  python scripts/strava_unsubscribe.py --list    # list only, no delete
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import httpx  # noqa: E402

from config import Config  # noqa: E402

BASE = "https://www.strava.com/api/v3/push_subscriptions"


def main():
    list_only = "--list" in sys.argv
    cfg = Config()
    if not (cfg.STRAVA_CLIENT_ID and cfg.STRAVA_CLIENT_SECRET):
        sys.exit("STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET missing from .env")

    params = {
        "client_id": cfg.STRAVA_CLIENT_ID,
        "client_secret": cfg.STRAVA_CLIENT_SECRET,
    }

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(BASE, params=params)
        resp.raise_for_status()
        subs = resp.json()
        if not subs:
            print("No active Strava subscriptions.")
            return

        for sub in subs:
            print(
                f"id={sub.get('id')} callback_url={sub.get('callback_url')} "
                f"created={sub.get('created_at')}"
            )
            if list_only:
                continue
            d = client.delete(f"{BASE}/{sub['id']}", params=params)
            if d.status_code in (200, 204):
                print(f"  → deleted {sub['id']}")
            else:
                print(f"  → delete failed {d.status_code}: {d.text}")


if __name__ == "__main__":
    main()
