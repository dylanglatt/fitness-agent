"""
obs.py — observability: logging setup, structured events, timing, Sentry.

Why this is a top-level module (AGENTS.md says don't add those lightly): it
must be importable by BOTH entry points (`main.py` and `api_server.py`) and by
every package under bot/ integrations/ ai/ data/ without creating an import
cycle. Nothing here imports project code, so it is safe to import first.

The three rules this module exists to enforce:

  1. **No failure is ever logged below WARNING.** Before this, ~103 except
     blocks either swallowed silently or logged at debug — invisible at the
     hardcoded INFO level. That is why a dead WHOOP token went unnoticed for
     a month. Every warning must name what is missing.
  2. **One grep-able `event=` key per meaningful line.** A dozen event names
     total (brief.start, brief.done, llm.call, webhook.received, sync.done,
     heartbeat.done, readiness.computed, ...). `LOG_FORMAT=json` makes them
     machine-queryable: `journalctl -o cat | jq 'select(.event=="brief.done")'`.
  3. **One request id stitches a trace together.** Set at each entry point
     (Discord message, slash command, webhook, API route) and stamped onto
     every record automatically, so webhook → upsert → brief → Notion write →
     DM is a single filterable trace.

Config (all optional, read from the environment):
    LOG_LEVEL     DEBUG|INFO|WARNING|ERROR      default INFO
    LOG_FORMAT    text|json                      default text
    SENTRY_DSN    dsn                            Sentry disabled if unset
    SENTRY_ENVIRONMENT                           default "production"
    RELEASE       version string                 defaults to the git short sha
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.config
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Iterator

try:
    # Entry points import obs BEFORE config, so .env has not been read yet and
    # LOG_LEVEL / LOG_FORMAT / SENTRY_DSN would be invisible. load_dotenv is
    # idempotent, so config.py calling it again later is harmless.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a hard dep, but never fail here
    pass

# ── Context ──────────────────────────────────────────────────────────────────

# The correlation id for the in-flight unit of work. "-" means "no request
# scope" (module import, scheduler tick that hasn't opened one yet).
request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

_service = "unknown"


def new_request_id(prefix: str = "") -> str:
    """Open a new request scope and return the id.

    Call at every entry point. The id is 8 hex chars, optionally prefixed so
    you can tell at a glance where a trace started: brief-1a2b3c4d,
    hook-9f8e7d6c, chat-..., api-..., cmd-....
    """
    rid = uuid.uuid4().hex[:8]
    if prefix:
        rid = f"{prefix}-{rid}"
    request_id.set(rid)
    return rid


@contextmanager
def request_scope(prefix: str = "") -> Iterator[str]:
    """Context manager form — restores the previous id on exit, so a nested
    scope (a webhook that fires a brief) doesn't clobber its parent's id."""
    token = request_id.set(
        f"{prefix}-{uuid.uuid4().hex[:8]}" if prefix else uuid.uuid4().hex[:8]
    )
    try:
        yield request_id.get()
    finally:
        request_id.reset(token)


def _release() -> str:
    """Deploy identifier. Prefer $RELEASE; fall back to the git short sha so
    Sentry can correlate a regression to a deploy without extra CI wiring."""
    env = os.getenv("RELEASE", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0:
            return out.stdout.strip() or "unknown"
    except Exception:
        # Never let observability setup break startup.
        pass
    return "unknown"


RELEASE = _release()


# ── Formatting ───────────────────────────────────────────────────────────────

# Attributes LogRecord always carries; anything else on the record is a field
# we (or a caller's extra=) put there and should be rendered.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


class _ContextFilter(logging.Filter):
    """Stamps service / rid / release onto every record in the process."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = _service
        record.rid = request_id.get()
        record.release = RELEASE
        if not hasattr(record, "event"):
            record.event = ""
        return True


def _fmt_value(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:g}"
    s = str(v)
    return f'"{s}"' if (" " in s or "=" in s) else s


class _TextFormatter(logging.Formatter):
    """Human format for dev and for `journalctl` reading:

    14:02:11 [INFO ] ai.coach {brief-1a2b3c4d} brief.done duration_ms=1840 …
    """

    default_time_format = "%Y-%m-%d %H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: v
            for k, v in vars(record).items()
            if k not in _RESERVED
            and k not in ("service", "rid", "release", "event")
            and not k.startswith("_")
        }
        # Fields go on the message line, NOT appended after format() — else a
        # record carrying a traceback prints "...RuntimeError: boom ok=False",
        # with the fields stranded on the last line of the stack trace.
        # Skipped when the record uses %-style args, so we never corrupt a
        # third-party library's lazy formatting.
        if not extras or record.args:
            return super().format(record)
        suffix = " ".join(f"{k}={_fmt_value(v)}" for k, v in extras.items())
        original = record.msg
        record.msg = f"{original} {suffix}" if original else suffix
        try:
            return super().format(record)
        finally:
            record.msg = original


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for production."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "service": getattr(record, "service", _service),
            "rid": getattr(record, "rid", "-"),
            "release": getattr(record, "release", RELEASE),
            "msg": record.getMessage(),
        }
        event = getattr(record, "event", "")
        if event:
            out["event"] = event
        for k, v in vars(record).items():
            if (
                k in _RESERVED
                or k in ("service", "rid", "release", "event")
                or k.startswith("_")
            ):
                continue
            try:
                json.dumps(v)
                out[k] = v
            except (TypeError, ValueError):
                out[k] = str(v)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


