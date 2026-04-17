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

    # Save to .env
    print("\nSaving to .env...")
    update_env("WHOOP_REFRESH_TOKEN", refresh_token)

    print("\n✅ Done! Restart the bot with: python main.py")


if __name__ == "__main__":
    main()
