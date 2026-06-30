#!/usr/bin/env bash
#
# One-shot setup for the Coach Aurelius API service on the droplet.
# Run it ON THE DROPLET, from inside your fitness-bot repo dir (the one with
# .env and venv/), as a user with sudo:
#
#     cd ~/fitness-bot && bash scripts/setup_api_droplet.sh
#
# It is idempotent — safe to re-run. It does NOT edit your Caddyfile (that's
# printed at the end for you to paste), since Caddyfile layouts vary.

set -euo pipefail

REPO="$(pwd)"
USER_NAME="$(whoami)"
VENV="$REPO/venv"

if [ ! -f "$REPO/api_server.py" ]; then
  echo "ERROR: api_server.py not found in $REPO — push the backend and deploy first." >&2
  exit 1
fi
if [ ! -x "$VENV/bin/uvicorn" ] && [ ! -x "$VENV/bin/pip" ]; then
  echo "ERROR: venv not found at $VENV." >&2
  exit 1
fi

# 1. API token in .env (generate one if missing).
if ! grep -q '^FITNESS_API_TOKEN=' .env 2>/dev/null; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  printf '\nFITNESS_API_TOKEN=%s\n' "$TOKEN" >> .env
  echo "✓ Added FITNESS_API_TOKEN to .env"
  echo "  → $TOKEN"
  echo "  (put this same value in the app's lib/config.js AUTH_TOKEN)"
else
  echo "✓ FITNESS_API_TOKEN already present in .env:"
  grep '^FITNESS_API_TOKEN=' .env | sed 's/^FITNESS_API_TOKEN=/  → /'
fi

# 2. Ensure deps (fastapi/uvicorn) are installed.
echo "Installing/refreshing dependencies…"
"$VENV/bin/pip" install --quiet -r requirements.txt
echo "✓ Dependencies installed"

# 3. systemd unit.
# NOTE: deliberately NO EnvironmentFile= here. config.py loads .env via
# python-dotenv (which strips inline `# comments`); systemd's EnvironmentFile
# parser does NOT strip them and would feed a comment into int(OWNER_USER_ID).
# WorkingDirectory puts .env next to config.py so dotenv finds it — same as the
# bot's own service.
sudo tee /etc/systemd/system/fitness-api.service >/dev/null <<EOF
[Unit]
Description=fitness-api (Coach Aurelius API)
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$REPO
ExecStart=$VENV/bin/uvicorn api_server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
echo "✓ Wrote /etc/systemd/system/fitness-api.service (User=$USER_NAME, dir=$REPO)"

sudo systemctl daemon-reload
sudo systemctl enable --now fitness-api
sleep 2
if sudo systemctl is-active --quiet fitness-api; then
  echo "✓ fitness-api is running on 127.0.0.1:8000"
else
  echo "✗ fitness-api failed to start — recent logs:"
  sudo journalctl -u fitness-api -n 25 --no-pager
  exit 1
fi

cat <<'NOTE'

──────────────────────────────────────────────────────────────────────────
Last step — add the API route to your Caddyfile (inside your site block),
then reload Caddy:

    handle_path /api/* {
        reverse_proxy 127.0.0.1:8000
    }

    sudo systemctl reload caddy

Verify:  curl -H "Authorization: Bearer $FITNESS_API_TOKEN" https://<your-host>/api/health
──────────────────────────────────────────────────────────────────────────
NOTE
