# Getting your Strava Refresh Token

You only need to do this once. After the first time, the bot refreshes its
own token automatically.

## Easy path: use the helper script

`strava_auth.py` in the repo root runs a tiny local web server, opens the
authorization URL in your browser, captures the redirect, exchanges the code
for tokens, and prints the refresh token to paste into `.env`.

```bash
# Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in your .env first.
python strava_auth.py
```

Make sure your Strava app's *Authorization Callback Domain* (in
[strava.com/settings/api](https://www.strava.com/settings/api)) is set to
`localhost`.

## Manual path

If you'd rather not run the helper:

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api) and
   note your **Client ID** and **Client Secret**.

2. Open this URL in your browser (replace `YOUR_CLIENT_ID`):

   ```
   https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=read,activity:read_all
   ```

3. Authorize the app. You'll be redirected to a `localhost` URL that won't
   load — that's fine. Copy the `code=` value from the URL.

4. Exchange the code for tokens (replace the placeholders):

   ```bash
   curl -X POST https://www.strava.com/oauth/token \
     -F client_id=YOUR_CLIENT_ID \
     -F client_secret=YOUR_CLIENT_SECRET \
     -F code=YOUR_CODE_FROM_STEP_3 \
     -F grant_type=authorization_code
   ```

5. Copy the `refresh_token` from the JSON response into your `.env` as
   `STRAVA_REFRESH_TOKEN`.

## Troubleshooting

If you start hitting `401` on `/athlete/activities`, your refresh token was
likely issued with too-narrow a scope (`read` only). Strava bakes scope into
the refresh token at authorization time — the fix is to redo the OAuth flow
and re-grant with `activity:read_all`.
