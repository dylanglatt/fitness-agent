"""Regression questions for the fitness-agent eval harness.

Each entry is a dict with:

    id              short slug, used in result tables and JSON output
    prompt          the user message sent through Coach._ask_claude
    expected_tools  list of tool names that should be called during the run.
                    An empty list means "no tool calls expected". The grader
                    requires EVERY tool in this list to actually be called
                    (no extras-disallowed — the model can call others too).
    expected_facts  list of case-insensitive substrings. The grader requires
                    EVERY substring to appear in the final answer text.
    notes           free-text human commentary; not used by the grader.

Adding a new question: append a dict to QUESTIONS. Keep prompts realistic —
write what you'd actually type in Discord, not a contrived test phrase.

Seed set (5 examples) was chosen to cover:
  - a daily-metrics tool path
  - a Strava aggregate tool path
  - a lift-progression tool path
  - the sauna question that motivated the harness (added 2026-05-20)
  - a no-tools coaching question that should answer from layered context
"""

QUESTIONS: list[dict] = [
    {
        "id": "daily_metrics_hrv_february",
        "prompt": "How was my HRV in February?",
        "expected_tools": ["get_whoop_aggregates"],
        "expected_facts": ["hrv"],
        "notes": (
            "Expects the model to recognize 'February' as a past-month "
            "trend question and call the aggregates tool. Answer should "
            "mention HRV explicitly."
        ),
    },
    {
        "id": "strava_aggregate_last_month",
        "prompt": "What were my total running miles in April?",
        "expected_tools": ["get_strava_aggregates"],
        "expected_facts": ["mile"],
        "notes": (
            "Aggregates path. 'mile' as a substring covers 'miles', 'mi', "
            "'mileage'."
        ),
    },
    {
        "id": "lift_progression_bench",
        "prompt": "Am I getting stronger on bench over the last 8 weeks?",
        "expected_tools": ["query_lift_progression"],
        "expected_facts": ["bench"],
        "notes": (
            "Progression path — the prompt nudges 'PREFER THIS' in the tool "
            "description, so the model should pick query_lift_progression "
            "over query_lifts."
        ),
    },
    {
        "id": "sauna_hr_three_months",
        "prompt": "How has my heart rate in the sauna changed over the past 3 months?",
        "expected_tools": ["query_whoop_workouts"],
        "expected_facts": ["sauna"],
        "notes": (
            "The motivating bug. Bot must call query_whoop_workouts (not "
            "blanket-deny that sauna is tracked). The honest answer "
            "acknowledges WHOOP returns null HR for passive sports and "
            "pivots to a derived signal. We grade only on tool-call and "
            "the word 'sauna' appearing — the framing-quality check is "
            "still a human read."
        ),
    },
    {
        "id": "open_coaching_no_tools",
        "prompt": "Give me a one-line read on today's training state.",
        "expected_tools": [],
        "expected_facts": [],
        "notes": (
            "Conversational coaching question — the layered context already "
            "has today's recovery/sleep/strain. Model should answer from "
            "context without calling any history tools. Grader passes on "
            "any non-empty answer."
        ),
    },
]
