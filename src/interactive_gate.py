"""Foreground activity gate for background work.

Scheduled/background agent tasks (cron-style tasks, email pollers, housekeeping
actions) are allowed to run only after normal UI/API traffic has settled, and
are interrupted if the user becomes active again mid-run. This keeps them from
competing with the user actually using Odysseus for GPU/model capacity, and
frees that capacity back up the moment the user shows up.

"Active" here means: an HTTP request against the app is in flight or just
finished (tracked by `_InteractiveActivityMiddleware` in app.py, via
`track_interactive_request`), or there is a live chat/agent stream (see
`_has_active_chat_stream`). Purely passive polling endpoints (status/notif
polls) are excluded by `should_track_interactive_request` so they don't
themselves count as "the user is here".
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import time
from typing import Optional


_ACTIVE_REQUESTS = 0
_LAST_ACTIVITY = 0.0
_COND: Optional[asyncio.Condition] = None
_COND_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _enabled() -> bool:
    return os.getenv("BACKGROUND_TASK_FOREGROUND_GATE", "true").lower() not in {"0", "false", "no", "off"}


def _quiet_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("BACKGROUND_TASK_QUIET_MS", "1500")) / 1000.0)
    except Exception:
        return 1.5


def _max_wait_seconds() -> float:
    """0 means wait indefinitely until the UI is quiet."""
    try:
        return max(0.0, float(os.getenv("BACKGROUND_TASK_MAX_WAIT_SECONDS", "0")))
    except Exception:
        return 0.0


def _condition() -> asyncio.Condition:
    # Rebuild if the running loop changed (e.g. a fresh event loop in tests):
    # an asyncio.Condition is bound to the loop it was created on and raises
    # if awaited from a different one.
    global _COND, _COND_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _COND is None or _COND_LOOP is not loop:
        _COND = asyncio.Condition()
        _COND_LOOP = loop
    return _COND


# Endpoints that poll on a timer regardless of whether a human is paying
# attention. Tracking these as "interactive" would mean a background task
# could never catch a quiet window while any tab is merely open.
_PASSIVE_EXACT_PATHS = {
    "/api/tasks/notifications",
    "/api/tasks/runs/recent",
    "/api/research/active",
    "/api/email/urgency-state",
    "/api/email/unread-state",
}

_PASSIVE_PREFIXES = (
    "/api/chat/stream_status",
    "/api/health",
    "/api/prefs",
)


def should_track_interactive_request(path: str, method: str = "GET") -> bool:
    if not _enabled():
        return False
    if (method or "").upper() == "OPTIONS":
        return False
    if path in _PASSIVE_EXACT_PATHS:
        return False
    if any(path.startswith(prefix) for prefix in _PASSIVE_PREFIXES):
        return False
    return True


@asynccontextmanager
async def track_interactive_request(path: str = "", method: str = ""):
    global _ACTIVE_REQUESTS, _LAST_ACTIVITY
    if not _enabled():
        yield
        return

    cond = _condition()
    async with cond:
        _ACTIVE_REQUESTS += 1
        _LAST_ACTIVITY = time.monotonic()
        cond.notify_all()
    try:
        yield
    finally:
        async with cond:
            _ACTIVE_REQUESTS = max(0, _ACTIVE_REQUESTS - 1)
            _LAST_ACTIVITY = time.monotonic()
            cond.notify_all()


def _has_active_chat_stream() -> bool:
    """Best-effort check for foreground model work that outlives HTTP requests.

    Chat/agent streams are detached from the browser SSE (src.agent_runs), so a
    stream can keep running after the request that started it has returned.
    Background LLM tasks must still wait for those runs, otherwise a scheduled
    task competes with the user's active chat for the same local model.
    """
    try:
        from routes import chat_routes as _chat_routes
        active_streams = getattr(_chat_routes, "_active_streams", {}) or {}
        if active_streams:
            return True
    except Exception:
        pass
    try:
        from src import agent_runs
        runs = getattr(agent_runs, "_RUNS", {}) or {}
        return any(getattr(run, "status", None) == "running" for run in runs.values())
    except Exception:
        return False


def has_foreground_activity(now: Optional[float] = None) -> bool:
    """Return True when foreground browser/model work should stop background jobs.

    Narrower than letting any request ever tracked count forever: only an
    in-flight request, one that finished within the quiet window, or a live
    chat/agent stream counts as "the user is here right now".
    """
    if not _enabled():
        return False
    t = now if now is not None else time.monotonic()
    if _ACTIVE_REQUESTS > 0:
        return True
    if _LAST_ACTIVITY > 0 and (t - _LAST_ACTIVITY) < _quiet_seconds():
        return True
    return _has_active_chat_stream()


async def wait_for_interactive_quiet(label: str = "") -> bool:
    """Wait until foreground requests have stopped for the configured window.

    Returns True if the caller had to wait at all. The label is intentionally
    only for future logging/debugging so callers can keep their code simple.
    """
    if not _enabled():
        return False

    quiet = _quiet_seconds()
    max_wait = _max_wait_seconds()
    deadline = time.monotonic() + max_wait if max_wait > 0 else None
    cond = _condition()
    waited = False

    while True:
        async with cond:
            now = time.monotonic()
            quiet_remaining = quiet - (now - _LAST_ACTIVITY)
            active_stream = _has_active_chat_stream()
            if _ACTIVE_REQUESTS <= 0 and quiet_remaining <= 0 and not active_stream:
                return waited

            waited = True
            timeout = 0.25 if (_ACTIVE_REQUESTS > 0 or active_stream) else min(max(quiet_remaining, 0.05), 0.5)
            if deadline is not None:
                remaining = deadline - now
                if remaining <= 0:
                    return waited
                timeout = min(timeout, remaining)
            try:
                await asyncio.wait_for(cond.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
