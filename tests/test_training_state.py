"""Unit tests for the deterministic recommendation engine (Phase 0+1).

Pure-logic tests — no DB, no LLM. Run: python -m pytest tests/test_training_state.py
(or `python tests/test_training_state.py` for a dependency-free run).
"""

from datetime import date

from ai.training_state import (
    classify_exercise, classify_run, build_training_state,
    classify_planned_session, assess_readiness,
    PUSH, PULL, LEGS, CORE, RUN_EASY, RUN_QUALITY, RUN_LONG,
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
