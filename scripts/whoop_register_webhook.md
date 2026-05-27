# Registering the WHOOP webhook

WHOOP does NOT expose a programmatic webhook-registration API — you register
the callback URL once in the developer dashboard. This file is the checklist
so you don't have to re-figure it out each deploy.

## Prereqs

- `WEBHOOK_PUBLIC_URL` in `.env` is set to your HTTPS base (e.g.
  `https://fitness-agent.example.com`).
- The bot is running and reachable — WHOOP sends a test POST when you save
  the webhook URL, and your server needs to verify the signature for that
  test to succeed.
- Your app has the `read:workout`, `read:sleep`, `read:recovery`,
  `read:cycles` scopes on the token. (Confirm in `whoop_auth.py` — should
  already be there.)

## Steps

1. Open the WHOOP developer dashboard:
   https://developer-dashboard.whoop.com/ → your app.
2. In **Webhooks**, set:
     - **Webhook URL:** `<WEBHOOK_PUBLIC_URL>/webhooks/whoop`
     - **Event types (subscribe to all we handle):**
         - `workout.updated`
         - `workout.deleted`
         - `recovery.updated`
         - `sleep.updated`
         - `cycle.updated`
3. Click **Save**. WHOOP will POST a signed test request to the URL. If
   the signature verification fails (wrong `WHOOP_CLIENT_SECRET` in `.env`),
   the save will error and WHOOP will keep the old URL.
4. Trigger a fresh workout or sync to confirm events are flowing. Check
   the bot log for `WHOOP event: ...` lines from
   `integrations/webhook_server.py::_whoop_event`.

## Notes

- WHOOP signs every webhook with HMAC-SHA256 over `timestamp + raw_body`,
  base64-encoded, using the app's `client_secret` as the key. We reject
  unsigned or wrong-signature requests — see `_verify_whoop_signature` in
  the webhook server. This is the only thing stopping a malicious POST
  from writing into our DB.
- Recovery, sleep, and cycle events do NOT have a v2 get-by-id endpoint,
  so on those we refresh the last 2 days and let the upsert dedupe. Workout
  events do have a get-by-id (`/v2/activity/workout/{id}`) — we fetch that
  specific record and upsert.
- Rotating `WHOOP_CLIENT_SECRET` invalidates signed events — re-save the
  webhook URL after a rotation so the dashboard re-signs with the new
  secret.
