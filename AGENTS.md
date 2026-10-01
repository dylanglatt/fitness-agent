# AGENTS.md — fitness-agent

Canonical guide for AI agents (Codex, Claude Code) working in this repo.
`CLAUDE.md` imports this file, so this is the single source of truth — edit here.

## What this is

A personal AI fitness coach that lives in Discord. It pulls WHOOP recovery and
Strava activity data, lets the owner log lifts by chat, writes a Notion training
journal in the background, and sends a data-driven morning brief the moment
overnight WHOOP recovery lands. A thin FastAPI layer (`api_server.py`) exposes
the same brain to a (pre-development) native iOS app.

Single-user by design: hard-coded to one Discord owner (`OWNER_USER_ID`), no
multi-tenancy. Prompts are tuned for Claude specifically — don't swap the LLM
provider. Python 3.11+, asyncio throughout.

> Repo name is `fitness-agent` (README, git remote). On this machine the local
> checkout is `~/Desktop/Projects/fitness-bot`; on the production droplet it is
> `~/fitness-bot`. "fitness-bot" and "fitness-agent" refer to the same project.

## Architecture

```
WHOOP API ─┐                              ┌─▶ Discord (brief + chat + slash)
Strava API ─┼─▶ webhook/poll ─▶ ai/coach ─┼─▶ Notion (5-DB journal)
Open-Meteo ─┘        │           (Claude)  └─▶ SQLite (source of truth)
                     └─ RAG (ChromaDB) ────────┘
iOS app ─▶ api_server.py (FastAPI) ─▶ same coach.py + database.py + SQLite
```

