# Contributing

This is a personal project, so the bar for "merged" is "I'd run this on my
own VPS." That said — bug fixes, generality improvements (anything that
makes the bot easier to fork for someone with different
WHOOP/Strava/Discord/Notion accounts), and tests are all welcome.

## Filing an issue

Useful issues include:

- A clear repro: what you ran, what `.env` keys were set (don't paste the
  values), what happened, what you expected.
- The minimum log line that surfaces the problem (the bot logs to stderr by
  default — `python main.py 2>&1 | tee bot.log` is fine).
- The Python and `discord.py` versions, plus your OS.

## Sending a pull request

1. Fork, branch off `main`, and keep changes scoped — one logical change
   per PR.
2. Match existing style. The codebase loosely follows
   [PEP 8](https://peps.python.org/pep-0008/) with longer-than-default
   inline comments where the *why* isn't obvious from the code.
3. If you touch any of the request paths in `integrations/` or the
   webhook handlers, add a unit test under `tests/` that mocks the
   external HTTP call. See `tests/test_debrief.py` for the pattern.
4. Don't introduce new top-level modules without a reason. Prefer
   extending the existing `bot/`, `integrations/`, `ai/`, or `data/`
   packages.
5. Don't commit `.env`, `bot.log`, `data/fitness_agent.db`, or anything from
   `data/chroma_db/`. The `.gitignore` already covers these — confirm with
   `git status` before pushing.

## Running tests locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -v
```

The test suite is fully offline — every external API (WHOOP, Strava,
Anthropic, Notion) is stubbed.

## Code style

There's no enforced linter yet, but if you want to match the existing
style:

```bash
pip install black ruff
black .
ruff check .
```

## Out of scope

- Multi-user / multi-tenant operation. The bot is hard-coded to one
  Discord owner; refactoring that out is a non-trivial change and not on
  the roadmap.
- Replacing Anthropic with another LLM provider. The prompts are tuned
  for Claude.
- Anything that requires a paid third-party (beyond the WHOOP / Strava /
  Anthropic / Notion accounts already needed to run it).

## Security disclosures

Please do **not** file public issues for security problems. See
[SECURITY.md](SECURITY.md).
