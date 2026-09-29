"""Carry on the chats a restart cut off.

Asked for on 2026-09-29: "after deploying are we able to auto continue when a
chat gets forced stopped?" A deploy stops every running reply so each is
saved as far as it got (agent_runs.stop_all), then restarts, and nothing
picked those chats up again: each sat at "[Message interrupted]" until the
user pressed Continue.

Before a restart, the chats still replying are written down; after startup,
each is started again with a note saying why, so the agent carries on from
where it stopped instead of repeating itself. Only recent notes count, so a
list left behind by a crash long ago is not replayed.
"""
import asyncio
import json
import logging
import os
import time
from typing import List

logger = logging.getLogger(__name__)

RESUME_WITHIN_S = 15 * 60
STARTUP_DELAY_S = 15            # let MCP servers and endpoints come back first

RESUME_PROMPT = (
    "[Continued after a restart]\n\n"
    "The server restarted (an update was deployed) while you were replying, so your reply was cut "
    "off; what you had written so far is above. Carry on from where you stopped: do not repeat what "
    "is already there, and if you were waiting on a tool, check its result again rather than "
    "assuming it finished."
)


def _path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "resume_after_restart.json")


def remember(session_ids: List[str]) -> int:
    """Add these chats to the list to carry on after the restart."""
    ids = [s for s in session_ids if s]
    if not ids:
        return 0
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        data = {}
    chats = dict(data.get("chats") or {})
    now = time.time()
    for s in ids:
        chats[s] = now
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"chats": chats}, f)
    os.replace(tmp, _path())
    logger.info("[resume] will carry on %d chat(s) after the restart", len(ids))
    return len(ids)


def remember_active() -> int:
    """The chats with a reply running right now."""
    from src import agent_runs
    return remember(agent_runs.active_sessions())


def take() -> List[str]:
    """The chats to carry on, recent ones only, and forget the list."""
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return []
    try:
        os.remove(_path())
    except OSError:
        pass
    now = time.time()
    return [s for s, t in (data.get("chats") or {}).items() if now - float(t or 0) <= RESUME_WITHIN_S]


def resume_pending() -> List[str]:
    """Start each remembered chat again. Returns the ones started."""
    from src.screen_control_resume import start_turn
    started = []
    for sid in take():
        try:
            if start_turn(sid, RESUME_PROMPT, note_source="resumed_after_restart",
                          reply_source="resumed_after_restart_run"):
                started.append(sid)
        except Exception as e:
            logger.warning("[resume] could not carry on %s: %s", sid[:8], e)
    if started:
        logger.info("[resume] carried on %d chat(s) after the restart", len(started))
    return started


async def resume_later(delay: float = STARTUP_DELAY_S) -> List[str]:
    await asyncio.sleep(delay)
    return resume_pending()
