"""A phone call's turns through the agent, the way the in-app call does it.

The in-app call (static/js/voiceCall.js) has no pipeline of its own: each
thing you say is sent into the open chat as an ordinary message marked
voice_call=1, which adds chat_routes.VOICE_CALL_NOTE (you are on a call,
keep it short and spoken) to that turn, and the reply is read out as it
streams. A phone call does the same with no
page open. Each call gets its own chat, and each turn goes through
chat_queue.run_headless: the message is saved in the chat, and the full agent
(tools, memory, the chat's system prompt) runs as a detached agent_runs run,
the same machinery a queued message uses, with the same voice call note. So the call is in Odysseus like any
other chat, an open page attaches to a reply live, and the reply stream is
read here by subscribing to that run.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SOURCE = "phone_call"
STOP_WAIT_S = 10.0


def _scope(owner: Optional[str]) -> Optional[str]:
    from src.auth_helpers import _auth_disabled
    return None if _auth_disabled() else owner


def _session_manager():
    from src.ai_interaction import get_session_manager
    return get_session_manager()


def resolve_model(owner: Optional[str], cfg: Dict, is_admin: bool = False) -> Tuple[str, str]:
    """(model, endpoint_id) for a call's chat: the one picked in Settings,
    else the default model a new chat in the web app starts on."""
    model, endpoint_id = str(cfg.get("model") or ""), str(cfg.get("endpoint_id") or "")
    if model and endpoint_id:
        return model, endpoint_id
    from routes.model_routes import resolve_default_chat
    dc = resolve_default_chat(owner or "", is_admin)
    return str(dc.get("model") or ""), str(dc.get("endpoint_id") or "")


def new_call_chat(owner: Optional[str], cfg: Dict, caller: str, direction: str = "inbound",
                  is_admin: bool = False, name: str = "") -> Tuple[str, object]:
    """A new chat for one call, named for it: "Phone call 14:05 (+1555...)"."""
    from routes.session_routes import create_direct_chat
    model, endpoint_id = resolve_model(owner, cfg, is_admin)
    if not (model and endpoint_id):
        raise RuntimeError("No model is set for phone calls and there is no default model.")
    label = "Call to" if direction == "outbound" else "Phone call"
    name = name or f"{label} {datetime.now().strftime('%H:%M')} ({caller or 'unknown'})"
    sid, sess = create_direct_chat(_session_manager(), _scope(owner), model, endpoint_id, name=name)
    logger.info("[phone] call chat %s for %s", sid, owner or "-")
    return sid, sess


async def _wait_idle(sid: str) -> None:
    from src import agent_runs
    if not agent_runs.is_active(sid):
        return
    agent_runs.stop(sid)
    deadline = asyncio.get_running_loop().time() + STOP_WAIT_S
    while agent_runs.is_active(sid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)


def _event(ev: str) -> Optional[Dict]:
    """The JSON of one SSE event string, or None ([DONE], comments)."""
    for line in str(ev).splitlines():
        if line.startswith("data: "):
            body = line[6:].strip()
            if not body or body == "[DONE]":
                return None
            try:
                d = json.loads(body)
            except ValueError:
                return None
            return d if isinstance(d, dict) else None
    return None


async def reply(sid: str, text: str) -> AsyncIterator[Tuple[str, str]]:
    """Send `text` into chat `sid` and yield the reply as it streams:
    ("delta", text) pieces of the answer (reasoning left out), then
    ("error", message) if the run failed. A reply still running from the
    turn before is stopped first (it keeps what it said), the way a new
    message in the web app cuts the old one off."""
    from src import agent_runs, chat_queue
    await _wait_idle(sid)
    if not await chat_queue.run_headless(sid, text, source=SOURCE, voice_call=True):
        yield ("error", "The chat could not take the message.")
        return
    async for ev in agent_runs.subscribe(sid):
        d = _event(ev)
        if not d:
            continue
        if isinstance(d.get("delta"), str) and not d.get("thinking"):
            yield ("delta", d["delta"])
        elif d.get("error") and ("event: error" in ev or d.get("type") == "error"):
            yield ("error", str(d.get("error"))[:200])


def stop(sid: str) -> None:
    """Hang-up: stop a reply nobody will hear (it saves what it has)."""
    try:
        from src import agent_runs
        agent_runs.stop(sid)
    except Exception:
        pass


def note(sid: str, text: str) -> None:
    """A line in the call's chat that is not a turn (a call that ended, a
    message from an unknown caller), saved as an assistant note."""
    try:
        from core.models import ChatMessage
        sm = _session_manager()
        sm.add_message(sid, ChatMessage("assistant", text, metadata={"source": SOURCE, "note": True}))
        sm.save_sessions()
    except Exception as e:
        logger.warning("[phone] could not save a note in %s: %s", sid, type(e).__name__)
