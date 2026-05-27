"""Eval runner — sends each prompt in questions.py through the live Coach
and grades the result on (a) which tools were called, (b) which expected
facts appear in the answer.

Usage:
    python -m evals.runner            # default — full run, write JSON, print table
    python -m evals.runner --quiet    # suppress per-question stdout
    python -m evals.runner --only sauna_hr_three_months   # run one question

Exit code: 0 if every question passes, 1 if any fails. CI-friendly.

This hits the real Anthropic API and the real local SQLite DB. Costs money
(small — ~5–15 calls per run). Run on demand or wire into the scheduler when
you trust the harness; it's not on a schedule yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make sure the project root is on sys.path when running as a script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ai.coach import Coach  # noqa: E402
from config import Config  # noqa: E402
from data.database import Database  # noqa: E402
from evals.questions import QUESTIONS  # noqa: E402

logger = logging.getLogger("evals")


async def _run_one(coach: Coach, q: dict, quiet: bool) -> dict:
    """Run one question, intercepting tool calls, and return a result dict."""
    called: list[str] = []
    original_execute_tool = coach._execute_tool

    async def _spy_execute_tool(name: str, args: dict) -> str:
        called.append(name)
        return await original_execute_tool(name, args)

    coach._execute_tool = _spy_execute_tool  # type: ignore[assignment]
    coach._conversation = []  # Each question is its own session.

    t0 = time.time()
    answer: str = ""
    error: str | None = None
    try:
        answer = await coach._ask_claude(
            q["prompt"],
            use_history=False,
            allow_tools=True,
            caller=f"eval:{q['id']}",
        )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.exception(f"[{q['id']}] _ask_claude raised")
    finally:
        coach._execute_tool = original_execute_tool  # type: ignore[assignment]

    duration_s = round(time.time() - t0, 2)

    # Grade.
    expected_tools = set(q.get("expected_tools") or [])
    expected_facts = q.get("expected_facts") or []
    answer_lower = (answer or "").lower()

    missing_tools = sorted(t for t in expected_tools if t not in called)
    missing_facts = [f for f in expected_facts if f.lower() not in answer_lower]
    passed = (
        error is None
        and not missing_tools
        and not missing_facts
        and (answer.strip() != "" if not expected_facts and not expected_tools else True)
    )

    if not quiet:
        status = "PASS" if passed else "FAIL"
        head = (answer or "").replace("\n", " ")[:120]
        print(
            f"  [{status}] {q['id']:<32s} "
            f"{duration_s:>5.2f}s · tools={called} "
            f"miss_tools={missing_tools or '-'} miss_facts={missing_facts or '-'}"
        )
        if error:
            print(f"          ERROR: {error}")
        elif head:
            print(f"          → {head}")

    return {
        "id": q["id"],
        "prompt": q["prompt"],
        "expected_tools": sorted(expected_tools),
        "expected_facts": expected_facts,
        "tools_called": called,
        "missing_tools": missing_tools,
        "missing_facts": missing_facts,
        "duration_s": duration_s,
        "answer": answer,
        "error": error,
        "passed": passed,
    }


async def _main_async(only: str | None, quiet: bool) -> int:
    config = Config()
    db = Database(config.DB_PATH)
    await db.initialize()
    coach = Coach(config, db)

    selected = [q for q in QUESTIONS if only is None or q["id"] == only]
    if only and not selected:
        print(f"No question matched id={only!r}. Known ids:")
        for q in QUESTIONS:
            print(f"  - {q['id']}")
        return 2

    print(f"Running {len(selected)} eval question(s)...\n")
    results: list[dict] = []
    for q in selected:
        result = await _run_one(coach, q, quiet=quiet)
        results.append(result)

    # Summary line.
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed
    total_s = sum(r["duration_s"] for r in results)
    print()
    print(
        f"Summary: {passed}/{len(results)} passed · {failed} failed · "
        f"{total_s:.1f}s total"
    )

    # Persist JSON.
    results_dir = _PROJECT_ROOT / "evals" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / f"{ts}.json"
    out_path.write_text(json.dumps(
        {
            "timestamp_utc": ts,
            "passed": passed,
            "failed": failed,
            "total": len(results),
            "results": results,
        },
        indent=2,
        default=str,
    ))
    print(f"Wrote {out_path}")

    return 0 if failed == 0 else 1


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("EVAL_LOG_LEVEL", "WARNING"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Run only the question with this id.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-question stdout (still prints summary).",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_main_async(args.only, args.quiet))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
