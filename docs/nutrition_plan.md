# Nutrition Integration — Design Plan (v1, for review)

**Your choices:** Natural-language logging · Goal-flexible (switchable cut / maintain / gain / perform; default *perform + maintain*, health-first) · Morning targets + evening check-in.

Nothing is built yet — react to the §7 defaults and the goal handling in §2 before I start.

---

## 1. The opportunity

Nutrition is the missing third of the recovery/training/fueling triangle. The bot already knows your training load (Strava + WHOOP) and recovery (HRV, sleep) — adding intake lets it close the loop: fuel the work you're actually doing, support recovery, and track body-composition trend. It slots into the architecture you already have rather than needing a new one.

**It reuses existing patterns wholesale:** a Haiku parser (like the lift/recovery parsers) → a `nutrition_log` table → Notion mirror → a deterministic target engine (like `training_state`) → surfaced in the brief, grounded by the literature RAG (you already have `knowledge/05_nutrition.md`).

**The make-or-break is logging.** Everything downstream is only as good as how consistently food gets logged. Natural language is the lowest-friction path and fits how you already talk to the bot — but macro numbers from text are *directional*, not lab-precise. The design leans on **trends over days**, not single-meal accuracy, and offers a manual-totals override for days you know exact numbers.

---

## 2. Goal handling ("all of the above")

Goal is a **switchable phase**, not a fixed setting — set via a command (e.g. `/nutrition goal cut|maintain|gain|perform`), stored like the training plan. The target engine computes different numbers per phase:

- **perform / maintain (default):** energy ≈ expenditure; protein high; carbs periodized to training.
- **cut:** modest deficit (~10–20%), protein up, hard floor on energy availability.
- **gain:** modest surplus (~5–10%), oriented to lean mass, running still fueled.

**Health-first guardrails are always on regardless of phase:** never prescribe an aggressive deficit; enforce a minimum energy-availability floor; for your training volume the engine biases toward *adequate fueling*, and is built to avoid any disordered-eating dynamic (frames around performance and health, never restriction for its own sake).

---

## 3. The risk worth naming up front

For someone doing ~15 runs + 12 lifts in two weeks, the real nutrition danger is **under-fueling (low energy availability)** — it tanks recovery, HRV, and adaptation, and for endurance athletes risks RED-S. So the engine's flagship guardrail is an **energy-availability check**: estimate expenditure from training (WHOOP already gives per-session kilojoules), compare to intake, and flag *under-eating* on high-load days — not just nag about a deficit. This is the lens the whole system is built through.

---

## 4. Architecture (5 phases, each shippable)

### N0 — Data foundation & logging
- `nutrition_log` table: date, meal/time, items text, calories, protein_g, carbs_g, fat_g, source ('chat'/'manual'), raw_message.
- `bodyweight_log` table: date, weight_lb, source. (Bodyweight drives targets + trend. WHOOP's body-measurement API won't help — third-party scales don't sync to it — so this is chat/manual: "weighed 178 today".)
- **Haiku nutrition parser** + a `_NUTRITION_HINT` pre-filter, mirroring `_try_parse_lift`. "Chicken, rice, broccoli, ~50g protein" → estimated cal + macros → one row; days aggregate.
- Notion **Nutrition** DB mirror (+ bodyweight as a column in Daily Log or its own DB).
- Same visibility discipline as the lift fixes: a message that looks like food but doesn't log emits a warning, not silence.

### N1 — Target engine (deterministic, the core)
- `nutrition_targets.py`: compute today's calorie + macro targets from **bodyweight + goal phase + today's training load**.
  - Protein ~1.6–2.2 g/kg (top of range on cut).
  - **Carb periodization:** more on long-run / quality / leg days, less on rest days.
  - Fat fills the remainder; calories from the phase (maintenance ± phase adjustment).
- **Energy-availability flag** (the §3 guardrail) using WHOOP kilojoules.
- Surface a `NUTRITION TARGETS` block in the morning brief — today's targets scaled to today's session, plus intake-so-far vs target.

### N2 — Evening check-in
- A scheduled evening DM (reusing the scheduler's windowed, once-per-day, sync_state-guarded pattern) reviewing the day's logged intake vs target: protein hit? fueled the session? under-eating? Sets up tomorrow.
- Doubles as the nudge that keeps logging consistent.

### N3 — Integration with training & recovery
- Pre/post-session fueling cues: protein post-lift; carbs around long/quality runs.
- **Hydration + electrolytes tied to your sauna use** — heat exposure raises fluid/electrolyte needs; this ties directly into the recovery work we just shipped.
- **Bodyweight trend vs intake** → energy-balance feedback; nudge targets if the trend diverges from the goal phase.

### N4 — Literature grounding
- Add sports-nutrition sources to `knowledge/` (protein timing, carb periodization for endurance, energy availability / RED-S, hydration) and re-ingest; extend the brief's retrieval query to pull them.

---

## 5. How a day works, end to end (target state)

1. **Morning brief** sets today's targets, scaled to today's session (e.g. long-run day → higher carbs) and recovery.
2. You **log meals** in natural language through the day; each is parsed and aggregated.
3. **Evening check-in** reviews intake vs target, flags protein/energy gaps, and frames tomorrow.
4. Bodyweight logged a few times a week feeds the trend and recalibrates targets.

Targets and the energy-availability flag are deterministic code; the LLM delivers and explains them — same split as the recommendation engine.

---

## 6. Validation

- Unit tests for the target engine and energy-availability flag: "leg day → higher carb target", "rest day → lower calories", "high load + low intake → under-fuel flag", "cut phase → deficit but protein up and never below the floor".
- Eval scenarios in `evals/` run before deploy.

---

## 7. Defaults to confirm (evidence-based; tunable)

- **Protein:** 1.8 g/kg default (2.0–2.2 on a cut).
- **Deficit/surplus:** −15% (cut) / +7% (gain) / ±0 (maintain·perform).
- **Carb periodization:** rest ~3 g/kg, moderate day ~5 g/kg, long/quality/leg day ~7+ g/kg.
- **Energy-availability floor:** flag when estimated availability drops below ~30 kcal/kg fat-free mass.
- **Logging:** natural language default; `manual totals` override when you know exact numbers.
- **Bodyweight:** prompt for it ~2–3x/week; trend computed on a rolling average to ignore daily water-weight noise.

---

## 8. Suggested build order

N0 → N1 first (you can log food and get training-scaled targets — immediate value), then N2 (evening loop), then N3 (training/recovery integration), then N4 (literature). Each ships independently.

---

**React to:** the goal handling in §2, the §7 defaults, and whether natural-language accuracy (directional, trend-based) is acceptable to you or you'd want the manual-totals path to be primary. Once you're happy, I'll start with N0 + N1.
