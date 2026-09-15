"""Unit tests for the deterministic recommendation engine (Phase 0+1).

Pure-logic tests — no DB, no LLM. Run: python -m pytest tests/test_training_state.py
(or `python tests/test_training_state.py` for a dependency-free run).
"""

from datetime import date

from ai.training_state import (
    classify_exercise, classify_run, build_training_state, is_lift_activity,
    classify_planned_session, assess_readiness,
    recovery_intensity_band, assess_deload,
    whoop_recovery_sessions, merge_recovery_sessions, render_recovery_block,
    render_readiness_block,
    PUSH, PULL, LEGS, CORE, FULL_BODY, RUN_EASY, RUN_QUALITY, RUN_LONG,
)

TODAY = date(2026, 6, 10)


def _iso(days_ago: int) -> str:
    from datetime import timedelta
    return (TODAY - timedelta(days=days_ago)).isoformat()


def test_classify_exercise():
    assert classify_exercise("Back squat")[0] == LEGS
    assert classify_exercise("Romanian deadlift")[0] == LEGS
    assert classify_exercise("Bench press")[0] == PUSH
    assert classify_exercise("Overhead press")[0] == PUSH
    assert classify_exercise("Lateral raises")[0] == PUSH
    assert classify_exercise("Dips")[0] == PUSH
    assert classify_exercise("Pull-ups")[0] == PULL
    assert classify_exercise("Barbell row")[0] == PULL
    assert classify_exercise("Bicep curl")[0] == PULL
    assert classify_exercise("Plank")[0] == CORE
    # unknown name falls back to the parser Workout tag, else None
    assert classify_exercise("Mystery move", workout_tag="Pull")[0] == PULL
    assert classify_exercise("Mystery move")[0] is None


def test_classify_exercise_specific_beats_generic_keyword():
    # "incline curl" collides with the generic "incline" -> push/chest
    # keyword; the specific curl entries must win since a curl on an
    # incline bench is biceps work, not chest.
    assert classify_exercise("Incline DB curl") == (PULL, "biceps")
    assert classify_exercise("Incline dumbbell curl") == (PULL, "biceps")
    # unaffected: real incline presses still classify as push/chest
    assert classify_exercise("Incline dumbbell press") == (PUSH, "chest")


def test_classify_exercise_hyphen_insensitive():
    assert classify_exercise("Rear-delt fly") == (PULL, "rear delts")
    assert classify_exercise("Trap-bar deadlift")[0] == LEGS


def test_classify_exercise_falls_back_to_free_exercise_db():
    # "Good Morning" isn't in the hand-authored keyword list but is in
    # free-exercise-db as a hamstring-primary powerlifting movement.
    pattern, muscle = classify_exercise("Good morning")
    assert pattern == LEGS
    assert muscle == "hamstrings"
    # still falls through to workout_tag / None when the DB has no match either
    assert classify_exercise("Totally made up exercise", workout_tag="Push")[0] == PUSH
    assert classify_exercise("Totally made up exercise")[0] is None


def test_classify_run():
    assert classify_run({"sport_type": "Run", "distance_m": 16000}) == RUN_LONG
    assert classify_run({"sport_type": "Run", "distance_m": 5000, "average_speed_mps": 4.0}) == RUN_QUALITY  # ~6:42/mi
    assert classify_run({"sport_type": "Run", "distance_m": 5000, "average_speed_mps": 2.8}) == RUN_EASY     # ~9:35/mi
    assert classify_run({"sport_type": "WeightTraining"}) is None


def test_push_yesterday_blocks_push_today():
    """The exact June 9 bug: trained push yesterday, template says push today."""
    lifts = [
        {"date": _iso(1), "exercise": "Overhead press"},
        {"date": _iso(1), "exercise": "Dips"},
        {"date": _iso(4), "exercise": "Barbell row"},   # pull, 4d ago
        {"date": _iso(3), "exercise": "Back squat"},     # legs, 3d ago
    ]
    state = build_training_state(lifts, [], TODAY)
    planned = ("lift", PUSH)
    r = assess_readiness(state, planned)
    assert r["status"] == "too_soon", r
    assert r["suggested"] is not None and r["suggested"][0] == "lift"
    # Freshest ready pattern should be pull (4d) over legs (3d).
    assert r["suggested"][1] == PULL, r


def test_ready_when_well_spaced():
    lifts = [{"date": _iso(3), "exercise": "Bench press"}]  # push 3d ago
    state = build_training_state(lifts, [], TODAY)
    r = assess_readiness(state, ("lift", PUSH))
    assert r["status"] == "ready", r


