# Webhooks on DigitalOcean + Caddy

This is the end-to-end setup for running the fitness-bot webhook receiver
on a DigitalOcean droplet with Caddy handling TLS. The goal: Strava and
WHOOP push events as they happen, so `/debrief` (and the brief) never waits
on a 3 AM polling job to see this morning's run.

## Architecture

```
  Internet
     │  HTTPS
     ▼
  Caddy (:443)     ← Let's Encrypt, auto-renews
     │  HTTP (loopback)
     ▼
  fitness-bot (127.0.0.1:8765)
     ├─ GET  /webhooks/strava   ← Strava verify handshake
     ├─ POST /webhooks/strava   ← Strava activity events
     ├─ POST /webhooks/whoop    ← WHOOP signed events
     └─ GET  /healthz           ← uptime probes
```

Key design choices:

- **One process** for the Discord bot AND the webhook server — same event
  loop, same OAuth client instances. Two processes would race WHOOP's
  rotating refresh token and invalidate each other.
- **Bind 127.0.0.1**, not `0.0.0.0`. The raw port is never exposed;
  Caddy is the only ingress. This means there's nothing useful an attacker
  can do even if they port-scan the VPS.
- **No database on the public path** — the WHOOP HMAC check and the Strava
  verify_token check happen before we touch SQLite or make any outbound
  call.

## DigitalOcean droplet setup

One-time, as root on a fresh Ubuntu 22.04 / 24.04 droplet:

```bash
# System
apt update && apt install -y python3-venv caddy ufw

# Firewall — SSH + HTTPS only
ufw allow ssh
ufw allow 80,443/tcp
ufw --force enable

# App user + directory
useradd -m -s /bin/bash coachrex
su - coachrex
git clone <your-repo> fitness-bot && cd fitness-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# …fill in .env (DISCORD_TOKEN, STRAVA_*, WHOOP_*, ANTHROPIC_API_KEY, …)
# …set WEBHOOK_HOST=127.0.0.1, WEBHOOK_PORT=8765, WEBHOOK_PUBLIC_URL=https://<your-host>
# …generate STRAVA_WEBHOOK_VERIFY_TOKEN with: python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Caddy config

Edit `/etc/caddy/Caddyfile`:

```caddy
coachrex.example.com {
    # Let's Encrypt kicks in automatically; you need an A record pointing
    # to the droplet's IP before this will renew.
    encode zstd gzip
    log {
        output file /var/log/caddy/coachrex.log
        format console
    }

    # Only forward the webhook + health routes. Everything else returns 404.
    handle_path /webhooks/* {
        reverse_proxy 127.0.0.1:8765
    }
    handle /healthz {
        reverse_proxy 127.0.0.1:8765
    }
    handle {
        respond "nothing to see here" 404
    }
}
```

Then:

```bash
systemctl reload caddy
# …Caddy will fetch a cert on first access. Trigger it once:
curl -I https://coachrex.example.com/healthz
```

## systemd service for the bot

`/etc/systemd/system/fitness-bot.service`:

```ini
[Unit]
Description=CoachRex fitness bot (Discord + webhook receiver)
After=network.target

[Service]
Type=simple
User=coachrex
WorkingDirectory=/home/coachrex/fitness-bot
EnvironmentFile=/home/coachrex/fitness-bot/.env
ExecStart=/home/coachrex/fitness-bot/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
systemctl daemon-reload
systemctl enable --now fitness-bot
journalctl -u fitness-bot -f
```

You should see `Webhook server listening on 127.0.0.1:8765` and the bot's
usual "Logged in as …" line.

## One-time webhook registration

Both services require a one-time registration step after the bot is running.

**Strava:**

```bash
su - coachrex
cd fitness-bot
source venv/bin/activate
python scripts/strava_subscribe.py
```

**WHOOP:**

Can't be scripted — see `scripts/whoop_register_webhook.md` for the
dashboard steps.

## Verifying it's working

```bash
# From the droplet — should be "ok"
curl -s https://coachrex.example.com/healthz

# Tail the logs and do a workout. You should see:
#   Strava event: create activity id=<id> owner=<id>
#   Strava activity <id> upserted from webhook.
#   WHOOP event: workout.updated id=<uuid> user=<id>
#   WHOOP workout <uuid> upserted from webhook.
journalctl -u fitness-bot -f
```

Then in Discord, run `/debrief` — it should grade the run using fresh HR,
zones, and pace within a minute of you ending the activity.

## Failure modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Strava events silent, `/debrief` says "not synced" | `WEBHOOK_PUBLIC_URL` mismatch, subscription stale | `scripts/strava_unsubscribe.py` then `strava_subscribe.py` |
| WHOOP events rejected 403 | `WHOOP_CLIENT_SECRET` was rotated | Re-save webhook URL in WHOOP dashboard |
| `signature verification failed` in logs | `WHOOP_CLIENT_SECRET` in `.env` doesn't match dashboard | Copy the latest secret into `.env`, `systemctl restart fitness-bot` |
| Caddy returns 502 | Bot not running or wrong port | `systemctl status fitness-bot`; check `WEBHOOK_PORT` in `.env` |
| 3 AM nightly sync still catching tons of events | Webhooks aren't delivering | Check Strava subscription list and WHOOP dashboard; verify `/healthz` reachable from outside |
