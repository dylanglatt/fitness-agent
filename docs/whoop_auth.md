# Getting your WHOOP Refresh Token

You only need to do this once. The bot handles token refresh automatically after setup.

## Steps

1. Go to [developer.whoop.com](https://developer.whoop.com), create an app, and note your **Client ID** and **Client Secret**.

2. Set your app's redirect URI to `http://localhost:8080/callback`.

3. Open this URL in your browser (replace `YOUR_CLIENT_ID`):

```
https://api.prod.whoop.com/oauth/oauth2/auth?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost:8080/callback&scope=offline read:recovery read:sleep read:workout read:cycles read:body_measurement
```

4. Authorize. You'll be redirected to localhost — copy the `code=` value from the URL.

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

6. Copy the `refresh_token` from the response and add it to your `.env`.
