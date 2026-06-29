"""
Run this once to get your WHOOP refresh token.
Usage: python whoop_auth.py
"""

import http.server
import threading
import webbrowser
import urllib.parse
import urllib.request
import json
import os
import re
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8080/callback"
SCOPE = "offline read:recovery read:sleep read:workout read:cycles read:body_measurement"
STATE = "fitness123abc"

auth_code = None
server = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authorized! You can close this tab and check your terminal.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            error = params.get("error_description", ["Unknown error"])[0]
            self.wfile.write(f"<h2>Error: {error}</h2>".encode())

    def log_message(self, format, *args):
        pass


def curl_post(url, params):
    """Use curl to make POST requests (avoids Cloudflare bot blocking)."""
    import subprocess
    args = ["curl", "-s", "-X", "POST", url, "-H", "Content-Type: application/x-www-form-urlencoded"]
    for k, v in params.items():
        args += ["-d", f"{k}={v}"]
    result = subprocess.run(args, capture_output=True, text=True)
    return json.loads(result.stdout)


def curl_get(url, token):
    """Use curl for GET requests."""
    import subprocess
    result = subprocess.run(
        ["curl", "-s", url, "-H", f"Authorization: Bearer {token}"],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)


def exchange_code(code):
    return curl_post("https://api.prod.whoop.com/oauth/oauth2/token", {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    })


def test_token(access_token):
    try:
        data = curl_get("https://api.prod.whoop.com/developer/v2/recovery?limit=1", access_token)
        if "error" in data or "records" not in data:
            return False, data
        return True, data
    except Exception as e:
        return False, str(e)


def test_refresh(refresh_token):
    result = curl_post("https://api.prod.whoop.com/oauth/oauth2/token", {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if "refresh_token" in result:
        return True, result
    return False, result


def update_env(key, value):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "r") as f:
        content = f.read()
    pattern = rf"^{key}=.*$"
    replacement = f"{key}={value}"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        content += f"\n{key}={value}"
    with open(env_path, "w") as f:
        f.write(content)
    print(f"  ✅ Updated {key} in .env")


def update_db_token(refresh_token):
    """Persist the fresh refresh token into the bot's durable oauth_tokens
    store. The running bot reads its WHOOP token from the DB (the DB copy wins
    over .env), so a re-auth that only updated .env would be silently ignored
    — the stale DB token would keep getting used and keep 400-ing. Writing here
    too keeps the two in sync. Uses stdlib sqlite3 so this script stays
    dependency-light and synchronous.
    """
    import sqlite3
    db_path = os.getenv("DB_PATH", "data/fitness_agent.db")
    db_path = os.path.join(os.path.dirname(__file__), db_path)
    if not os.path.exists(db_path):
        print(f"  ⚠️  DB not found at {db_path} — skipping DB token write. "
              f"It will migrate from .env on next bot start.")
        return
    try:
        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                provider TEXT PRIMARY KEY,
                refresh_token TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        con.execute(
            """
            INSERT INTO oauth_tokens (provider, refresh_token, updated_at)
            VALUES ('whoop', ?, datetime('now'))
            ON CONFLICT(provider) DO UPDATE SET
                refresh_token=excluded.refresh_token,
                updated_at=excluded.updated_at
            """,
            (refresh_token,),
        )
        con.commit()
        con.close()
        print("  ✅ Updated WHOOP token in DB store (oauth_tokens)")
    except Exception as e:
        print(f"  ⚠️  Could not write token to DB ({e}). "
              f".env is updated; restart the bot to migrate it.")


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET must be set in your .env")
        return

    print(f"Using Client ID: {CLIENT_ID}")
    print()

    # Start local server
    srv = http.server.HTTPServer(("localhost", 8080), CallbackHandler)
    thread = threading.Thread(target=srv.handle_request)
    thread.daemon = True
    thread.start()

    # Open browser
    auth_url = (
        f"https://api.prod.whoop.com/oauth/oauth2/auth"
        f"?client_id={urllib.parse.quote(CLIENT_ID)}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&scope={urllib.parse.quote(SCOPE, safe='')}"
        f"&state={STATE}"
    )
    print("Opening browser for WHOOP authorization...")
    webbrowser.open(auth_url)
    print("Waiting for authorization (you have 2 minutes)...")

    thread.join(timeout=120)

    if not auth_code:
        print("\nNo code received automatically.")
        print("Paste the full redirect URL here:")
        url = input("> ").strip()
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" not in params:
            print("ERROR: No code found in URL")
            return
        auth_code_val = params["code"][0]
    else:
        auth_code_val = auth_code

    print(f"\nGot auth code. Exchanging for tokens...")

    tokens = exchange_code(auth_code_val)

    if "error" in tokens:
        print(f"ERROR exchanging code: {tokens}")
        return

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    print(f"\n✅ Got tokens!")
    print(f"   Access token:  {access_token[:30]}...")
    print(f"   Refresh token: {refresh_token[:30]}...")

    # Test access token
    print("\nTesting access token against WHOOP API...")
    ok, result = test_token(access_token)
    if ok:
        print("✅ Access token works! WHOOP API is responding.")
    else:
        print(f"❌ Access token failed: {result}")

    # Test refresh token
    print("\nTesting refresh token...")
    ok, result = test_refresh(refresh_token)
    if ok:
        new_refresh = result.get("refresh_token", refresh_token)
        print("✅ Refresh token works!")
        refresh_token = new_refresh
    else:
        print(f"⚠️  Refresh token test: {result.get('error_description', result)}")
        print("   (The bot will still work — it'll use the access token until it can refresh)")

    # Save to .env AND the durable DB store (the bot prefers the DB copy).
    print("\nSaving token...")
    update_env("WHOOP_REFRESH_TOKEN", refresh_token)
    update_db_token(refresh_token)

    print("\n✅ Done! Restart the bot with: python main.py")


if __name__ == "__main__":
    main()
