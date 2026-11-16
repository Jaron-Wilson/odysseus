"""Private chats: never shared with other chats or models.

Asked for on 2026-11-16: the Phone & Messages mod mirrors his business SIP
chat (jarondynamics) into a chat in Odysseus, "Text: jarondynamics". Company
and legal content must not leak into a model or a place it should not be,
so that chat is private: it is kept and shown, and nothing else reads it.

A chat is private when its per-chat switch "private" is on (chat_prefs; a
mod sets it with POST /api/mods/v1/chats with "private": true, or
POST chats/<id>/private), when it is named like one of FALLBACK_NAMES (a
chat made before the switch existed), or when it is a side thread of a
private chat. A private chat is:

- never sent to a model: a turn in it is refused (agent_runs.start, /api/chat,
  the mod API's turn, send_to_session), and so are tidy, memory suggestions,
  threads, forks and branches of it;
- never read from another chat: search_chats and the transcript search
  (session_search), list_sessions, the "Recent chats" a new chat is shown
  (chat_memory.recent), and the AI folder sort (session_actions) leave it out.
"""
import logging
from typing import Optional, Set

logger = logging.getLogger(__name__)

PREF = "private"
FALLBACK_NAMES = frozenset({"text: jarondynamics"})
REFUSAL = ("This chat is private: it is never sent to a model or shared with other chats. "
           "Use another chat to talk to the agent.")


def _name_private(name: Optional[str]) -> bool:
    return " ".join(str(name or "").split()).lower() in FALLBACK_NAMES


def _flagged() -> Set[str]:
    try:
        from src import chat_prefs
        return {sid for sid, p in chat_prefs._load().items() if isinstance(p, dict) and p.get(PREF) is True}
    except Exception:
        logger.exception("[private] could not read chat prefs")
        return set()


def _session(session_id: str):
    try:
        from src.ai_interaction import get_session_manager
        sm = get_session_manager()
        if sm is None:
            return None
        s = getattr(sm, "sessions", {}).get(session_id)
        if s is None:
            try:
                s = sm.get_session(session_id)
            except Exception:
                s = None
        return s
    except Exception:
        return None


def _db_row(session_id: str):
    try:
        from core.database import Session as DbSession, SessionLocal
        db = SessionLocal()
        try:
            r = db.query(DbSession).filter(DbSession.id == session_id).first()
            return (r.name, r.parent_session_id) if r else None
        finally:
            db.close()
    except Exception:
        return None


def is_private(session_id: Optional[str], name: Optional[str] = None, _depth: int = 0) -> bool:
    """Whether the chat is private (its switch, its name, or a private parent)."""
    if not session_id:
        return _name_private(name)
    if session_id in _flagged() or _name_private(name):
        return True
    s = _session(session_id)
    if s is not None:
        nm, parent = getattr(s, "name", ""), getattr(s, "parent_session_id", None)
    else:
        row = _db_row(session_id)
        nm, parent = row if row else ("", None)
    if _name_private(nm):
        return True
    if parent and _depth < 5:
        return is_private(parent, _depth=_depth + 1)
    return False


def private_ids(db=None) -> Set[str]:
    """Every private chat id: flagged, named like one, and their side
    threads (any depth), from the database (`db`, else a new session)."""
    ids = set(_flagged())
    try:
        from core.database import Session as DbSession, SessionLocal
        own = db is None
        if own:
            db = SessionLocal()
        try:
            rows = db.query(DbSession.id, DbSession.name, DbSession.parent_session_id).all()
        finally:
            if own:
                db.close()
        ids |= {r[0] for r in rows if _name_private(r[1])}
        children = {}
        for sid, _, parent in rows:
            if parent:
                children.setdefault(parent, []).append(sid)
        todo = list(ids)
        while todo:
            for c in children.get(todo.pop(), []):
                if c not in ids:
                    ids.add(c)
                    todo.append(c)
    except Exception:
        logger.exception("[private] could not read chats")
    return ids


def set_private(session_id: str, on: bool = True) -> dict:
    from src import chat_prefs
    return chat_prefs.set_pref(session_id, PREF, bool(on))


async def refusal_stream(session_id: str):
    """What a refused turn streams instead of a reply (the SSE shape of a run)."""
    import json
    logger.info("[private] turn refused in private chat %s", (session_id or "")[:8])
    yield "event: error\n" + f"data: {json.dumps({'error': REFUSAL, 'status': 403, 'code': 'private'})}\n\n"
    yield "data: [DONE]\n\n"
