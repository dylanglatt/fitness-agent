# Coach eval harness

A regression suite for the bot's conversational chat path. Each question
gets sent through `Coach._ask_claude` against the real Anthropic API and
the real local SQLite DB. The grader checks whether the model called the
tools you expected and whether key facts appear in the answer.

This exists because the bot's failure mode is silent confabulation when
it lacks a tool — see the sauna bug fixed on 2026-05-20. Without an eval
suite you only find these gaps by happening to ask the wrong question
in Discord; with one, they show up here first.

## Run

```bash
# From the repo root, with the venv active.
python -m evals.runner

# Run just one question.
python -m evals.runner --only sauna_hr_three_months

# Quieter (only summary + results-file path).
python -m evals.runner --quiet
```

Exit code is 0 if every question passes, 1 if any failed. Wire that into
a scheduled task or a GitHub Action once you trust the suite.

Each run writes `evals/results/<UTC-timestamp>.json` with the full per-
question detail (prompt, tools called, missing tools, missing facts, the
full answer text, duration). That file is the audit trail — diff old vs.
new runs to see regressions.

## Cost

Each question is one (sometimes two or three, if the model loops on
tools) call to the chat model — usually Haiku. A full 5-question seed
run is a few cents. Don't loop this in CI on every push; daily is plenty.

## Adding a question

Edit `evals/questions.py` and append a dict to `QUESTIONS`:

```python
{
    "id": "short_slug",
    "prompt": "What you'd actually type in Discord.",
    "expected_tools": ["query_whoop_workouts"],  # may be []
    "expected_facts": ["sauna"],                  # case-insensitive substrings
    "notes": "Why this question matters.",
},
```

Grading rules:

- **Tools**: every name in `expected_tools` must appear in the calls the
  model actually made. The model may also call OTHER tools — that's not
  a failure (good behavior to allow exploration).
- **Facts**: every string in `expected_facts` must appear as a
  case-insensitive substring of the answer text. Use short, durable
  substrings ("hrv", "mile", "sauna") rather than full sentences.
- **No-tools questions**: set both lists to `[]`. The question passes
  as long as the model returns any non-empty answer without raising.

Keep prompts realistic. Contrived test phrases ("query my whoop data
please") teach you nothing — write what you'd actually ask Dylan-style.

## What this does NOT do

- It does not check answer quality, tone, or correctness beyond
  substring-matching. A wrong-but-confident answer that happens to
  contain the right substring will pass. Human review still matters.
- It does not test the daily brief, weekly summary, debrief, or any
  other code path that goes through `_ask_claude` with tools off and
  a fixed prompt template. Those are good candidates for a future
  golden-output suite.
- It does not test the Discord layer (slash commands, embed rendering,
  permission gating). Those want a different harness.
