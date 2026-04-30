"""
One-time Strava webhook subscription helper.

Strava's push model requires a single per-app subscription that maps your
app's client_id/client_secret to a publicly-reachable callback_url. Strava
then POSTs activity events to that URL. Only ONE subscription exists per
app at a time — you cannot re-run this while one is already active; use
scripts/strava_unsubscribe.py first if you need to rotate the URL or token.

Handshake (Strava doc — Webhooks v1):

1. POST /push_subscriptions with:
     - client_id, client_secret
     - callback_url   (must be HTTPS, publicly reachable)
     - verify_token   (random string you choose)
2. Strava immediately GETs your callback_url with:
     ?hub.mode=subscribe&hub.challenge=<tok>&hub.verify_token=<yours>
   Your server (integrations/webhook_server.py::_strava_verify) must
   respond 200 with JSON {"hub.challenge": "<tok>"} iff the verify_token
   matches. If that succeeds, Strava returns a subscription id.
3. From then on, Strava POSTs aspect_type/object_id events to the same URL.

Usage:
  1. Set STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, WEBHOOK_PUBLIC_URL, and
     STRAVA_WEBHOOK_VERIFY_TOKEN in .env.
  2. Make sure the bot is RUNNING and reachable — Strava tests the callback
     as part of the POST, so your GET /webhooks/strava route needs to be
     live when this script runs. Run `python main.py` in another terminal
     (or systemctl start fitness-bot) first.
  3. `python scripts/strava_subscribe.py`

Output: the subscription id, which you may want to keep for the
unsubscribe script.
"""

from __future__ import annotations

import os
import sys

# Let the script import from the project root regardless of cwd.
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import httpx  # noqa: E402

from config import Config  # noqa: E402


def main():
    cfg = Config()
    if not (cfg.STRAVA_CLIENT_ID and cfg.STRAVA_CLIENT_SECRET):
        sys.exit("STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET missing from .env")
    if not cfg.WEBHOOK_PUBLIC_URL:
        sys.exit("WEBHOOK_PUBLIC_URL must be set (e.g. https://fitness-bot.example.com)")
    if not cfg.STRAVA_WEBHOOK_VERIFY_TOKEN:
        sys.exit(
            "STRAVA_WEBHOOK_VERIFY_TOKEN must be set. Generate one with:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )

    callback_url = cfg.WEBHOOK_PUBLIC_URL.rstrip("/") + "/webhooks/strava"
    print(f"Registering Strava webhook callback: {callback_url}")

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            "https://www.strava.com/api/v3/push_subscriptions",
            data={
                "client_id": cfg.STRAVA_CLIENT_ID,
                "client_secret": cfg.STRAVA_CLIENT_SECRET,
                "callback_url": callback_url,
                "verify_token": cfg.STRAVA_WEBHOOK_VERIFY_TOKEN,
            },
        )

    if resp.status_code == 201:
        data = resp.json()
        print(f"✅ Subscription created. id={data.get('id')}")
        print(
            "Keep this id around if you want to delete the subscription later: "
            f"\n  STRAVA_SUBSCRIPTION_ID={data.get('id')}"
        )
        return

    if resp.status_code == 400 and "already exists" in resp.text.lower():
        print(
            "⚠ A subscription already exists for this app. List it with:\n"
            "  GET https://www.strava.com/api/v3/push_subscriptions"
            "?client_id=<id>&client_secret=<secret>\n"
            "Then run scripts/strava_unsubscribe.py to delete it before re-creating."
        )
        sys.exit(1)

    print(f"Strava returned {resp.status_code}:\n{resp.text}")
    sys.exit(1)


if __name__ == "__main__":
    main()
