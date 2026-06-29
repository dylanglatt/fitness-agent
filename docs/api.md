# Coach Aurelius API (`api_server.py`)

A thin FastAPI layer over the existing coaching brain (`data/database.py` +,
lazily, `ai/coach.py`). It serves the JSON shapes the iOS app's `lib/api.js`
expects. Runs on the same host as the Discord bot, sharing the same `.env` and
SQLite DB.

## Endpoints

| Method | Path | Source | Notes |
|---|---|---|---|
| GET | `/today` | DB + coach | recovery/sleep/strain, prescribed session, recovery sessions, brief + Stoic quote |
| GET | `/train` | DB | today's session + recent workouts (volume from `lift_sets`) |
| GET | `/trends` | DB | bench 1RM / mileage / HRV / bodyweight series, run split, recovery, body comp |
| GET | `/goals` | DB | active goals w/ progress + ETA, weekly plan, integration status |
| GET | `/coach` | DB | opener + suggestions (chat happens via POST `/chat`) |
| POST | `/chat` | coach | `{message}` → `{reply}` (Anthropic) |
| POST | `/log-set` | DB | log a lift set from the live logger |
| POST | `/recovery` | DB | log a sauna/cold/etc. session |
| POST | `/goals` / PATCH `/goals/{id}` | DB | create / retire (status) a goal |
| POST | `/body` | DB | manual weight (BF% accepted, no column yet) |
| GET | `/health` | DB | liveness + latest WHOOP date (no auth required if token unset) |

Data endpoints read straight from SQLite — they work even when Anthropic / the
live WHOOP token are unavailable. `/today` falls back to your most recent
reading when the recent window is empty (stale sync), and the brief degrades to
an assembled summary if the coach can't be reached.

## Run

Local / simulator (bind all interfaces so the iOS sim can reach it over LAN):

```bash
pip install -r requirements.txt
# add a secret to .env:  FITNESS_API_TOKEN=<long-random-string>
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Auth: every request must send `Authorization: Bearer $FITNESS_API_TOKEN`.
If `FITNESS_API_TOKEN` is unset, auth is disabled (dev only).

## On the DigitalOcean droplet (next to the bot)

Runs as a **second systemd service** sharing the bot's venv, `.env`, and SQLite
DB. This is safe alongside the single-process bot because the rotating WHOOP
refresh token now lives in the DB (`oauth_tokens`), not `.env` — so the two
processes don't invalidate each other. SQLite WAL mode (set in
`database.initialize()`) handles concurrent read/write.

Bind to **loopback only** — Caddy is the sole ingress, same as the webhook
server.

```ini
# /etc/systemd/system/fitness-api.service
[Unit]
Description=fitness-api
After=network.target
[Service]
User=fitness-agent
WorkingDirectory=/home/fitness-agent/fitness-bot
EnvironmentFile=/home/fitness-agent/fitness-bot/.env
ExecStart=/home/fitness-agent/fitness-bot/venv/bin/uvicorn api_server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now fitness-api
sudo journalctl -u fitness-api -f
```

Add the API route to your existing Caddyfile (the `--legacy-peer`… block that
already proxies `/webhooks/*` to `:8765`):

```caddy
your-host.com {
    handle_path /api/* {
        reverse_proxy 127.0.0.1:8000
    }
    # …existing /webhooks/* reverse_proxy to 127.0.0.1:8765…
}
```

`handle_path` strips the `/api` prefix, so the app calls `https://your-host.com/api/today`
and the service sees `/today`. Reload Caddy: `sudo systemctl reload caddy`.

## Point the app at it

In the app repo `lib/config.js`:

```js
export const API_BASE_URL = 'https://your-host.com/api'; // droplet behind Caddy
// export const API_BASE_URL = 'http://<LAN-IP>:8000';   // local/sim dev
export const AUTH_TOKEN = '<same FITNESS_API_TOKEN>';
export const USE_MOCK = false;                            // flip off mock
```
