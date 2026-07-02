"""
Coach Aurelius API — a thin FastAPI layer over the existing coaching brain.

Wraps data/database.py (and, lazily, ai/coach.py for LLM endpoints) and exposes
the JSON shapes the iOS app's lib/api.js already expects: /today, /train,
/trends, /goals, /coach plus POST /chat, /log-set, /recovery, /goals, /body.

Run it:
    pip install fastapi "uvicorn[standard]"
    FITNESS_API_TOKEN=<some-secret> uvicorn api_server:app --host 0.0.0.0 --port 8000

Auth: every request must send `Authorization: Bearer $FITNESS_API_TOKEN`.
If FITNESS_API_TOKEN is unset, auth is disabled (dev only).

Data endpoints read straight from SQLite — no LLM, so they work even when the
Anthropic key / live WHOOP token are absent. /chat and the /today brief use the
Coach (Anthropic) and degrade gracefully if it's unavailable.
"""

import os
import json
import logging
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import datetime, timedelta

import aiosqlite
import pytz
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware

from config import Config
from data.database import Database

logger = logging.getLogger("api_server")

config = Config()
db = Database(config.DB_PATH)
API_TOKEN = os.getenv("FITNESS_API_TOKEN", "")
TZ = pytz.timezone(getattr(config, "TIMEZONE", "America/New_York"))

# Coach is heavy (Anthropic + integration clients) — build it on first LLM use.
_coach = None


def get_coach():
    global _coach
    if _coach is None:
        from ai.coach import Coach
        _coach = Coach(config, db)
    return _coach


# Per-local-date cache for the morning brief so repeated app opens don't re-bill.
_brief_cache: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.initialize()
    yield


