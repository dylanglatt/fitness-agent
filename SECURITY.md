# Security Policy

## Reporting a vulnerability

If you find a security issue in this project, **please don't open a public
GitHub issue.** Instead, use GitHub's private vulnerability reporting:

> Repo → **Security** tab → **Report a vulnerability**

Include:

- A description of the issue.
- The minimum steps to reproduce, or a proof-of-concept.
- The commit SHA you tested against.
- Whether you'd like to be credited in the fix commit.

I'll acknowledge receipt within 7 days and aim to ship a fix (or a clear
won't-fix decision) within 30 days for anything serious.

## Scope

In scope:

- Anything in this repository's source code.
- The webhook signature verification path (`integrations/webhook_server.py`).
- The way secrets are loaded from `.env` (`config.py`).
- The deploy workflow (`.github/workflows/deploy.yml`).

Out of scope:

- Vulnerabilities in upstream dependencies — please report those to the
  upstream project. If a dep version pinned here is known-vulnerable, that
  *is* in scope and I'll bump it.
- Issues that require an attacker to already have shell access on the
  host running the bot.
- Bot account takeover via leaking your own `.env` (it's your `.env`).

## Known caveats

- The bot stores plaintext WHOOP/Strava OAuth refresh tokens in `.env`
  and Notion data in plaintext locally. Treat the `.env` file and the
  `data/` directory as sensitive — chmod 600 and don't sync them.
- The `.github/workflows/deploy.yml` workflow does `git reset --hard
  origin/main` on the deploy host. Anything pushed to `main` runs on
  the host with the user account configured in `DEPLOY_USER`. Treat
  push access to this repo as equivalent to shell access on that host.
- If you expose the webhook server to the internet, only `/webhooks/strava`
  and `/webhooks/whoop` are intended to be public. Everything else (the
  Discord control plane, the SQLite DB) lives inside the same process
  but is not bound to a public socket — confirm your reverse-proxy
  config matches.
