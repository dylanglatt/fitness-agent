"""Muse MCP server (integrations/mcp_server.py): tool surface, plan writes
through data/plan_ops.py, and the bearer guard over real HTTP."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

import pytz

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data.database import Database
from integrations.mcp_server import build_mcp_server, mount_mcp
from scripts.set_full_body_plan import TEMPLATE

TZ = pytz.timezone("America/New_York")
NOW = TZ.localize(datetime(2026, 10, 14, 9, 0))  # Wednesday (Day B)


class FakeCoach:
    def __init__(self, db):
        self.db = db
        self.generated = 0

    async def generate_todays_session(self):
        self.generated += 1
        await self.db.save_planned_session(
            NOW.date().isoformat(),
            "full_body",
            [{"name": "Leg press", "sets": 3, "reps": "8", "target_weight_lb": 180}],
        )
        return "ok"

    async def edit_todays_session(self, find, replacement):
        return f"Swapped {find} -> {replacement}"


def payload(result):
    """Structured tool output from a CallToolResult."""
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


class McpToolTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
        asyncio.run(self.db.initialize())
        asyncio.run(self.db.save_plan("Full body", "test", TEMPLATE, activate=True))
        self.coach = FakeCoach(self.db)
        self.changed = []
        self.mcp = build_mcp_server(
            self.db,
            lambda: self.coach,
            lambda: NOW,
            on_plan_change=self.changed.extend,
        )

    def call(self, name, args=None):
        return asyncio.run(self.mcp.call_tool(name, args or {}))

    def test_tool_surface_and_hints(self):
        tools = {t.name: t for t in asyncio.run(self.mcp.list_tools())}
        self.assertEqual(
            set(tools),
            {
                "get_today",
                "get_week",
                "get_progress",
                "move_session",
                "set_day",
                "swap_exercise",
            },
        )
        for name in ("get_today", "get_week", "get_progress"):
            self.assertTrue(tools[name].annotations.read_only_hint, name)
        for name in ("move_session", "set_day", "swap_exercise"):
            self.assertFalse(tools[name].annotations.read_only_hint, name)
            self.assertTrue(tools[name].description, name)
        # trading two days twice undoes it; clients must not auto-retry it
        self.assertFalse(tools["move_session"].annotations.idempotent_hint)

    def test_get_today_builds_session_once(self):
        out = payload(self.call("get_today"))
        self.assertEqual(out["session"]["day_label"], "B")
        self.assertEqual(out["exercises"][0]["name"], "Leg press")
        payload(self.call("get_today"))
        self.assertEqual(self.coach.generated, 1)  # reused the persisted plan

    def test_move_session_then_week_view(self):
        payload(
            self.call("move_session", {"from_date": "today", "to_date": "tomorrow"})
        )
        self.assertEqual(sorted(self.changed), ["2026-10-14", "2026-10-15"])
        week = payload(self.call("get_week"))
        by_day = {d["weekday"]: d["planned"] for d in week["days"]}
        self.assertEqual(by_day["Thursday"]["day_label"], "B")
        self.assertTrue(by_day["Thursday"]["is_override"])
        self.assertEqual(by_day["Wednesday"]["type"], "run")

    def test_bad_input_reaches_the_agent_as_a_tool_error(self):
        # ToolError text is shown to the model; any other exception is hidden
        # behind a generic "Error executing tool", which would leave Muse
        # guessing why a swap failed.
        from mcp.server.mcpserver.exceptions import ToolError

        with self.assertRaisesRegex(ToolError, "Unknown target"):
            self.call("set_day", {"target": "push"})
        with self.assertRaisesRegex(ToolError, "same"):
            self.call("move_session", {"from_date": "today", "to_date": "wednesday"})
        with self.assertRaisesRegex(ToolError, "Can't read date"):
            self.call("get_week", {"week_of": "someday"})


class McpHttpTests(unittest.TestCase):
    def test_bearer_guard_and_list_tools(self):
        from contextlib import asynccontextmanager

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
        asyncio.run(db.initialize())
        mcp = build_mcp_server(db, lambda: None, lambda: NOW)
        holder = {}

        @asynccontextmanager
        async def lifespan(app):
            async with holder["sm"].run():
                yield

        app = FastAPI(lifespan=lifespan)
        holder["sm"] = mount_mcp(app, mcp, "s3cret", ["testserver"])

        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": "2025-06-18",
        }
        with TestClient(app) as client:
            self.assertEqual(
                client.post("/muse/mcp", json=body, headers=headers).status_code, 401
            )
            bad = dict(headers, authorization="Bearer nope")
            self.assertEqual(
                client.post("/muse/mcp", json=body, headers=bad).status_code, 401
            )
            ok = dict(headers, authorization="Bearer s3cret")
            r = client.post("/muse/mcp", json=body, headers=ok)
            self.assertEqual(r.status_code, 200, r.text)
            names = {t["name"] for t in r.json()["result"]["tools"]}
            self.assertIn("move_session", names)


if __name__ == "__main__":
    unittest.main()