def test_leg_run_interference():
    """Hard/long run planned, but legs trained yesterday -> interference."""
    lifts = [{"date": _iso(1), "exercise": "Back squat"}]   # legs yesterday
    state = build_training_state(lifts, [], TODAY)
    r = assess_readiness(state, ("run", RUN_QUALITY))
    assert r["status"] == "interference", r
    assert r["suggested"] == ("run", RUN_EASY)


def test_planned_session_classification():
    assert classify_planned_session({"session_type": "lift", "focus": "push (upper)"}) == ("lift", PUSH)
    assert classify_planned_session({"session_type": "lift", "focus": "legs"}) == ("lift", LEGS)
    assert classify_planned_session({"session_type": "run", "focus": "easy aerobic"}) == ("run", RUN_EASY)
    assert classify_planned_session({"session_type": "run", "focus": "quality (tempo or intervals)"}) == ("run", RUN_QUALITY)
    assert classify_planned_session({"session_type": "rest"}) == ("rest", None)
    assert classify_planned_session(None) == ("rest", None)


def test_recency_picks_latest_date():
    """A pattern trained twice uses the most recent date for spacing."""
    lifts = [
        {"date": _iso(5), "exercise": "Bench press"},
        {"date": _iso(1), "exercise": "Incline press"},  # push again yesterday
    ]
    state = build_training_state(lifts, [], TODAY)
    assert state["patterns"][PUSH]["days_ago"] == 1
    assert state["patterns"][PUSH]["count_7d"] == 2


def test_recovery_bands():
    assert recovery_intensity_band(80)["band"] == "green"
    assert recovery_intensity_band(50)["band"] == "yellow"
    assert recovery_intensity_band(50)["rpe_cap"] == 8
    assert recovery_intensity_band(20)["band"] == "red"
    assert recovery_intensity_band(None)["band"] == "unknown"


def test_hrv_suppression_downgrades_green():
    # Green score but HRV 20% below baseline -> treated as yellow.
    b = recovery_intensity_band(75, hrv=60.0, hrv_baseline=75.0)
    assert b["band"] == "yellow", b


def test_deload_triggers_on_low_recovery_run():
    # Many low days in the last week -> deload.
    series = [70, 68, 65, 38, 35, 39, 33, 40, 36]  # last 7 mostly ≤40
    d = assess_deload(series, baseline_recovery=68)
    assert d["suggested"] is True, d


def test_no_deload_when_recovered():
    series = [70, 72, 68, 71, 69, 74, 70, 73, 71]
    d = assess_deload(series, baseline_recovery=70)
    assert d["suggested"] is False, d


def test_whoop_recovery_extraction():
    wos = [
        {"sport_id": 233, "sport_name": "Sauna", "start_date": "2026-06-09",
         "start_utc": "2026-06-09T19:00:00.000Z", "end_utc": "2026-06-09T19:20:00.000Z"},
        {"sport_id": 0, "sport_name": "Running", "start_date": "2026-06-09"},  # not recovery
        {"sport_id": 88, "sport_name": "Ice Bath", "start_date": "2026-06-08",
         "start_utc": "2026-06-08T18:00:00.000Z", "end_utc": "2026-06-08T18:05:00.000Z"},
    ]
    out = whoop_recovery_sessions(wos)
    assert len(out) == 2, out
    sauna = [o for o in out if o["session_type"] == "sauna"][0]
    assert sauna["duration_min"] == 20 and sauna["source"] == "whoop"


def test_recovery_merge_dedupes():
    chat = [{"date": "2026-06-09", "session_type": "sauna", "duration_min": 20, "notes": "post-lift"}]
    whoop = [
        {"date": "2026-06-09", "session_type": "sauna", "duration_min": 20, "source": "whoop"},
        {"date": "2026-06-07", "session_type": "ice_bath", "duration_min": 5, "source": "whoop"},
    ]
    merged = merge_recovery_sessions(chat, whoop)
    # Sauna on 06-09 logged in both -> one row, source 'both', keeps the note.
    assert len(merged) == 2, merged
    sauna = [m for m in merged if m["session_type"] == "sauna"][0]
    assert sauna["source"] == "both" and sauna.get("notes") == "post-lift"
    # Non-empty render proves the brief would show recovery (not "zero").
    assert "RECENT RECOVERY SESSIONS" in render_recovery_block(merged)


