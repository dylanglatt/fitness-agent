# Recommendation Engine — Design Plan (v1, for review)

**Goal profile (your choices):** Balanced hybrid (muscle/strength + running + longevity) · Fixed split that auto-adjusts · Auto-adapt and explain.

This is a proposal to react to — nothing is built yet. Push back on any of the defaults in §7.

---

## 1. The core problem

Today the recommendation is a **static weekday template**. `get_effective_session_for_date(date)` returns `daily_overrides[date]` or else `training_plans[weekday]`, and the morning brief delivers that slot verbatim. The only "memory" is the plan-adherence ledger in `_build_layered_context`, and it buckets every day as just **LIFT / RUN / REST** — it has **no concept of push vs pull vs legs, or of recovery spacing.** So if you train push off-schedule, the calendar still says "Wednesday = push" and you get push twice.

**The fix is architectural, not a prompt tweak:** move the *scheduling decision* out of the LLM/template and into deterministic, tested code that knows what you actually trained and when. The LLM's job becomes explaining the decision, not making it. That separation is what makes it reliable and testable instead of vibes.

---

## 2. Design principles (the "world-class" part)

1. **Decision in code, prose in the LLM.** The engine computes *what* to train; Claude writes the brief that delivers and justifies it. Deterministic logic = unit-testable and consistent.
2. **Recency-aware.** Track time-since-last and rolling volume per movement pattern, not per "lift/run."
3. **Recovery spacing.** Don't hit the same pattern hard within ~48h; don't stack hard runs; guarantee adequate easy/rest.
4. **Concurrent-training interference management.** The interference effect is worst between **heavy legs and hard/long running**. Separate leg day from quality/long runs; pair easy runs with upper-body days. (This is the heart of "balanced hybrid.")
5. **Autoregulation by recovery.** HRV/recovery state modulates intensity: green = progress, yellow = maintain/cap RPE, red = easy or rest.
6. **Progressive overload + periodization.** Track per-lift progression, suggest load increments, schedule deloads on a cadence and when HRV trends down.
7. **Resilient, not rigid.** The fixed split is the *intent*; when you deviate or recover poorly, the engine re-solves the rest of the week to preserve balance + spacing rather than blindly following the weekday slot.
8. **Grounded in literature.** Wire the existing knowledge base into the brief so recommendations cite real principles, and expand the library.

---

## 3. What already exists (leverage, don't rebuild)

- **Coarse pattern tags already exist.** `_try_parse_lift` returns a `workout` field (Push/Pull/Legs/Other) and the Notion Lifts DB has a `Workout` select. We can build on this, with a deterministic exercise→pattern map as the source of truth and the existing tag as a cross-check.
- **Per-set data is captured** (`lift_sets`: exercise, set, reps, weight, source) — enough for volume and progression math.
- **Recovery data is live** (`whoop_recovery`: HRV, RHR, recovery score) — enough for autoregulation.
- **Run data is rich** (Strava + WHOOP zones) — enough to classify easy/quality/long.
- **A literature RAG exists** (`knowledge/*.md` → Chroma, `knowledge_retriever.py`) — just not wired into the brief.
- **An eval harness exists** (`evals/`) — we'll add scenario tests here.

---

## 4. Architecture (5 phases, each shippable on its own)

### Phase 0 — Data foundation: classify what was trained
*The keystone. Everything downstream needs this.*
- **Exercise → movement-pattern map**: a maintainable lookup (e.g. `back squat → legs/quad`, `bench → push/chest`, `row → pull/back`) with a sensible fallback and the parser's `workout` tag as backup. Lives in code, easy to extend.
- **Run classifier**: easy / quality(tempo,intervals) / long, derived from distance + pace + HR zones + the plan's focus.
- **New DB helpers** in `data/database.py`:
  - `get_training_recency()` → per pattern: hours since last trained, last intensity.
  - `get_weekly_volume()` → sets per muscle group (7-day rolling) and run minutes by type.
- **Deliverable:** a "training state" object the brief and scheduler both read.

