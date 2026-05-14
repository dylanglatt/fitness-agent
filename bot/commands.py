"""
All slash commands for fitness-bot, registered via register_commands(bot).

Design notes:

1. Hybrid commands (prefix + slash) are used wherever the UX is a single
   verb with optional args. `!brief` still works; `/brief` works too, same
   handler. No regression for muscle memory.

2. `/goal` is a slash-only app_commands.Group because subcommands (add/list/
   progress/retire) are ergonomic in Discord's slash UI and ugly in prefix.

3. Owner-gating is enforced on every command. The bot is personal; anything
   that leaks should fail closed.

4. Long-running work (Claude calls, WHOOP fetches) defers the interaction so
   we don't hit Discord's 3-second ack ceiling. `ctx.typing()` works in
   prefix context and is a near-no-op for slash; `ctx.defer()` is the slash
   equivalent. hybrid_command handlers pick the right one via ctx.

5. Output chunking: Discord caps messages at 2000 chars. We reuse the
   chunker pattern from the old !context command wherever a response might
   exceed it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, date
from typing import Optional

import discord
import pytz
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────

_DAYS_ORDER = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]


def _owner_ok(ctx_or_interaction, owner_id: int) -> bool:
    """Single owner-gate for both prefix (ctx) and slash (interaction)."""
    author_id = getattr(ctx_or_interaction, "author", None)
    if author_id is not None:
        author_id = author_id.id
    else:
        # discord.Interaction has .user, not .author
        user = getattr(ctx_or_interaction, "user", None)
        author_id = user.id if user else None
    return author_id is not None and author_id == owner_id


async def _send_chunked(ctx_or_interaction, text: str, code_block: bool = False):
    """Send possibly-long text, chunked to fit Discord's 2000-char cap.

    Works for both discord.ext.commands.Context and discord.Interaction.
    """
    CHUNK = 1900  # leave headroom for code fences + safety
    chunks = [text[i : i + CHUNK] for i in range(0, max(len(text), 1), CHUNK)] or [""]
    first = True
    for chunk in chunks:
        payload = f"```\n{chunk}\n```" if code_block else chunk
        # Context has .send; Interaction.followup.send works after defer().
        if hasattr(ctx_or_interaction, "send"):
            await ctx_or_interaction.send(payload)
        else:
            if first:
                await ctx_or_interaction.response.send_message(payload)
            else:
                await ctx_or_interaction.followup.send(payload)
        first = False


def _parse_window(window: str) -> int:
    """Convert '7d' / '30d' / '90d' / 'ytd' / 'all' into a day count.

    Returns a large int for 'all' so callers can still compare.
    """
    w = (window or "30d").strip().lower()
    if w == "ytd":
        today = date.today()
        return (today - date(today.year, 1, 1)).days or 1
    if w == "all":
        return 365 * 10  # practical infinity
    # strip trailing 'd' if present, parse int
    if w.endswith("d"):
        w = w[:-1]
    try:
        return max(1, int(w))
    except ValueError:
        return 30


def _fmt_delta(recent: Optional[float], baseline: Optional[float], unit: str = "", good_up: bool = True) -> str:
    """Return a formatted recent vs. baseline line with direction arrow."""
    if recent is None or baseline is None:
        return f"  —{unit}"
    diff = recent - baseline
    arrow = "→"
    if abs(diff) >= 0.5 if unit != "%" else abs(diff) >= 1:
        if diff > 0:
            arrow = "↑" if good_up else "↑(worse)"
        else:
            arrow = "↓" if not good_up else "↓(worse)"
    return f"{recent:g}{unit} (baseline {baseline:g}{unit}) {arrow}"


def _mi(meters: Optional[float]) -> float:
    return round((meters or 0) / 1609.344, 2)


def _ft(meters: Optional[float]) -> float:
    return round((meters or 0) * 3.28084, 0)


def _kg_to_lb(kg: Optional[float]) -> Optional[float]:
    return round(kg * 2.20462, 1) if kg else None


def _pace_str(mps: Optional[float]) -> Optional[str]:
    if not mps or mps <= 0:
        return None
    sec_per_mi = 1609.344 / mps
    m, s = divmod(int(sec_per_mi), 60)
    return f"{m}:{s:02d}/mi"


def _interpret_recovery(score: Optional[float]) -> str:
    """One-line read in voice — no philosophy-poster language."""
    if score is None:
        return "No recovery number yet today — body's still reporting in."
    if score >= 67:
        return "Green. The body's ready for work; don't waste it on junk volume."
    if score >= 34:
        return "Yellow. Not a hard day — earn it with good reps, not a PR attempt."
    return "Red. Today is the training — not the workout. Sleep, fuel, move easy."


def _interpret_sleep(hours: Optional[float], eff: Optional[float]) -> str:
    if hours is None:
        return "No sleep record posted yet."
    if hours >= 7.5 and (eff or 0) >= 85:
        return "Enough, and cleanly. Good raw material for whatever today asks."
    if hours < 6:
        return "Short. Today's ceiling is lower — respect it."
    if (eff or 100) < 80:
        return "Time in bed was fine, but the night was choppy — expect the body to feel it."
    return "Adequate. Not exceptional. Fine to build on."


def _interpret_strain(strain: Optional[float]) -> str:
    if strain is None:
        return "No strain recorded yet."
    if strain >= 18:
        return "Very high. Tomorrow will cost something — plan to pay it with recovery, not another hard day."
    if strain >= 14:
        return "High-moderate. Solid session imprint."
    if strain >= 10:
        return "Moderate. Steady aerobic work showing up."
    return "Light. Not every day needs to be hard; today wasn't, and that's fine."


# ── Owner gate decorators ───────────────────────────────────────────────────

def _require_owner_hybrid(config):
    """Decorator factory for hybrid commands — checks ctx.author.id."""
    def check(ctx):
        return ctx.author.id == config.OWNER_USER_ID
    return commands.check(check)


def _require_owner_slash(config):
    """Decorator factory for slash (app_commands) — checks interaction.user.id."""
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == config.OWNER_USER_ID
    return app_commands.check(predicate)


# ── Main registration ──────────────────────────────────────────────────────

def register_commands(bot):
    """Register every command on the given bot instance.

    Called from FitnessBot.setup_hook(). The bot carries config, db, coach,
    scheduler as attributes.
    """
    config = bot.config
    db = bot.db
    coach = bot.coach
    scheduler = bot.scheduler
    tz = pytz.timezone(config.TIMEZONE)

    is_owner_hybrid = _require_owner_hybrid(config)
    is_owner_slash = _require_owner_slash(config)

    # ── Existing commands, converted to hybrid (prefix + slash) ─────────────

    @bot.hybrid_command(name="brief", description="Fire the morning brief on demand.")
    @is_owner_hybrid
    async def brief_cmd(ctx: commands.Context):
        await ctx.defer()
        try:
            await scheduler._refresh_recent_whoop_into_db(days=2)
        except Exception as e:
            logger.warning(f"Pre-brief refresh failed (non-fatal): {e}")
        text = await coach.daily_brief()
        await _send_chunked(ctx, text)

    @bot.hybrid_command(
        name="context",
        description="Dump the layered context the coach sees (debug).",
    )
    @is_owner_hybrid
    async def context_cmd(ctx: commands.Context):
        await ctx.defer()
        text = await coach._build_layered_context()
        await _send_chunked(ctx, text, code_block=True)

    @bot.hybrid_command(
        name="plan",
        description="Show the active training plan. Args: today (default), week, full, or <day>.",
    )
    @is_owner_hybrid
    async def plan_cmd(ctx: commands.Context, arg: str = "today"):
        plan = await db.get_active_plan()
        if not plan:
            await ctx.send(
                "No active plan. (DB seeds a default on init — check logs if missing.)"
            )
            return

        arg = (arg or "today").strip().lower()
        today_name = datetime.now(tz).strftime("%A").lower()

        def fmt_day(name: str, sess: dict, include_notes: bool = True) -> str:
            stype = sess.get("session_type", "?").upper()
            focus = sess.get("focus", "")
            presc = sess.get("prescription", "")
            notes = sess.get("notes", "")
            marker = "  ← today" if name == today_name else ""
            out = f"**{name.title()}**{marker} — {stype} / {focus}\n{presc}"
            if include_notes and notes:
                out += f"\n_{notes}_"
            return out

        tpl = plan.get("weekly_template") or {}

        if arg == "today":
            today_iso = datetime.now(tz).date().isoformat()
            # Override-aware lookup so /plan today honors /swap.
            sess = await db.get_effective_session_for_date(today_iso)
            if not sess:
                await ctx.send(f"No session defined for {today_name}.")
                return
            header = f"**{plan['name']}** — today"
            if sess.get("is_override"):
                base = tpl.get(today_name) or {}
                base_type = (base.get("session_type") or "rest").upper()
                header += f"  _(override; weekly template says {base_type})_"
            msg = header + "\n\n" + fmt_day(today_name, sess)
            await ctx.send(msg[:1990])
            return

        if arg == "week":
            lines = [f"**{plan['name']}** — this week at a glance"]
            for day in _DAYS_ORDER:
                sess = tpl.get(day, {})
                stype = sess.get("session_type", "?")
                focus = sess.get("focus", "")
                marker = " ← today" if day == today_name else ""
                lines.append(f"  {day.title()[:3]}: {stype} / {focus}{marker}")
            await ctx.send("\n".join(lines)[:1990])
            return

        if arg in _DAYS_ORDER:
            sess = tpl.get(arg)
            if not sess:
                await ctx.send(f"No session defined for {arg}.")
                return
            msg = f"**{plan['name']}**\n\n" + fmt_day(arg, sess)
            await ctx.send(msg[:1990])
            return

        if arg == "full":
            lines = [f"**{plan['name']}** — {plan.get('goal', '')}", ""]
            for day in _DAYS_ORDER:
                sess = tpl.get(day)
                if not sess:
                    continue
                lines.append(fmt_day(day, sess, include_notes=False))
                lines.append("")
            full = "\n".join(lines).strip()
            await _send_chunked(ctx, full)
            return

        await ctx.send(
            "Unknown arg. Usage: `/plan` (today), `/plan week`, `/plan full`, "
            "or `/plan <monday..sunday>`."
        )

    # ── /swap — override today's plan ─────────────────────────────────────
    #
    # Single deterministic way to flip today's prescription without editing
    # the weekly template. Backed by data/database.py:set_daily_override,
    # which UPSERTs by date. /swap reset clears the override.
    #
    # Vocab: user-facing names accepted (Push / Pull / Legs / Run / Rest /
    # Cross / Cross-train). Internally normalized to the same session_type
    # vocabulary the weekly_template uses ('lift' / 'run' / 'rest' /
    # 'cross_train'), with the user-facing label preserved as `focus`.
    # That way /plan today shows e.g. "LIFT — Pull" rather than the bare
    # session_type, which matches how the weekly template reads.

    _SWAP_ALIASES = {
        # session_type → list of accepted user inputs
        "lift_push":  {"push", "lift_push", "bench"},
        "lift_pull":  {"pull", "lift_pull", "row"},
        "lift_legs":  {"legs", "lift_legs", "squat", "deadlift"},
        "lift":       {"lift", "weights", "strength"},
        "run":        {"run", "easy", "tempo", "intervals", "long"},
        "rest":       {"rest", "off", "recovery"},
        "cross_train": {"cross", "cross-train", "crosstrain", "bike", "swim", "yoga"},
    }

    def _normalize_swap(label: str) -> tuple[str, str] | None:
        """Return (session_type, focus_label) for a user-typed swap target.

        Returns None if the input doesn't match any known category. The
        focus label is the title-cased original word so "/swap pull"
        becomes session_type='lift', focus='Pull' — readable in both
        adherence + brief output.
        """
        norm = (label or "").strip().lower()
        for kind, aliases in _SWAP_ALIASES.items():
            if norm in aliases:
                if kind.startswith("lift_"):
                    sub = kind.split("_", 1)[1].title()  # Push / Pull / Legs
                    return ("lift", sub)
                if kind == "lift":
                    return ("lift", "lift")
                if kind == "cross_train":
                    return ("cross_train", norm.title())
                return (kind, norm.title())
        return None

    @bot.hybrid_command(
        name="swap",
        description=(
            "Override today's plan. Usage: /swap <push|pull|legs|run|rest|cross>"
            " or /swap reset to clear."
        ),
    )
    @is_owner_hybrid
    async def swap_cmd(ctx: commands.Context, target: str = ""):
        today_iso = datetime.now(tz).date().isoformat()
        today_name = datetime.now(tz).strftime("%A")
        # Look up what the weekly template says for today, for the
        # before/after reply line.
        plan = await db.get_active_plan()
        tpl = (plan or {}).get("weekly_template") or {}
        base_session = tpl.get(today_name.lower()) or {}
        base_label = (base_session.get("session_type") or "rest").upper()
        base_focus = base_session.get("focus") or ""

        norm = (target or "").strip().lower()
        if not norm:
            await ctx.send(
                "Usage:\n"
                "  `/swap <push|pull|legs|run|rest|cross>`  — override today\n"
                "  `/swap reset`                            — clear today's override\n"
                "  `/swap status`                           — what's today's effective plan?"
            )
            return

        if norm == "status":
            sess = await db.get_effective_session_for_date(today_iso)
            if not sess:
                await ctx.send(f"No plan for {today_name}.")
                return
            stype = (sess.get("session_type") or "?").upper()
            focus = sess.get("focus") or ""
            if sess.get("is_override"):
                await ctx.send(
                    f"Today ({today_name}, {today_iso}): **{stype}** — {focus}  "
                    f"(override; weekly template says {base_label} — {base_focus})"
                )
            else:
                await ctx.send(
                    f"Today ({today_name}, {today_iso}): **{stype}** — {focus}  "
                    "(weekly template, no override)"
                )
            return

        if norm == "reset":
            removed = await db.clear_daily_override(today_iso)
            if removed:
                await ctx.send(
                    f"✅ Cleared today's override. Back to the weekly template: "
                    f"**{base_label}** — {base_focus}"
                )
            else:
                await ctx.send(
                    f"No override for today to clear. Weekly template stands: "
                    f"**{base_label}** — {base_focus}"
                )
            return

        normalized = _normalize_swap(norm)
        if not normalized:
            await ctx.send(
                f"Unknown swap target {target!r}. Try: push / pull / legs / run / "
                "rest / cross. Or `/swap reset` to clear."
            )
            return
        new_type, new_focus = normalized
        await db.set_daily_override(
            date=today_iso,
            session_type=new_type,
            focus=new_focus,
            prescription="",
            notes=f"manual /swap from weekly template ({base_label})",
            source="swap_cmd",
        )
        await ctx.send(
            f"✅ Switched today ({today_name} {today_iso}) → **{new_type.upper()}** "
            f"— {new_focus}.\nWeekly template ({base_label} — {base_focus}) unchanged. "
            "Use `/swap reset` to undo."
        )

    # ── /recovery — today's WHOOP recovery + one-line read ─────────────────

    @bot.hybrid_command(
        name="recovery",
        description="Today's WHOOP recovery score with a short interpretive read.",
    )
    @is_owner_hybrid
    async def recovery_cmd(ctx: commands.Context):
        await ctx.defer()
        # Try DB first for speed; fall back to live snapshot.
        today_iso = datetime.now(tz).date().isoformat()
        rows = await db.get_whoop_daily(today_iso, today_iso)
        row = rows[0] if rows else None
        if not row:
            try:
                snap = await coach.whoop.get_today_snapshot()
                rec = (snap or {}).get("recovery") or {}
                score = (rec.get("score") or {}).get("recovery_score")
                hrv = round((rec.get("score") or {}).get("hrv_rmssd_milli") or 0, 1)
                rhr = (rec.get("score") or {}).get("resting_heart_rate")
            except Exception as e:
                await ctx.send(f"Couldn't reach WHOOP: {e}")
                return
        else:
            score = row.get("recovery_score")
            hrv = row.get("hrv_rmssd_ms")
            rhr = row.get("resting_hr")

        read = _interpret_recovery(int(score) if score is not None else None)
        color = "🟢" if (score or 0) >= 67 else ("🟡" if (score or 0) >= 34 else "🔴")

        # Pull 12-mo baseline so the number has context.
        d365 = (datetime.now(tz).date() - timedelta(days=365)).isoformat()
        agg = await db.get_whoop_aggregates(d365, today_iso)
        baseline_rec = int(agg["avg_recovery"]) if agg and agg.get("avg_recovery") else None
        baseline_hrv = round(agg["avg_hrv"] or 0, 1) if agg and agg.get("avg_hrv") else None
        baseline_rhr = round(agg["avg_rhr"] or 0, 1) if agg and agg.get("avg_rhr") else None

        lines = [
            f"**Recovery** {color}",
            f"  Score: {int(score) if score is not None else '—'}%"
            + (f" (12-mo baseline {baseline_rec}%)" if baseline_rec else ""),
            f"  HRV: {hrv}ms"
            + (f" (baseline {baseline_hrv}ms)" if baseline_hrv else ""),
            f"  RHR: {int(rhr) if rhr else '—'} bpm"
            + (f" (baseline {baseline_rhr} bpm)" if baseline_rhr else ""),
            "",
            read,
        ]
        await ctx.send("\n".join(lines))

    # ── /sleep — last night's sleep ────────────────────────────────────────

    @bot.hybrid_command(
        name="sleep",
        description="Last night's sleep breakdown with a short read.",
    )
    @is_owner_hybrid
    async def sleep_cmd(ctx: commands.Context):
        await ctx.defer()
        today_iso = datetime.now(tz).date().isoformat()
        async with __import__("aiosqlite").connect(db.db_path) as conn:
            conn.row_factory = __import__("aiosqlite").Row
            async with conn.execute(
                "SELECT * FROM whoop_sleep WHERE date = ?", (today_iso,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            # Fall back to most recent row
            async with __import__("aiosqlite").connect(db.db_path) as conn:
                conn.row_factory = __import__("aiosqlite").Row
                async with conn.execute(
                    "SELECT * FROM whoop_sleep ORDER BY date DESC LIMIT 1"
                ) as cur:
                    row = await cur.fetchone()
        if not row:
            await ctx.send("No sleep data yet. WHOOP may not have synced overnight.")
            return
        r = dict(row)
        hrs = r.get("total_asleep_hours")
        eff = r.get("sleep_efficiency_pct")
        rem = r.get("rem_hours")
        sws = r.get("sws_hours")
        disturb = r.get("disturbance_count")

        read = _interpret_sleep(hrs, eff)
        lines = [
            f"**Sleep** — {r.get('date')}",
            f"  Asleep: {hrs}h"
            + (f" @ {int(eff)}% efficiency" if eff is not None else ""),
            f"  Stages: REM {rem}h | SWS {sws}h",
            f"  Disturbances: {disturb}" if disturb is not None else "",
            "",
            read,
        ]
        await ctx.send("\n".join(l for l in lines if l is not None and l != ""))

    # ── /strain — yesterday's strain ───────────────────────────────────────

    @bot.hybrid_command(
        name="strain",
        description="Yesterday's WHOOP strain with a short read.",
    )
    @is_owner_hybrid
    async def strain_cmd(ctx: commands.Context):
        await ctx.defer()
        today = datetime.now(tz).date()
        # Strain is a yesterday-facing number; if today's cycle is still open,
        # the most recent *closed* cycle is what we want.
        async with __import__("aiosqlite").connect(db.db_path) as conn:
            conn.row_factory = __import__("aiosqlite").Row
            async with conn.execute(
                "SELECT date, strain, average_hr, max_hr, kilojoule FROM whoop_cycle "
                "ORDER BY date DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
        if not row:
            await ctx.send("No strain data yet.")
            return
        r = dict(row)
        read = _interpret_strain(r.get("strain"))

        # 30d avg for context
        d30 = (today - timedelta(days=30)).isoformat()
        agg = await db.get_whoop_aggregates(d30, today.isoformat())
        avg30 = round(agg.get("avg_strain") or 0, 1) if agg else None

        lines = [
            f"**Strain** — {r.get('date')}",
            f"  Day strain: {round(r.get('strain') or 0, 1)}"
            + (f" (30d avg {avg30})" if avg30 else ""),
            f"  Avg HR: {int(r.get('average_hr') or 0)} | Max HR: {int(r.get('max_hr') or 0)}",
            "",
            read,
        ]
        await ctx.send("\n".join(lines))

    # ── /load — acute:chronic load + weekly volume ─────────────────────────

    @bot.hybrid_command(
        name="load",
        description="Training load: 7d strain avg, 28d strain avg, and acute:chronic ratio.",
    )
    @is_owner_hybrid
    async def load_cmd(ctx: commands.Context):
        await ctx.defer()
        today = datetime.now(tz).date()
        d7 = (today - timedelta(days=7)).isoformat()
        d28 = (today - timedelta(days=28)).isoformat()
        today_iso = today.isoformat()

        # WHOOP strain averages.
        agg7 = await db.get_whoop_aggregates(d7, today_iso)
        agg28 = await db.get_whoop_aggregates(d28, today_iso)
        avg7 = round(agg7.get("avg_strain") or 0, 2) if agg7 else 0
        avg28 = round(agg28.get("avg_strain") or 0, 2) if agg28 else 0
        acwr = round(avg7 / avg28, 2) if avg28 else 0

        # Strava volume for the same windows.
        s7 = await db.get_strava_aggregates(d7, today_iso)
        s28 = await db.get_strava_aggregates(d28, today_iso)
        mi7 = round((s7.get("total_distance_km") or 0) * 0.621371, 1) if s7 else 0
        mi28 = round((s28.get("total_distance_km") or 0) * 0.621371, 1) if s28 else 0

        flag = ""
        if acwr >= 1.5:
            flag = " ⚠ above injury-risk threshold (>1.5)"
        elif acwr and acwr <= 0.8:
            flag = " ↓ detraining zone (<0.8)"

        read = (
            "Acute load is outrunning chronic. Pull back the hard days, or pay it on the back end."
            if acwr >= 1.5
            else ("Load is on the light side of baseline — fine for a deload week, less fine if unintended."
                  if acwr and acwr <= 0.8
                  else "Load ratio is in the sustainable band. Keep stacking.")
        )

        lines = [
            f"**Training load**",
            f"  Strain 7d avg: {avg7}  |  28d avg: {avg28}",
            f"  ACWR (7d/28d): {acwr}{flag}",
            f"  Strava volume: {mi7} mi (7d) | {mi28} mi (28d)",
            "",
            read,
        ]
        await ctx.send("\n".join(lines))

    # ── /performance — multi-metric progress over a window ─────────────────

    @bot.hybrid_command(
        name="performance",
        description="Progress over a window (7d, 30d, 90d, ytd, all). Default 30d.",
    )
    @is_owner_hybrid
    async def performance_cmd(ctx: commands.Context, window: str = "30d"):
        await ctx.defer()
        days = _parse_window(window)
        today = datetime.now(tz).date()
        start = (today - timedelta(days=days)).isoformat()
        prev_start = (today - timedelta(days=days * 2)).isoformat()
        prev_end = (today - timedelta(days=days + 1)).isoformat()
        today_iso = today.isoformat()

        # Current window
        cur_w = await db.get_whoop_aggregates(start, today_iso)
        cur_s = await db.get_strava_aggregates(start, today_iso)
        prev_w = await db.get_whoop_aggregates(prev_start, prev_end)
        prev_s = await db.get_strava_aggregates(prev_start, prev_end)

        def _round(v, n=1):
            return round(v, n) if v is not None else None

        cur_rec = _round(cur_w.get("avg_recovery"), 0) if cur_w else None
        prv_rec = _round(prev_w.get("avg_recovery"), 0) if prev_w else None
        cur_hrv = _round(cur_w.get("avg_hrv"), 1) if cur_w else None
        prv_hrv = _round(prev_w.get("avg_hrv"), 1) if prev_w else None
        cur_rhr = _round(cur_w.get("avg_rhr"), 1) if cur_w else None
        prv_rhr = _round(prev_w.get("avg_rhr"), 1) if prev_w else None
        cur_sleep = _round(cur_w.get("avg_sleep_hours"), 1) if cur_w else None
        prv_sleep = _round(prev_w.get("avg_sleep_hours"), 1) if prev_w else None
        cur_strain = _round(cur_w.get("avg_strain"), 1) if cur_w else None
        prv_strain = _round(prev_w.get("avg_strain"), 1) if prev_w else None

        cur_mi = round((cur_s.get("total_distance_km") or 0) * 0.621371, 1) if cur_s else 0
        prv_mi = round((prev_s.get("total_distance_km") or 0) * 0.621371, 1) if prev_s else 0
        cur_hrs = round(cur_s.get("total_hours") or 0, 1) if cur_s else 0
        prv_hrs = round(prev_s.get("total_hours") or 0, 1) if prev_s else 0
        cur_acts = cur_s.get("activity_count") if cur_s else 0
        prv_acts = prev_s.get("activity_count") if prev_s else 0

        # Lift count in window
        async with __import__("aiosqlite").connect(db.db_path) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM lifts WHERE date BETWEEN ? AND ?",
                (start, today_iso),
            ) as cur:
                (cur_lifts,) = await cur.fetchone()
            async with conn.execute(
                "SELECT COUNT(*) FROM lifts WHERE date BETWEEN ? AND ?",
                (prev_start, prev_end),
            ) as cur:
                (prv_lifts,) = await cur.fetchone()

        def line(label, cur_v, prv_v, unit="", good_up=True):
            if cur_v is None and prv_v is None:
                return f"  {label}: —"
            delta = ""
            if cur_v is not None and prv_v is not None and prv_v != 0:
                diff = cur_v - prv_v
                sign = "+" if diff > 0 else ""
                arrow = "→"
                # significant move?
                pct = abs(diff / prv_v) * 100 if prv_v else 0
                if pct >= 3:
                    if diff > 0:
                        arrow = "↑" if good_up else "↑(worse)"
                    else:
                        arrow = "↓(worse)" if good_up else "↓(better)"
                delta = f" {arrow} {sign}{round(diff, 1)}{unit}"
            return f"  {label}: {cur_v}{unit} (prev {prv_v}{unit}){delta}"

        # Window label for the header
        if window.lower() in ("ytd",):
            header_window = "Year-to-date"
        elif window.lower() in ("all",):
            header_window = "All time"
        else:
            header_window = f"Last {days} days"

        lines = [
            f"**Performance — {header_window}** (vs. prior equivalent period)",
            "",
            "**Recovery & body**",
            line("Recovery", cur_rec, prv_rec, "%", good_up=True),
            line("HRV", cur_hrv, prv_hrv, "ms", good_up=True),
            line("RHR", cur_rhr, prv_rhr, " bpm", good_up=False),
            line("Sleep", cur_sleep, prv_sleep, "h", good_up=True),
            "",
            "**Training volume**",
            line("Activities", cur_acts, prv_acts, "", good_up=True),
            line("Miles", cur_mi, prv_mi, " mi", good_up=True),
            line("Hours moving", cur_hrs, prv_hrs, "h", good_up=True),
            line("Avg strain", cur_strain, prv_strain, "", good_up=True),
            line("Lifts logged", cur_lifts, prv_lifts, "", good_up=True),
        ]

        # Ask Claude for the one-line read at the top. Keep it cheap — Haiku.
        # Voice: CoachRex-style, concrete not preachy.
        summary_prompt = (
            "You're CoachRex. In ONE short sentence (under 30 words), read "
            "the honest story in this window vs. the prior one. Plain language, "
            "no philosophy-poster phrasing, no 'keep pushing' nonsense. "
            "If the window is too short to have signal, say that. Data:\n\n"
            + "\n".join(lines)
        )
        try:
            resp = await coach.claude.messages.create(
                model=coach.cheap_model,
                max_tokens=120,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            one_liner = resp.content[0].text.strip()
            lines.insert(1, "")
            lines.insert(2, f"_{one_liner}_")
        except Exception as e:
            logger.warning(f"/performance one-liner failed: {e}")

        # Note on window signal quality — short windows lie about body-comp/HRV.
        if days <= 7:
            lines.append("")
            lines.append("_Note: 7d is recent signal, not a trend. Rolling "
                         "averages like HRV and weight need ~3 weeks to stabilize._")

        await _send_chunked(ctx, "\n".join(lines))

    # ── /ask — conversational with an explicit slash UX ────────────────────

    @bot.hybrid_command(
        name="ask",
        description="Ask CoachRex a question with full context.",
    )
    @is_owner_hybrid
    async def ask_cmd(ctx: commands.Context, *, question: str):
        await ctx.defer()
        reply = await coach.chat(question)
        await _send_chunked(ctx, reply)

    # ── /log — quick subjective note ──────────────────────────────────────

    @bot.hybrid_command(
        name="log",
        description="Log a quick note (soreness, mood, fuel, sleep quality, etc.).",
    )
    @is_owner_hybrid
    async def log_cmd(ctx: commands.Context, *, note: str):
        today_iso = datetime.now(tz).strftime("%Y-%m-%d")
        await db.log_note(today_iso, note)
        await ctx.send(f"Logged for {today_iso}.")

    # ── /workout — today's recommended session ────────────────────────────

    @bot.hybrid_command(
        name="workout",
        description="Today's recommended workout, modulated by recovery and load.",
    )
    @is_owner_hybrid
    async def workout_cmd(ctx: commands.Context):
        await ctx.defer()
        text = await coach.recommend_workout()
        await _send_chunked(ctx, text)

    # ── /liftstart — begin a guided, set-by-set lift session ──────────────

    @bot.hybrid_command(
        name="liftstart",
        description="Start a guided lift session. The bot prompts each set with a recommended weight.",
    )
    @is_owner_hybrid
    async def liftstart_cmd(ctx: commands.Context, force: Optional[str] = None):
        """Begin a guided lift session.

        Pulls today's planned session from the active plan, parses the
        prescription into a structured exercise list, and walks Dylan
        through each set: bot says "Bench, set 1 — recommended 155 x 6",
        Dylan replies with what he actually did ("150 x 6"), bot logs
        and moves to the next set.

        While a session is active, ALL non-command messages route through
        the session handler — not the general chat coach. /liftend (or
        replying "stop") closes it; 2h of silence auto-ends it.

        Pass `force:true` to start a session on a non-lift day (e.g. you
        decided to lift on a planned rest day).
        """
        await ctx.defer()
        force_flag = (force or "").strip().lower() in ("true", "1", "yes", "force")
        text = await coach.start_lift_session(force=force_flag)
        await _send_chunked(ctx, text)

    @bot.hybrid_command(
        name="liftend",
        description="End the active lift session and get a recap.",
    )
    @is_owner_hybrid
    async def liftend_cmd(ctx: commands.Context):
        await ctx.defer()
        text = await coach.end_lift_session()
        await _send_chunked(ctx, text)

    # ── /debrief — post-workout breakdown (WHOOP + Strava merged) ─────────

    @bot.hybrid_command(
        name="debrief",
        description="Debrief your most recent workout — WHOOP HR + Strava pace merged.",
    )
    @is_owner_hybrid
    async def debrief_cmd(
        ctx: commands.Context,
        hours: int = 8,
        activity_id: Optional[int] = None,
    ):
        """Usage: /debrief             → last 8h window.
                  /debrief 24          → widen the lookback.
                  /debrief activity_id=<id>  → debrief a specific Strava activity.

        The debrief is WHOOP-authoritative for HR and zones and Strava-
        authoritative for pace/distance. If Strava hasn't synced yet we still
        return a HR/zone debrief rather than silently blocking — webhooks
        usually catch up within a minute or two.
        """
        await ctx.defer()
        try:
            text = await coach.debrief_run(
                hours_back=max(1, int(hours)),
                activity_id=activity_id,
            )
        except Exception as e:
            logger.error(f"/debrief failed: {e}", exc_info=True)
            await ctx.send(f"Debrief failed: {e}")
            return
        await _send_chunked(ctx, text)

    # ── /pr — log a PR (thin wrapper over !log for now) ───────────────────

    @bot.hybrid_command(
        name="pr",
        description="Log a personal record (e.g. 'bench 225 1RM' or '5k 22:14').",
    )
    @is_owner_hybrid
    async def pr_cmd(ctx: commands.Context, *, detail: str):
        today_iso = datetime.now(tz).strftime("%Y-%m-%d")
        note = f"PR: {detail}"
        await db.log_note(today_iso, note)
        # Also push through lift parser in case it's a weightlifting PR.
        try:
            parsed = await coach._try_parse_lift(detail)
            if parsed:
                await db.log_lift(
                    date=today_iso,
                    exercise=parsed["exercise"],
                    details=parsed["details"] + " (PR)",
                    raw=detail,
                )
        except Exception as e:
            logger.debug(f"/pr lift parse failed (non-fatal): {e}")
        await ctx.send(f"PR logged: {detail}")

    # ── /streak — show current training streaks ────────────────────────────

    @bot.hybrid_command(
        name="streak",
        description="Current training streaks (lift days, run days, active days).",
    )
    @is_owner_hybrid
    async def streak_cmd(ctx: commands.Context):
        await ctx.defer()
        today = datetime.now(tz).date()
        # Active days = day had either a Strava activity OR a logged lift
        async with __import__("aiosqlite").connect(db.db_path) as conn:
            async with conn.execute(
                "SELECT DISTINCT date FROM strava_activities "
                "WHERE date BETWEEN ? AND ? ORDER BY date DESC",
                ((today - timedelta(days=90)).isoformat(), today.isoformat()),
            ) as cur:
                strava_days = {r[0] for r in await cur.fetchall()}
            async with conn.execute(
                "SELECT DISTINCT date FROM lifts "
                "WHERE date BETWEEN ? AND ? ORDER BY date DESC",
                ((today - timedelta(days=90)).isoformat(), today.isoformat()),
            ) as cur:
                lift_days = {r[0] for r in await cur.fetchall()}

        active_days = strava_days | lift_days

        # Current active streak ending today or yesterday
        def streak_from(day_set: set, end_date: date) -> int:
            n = 0
            d = end_date
            # allow a 1-day grace (yesterday counts as "current" at 6am today)
            grace = 0
            while True:
                if d.isoformat() in day_set:
                    n += 1
                    d = d - timedelta(days=1)
                else:
                    if grace == 0 and d == end_date:
                        grace = 1
                        d = d - timedelta(days=1)
                        continue
                    break
            return n

        cur_active = streak_from(active_days, today)
        cur_lift = streak_from(lift_days, today)

        # Weekly consistency: last 4 weeks, sessions per week
        weekly = []
        for w in range(4):
            w_end = today - timedelta(days=w * 7)
            w_start = w_end - timedelta(days=6)
            count = sum(
                1 for ds in active_days
                if w_start.isoformat() <= ds <= w_end.isoformat()
            )
            weekly.append((w_start, count))

        lines = [
            "**Streaks**",
            f"  Active-day streak: {cur_active} day{'s' if cur_active != 1 else ''}",
            f"  Lift-day streak: {cur_lift} day{'s' if cur_lift != 1 else ''}",
            "",
            "**Last 4 weeks (active days / 7)**",
        ]
        for w_start, count in weekly:
            marker = "  ← this week" if w_start <= today <= w_start + timedelta(days=6) else ""
            lines.append(f"  week of {w_start.isoformat()}: {count}/7{marker}")
        lines.append("")
        lines.append("_Streaks are a lagging indicator of effort, not a moral scoreboard. "
                     "A broken streak isn't failure; it's information._")
        await ctx.send("\n".join(lines))

    # ── /reflect — on-demand Sunday-style reflection ───────────────────────

    @bot.hybrid_command(
        name="reflect",
        description="On-demand Stoic reflection on the recent training arc.",
    )
    @is_owner_hybrid
    async def reflect_cmd(ctx: commands.Context):
        await ctx.defer()
        text = await coach.stoic_reflection()
        await _send_chunked(ctx, text)

    # ── /composition — body-comp snapshot (gated by data source) ──────────

    @bot.hybrid_command(
        name="composition",
        description="Body composition snapshot (weight; BF%/lean mass pending FitDays pipeline).",
    )
    @is_owner_hybrid
    async def composition_cmd(ctx: commands.Context):
        await ctx.defer()
        # WHOOP's public API only exposes weight + height + max HR.
        try:
            body = await coach.whoop.get_body_measurement()
        except Exception as e:
            logger.warning(f"WHOOP body fetch failed: {e}")
            body = None
        lines = ["**Composition snapshot**"]
        if body:
            w_lb = _kg_to_lb(body.get("weight_kilogram"))
            h_m = body.get("height_meter")
            h_in = round((h_m or 0) * 39.3701, 1) if h_m else None
            lines.append(f"  Weight: {w_lb} lb" if w_lb is not None else "  Weight: —")
            lines.append(f"  Height: {h_in}\"" if h_in else "")
            lines.append(f"  Max HR (on file): {body.get('max_heart_rate')}")
        else:
            lines.append("  No body measurement available from WHOOP.")
        lines.append("")
        lines.append(
            "_BF%, lean mass, visceral fat, and body water are NOT exposed by the WHOOP v2 API. "
            "To add them, wire FitDays → Apple Health → webhook (or manual CSV import). "
            "See composition-source TODO in README._"
        )
        await ctx.send("\n".join(l for l in lines if l))

    # ── Stubs for commands that need new data sources ─────────────────────
    # Each replies with a clear "coming soon" + what's blocking.

    @bot.hybrid_command(
        name="uv",
        description="[stub] Today's UV peak window. Needs a UV data source.",
    )
    @is_owner_hybrid
    async def uv_cmd(ctx: commands.Context):
        # TODO: integrate OpenUV (openuv.io — free tier), or Open-Meteo's UV
        # index (no auth, free). Open-Meteo fits cleanest with the existing
        # weather.py client. Then compute peak window + duration to hit a
        # daily target dose.
        await ctx.send(
            "_/uv isn't wired yet — needs a UV index source. "
            "Open-Meteo's free UV endpoint is the simplest drop-in; "
            "can live next to integrations/weather.py._"
        )

    @bot.hybrid_command(
        name="daylight",
        description="[stub] Sunrise, solar noon, sunset for today.",
    )
    @is_owner_hybrid
    async def daylight_cmd(ctx: commands.Context):
        # TODO: Either compute locally (astral / suncalc) or hit
        # api.sunrise-sunset.org (free, no auth). Use HOME_LAT/HOME_LNG.
        await ctx.send(
            "_/daylight isn't wired yet — will compute from HOME_LAT/LNG "
            "once a small astronomy helper lands (astral lib or "
            "api.sunrise-sunset.org)._"
        )

    @bot.hybrid_command(
        name="swap",
        description="[stub] Swap today's planned session for an alternative at equivalent stimulus.",
    )
    @is_owner_hybrid
    async def swap_cmd(ctx: commands.Context, *, reason: str = ""):
        # TODO: coach method that takes today's planned session + a reason
        # (tired legs, no gym, etc.) and returns a same-stimulus alternative.
        await ctx.send(
            "_/swap isn't wired yet — the planner has to know the stimulus "
            "space (aerobic/threshold/strength/power) to substitute safely. "
            "Coming after /workout bakes for a bit._"
        )

    @bot.hybrid_command(
        name="warmup",
        description="[stub] A 5–10 minute warmup tuned to today's planned session.",
    )
    @is_owner_hybrid
    async def warmup_cmd(ctx: commands.Context):
        # TODO: small coach method; warmup is highly deterministic once
        # session_type is known (run → lunge matrix + strides; lift → joint
        # prep + working-weight ramps). Can be template-based, no Claude needed.
        await ctx.send(
            "_/warmup isn't wired yet — will be template-driven once the "
            "per-session templates land._"
        )

    # ── /goal slash group (add / list / progress / retire) ────────────────

    goal_group = app_commands.Group(
        name="goal",
        description="Manage training goals.",
    )

    @goal_group.command(
        name="add",
        description="Add a new goal. Type: weight, pace, strength, bf, or habit.",
    )
    @app_commands.describe(
        goal_type="weight | pace | strength | bf | habit",
        title="Short description, e.g. 'cut to 175 lb' or 'bench 225'",
        target="Target value (e.g. 175, 225, 22.5 for minutes)",
        unit="Unit: lb | sec_per_mi | pct | sessions_per_week (optional)",
        deadline="Deadline YYYY-MM-DD (optional)",
        exercise="For strength goals: the lift name (e.g. bench)",
    )
    @is_owner_slash
    async def goal_add(
        interaction: discord.Interaction,
        goal_type: str,
        title: str,
        target: float,
        unit: Optional[str] = None,
        deadline: Optional[str] = None,
        exercise: Optional[str] = None,
    ):
        await interaction.response.defer()
        goal_type = goal_type.lower().strip()
        if goal_type not in ("weight", "pace", "strength", "bf", "habit"):
            await interaction.followup.send(
                "Unknown goal type. Use: weight, pace, strength, bf, or habit."
            )
            return

        # Capture a baseline from the appropriate source so progress math works.
        baseline_value: Optional[float] = None
        baseline_date: Optional[str] = None
        today_iso = datetime.now(tz).date().isoformat()

        metadata: dict = {}
        if goal_type == "strength":
            if not exercise:
                await interaction.followup.send(
                    "Strength goals need an `exercise` arg (e.g. bench, squat)."
                )
                return
            metadata["exercise"] = exercise
            # Latest logged lift for this exercise as baseline (user can correct later).
            rows = await db.get_lifts_for_exercise(exercise, limit=1)
            if rows:
                baseline_date = rows[0]["date"]
                # Don't try to parse weight out of free-form details — leave null,
                # user can edit. Just record the most recent session date.

        elif goal_type == "weight":
            # WHOOP API gives latest weight only.
            try:
                body = await coach.whoop.get_body_measurement()
                if body and body.get("weight_kilogram"):
                    baseline_value = _kg_to_lb(body["weight_kilogram"])
                    baseline_date = today_iso
            except Exception as e:
                logger.info(f"Could not capture weight baseline: {e}")

        elif goal_type == "bf":
            # We can't auto-capture BF% yet — no API source.
            metadata["note"] = (
                "BF% baseline must be entered manually; WHOOP API does not expose it. "
                "Wire FitDays → Apple Health → webhook to automate."
            )

        elif goal_type == "habit":
            metadata["note"] = "Session count tracked from Strava + lifts."

        elif goal_type == "pace":
            # Pace goals should anchor to an HR or a distance. Capture both in metadata.
            metadata["note"] = (
                "Pace goal — best anchored to a specific effort (e.g. Z2 pace at 145 bpm) "
                "or a specific race distance. Edit metadata later if needed."
            )

        goal_id = await db.create_goal(
            goal_type=goal_type,
            title=title,
            target_value=target,
            target_unit=unit or "",
            baseline_value=baseline_value,
            baseline_date=baseline_date or today_iso,
            deadline=deadline,
            metadata=metadata,
        )
        msg = [
            f"✅ Goal #{goal_id} added: **{title}**",
            f"  Type: {goal_type}",
            f"  Target: {target}{' ' + unit if unit else ''}",
        ]
        if baseline_value is not None:
            msg.append(f"  Baseline: {baseline_value}{' ' + unit if unit else ''} ({baseline_date})")
        if deadline:
            msg.append(f"  Deadline: {deadline}")
        if metadata.get("note"):
            msg.append(f"  Note: {metadata['note']}")
        await interaction.followup.send("\n".join(msg))

    @goal_group.command(
        name="list",
        description="List active goals (use 'all' arg to include retired/completed).",
    )
    @is_owner_slash
    async def goal_list(interaction: discord.Interaction, scope: Optional[str] = "active"):
        await interaction.response.defer()
        goals = await db.list_goals(status=None if (scope or "").lower() == "all" else "active")
        if not goals:
            await interaction.followup.send("No goals yet. Add one with `/goal add`.")
            return
        lines = [f"**Goals ({len(goals)})**"]
        for g in goals:
            lines.append(
                f"  #{g['id']} [{g['status']}] {g['goal_type']}: {g['title']} → "
                f"{g.get('target_value')}{' ' + g.get('target_unit') if g.get('target_unit') else ''}"
                + (f" by {g.get('deadline')}" if g.get("deadline") else "")
            )
        await interaction.followup.send("\n".join(lines))

    @goal_group.command(
        name="progress",
        description="Show progress on a goal by id.",
    )
    @is_owner_slash
    async def goal_progress(interaction: discord.Interaction, goal_id: int):
        await interaction.response.defer()
        goal = await db.get_goal(goal_id)
        if not goal:
            await interaction.followup.send(f"No goal with id {goal_id}.")
            return
        prog = await db.compute_goal_progress(goal, coach=coach)
        lines = [
            f"**Goal #{goal['id']} — {goal['title']}**",
            f"  Type: {goal['goal_type']} | Status: {goal['status']}",
        ]
        if goal.get("baseline_value") is not None:
            lines.append(
                f"  Baseline: {goal['baseline_value']}{' ' + (goal.get('target_unit') or '')}"
                + (f" ({goal.get('baseline_date')})" if goal.get('baseline_date') else "")
            )
        if prog.get("current_value") is not None:
            lines.append(f"  Current: {prog['current_value']}{' ' + (goal.get('target_unit') or '')}")
        if goal.get("target_value") is not None:
            lines.append(f"  Target: {goal['target_value']}{' ' + (goal.get('target_unit') or '')}")
        if prog.get("pct_done") is not None:
            lines.append(f"  Progress: {prog['pct_done']}%")
        if prog.get("eta"):
            lines.append(f"  Projected ETA: {prog['eta']}")
        if prog.get("note"):
            lines.append(f"  Note: {prog['note']}")
        await interaction.followup.send("\n".join(lines))

    @goal_group.command(
        name="retire",
        description="Close a goal — mark done, abandoned, or paused.",
    )
    @app_commands.describe(
        goal_id="Goal id from /goal list",
        outcome="completed | abandoned | paused",
        note="Optional close note",
    )
    @is_owner_slash
    async def goal_retire(
        interaction: discord.Interaction,
        goal_id: int,
        outcome: str = "completed",
        note: Optional[str] = None,
    ):
        await interaction.response.defer()
        outcome = outcome.lower().strip()
        if outcome not in ("completed", "abandoned", "paused"):
            await interaction.followup.send(
                "Outcome must be: completed, abandoned, or paused."
            )
            return
        ok = await db.update_goal_status(goal_id, outcome, note=note or "")
        if not ok:
            await interaction.followup.send(f"No goal with id {goal_id}.")
            return
        await interaction.followup.send(f"Goal #{goal_id} → {outcome}.")

    bot.tree.add_command(goal_group)

    # ── Error reporting for owner-gated slash failures ─────────────────────

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.CheckFailure):
            # Silent fail — don't leak command existence to non-owners.
            try:
                await interaction.response.send_message(
                    "This bot is personal.", ephemeral=True
                )
            except Exception:
                pass
            return
        logger.error(f"Slash command error: {error}", exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"Command failed: {error}")
            else:
                await interaction.response.send_message(
                    f"Command failed: {error}", ephemeral=True
                )
        except Exception:
            pass