def test_classify_planned_session_full_body():
    assert classify_planned_session(
        {"session_type": "full_body"}
    ) == ("lift", FULL_BODY)
    assert classify_planned_session(
        {"session_type": "lift", "focus": "Full body"}
    ) == ("lift", FULL_BODY)
    assert classify_planned_session(
        {"session_type": "lift", "focus": "push (upper)"}
    ) == ("lift", PUSH)


def test_full_body_never_blocks_the_session():
    """Full body trains every pattern each time — a pattern still inside the
    spacing window is volume guidance, not a reason to skip the day (unlike a
    single PPL pattern, which does get blocked — see test_push_yesterday_blocks_push_today)."""
    lifts = [
        {"date": _iso(1), "exercise": "back squat"},   # legs, 1d ago — inside 48h
        {"date": _iso(5), "exercise": "bench press"},  # push, 5d ago — clear
    ]
    state = build_training_state(lifts, [], TODAY)
    r = assess_readiness(state, ("lift", FULL_BODY))
    assert r["status"] == "ready"
    assert r["suggested"] is None
    assert "legs: trained 1d ago" in r["reason"]
    assert "keep volume light" in r["reason"]
    assert "push: last 5d ago" in r["reason"]
    assert "normal volume" in r["reason"]
    assert "pull: last not yet logged" in r["reason"]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


# ── Unclassified lifting sessions (Strava / WHOOP activities) ────────────────
#
# The bug: build_training_state derived push/pull/legs recency ONLY from the
# chat-logged `lifts` table. The activities it was also handed went through
# classify_run(), which returns None for weight training — so every Strava
# `WeightTraining` and WHOOP `Weightlifting` session was invisible to the
# engine, while _compute_plan_adherence counted them. The same brief could say
# "lifts 1/3 last week" and "Push: not trained in 14d, READY" in adjacent
# blocks, and the header called itself "authoritative".


def _act(days_ago: int, sport: str, **kw) -> dict:
    row = {"date": _iso(days_ago), "sport_type": sport}
    row.update(kw)
    return row


def test_weight_training_activity_is_not_a_run():
    assert classify_run(_act(1, "WeightTraining")) is None
    assert is_lift_activity(_act(1, "WeightTraining"))
    assert is_lift_activity(_act(1, "Weightlifting"))
    assert is_lift_activity({"sport_name": "Strength Training"})
    assert not is_lift_activity(_act(1, "Run"))
    assert not is_lift_activity({})


def test_activity_lifts_are_tracked_as_unclassified():
    state = build_training_state(
        lifts=[],
        activities=[_act(1, "WeightTraining"), _act(4, "WeightTraining")],
        today=TODAY,
    )
    ul = state["unlogged_lifts"]
    assert ul["last_days_ago"] == 1
    assert ul["count_7d"] == 2
    # We know a lift happened; we still don't know which pattern.
    assert state["patterns"] == {}


def test_readiness_will_not_claim_ready_over_an_unclassified_lift():
    """The regression that matters: chat logging lapses, Strava still shows a
    lift yesterday, and the engine used to answer READY to everything."""
    state = build_training_state(
        lifts=[], activities=[_act(1, "WeightTraining")], today=TODAY
    )
    r = assess_readiness(state, ("lift", PUSH))
    assert r["status"] == "unknown"
    assert r["unlogged_lift_days_ago"] == 1
    assert r["pattern_data_complete"] is False
    assert "no movement pattern attached" in r["reason"]

    block = render_readiness_block(state, r)
    assert "INCOMPLETE" in block
    assert "authoritative" not in block
    assert "Unclassified lifting sessions" in block


def test_real_chat_history_still_reads_as_ready():
    """A pattern with genuine logged history outside the spacing window is
    still READY — the caveat must not swallow real signal."""
    state = build_training_state(
        lifts=[{"date": _iso(5), "exercise": "bench press"}],
        activities=[_act(1, "WeightTraining")],
        today=TODAY,
    )
    r = assess_readiness(state, ("lift", PUSH))
    assert r["status"] == "ready"
    assert "no movement pattern attached" in r["reason"]  # caveat still shown


def test_no_unclassified_lifts_keeps_the_authoritative_header():
    state = build_training_state(
        lifts=[{"date": _iso(5), "exercise": "bench press"}],
        activities=[_act(2, "Run", distance_m=5000, average_speed_mps=3.0)],
        today=TODAY,
    )
    r = assess_readiness(state, ("lift", PUSH))
    assert r["pattern_data_complete"] is True
    assert "authoritative" in render_readiness_block(state, r)