Data pipeline: WHOOP + Strava APIs → SQLite (local source of truth) → Notion
5-DB journal (mirror). Ingestion is webhook-driven (Strava + WHOOP push into an
aiohttp server co-hosted in the bot's event loop) with a scheduler poll loop as
fallback. The morning brief fires on WHOOP-data-arrival inside a window
(`DAILY_BRIEF_POLL_START` … `DAILY_BRIEF_BACKSTOP`), not on a fixed clock time.

## Repo layout

```
main.py                    Entry point (python main.py) — starts the Discord bot
api_server.py              FastAPI service for the iOS app (uvicorn api_server:app)
config.py                  All config/secrets, loaded from .env via python-dotenv
requirements.txt
bot/
  discord_bot.py           Bot setup + message routing
  commands.py              Slash-command tree (/recovery /plan /debrief /liftstart …)
  scheduler.py             Brief / weekly summary / Sunday reflection triggers
integrations/
  whoop.py                 WHOOP API client (recovery/sleep/HRV/strain/workouts)
  strava.py                Strava API client (activities)
  notion.py                Notion write client (5 databases)
  weather.py               Open-Meteo weather + AQI (no key)
  webhook_server.py        aiohttp receiver for Strava + WHOOP push
ai/
  coach.py                 Orchestrates data + Claude calls (the brain; largest file)
  prompts.py               System prompt + persona ("CoachRex"/"Aurelius")
  training_state.py        Readiness / autoregulation / adherence logic
  knowledge_retriever.py   ChromaDB RAG retriever
data/
  database.py              SQLite access layer (aiosqlite, WAL mode)
  fitness_bot.db           Live SQLite DB (gitignored; the real one lives on droplet)
  chroma_db/               Vector store (gitignored)
knowledge/                 Markdown RAG corpus + ingest_knowledge.py
scripts/                   One-time setup + maintenance (backfill, subscribe, diagnose)
tests/                     pytest — 13 offline modules (146 tests): planning, progression, brief quality, scheduler, debrief
docs/                      api.md, webhooks.md, strava_auth.md, whoop_auth.md
evals/                     Coach eval harness (runner.py, questions.py)
```

## Setup, run, test

```bash
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in credentials

python main.py                # run the Discord bot
uvicorn api_server:app --host 0.0.0.0 --port 8000   # run the iOS API (needs FITNESS_API_TOKEN)
python -m pytest tests/ -v    # tests are fully offline; all external APIs stubbed
```

RAG is optional: `python knowledge/ingest_knowledge.py` (one-time, ~80 MB model
download). The bot runs without it — retrieval just returns empty.

## Configuration

All config lives in `config.py`, read from environment via `python-dotenv`.
`.env.example` documents every key. Notes that aren't obvious from the code:

- **Two-model cost split.** `CLAUDE_MODEL` (Sonnet) for briefs/summaries/
  reflection/debriefs; `CHAT_MODEL` (Haiku) for conversational chat.
  `COACH_CHEAP_MODE=1` (default) uses the cheap model for chat; set `0` to use
  `CLAUDE_MODEL` everywhere.
- **Notion is 5 independent DBs** — Schedule, Lifts, Lift Sets, Runs, Daily Log.
  Every `NOTION_*_DATABASE_ID` is optional: leave one blank and the bot skips
  writes to that DB rather than crashing. The integration must be added to each
  DB's Connections or the API 404s even with a valid key.
- **Webhooks are optional.** `WEBHOOK_PORT=0` disables them (poll-only). When
  enabled, bind `WEBHOOK_HOST=127.0.0.1` — Caddy is the only public ingress.
- **DB path** is `DB_PATH` (live file is `data/fitness_bot.db`).
- Weather/AQI is skipped if `HOME_LAT`/`HOME_LNG` are 0 or `HOME_CITY` is empty.

## Conventions

- Style: loosely PEP 8, `black` + `ruff` (not enforced by CI). Run `black .`
  and `ruff check .` before committing. Keep the longer-than-usual inline
  comments explaining *why* — matching the existing code.
- Everything is `async`; use `aiosqlite`/`httpx` async clients, never blocking IO
  in the event loop.
- Don't add new top-level modules without reason — extend `bot/`, `integrations/`,
  `ai/`, or `data/`.
- Touching any request path in `integrations/` or the webhook handlers → add a
  `tests/` unit test that mocks the external HTTP call (see `tests/test_debrief.py`).
- One logical change per PR/commit; branch off `main`.
- Crash reporting goes to Sentry → Discord `#fitness-bot`; errors only, no PII.

## Deployment (production is a DigitalOcean droplet, NOT local)

Production runs on a DO droplet — the local checkout can be stale, and the
droplet's SQLite is the live source of truth.

- SSH: `ssh root@68.183.19.78`; repo at `~/fitness-bot`
- Two systemd services share one venv, `.env`, and SQLite DB:
  `systemctl status fitness-bot` (Discord bot) and `fitness-api` (FastAPI).
  Concurrent access is safe via SQLite WAL mode.
- Run scripts on the droplet with `venv/bin/python` from `~/fitness-bot`.
- Logs: `journalctl -u fitness-bot -f` (or `-u fitness-api`).
- Public ingress: Caddy terminates TLS and reverse-proxies `/webhooks/*` →
  `127.0.0.1:8765` and `/api/*` → `127.0.0.1:8000`. Webhook host is
  `https://68.183.19.78.nip.io`.
- CI: pushing to `main` triggers `.github/workflows/deploy.yml` (zero-downtime
  redeploy).

## Critical gotchas — read before touching data or auth

- **WHOOP refresh tokens rotate.** The current live token lives only on the
  droplet (now in the DB `oauth_tokens` table, no longer `.env`). Run any WHOOP
  re-pull ON the droplet — never from a stale local checkout, or you risk
  desyncing auth for the live bot.
- **SQLite is the source of truth**, Notion is a mirror. Backfill flows one way:
  APIs → SQLite (`sync_history.py`, idempotent) → Notion
  (`scripts/backfill_notion.py`, skips existing rows, safe to re-run). Diff/audit
  read-only with `scripts/diagnose_backfill.py`.
- **Never commit** `.env`, `bot.log`, `data/fitness_bot.db`, or `data/chroma_db/`
  — `.gitignore` covers them; confirm with `git status` before pushing.
- Bot go-live (first WHOOP record) = **2025-07-23**; that's the earliest date any
  backfill/audit should expect data for.
- Don't file public issues for security problems (see `SECURITY.md`).

## The FastAPI service (`api_server.py`)

Thin read-mostly layer over `data/database.py` (and lazily `ai/coach.py`) serving
JSON for the iOS app: `/today`, `/train`, `/trends`, `/goals`, `/coach`,
`POST /chat`, `POST /log-set`, `POST /recovery`, goals/body CRUD, `/health`.
Data endpoints read straight from SQLite so they work even when Anthropic or the
live WHOOP token are down. Auth is `Authorization: Bearer $FITNESS_API_TOKEN`
(disabled only if the token is unset — dev only). Full contract in `docs/api.md`.
