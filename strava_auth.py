"""
Run this once to get your Strava refresh token with the right scopes.
Usage: python strava_auth.py

The 401 you were seeing on /athlete/activities almost always means the
refresh token in .env was issued with too-narrow a scope (e.g. just `read`).
Strava bakes scope into the refresh token at authorization time, so the only
fix is to redo the OAuth flow and re-grant with `activity:read_all`.
"""

import http.server
import threading
import webbrowser
import urllib.parse
import json
import os
import re
import subprocess
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8080/callback"
# activity:read_all covers all activities (incl. private). read covers profile.
SCOPE = "read,activity:read_all,profile:read_all"

auth_code = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            auth_code = params["code"][0]
            granted = params.get("scope", [""])[0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<h2>Authorized! Granted scopes: {granted}</h2>"
                "<p>You can close this tab and check your terminal.</p>".encode()
            )
        else:
            self.send_response(400)
            self.end_headers()
            error = params.get("error", ["Unknown error"])[0]
            self.wfile.write(f"<h2>Error: {error}</h2>".encode())

    def log_message(self, format, *args):
        pass


def curl_post(url, params):
    args = ["curl", "-s", "-X", "POST", url,
            "-H", "Content-Type: application/x-www-form-urlencoded"]
    for k, v in params.items():
        args += ["-d", f"{k}={v}"]
    result = subprocess.run(args, capture_output=True, text=True)
    return json.loads(result.stdout)


def curl_get(url, token):
    result = subprocess.run(
        ["curl", "-s", url, "-H", f"Authorization: Bearer {token}"],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)


def exchange_code(code):
    return curl_post("https://www.strava.com/oauth/token", {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    })


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


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in .env")
        return

    print(f"Using Client ID: {CLIENT_ID}")
    print(f"Requesting scope: {SCOPE}")
    print()
    print("⚠️  In the Strava authorization page, make sure ALL boxes are checked")
    print("    (especially 'View data about your private activities'). If any")
    print("    box is unchecked, the refresh token won't work for that scope.")
    print()

    srv = http.server.HTTPServer(("localhost", 8080), CallbackHandler)
    thread = threading.Thread(target=srv.handle_request)
    thread.daemon = True
    thread.start()

    auth_url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={urllib.parse.quote(CLIENT_ID)}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&approval_prompt=force"
        f"&scope={urllib.parse.quote(SCOPE, safe='')}"
    )
    print("Opening browser for Strava authorization...")
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
        code_val = params["code"][0]
    else:
        code_val = auth_code

    print(f"\nGot auth code. Exchanging for tokens...")
    tokens = exchange_code(code_val)

    if "errors" in tokens or "message" in tokens and tokens.get("message") != "":
        if "errors" in tokens:
            print(f"ERROR exchanging code: {tokens}")
            return

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    granted_scope = tokens.get("scope", "")
    athlete = tokens.get("athlete", {})

    if not refresh_token:
        print(f"ERROR: no refresh_token in response: {tokens}")
        return

    print(f"\n✅ Got tokens for athlete: {athlete.get('firstname', '?')} {athlete.get('lastname', '')}")
    print(f"   Access token:  {access_token[:30]}...")
    print(f"   Refresh token: {refresh_token[:30]}...")
    print(f"   Granted scope: {granted_scope or '(not returned in token response — check the URL you were redirected to)'}")

    # Sanity check: hit /athlete/activities so we fail loud if the scope is wrong
    print("\nTesting access token against /athlete/activities...")
    test = curl_get("https://www.strava.com/api/v3/athlete/activities?per_page=1", access_token)
    if isinstance(test, list):
        print(f"✅ Activities endpoint works ({len(test)} activity returned).")
    else:
        print(f"❌ Activities endpoint failed: {test}")
        print("   This usually means activity:read_all wasn't granted.")
        print("   Re-run this script and make sure you tick every checkbox on the Strava auth page.")
        return

    print("\nSaving to .env...")
    update_env("STRAVA_REFRESH_TOKEN", refresh_token)

    print("\n✅ Done! Restart the bot with: python main.py")


if __name__ == "__main__":
    main()
