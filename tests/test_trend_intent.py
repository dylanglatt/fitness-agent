"""Tests for ai/coach.py's _TREND_INTENT regex — the keyword gate that
decides whether a chat message gets `allow_tools=True` (and, in turn,
whether CHAT_PROMPT gets pointed at a real tool or told none are attached;
see CHAT_TOOL_GUIDANCE_ON/_OFF in ai/prompts.py).

This gate is not covered by evals/questions.py: evals/runner.py calls
Coach._ask_claude(..., allow_tools=True) directly, bypassing the chat()
method entirely, so a regex miss here is invisible to `python -m
evals.runner`. It only shows up live, in Discord, when the model gets asked
a question the gate didn't recognize as needing data — which is exactly
what happened on 2026-09-16: "how has my HRV correlated with my running
performance" didn't match any keyword, tools stayed off, and the model
wrote out a fake tool-call in plain text instead of either using
query_correlated_runs or admitting it couldn't look it up.

This test is the cheap, offline backstop for that class of bug: keep this
list current as new phrasings come up in chat.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai.coach import _TREND_INTENT  # noqa: E402


class TrendIntentShouldTriggerTools(unittest.TestCase):
    """Messages that need a real data pull — tools must turn on for these."""

    MESSAGES = [
        # The exact prompt that triggered the fake tool-call incident.
        "Can you tell me how my HRV has correlated with my running performance?",
        "how does my sleep correlate with strain",
        "what's the relationship between my HRV and my pace",
        "how was my HRV in February?",
        "what were my total running miles in April?",
        "am I getting stronger on bench over the last 8 weeks?",
        "how has my running changed over the last month?",
        "what's my average resting heart rate this year",
        "how do I compare to last month",
        "show me my HRV trend",
        "what's my mile time history looking like",
        "hrv over the last 30 days",
    ]

    def test_all_trigger(self):
        for msg in self.MESSAGES:
            with self.subTest(msg=msg):
                self.assertTrue(
                    _TREND_INTENT.search(msg),
                    f"expected tools to turn on for: {msg!r}",
                )


class TrendIntentShouldNotTriggerTools(unittest.TestCase):
    """Ordinary chat that the small context snapshot already covers — tools
    should stay off so the cheap path stays cheap."""

    MESSAGES = [
        "bench 3x10 at 145",
        "good morning",
        "how am I today",
        "what should I do this morning",
        "did I run yet today",
        "log my squat 5x5 225",
        "I'm feeling pretty good today",
        "what time is it",
    ]

    def test_none_trigger(self):
        for msg in self.MESSAGES:
            with self.subTest(msg=msg):
                self.assertFalse(
                    _TREND_INTENT.search(msg),
                    f"expected tools to stay off for: {msg!r}",
                )


if __name__ == "__main__":
    unittest.main()
