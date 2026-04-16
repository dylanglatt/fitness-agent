# Getting your Strava Refresh Token

You only need to do this once. After the first time, the bot refreshes its own token automatically.

## Steps

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api) and note your **Client ID** and **Client Secret**.

2. Open this URL in your browser (replace `YOUR_CLIENT_ID`):

```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=read,activity:read_all
```

3. Authorize the app. You'll be redirected to a `localhost` URL that won't load — that's fine. Copy the `code=` value from the URL.

4. Run this curl command (replace the placeholders):

```bash
curl -X POST https://www.strava.com/oauth/token \
  -F client_id=YOUR_CLIENT_ID \
  -F client_secret=YOUR_CLIENT_SECRET \
  -F code=YOUR_CODE_FROM_STEP_3 \
  -F grant_type=authorization_code
```

5. Copy the `refresh_token` from the JSON response and add it to your `.env`.