### Phase 1 — Recency + spacing in the recommendation
- Extend the adherence ledger from LIFT/RUN/REST to **push/pull/legs + run-type**.
- **Spacing rules engine** (pure function): given today's training state, return each candidate session's status — `ready` / `too-soon (trained Xh ago)` / `interference-conflict`.
- Feed this into the brief context and add prompt rules: *never prescribe a pattern trained <48h ago unless recovered and it's the plan.*
- **This phase alone kills push-after-push.**

### Phase 2 — Adaptive weekly scheduler
- Represent the week as a **target set of sessions** (your fixed split: e.g. Push / Pull / Legs lifts + 1 long, 1 quality, 2 easy runs) rather than rigid weekday slots.
- When you deviate or miss a day, **re-solve the remaining days** to satisfy, in priority order: recovery spacing → weekly balance (every pattern hit) → interference separation (legs away from hard runs) → your time/day preferences.
- "Auto-adapt and tell me": the brief states the adjusted week and the *why*.

### Phase 3 — Autoregulation + periodization
- Explicit recovery→intensity mapping in the recommendation (green/yellow/red with concrete RPE and volume caps), replacing today's vague "modulate based on recovery."
- Per-lift **progression**: track top sets, suggest load increments when you hit all reps.
- **Deload** scheduling: every ~4–6 weeks, or triggered by a sustained HRV downtrend / accumulated fatigue.

### Phase 4 — Literature grounding
- Wire `_retrieve_knowledge` into `daily_brief` so it pulls principles relevant to *today's decision* (concurrent training, the target muscle group, recovery), not just chat Q&A.
- Expand `knowledge/` with strong sources (programming/periodization, hybrid/concurrent training, autoregulation, volume landmarks) and re-run `ingest_knowledge.py`.
- Let the brief cite the principle: *"legs spaced 48h from Thursday's intervals — concurrent-interference."*

---

## 5. How a morning brief would work, end to end (target state)

1. Build **training state** (recency, weekly volume, recovery).
2. **Scheduler** picks today's session: the planned split slot, adjusted for spacing / interference / recovery / what you've actually done this week.
3. **Autoregulation** sets the intensity band from today's recovery.
4. **Knowledge retrieval** pulls the relevant principle(s).
5. **Claude** writes the brief: delivers the session, states the intensity, explains *why this and not that* ("you did push yesterday, so today is pull; HRV's green so progress the row").

The decision in steps 2–3 is deterministic code. Claude only does step 5.

---

## 6. Validation (how we know it's sound)

- **Unit tests** for the classifier, spacing rules, and scheduler — pure functions, no LLM needed.
- **Eval scenarios** in `evals/`: "trained push yesterday → must not prescribe push today"; "low HRV → caps intensity / suggests easy"; "missed leg day → reschedules within spacing, not blindly"; "hard run + leg day not adjacent."
- Run before each deploy so regressions are caught.

---

## 7. Defaults to confirm (evidence-based starting points — tune freely)

- **Hard-session spacing per muscle group:** 48h. (Light/pump work can be closer.)
- **Weekly volume target:** ~10–18 working sets per major muscle group, scaled down to leave running legs fresh (hybrid compromise).
- **Run mix:** 1 long, 1 quality, 2 easy per week; easy runs paired with upper-body lift days, quality runs kept off leg day.
- **Interference rule:** no heavy legs within 24h of a hard/long run (either direction).
- **Recovery bands:** recovery ≥67% (green) progress · 34–66% (yellow) maintain, cap RPE ~8 · <34% (red) easy or rest.
- **Deload:** every 5th week, or after a sustained ~7-day HRV decline.
- **Weekly frequency:** ~6 sessions (3 lift / 3 run) with 1 full rest — matching your recent pattern; adjustable.

---

## 8. Suggested build order

Phase 0 → Phase 1 first (this is what fixes the bug you hit and delivers immediate value), then 3 (autoregulation), then 2 (adaptive scheduler), then 4 (literature). Each phase is independently deployable and testable.

---

**React to:** the §7 defaults, the phase order, and whether the deterministic-decision / LLM-explains split is what you want. Once you're happy, I'll start with Phase 0+1.
