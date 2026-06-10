"""
Webhook server — accepts pushes from Strava and WHOOP, fetches the referenced
record, and upserts it into SQLite. Replaces the 3 AM polling loop for
near-real-time freshness.

Architecture:

1. Single aiohttp Application, co-hosted in the same event loop as the
   Discord bot (see bot/discord_bot.py). We never want a second process —
   two processes mean two token-refresh races and two sets of logs to read.

2. Strava subscription is CREATE → VERIFY (GET) → EVENTS (POST). The verify
   token in our GET handler must match the one we used to create the
   subscription (one-time, scripts/strava_subscribe.py). Events give us an
   activity id only, so we call strava.get_activity_detail(id) and upsert.

3. WHOOP webhooks are SIGNED (HMAC-SHA256, base64, in
   `X-WHOOP-Signature`, with a `X-WHOOP-Signature-Timestamp` request header
   that goes into the signed payload). We reject any unsigned or wrong-sig
   request — otherwise anyone who knows the URL could poison our DB. Events
   give us a record id + type; we fetch the full record and upsert.

4. Token-refresh is handled by the existing StravaClient/WhoopClient — we
   just call their methods. No duplicate auth flow here.

5. The one-time Strava subscription script is in scripts/strava_subscribe.py.
   WHOOP's webhook URL has to be pasted into the developer dashboard manually;
   it can't be scripted (intentional UX decision by WHOOP).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


# ── Signature verification for WHOOP ─────────────────────────────────────────

def _verify_whoop_signature(
    body_bytes: bytes,
    timestamp: str,
    signature: str,
    client_secret: str,
) -> bool:
    """Verify a WHOOP webhook signature.

    WHOOP signs `<timestamp><raw_body>` with HMAC-SHA256 keyed on the app's
    client secret and base64-encodes the result. Reject any request where
    we can't reproduce the signature exactly — this is the only thing
    stopping a malicious POST from writing whatever it wants into our DB.

    Returns True iff the provided signature matches.
    """
    if not (body_bytes is not None and timestamp and signature and client_secret):
        return False
    try:
        payload = timestamp.encode("utf-8") + body_bytes
        mac = hmac.new(
            client_secret.encode("utf-8"), payload, hashlib.sha256
        ).digest()
        expected = base64.b64encode(mac).decode("ascii")
        # Constant-time compare to avoid timing side-channels.
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


# ── Handlers ─────────────────────────────────────────────────────────────────

async def _strava_verify(request: web.Request) -> web.Response:
    """Strava's initial subscription handshake.

    Strava calls this once when we create the subscription. It sends:
      GET /webhooks/strava?hub.mode=subscribe&hub.challenge=<tok>&hub.verify_token=<ours>
    and expects back: {"hub.challenge": "<tok>"} with the verify_token matching.
    """
    expected = request.app["strava_verify_token"]
    mode = request.query.get("hub.mode")
    challenge = request.query.get("hub.challenge")
    token = request.query.get("hub.verify_token")
    if mode != "subscribe" or not challenge:
        return web.Response(status=400, text="bad hub.mode")
    if not expected or token != expected:
        logger.warning("Strava verify: token mismatch (got %r)", token)
        return web.Response(status=403, text="verify_token mismatch")
    return web.json_response({"hub.challenge": challenge})


async def _strava_event(request: web.Request) -> web.Response:
    """Strava sends one of these on every activity create/update/delete.

    Payload (JSON):
      {"aspect_type":"create","event_time":..,"object_id":<activity_id>,
       "object_type":"activity","owner_id":..,"subscription_id":..,
       "updates": {...}}

    Strava requires a 2xx response within 2 seconds or it will retry. So we
    queue the fetch+upsert to the background and ack immediately.
    """
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    object_type = payload.get("object_type")
    aspect = payload.get("aspect_type")
    object_id = payload.get("object_id")
    owner_id = payload.get("owner_id")
    logger.info(
        "Strava event: %s %s id=%s owner=%s", aspect, object_type, object_id, owner_id
    )

    coach = request.app["coach"]
    db = request.app["db"]

    async def _handle():
        try:
            if object_type != "activity":
                # Athlete profile updates, etc. — ignore for now.
                return
            if aspect == "delete":
                # We keep historical rows; nothing to do.
                return
            if not object_id:
                return
            # Enrich (Detail + zones) so HR fields and HR-zone distribution
            # land in raw_json on first write — same shape the backfill produces,
            # so downstream code (Notion sync, coach analytics) doesn't need
            # to special-case webhook-sourced rows.
            enriched = await coach.strava.enrich_activity({"id": int(object_id)})
            if enriched and enriched.get("id"):
                await db.upsert_strava_activity(enriched)
                logger.info("Strava activity %s upserted from webhook.", object_id)

                # Push to Notion immediately so the row appears in real time
                # rather than waiting for the next morning brief. Look up the
                # matching WHOOP workout (if WHOOP recorded the same session)
                # and pass it so HR + zones come from WHOOP's wrist data.
                # Wrapped in its own try/except — a Notion failure shouldn't
                # roll back the SQLite write that's already succeeded.
                try:
                    whoop_match = await db.find_whoop_workout_for_strava_activity(enriched)
                    await coach.notion.log_strava_activity(enriched, whoop_workout=whoop_match)
                except Exception as e:
                    logger.warning("Notion write from webhook failed for %s: %s", object_id, e)
        except Exception as e:
            logger.error("Strava webhook fetch/upsert failed: %s", e, exc_info=True)

    # Fire-and-forget; we ack right away.
    request.app.loop.create_task(_handle())
    return web.Response(status=200, text="ok")


async def _whoop_event(request: web.Request) -> web.Response:
    """WHOOP sends a signed POST on workout/recovery/sleep/cycle updates.

    Payload (JSON):
      {"user_id":..,"id":"<record_id>","type":"workout.updated","trace_id":".."}

    We verify the HMAC signature against the raw body + timestamp header,
    then fetch and upsert the referenced record.
    """
    secret = request.app["whoop_client_secret"]
    timestamp = request.headers.get("X-WHOOP-Signature-Timestamp", "")
    signature = request.headers.get("X-WHOOP-Signature", "")

    body_bytes = await request.read()
    if not _verify_whoop_signature(body_bytes, timestamp, signature, secret):
        logger.warning("WHOOP webhook: signature verification failed.")
        return web.Response(status=403, text="invalid signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return web.Response(status=400, text="invalid json")

    event_type = payload.get("type") or ""
    record_id = payload.get("id")
    user_id = payload.get("user_id")
    logger.info("WHOOP event: %s id=%s user=%s", event_type, record_id, user_id)

    coach = request.app["coach"]
    db = request.app["db"]

    async def _handle():
        try:
            if event_type.startswith("workout."):
                if event_type == "workout.deleted":
                    return
                rec = await coach.whoop.get_workout_by_id(str(record_id))
                if rec:
                    row = coach.whoop.normalize_workout(rec)
                    await db.upsert_whoop_workout(row, rec)
                    logger.info("WHOOP workout %s upserted from webhook.", record_id)
            elif event_type.startswith("recovery."):
                # Recoveries don't expose a direct get-by-id in v2; fetch the
                # last 2 days and let upsert dedupe.
                recs = await coach.whoop.get_recovery(days=2)
                for r in recs:
                    date, row = coach.whoop.normalize_recovery(r)
                    if date:
                        await db.upsert_whoop_recovery(date, row, r)
                logger.info("WHOOP recoveries refreshed (%d) from webhook.", len(recs))

                # Recovery is computed from the main sleep — so this event is
                # the "sleep processed" signal that the morning brief waits on.
                # Hand off to the scheduler, which decides (window + freshness +
                # once-per-day guard) whether to fire now. Its guards make this
                # safe to call on every recovery event, including re-scores.
                scheduler = request.app.get("scheduler")
                if scheduler is not None:
                    try:
                        await scheduler.on_recovery_webhook()
                    except Exception as e:
                        logger.error(
                            "Recovery-webhook brief trigger failed: %s",
                            e,
                            exc_info=True,
                        )
            elif event_type.startswith("sleep."):
                recs = await coach.whoop.get_sleep(days=2)
                for r in recs:
                    date, row = coach.whoop.normalize_sleep(r)
                    if date:
                        await db.upsert_whoop_sleep(date, row, r)
                logger.info("WHOOP sleep refreshed (%d) from webhook.", len(recs))
            elif event_type.startswith("cycle."):
                recs = await coach.whoop.get_strain(days=2)
                for r in recs:
                    date, row = coach.whoop.normalize_cycle(r)
                    if date:
                        await db.upsert_whoop_cycle(date, row, r)
                logger.info("WHOOP cycles refreshed (%d) from webhook.", len(recs))
            else:
                logger.info("WHOOP event type %r — no handler, ignoring.", event_type)
        except Exception as e:
            logger.error("WHOOP webhook fetch/upsert failed: %s", e, exc_info=True)

    request.app.loop.create_task(_handle())
    return web.Response(status=200, text="ok")


async def _healthz(request: web.Request) -> web.Response:
    """Plain health check — useful for Caddy/uptime monitors."""
    return web.Response(status=200, text="ok")


# ── App factory + lifecycle ──────────────────────────────────────────────────

def build_app(config, db, coach, scheduler=None) -> web.Application:
    """Build the aiohttp Application that hosts both services' webhook routes.

    `scheduler` is optional so the app can be built in tests without one; when
    provided, the WHOOP recovery handler uses it to fire the morning brief the
    moment recovery is scored.
    """
    app = web.Application()
    app["config"] = config
    app["db"] = db
    app["coach"] = coach
    app["scheduler"] = scheduler
    app["strava_verify_token"] = getattr(config, "STRAVA_WEBHOOK_VERIFY_TOKEN", "") or ""
    app["whoop_client_secret"] = getattr(config, "WHOOP_CLIENT_SECRET", "") or ""

    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/webhooks/strava", _strava_verify)
    app.router.add_post("/webhooks/strava", _strava_event)
    app.router.add_post("/webhooks/whoop", _whoop_event)

    return app


async def start_webhook_server(
    config, db, coach, scheduler=None
) -> Optional[tuple[web.AppRunner, web.TCPSite]]:
    """Start the webhook server on config.WEBHOOK_HOST:WEBHOOK_PORT.

    Returns (runner, site) so the caller can clean up on shutdown, or None
    if webhooks are disabled (port 0 / unset). `scheduler`, when passed, lets
    the WHOOP recovery handler fire the morning brief on recovery arrival.
    """
    port = int(getattr(config, "WEBHOOK_PORT", 0) or 0)
    if port <= 0:
        logger.info("Webhook server disabled (WEBHOOK_PORT not set).")
        return None

    host = getattr(config, "WEBHOOK_HOST", "127.0.0.1") or "127.0.0.1"
    app = build_app(config, db, coach, scheduler=scheduler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("Webhook server listening on %s:%d", host, port)
    return runner, site
