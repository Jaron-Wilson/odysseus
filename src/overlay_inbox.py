"""What the AI has said while you were somewhere else, for the desktop overlay.

The overlay (tools/music_overlay) is a frameless always-on-top window on the
PC. Windows holds its own notifications back while a game is running (Focus
Assist), and a browser notification cannot take a typed reply there. The
overlay polls this inbox instead, shows new messages over the game, and
replies through /api/overlay/reply.

Recorded: every notification Odysseus sends (a reply ready, a question), and
any reply that finishes with no page watching the chat. In memory: missing a
message across a server restart only means it is not shown on the overlay;
the chat itself has it.
"""

import collections
import time
import uuid
from typing import Deque, Dict, List, Optional

_EVENTS: Deque[Dict] = collections.deque(maxlen=200)
_DEDUPE_S = 90


def _session_owner_and_name(session_id: str):
    try:
        from src.ai_interaction import get_session_manager
        sess = get_session_manager().get_session(session_id)
        return (getattr(sess, "owner", "") or ""), (getattr(sess, "name", "") or "").strip()
    except Exception:
        return "", ""


def record(session_id: str, kind: str, heading: str, body: str,
           options: Optional[List[str]] = None) -> Optional[Dict]:
    if not session_id:
        return None
    now = time.time()
    for e in reversed(_EVENTS):
        if now - e["ts"] > _DEDUPE_S:
            break
        if e["session_id"] == session_id and e["kind"] == kind and e["body"] == body:
            return e                  # the same message, already recorded
    owner, name = _session_owner_and_name(session_id)
    ev = {"id": uuid.uuid4().hex[:10], "ts": now, "owner": owner, "kind": kind,
          "session_id": session_id, "chat": name, "heading": heading, "body": body[:600],
          "options": [str(o)[:80] for o in (options or [])][:6]}
    _EVENTS.append(ev)
    return ev


def since(owner: str, ts: float = 0.0) -> List[Dict]:
    return [dict(e) for e in _EVENTS
            if e["ts"] > ts and (not owner or not e["owner"] or e["owner"] == owner)]
