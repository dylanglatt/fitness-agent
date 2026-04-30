# Getting your WHOOP Refresh Token

You only need to do this once. The bot handles token refresh automatically
after setup.

## Easy path: use the helper script

`whoop_auth.py` in the repo root runs a tiny local server, opens the
authorization URL, captures the redirect, exchanges the code for tokens, and
prints the refresh token to paste into `.env`.

```bash
# Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in your .env first.
python whoop_auth.py
```

Make sure your WHOOP app's redirect URI (in
[developer.whoop.com](https://developer.whoop.com)) is set to
`http://localhost:8080/callback`.

## Manual path

If you'd rather not run the helper:

1. Go to [developer.whoop.com](https://developer.whoop.com), create an app,
   and note your **Client ID** and **Client Secret**.

2. Set your app's redirect URI to `http://localhost:8080/callback`.

3. Open this URL in your browser (replace `YOUR_CLIENT_ID`):

   ```
   https://api.prod.whoop.com/oauth/oauth2/auth?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost:8080/callback&scope=offline read:recovery read:sleep read:workout read:cycles read:body_measurement
   ```

4. Authorize. You'll be redirected to localhost — copy the `code=` value from
   the URL.

5. Exchange the code for tokens:

   ```bash
   curl -X POST https://api.prod.whoop.com/oauth/oauth2/token \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "grant_type=authorization_code" \
     -d "code=YOUR_CODE" \
     -d "client_id=YOUR_CLIENT_ID" \
     -d "client_secret=YOUR_CLIENT_SECRET" \
     -d "redirect_uri=http://localhost:8080/callback"
   ```

6. Copy the `refresh_token` from the response into your `.env` as
   `WHOOP_REFRESH_TOKEN`.