# ── Setup ────────────────────────────────────────────────────────────────────

# Library loggers that emit one INFO line per HTTP request / gateway event.
# Without this, our own ~58 INFO lines are drowned by httpx chatter for every
# WHOOP, Strava, Notion and Anthropic call.
_QUIET_LIBRARIES = (
    "httpx",
    "httpcore",
    "discord",
    "discord.gateway",
    "discord.client",
    "discord.http",
    "aiohttp.access",
    "chromadb",
    "urllib3",
    "asyncio",
)

_configured = False


def setup_logging(service: str) -> logging.Logger:
    """Configure logging for the whole process. Call FIRST in every entry
    point — including `api_server.py`, which previously had no root handler at
    all, so uvicorn dropped every INFO line it logged.

    Idempotent: safe to call twice (uvicorn --reload, tests).
    """
    global _service, _configured
    _service = service

    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = "INFO"
    use_json = os.getenv("LOG_FORMAT", "text").lower() == "json"

    handler = logging.StreamHandler()
    handler.addFilter(_ContextFilter())
    if use_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _TextFormatter(
                fmt="%(asctime)s [%(levelname)-5s] %(name)s {%(rid)s} %(message)s"
            )
        )

    root = logging.getLogger()
    # Replace rather than append so a second call doesn't double every line.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
    log = logging.getLogger(service)
    log_event(
        log,
        logging.INFO,
        "logging.ready",
        log_level=level,
        log_format="json" if use_json else "text",
    )
    return log


def init_sentry(service: str) -> bool:
    """Initialize Sentry from the environment. Returns True if enabled.

    Differences from the previous inline init in main.py / api_server.py:
      - DSN comes from env only (the literal was committed to source, twice).
      - `environment`, `release` and `server_name` are set, so bot and API
        events are distinguishable and a regression maps to a deploy.
      - Breadcrumb level is raised to WARNING. At the default (INFO) every
        INFO line became a breadcrumb on every event — including
        `coach.py`'s full tool-call arguments, i.e. recovery scores, HRV and
        lift history. AGENTS.md claims "errors only, no PII"; this makes that
        claim true.
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            release=RELEASE,
            server_name=service,
            integrations=[
                LoggingIntegration(
                    level=logging.WARNING,  # breadcrumbs
                    event_level=logging.ERROR,  # events
                )
            ],
        )
        return True
    except Exception as e:
        logging.getLogger(service).warning(
            "sentry.init_failed", extra={"event": "sentry.init_failed", "err": str(e)}
        )
        return False


# ── Emitting ─────────────────────────────────────────────────────────────────


def log_event(logger: logging.Logger, level: int, event: str, /, **fields: Any) -> None:
    """Emit one structured event line.

        log_event(log, logging.WARNING, "context.source_failed",
                  source="whoop_snapshot", stage="brief", err=type(e).__name__)

    `exc_info=True` in fields attaches a stack trace. The first three
    parameters are positional-only so that a field named `level`, `event` or
    `logger` is treated as data rather than colliding with the signature.
    """
    exc_info = fields.pop("exc_info", None)
    extra = {"event": event}
    for k, v in fields.items():
        # Never shadow a LogRecord attribute — logging raises on collision.
        extra[k if k not in _RESERVED else f"f_{k}"] = v
    logger.log(level, event, extra=extra, exc_info=exc_info)


def source_failed(
    logger: logging.Logger,
    source: str,
    stage: str,
    err: BaseException,
    /,
    **fields: Any,
) -> None:
    """The replacement for `except Exception: logger.debug(...)`.

    A context source failed and the caller is continuing on partial data. This
    is a WARNING, always, and it always names the source — so the log answers
    "which of the twelve inputs was empty?" instead of going quiet.
    """
    log_event(
        logger,
        logging.WARNING,
        "context.source_failed",
        source=source,
        stage=stage,
        err=type(err).__name__,
        detail=str(err)[:200],
        **fields,
    )


@asynccontextmanager
async def timed(logger: logging.Logger, event: str, /, **fields: Any):
    """Time an async block and emit ONE line on exit carrying duration_ms.

        async with timed(log, "brief.done", reason=reason) as ctx:
            ...
            ctx["sources_failed"] = failed

    Adds no extra lines — the duration rides on the event you were already
    logging. On exception it emits the same event at ERROR with ok=False and
    a stack trace, then re-raises.
    """
    started = time.perf_counter()
    ctx: dict[str, Any] = {}
    try:
        yield ctx
    except Exception:
        log_event(
            logger,
            logging.ERROR,
            event,
            ok=False,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            exc_info=True,
            **{**fields, **ctx},
        )
        raise
    else:
        log_event(
            logger,
            logging.INFO,
            event,
            ok=True,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            **{**fields, **ctx},
        )