app = FastAPI(title="Coach Aurelius API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


async def auth(authorization: str = Header(default="")):
    if API_TOKEN and authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")
    return True


# ── helpers ────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(TZ)


def _today_iso():
    return _now().strftime("%Y-%m-%d")


def _round(v, n=0):
    if v is None:
        return None
    return round(v, n) if n else round(v)


def _hours_label(h):
    if not h:
        return "—"
    mins = round(h * 60)
    return f"{mins // 60}h {mins % 60:02d}m"


def _epley(weight, reps):
    if not weight or not reps:
        return None
    return weight * (1 + reps / 30.0)


def _weekly(rows, date_key, value_fn, agg="max", weeks=8):
    """Bucket rows into ISO weeks and aggregate. Returns a chronological list."""
    buckets = defaultdict(list)
    for r in rows:
        d = r.get(date_key)
        if not d:
            continue
        try:
            dt = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        v = value_fn(r)
        if v is not None:
            buckets[dt.isocalendar()[:2]].append(v)
    out = []
    for k in sorted(buckets.keys())[-weeks:]:
        vals = buckets[k]
        if agg == "max":
            out.append(max(vals))
        elif agg == "sum":
            out.append(sum(vals))
        else:
            out.append(sum(vals) / len(vals))
    return out


def _trend_card(key, label, series, unit="", decimals=0, lower_is_better=False):
    if not series:
        return {"key": key, "label": label, "current": "—", "delta": "—", "good": True, "series": []}
    last, first = series[-1], series[0]
    cur = f"{round(last, decimals) if decimals else round(last)}{(' ' + unit) if unit else ''}"
    if first and last != first:
        pct = (last - first) / abs(first) * 100
        delta = f"{'+' if pct >= 0 else ''}{round(pct, 1)}%"
        rising = last > first
        good = (not rising) if lower_is_better else rising
    else:
        delta, good = "—", True
    return {"key": key, "label": label, "current": cur, "delta": delta, "good": good, "series": [round(v, 2) for v in series]}


# ── GET endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True, "latest_whoop": await db.get_latest_whoop_date()}


@app.get("/today", dependencies=[Depends(auth)])
async def today():
    now = _now()
    today_iso = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    daily = await db.get_whoop_daily(start, today_iso)  # date DESC
    if not daily:
        # Stale/late sync — fall back to the most recent reading we have so the
        # Today screen always shows your latest known recovery, not blanks.
        daily = await db.get_whoop_daily((now - timedelta(days=400)).strftime("%Y-%m-%d"), today_iso)
    latest = daily[0] if daily else {}

    agg = await db.get_whoop_aggregates((now - timedelta(days=30)).strftime("%Y-%m-%d"), today_iso)
    baseline = _round(agg.get("avg_recovery")) if agg else None

    sess = await db.get_effective_session_for_date(today_iso) or {}

    # Recovery sessions logged today.
    rec_rows = await db.get_recent_recovery_sessions(days=2)
    rec_today = [
        {"id": i + 1, "type": (r.get("session_type") or "").title(), "duration": r.get("duration_min"),
         "temp": r.get("temp_f"), "source": "manual"}
        for i, r in enumerate(rec_rows) if str(r.get("date"))[:10] == today_iso
    ]

    # Brief (LLM, cached per day, degrades to an assembled summary).
    brief_body = _brief_cache.get(today_iso)
    if brief_body is None:
        try:
            brief_body = await get_coach().daily_brief()
            _brief_cache[today_iso] = brief_body
        except Exception as e:
            logger.warning("daily_brief unavailable: %s", e)
            brief_body = _assemble_brief(latest, sess)

    quote = _daily_quote(today_iso)
    hour = now.hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"

    return {
        "greeting": greeting,
        "date": now.strftime("%A, %B %-d"),
        "recovery": {
            "score": _round(latest.get("recovery_score")),
            "hrv": _round(latest.get("hrv_rmssd_ms")),
            "rhr": _round(latest.get("resting_hr")),
            "baseline": baseline,
        },
        "sleep": {
            "label": _hours_label(latest.get("total_asleep_hours")),
            "efficiency": _round(latest.get("sleep_efficiency_pct")),
            "hours": latest.get("total_asleep_hours"),
        },
        "strain": {"yesterday": _round(latest.get("strain"), 1)},
        "brief": {"body": brief_body, "quote": quote},
        "session": {
            "type": (sess.get("session_type") or "rest").title(),
            "focus": (sess.get("focus") or sess.get("prescription", "")[:80] or "").strip(),
            "isOverride": bool(sess.get("is_override")),
        },
        "recoverySessions": rec_today,
    }


@app.get("/train", dependencies=[Depends(auth)])
async def train():
    now = _now()
    today_iso = now.strftime("%Y-%m-%d")
    sess = await db.get_effective_session_for_date(today_iso) or {}

    # Today's prescribed exercises aren't structured in the plan, so surface the
    # most recent logged session's lifts as the working set (real "last" data).
    # Grouped one entry per exercise with its structured sets from lift_sets;
    # falls back to the free-text lifts rows when a date predates set logging.
    recent_lifts = await db.get_recent_lifts(days=21)
    by_date = defaultdict(list)
    for l in recent_lifts:
        by_date[str(l.get("date"))[:10]].append(l)
    dates = sorted(by_date.keys(), reverse=True)

    def _set_label(s):
        w, r = s.get("weight_lb"), s.get("reps")
        label = f"{_round(w)} lb × {r}" if w and r else (f"{r} reps" if r else (s.get("notes") or "—"))
        return f"{label} · failure" if s.get("to_failure") else label

    exercises = []
    if dates:
        grouped = {}  # name → [set labels], insertion-ordered
        set_rows = await db.get_lift_sets_for_date(dates[0])
        for s in set_rows:
            name = (s.get("exercise") or "").title()
            grouped.setdefault(name, []).append(_set_label(s))
        if not grouped:
            for l in by_date[dates[0]]:
                name = (l.get("exercise") or "").title()
                grouped.setdefault(name, []).append(l.get("details") or "—")
        for name, sets in grouped.items():
            exercises.append({
                "name": name,
                "sets": sets,
                "summary": f"{len(sets)} set{'s' if len(sets) != 1 else ''}",
            })

    # Recent workouts: one per day, volume from lift_sets.
    recent = []
    for d in dates[:5]:
        sets = await db.get_lift_sets_for_date(d)
        vol = sum((s.get("weight_lb") or 0) * (s.get("reps") or 0) for s in sets)
        ds = await db.get_effective_session_for_date(d) or {}
        try:
            label = (ds.get("focus") or ds.get("session_type") or "Workout").title()
        except Exception:
            label = "Workout"
        recent.append({
            "date": datetime.strptime(d, "%Y-%m-%d").strftime("%a · %b %-d"),
            "label": label,
            "volume": f"{int(vol):,} lb" if vol else "—",
            "duration": "—",
            "pr": None,
        })

    return {
        "session": {
            "type": (sess.get("session_type") or "rest").title(),
            "focus": (sess.get("focus") or "").strip(),
            "exercises": exercises,
        },
        "recent": recent,
    }


@app.get("/trends", dependencies=[Depends(auth)])
async def trends():
    now = _now()
    today_iso = now.strftime("%Y-%m-%d")
    wk8 = (now - timedelta(weeks=8)).strftime("%Y-%m-%d")

    bench_sets = await db.get_lift_sets_for_exercise("bench", weeks=8)
    bench = _trend_card("bench", "Bench est. 1RM",
                        _weekly(bench_sets, "date", lambda r: _epley(r.get("weight_lb"), r.get("reps")), "max"),
                        unit="lb")

    runs = await db.get_strava_activities_range(wk8, today_iso)
    runs = [r for r in runs if "run" in (r.get("sport_type") or "").lower()]
    mileage = _trend_card("mileage", "Weekly mileage",
                          _weekly(runs, "date", lambda r: (r.get("distance_m") or 0) / 1609.34, "sum"),
                          unit="mi")

    daily = await db.get_whoop_daily(wk8, today_iso)
    hrv = _trend_card("hrv", "HRV (30-day)",
                      _weekly(daily, "date", lambda r: r.get("hrv_rmssd_ms"), "avg"), unit="ms")

    body = await db.get_body_measurement_history(days=60)
    bw = _trend_card("bodyweight", "Body weight",
                     _weekly(body, "date", lambda r: (r.get("weight_kg") or 0) * 2.20462 if r.get("weight_kg") else None, "avg"),
                     unit="lb", decimals=1, lower_is_better=True)

    split = await _run_split(today_iso)
    recovery = await _recovery_summary()
    body_comp = await _body_comp(body)

    return {"bench": bench, "mileage": mileage, "hrv": hrv, "bodyweight": bw,
            "split": split, "recovery": recovery, "bodyComp": body_comp}


@app.get("/goals", dependencies=[Depends(auth)])
async def goals():
    rows = await db.list_goals(status="active")
    active = []
    coach = None
    for g in rows:
        try:
            prog = await db.compute_goal_progress(g, coach=coach)
        except Exception:
            prog = {}
        cur = prog.get("current_value")
        base = g.get("baseline_value")
        active.append({
            "id": g.get("id"),
            "title": g.get("title"),
            "tag": (g.get("goal_type") or "").title(),
            "current": cur if cur is not None else (base or 0),
            "start": base if base is not None else 0,
            "target": g.get("target_value") or 0,
            "unit": g.get("target_unit") or "",
            "eta": prog.get("eta") or (prog.get("note") or "In progress")[:40],
        })

    plan = await _weekly_plan()
    integrations = await _integrations()
    return {"active": active, "plan": plan, "integrations": integrations}


@app.get("/coach", dependencies=[Depends(auth)])
async def coach():
    daily = await db.get_whoop_daily((_now() - timedelta(days=3)).strftime("%Y-%m-%d"), _today_iso())
    rec = daily[0].get("recovery_score") if daily else None
    opener = (f"Morning. {round(rec)}% recovery — let's look at today." if rec is not None
              else "Morning. Ask me anything about today's training.")
    return {
        "messages": [{"id": 1, "from": "coach", "text": opener}],
        "suggestions": ["How has my bench progressed?", "Plan my week", "Why am I tired?"],
    }


# ── POST endpoints (writes) ──────────────────────────────────────────────────

@app.post("/chat", dependencies=[Depends(auth)])
async def chat(payload: dict = Body(...)):
    msg = (payload or {}).get("message", "")
    try:
        reply = await get_coach().chat(msg)
    except Exception as e:
        logger.warning("chat failed: %s", e)
        raise HTTPException(status_code=503, detail="coach unavailable")
    return {"reply": reply}


@app.post("/log-set", dependencies=[Depends(auth)])
async def log_set(payload: dict = Body(...)):
    p = payload or {}
    date = p.get("date") or _today_iso()
    exercise = p.get("exercise", "exercise")
    reps = p.get("reps")
    weight = p.get("weightLb") or p.get("weight")
    details = p.get("details") or f"{weight} x {reps}" if weight else ""
    lift_id = await db.log_lift(date, exercise, details, raw=json.dumps(p))
    await db.log_lift_set(lift_id, date, exercise, p.get("setNumber", 1),
                          reps=reps, weight_lb=weight, rpe=p.get("rpe"), source="app")
    return {"ok": True, "lift_id": lift_id}


@app.post("/swap-session", dependencies=[Depends(auth)])
async def swap_session(payload: dict = Body(...)):
    """Override today's plan from the app — same semantics as Discord /swap.

    Body: {"target": "push"|"pull"|"legs"|"run"|"rest"|"cross"|"reset"}
    "reset" clears today's override, falling back to the weekly template.
    Returns the now-effective session so the UI can update without a
    second round-trip.
    """
    from data.plan_vocab import normalize_swap

    target = ((payload or {}).get("target") or "").strip().lower()
    today_iso = _today_iso()

    if not target:
        raise HTTPException(status_code=422, detail="missing 'target'")

    if target == "reset":
        await db.clear_daily_override(today_iso)
    else:
        normalized = normalize_swap(target)
        if not normalized:
            raise HTTPException(
                status_code=422,
                detail=f"unknown target {target!r} — try push/pull/legs/run/rest/cross/reset",
            )
        new_type, new_focus = normalized
        await db.set_daily_override(
            date=today_iso,
            session_type=new_type,
            focus=new_focus,
            prescription="",
            notes="swap from iOS app",
            source="app_swap",
        )

    # The cached morning brief may reference the old session — rebuild on
    # next /today so the brief matches the new plan.
    _brief_cache.pop(today_iso, None)

    sess = await db.get_effective_session_for_date(today_iso) or {}
    return {
        "ok": True,
        "session": {
            "type": (sess.get("session_type") or "rest").title(),
            "focus": (sess.get("focus") or sess.get("prescription", "")[:80] or "").strip(),
            "isOverride": bool(sess.get("is_override")),
        },
    }


@app.post("/recovery", dependencies=[Depends(auth)])
async def log_recovery(payload: dict = Body(...)):
    p = payload or {}
    await db.log_recovery_session(
        date=p.get("date") or _today_iso(),
        session_type=p.get("type", "Sauna"),
        duration_min=p.get("duration"),
        temp_f=p.get("temp"),
        notes=p.get("notes", ""),
        raw_message=json.dumps(p),
    )
    return {"ok": True}


@app.post("/goals", dependencies=[Depends(auth)])
async def create_goal(payload: dict = Body(...)):
    p = payload or {}
    gid = await db.create_goal(
        goal_type=(p.get("tag") or p.get("type") or "habit").lower().replace("body comp", "bf"),
        title=p.get("title", "Goal"),
        target_value=p.get("target"),
        target_unit=p.get("unit", ""),
        baseline_value=p.get("start") or p.get("current"),
        baseline_date=_today_iso(),
        deadline=p.get("deadline"),
    )
    return {"ok": True, "id": gid}


@app.patch("/goals/{goal_id}", dependencies=[Depends(auth)])
async def update_goal(goal_id: int, payload: dict = Body(...)):
    status = (payload or {}).get("status")
    if status:
        await db.update_goal_status(goal_id, status)
    return {"ok": True}


@app.post("/body", dependencies=[Depends(auth)])
async def log_body(payload: dict = Body(...)):
    # Weight persists via the WHOOP body-measurement table; BF% has no column
    # yet (source TBD) — accepted and echoed so the UI updates.
    p = payload or {}
    weight_lb = p.get("weight")
    if weight_lb:
        try:
            kg = float(str(weight_lb).replace("lb", "").strip()) / 2.20462
            await db.upsert_whoop_body_measurement({"weight_kilogram": kg, "measurement_date": _today_iso()})
        except Exception as e:
            logger.warning("body weight upsert failed: %s", e)
    return {"ok": True, "weight": p.get("weight"), "bodyFat": p.get("bodyFat")}


# ── assembly helpers ─────────────────────────────────────────────────────────

def _daily_quote(seed_date):
    try:
        from ai.prompts import STOIC_QUOTES
        import hashlib
        idx = int(hashlib.md5(seed_date.encode()).hexdigest(), 16) % len(STOIC_QUOTES)
        text, author = STOIC_QUOTES[idx]
        return {"text": text, "author": author}
    except Exception:
        return {"text": "The impediment to action advances action.", "author": "Marcus Aurelius"}


def _assemble_brief(latest, sess):
    rec = latest.get("recovery_score")
    parts = []
    if rec is not None:
        parts.append(f"Recovery {round(rec)}% today.")
    focus = (sess.get("focus") or sess.get("session_type") or "").strip()
    if focus:
        parts.append(f"Planned: {focus}.")
    parts.append("Live brief unavailable — showing the latest stored numbers.")
    return " ".join(parts)


async def _run_split(today_iso):
    start = (_now() - timedelta(days=7)).strftime("%Y-%m-%d")
    z = {"easy": 0, "hard": 0}
    try:
        async with aiosqlite.connect(db.db_path) as conn:
            async with conn.execute(
                "SELECT zone1_ms, zone2_ms, zone3_ms, zone4_ms, zone5_ms "
                "FROM whoop_workouts WHERE start_date BETWEEN ? AND ? "
                "AND LOWER(sport_name) LIKE '%run%'", (start, today_iso)) as cur:
                for r in await cur.fetchall():
                    z["easy"] += (r[0] or 0) + (r[1] or 0)
                    z["hard"] += (r[2] or 0) + (r[3] or 0) + (r[4] or 0)
    except Exception as e:
        logger.debug("split query failed: %s", e)
    total = z["easy"] + z["hard"]
    if total <= 0:
        return {"easy": 0, "hard": 0}
    return {"easy": round(z["easy"] / total * 100), "hard": round(z["hard"] / total * 100)}


async def _recovery_summary():
    rows = await db.get_recent_recovery_sessions(days=7)
    heat = sum((r.get("duration_min") or 0) for r in rows if (r.get("session_type") or "").lower() in ("sauna", "steam"))
    cold = sum((r.get("duration_min") or 0) for r in rows if "cold" in (r.get("session_type") or "").lower() or "ice" in (r.get("session_type") or "").lower())
    return {"saunaMin": int(heat), "coldMin": int(cold), "sessions": len(rows)}


async def _body_comp(body_rows):
    weight = "—"
    if body_rows:
        kg = body_rows[0].get("weight_kg")
        if kg:
            weight = f"{round(kg * 2.20462, 1)} lb"
    return {"weight": weight, "bodyFat": None, "leanMass": None}


async def _weekly_plan():
    plan = await db.get_active_plan()
    if not plan:
        return []
    # get_active_plan already parses weekly_template to a dict; tolerate a raw
    # string too in case that changes.
    tmpl = plan.get("weekly_template") or {}
    if isinstance(tmpl, str):
        try:
            tmpl = json.loads(tmpl)
        except Exception:
            tmpl = {}
    order = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    today_name = _now().strftime("%A").lower()
    kind_map = {"lift": "lift", "run": "run", "rest": "rest", "cross_train": "run"}
    out = []
    for day in order:
        e = tmpl.get(day) or {}
        st = (e.get("session_type") or "rest").lower()
        out.append({
            "day": day[:3].title(),
            "session": (e.get("focus") or st).title(),
            "kind": kind_map.get(st, "rest"),
            "today": day == today_name,
        })
    return out


async def _integrations():
    latest_whoop = await db.get_latest_whoop_date()
    latest_strava = await db.get_latest_strava_timestamp()
    def ok(v):
        return bool(v)
    return [
        {"name": "WHOOP", "status": "Connected" if ok(latest_whoop) else "Not synced", "ok": ok(latest_whoop)},
        {"name": "Strava", "status": "Connected" if ok(latest_strava) else "Not synced", "ok": ok(latest_strava)},
        {"name": "Apple Health", "status": "Not connected", "ok": False},
    ]
