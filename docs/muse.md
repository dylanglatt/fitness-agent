# Muse (and other MCP agents)

`integrations/mcp_server.py` exposes the coach to an external agent over MCP
(streamable HTTP). It's built for Meta's Muse, but any MCP client works. It runs
inside the `fitness-api` service (same process, DB, Coach and token as
`api_server.py`), so there's no new service to run.

**URL:** `https://68.183.19.78.nip.io/api/muse/mcp`
**Auth:** `Authorization: Bearer $FITNESS_API_TOKEN` (same token as the app API)

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `get_today` | read | WHOOP recovery, today's planned session (type, focus, Day A/B/C, override flag), exercises with sets/reps/weights on lift days |
| `get_week` | read | Mon-Sun: each day's planned session next to what was actually logged (lifts, Strava) |
| `get_progress` | read | active goals with progress + 8-week trends (bench e1RM, mileage, HRV, body weight, run split) |
| `move_session` | write | two dates trade sessions ("running today, lift tomorrow") |
| `set_day` | write | set what one day is: `day b`, `lift`, `run`, `long`, `rest`, `cross`, a weekday, or `reset` |
| `swap_exercise` | write | swap or drop one exercise in today's lift, with a computed weight |

All writes go through the same code as Discord `/swap`, `/move` and
`/editsession` (`data/plan_ops.py`, `Coach.edit_todays_session`). They only
write `daily_plan_overrides` and `planned_sessions`. The weekly template,
logged lifts, and WHOOP/Strava data and tokens are never touched.

Write tools are annotated `readOnlyHint=false`; read tools `readOnlyHint=true`.
In Muse, set reads to allow and writes to approval-required until you trust it.

## Deploy

1. Merge to `main` (CI installs `mcp` from requirements.txt and restarts
   `fitness-api`).
2. Make sure `.env` on the droplet has `FITNESS_API_TOKEN` and
   `WEBHOOK_PUBLIC_URL=https://68.183.19.78.nip.io/...` (its host is what the
   MCP endpoint accepts in the `Host` header).
3. Check: `journalctl -u fitness-api -n 50 | grep MCP` should say
   `MCP endpoint mounted at /muse/mcp`.
4. Smoke test from anywhere:

   ```bash
   curl -s https://68.183.19.78.nip.io/api/muse/mcp \
     -H "Authorization: Bearer $FITNESS_API_TOKEN" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -H "MCP-Protocol-Version: 2025-06-18" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
   ```

No Caddy change is needed: `/api/*` already proxies to the API on `:8000`.

## Connect in Muse

Muse adds custom MCP servers from chat, not Settings:

> Build a custom integration to my MCP server at
> https://68.183.19.78.nip.io/api/muse/mcp, test it, and save it as a skill.

Choose HTTP transport and bearer-token auth, and paste the token into Muse's
secure credential prompt, never into the chat. Then set the approval policy in
Settings. Calendar changes go through Muse's own Google Calendar connector.

## Known limits

- `swap_exercise` edits today only. To change another day's lift, set or move the day.
- A swap made while a `/liftstart` session is running doesn't reach that session.
- Changes made from Muse don't post to Discord. `/plan`, `/session` and the next
  brief reflect them.
- The token is shared with the app. Rotating it means updating both.
