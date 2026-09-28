"""Side threads: a small chat inside a chat, merged back when done.

Asked for on 2026-09-28: "lets add threading, where I can have a side chat
and then I can press merge to go back to main chat, or use as reference in
main chat". A thread is a child session (parent_session_id), started from
one message of the parent and seeded with just that exchange, so it reads a
few thousand tokens instead of the whole chat. It is hidden from the sidebar
and shown under its message in the parent. See src/chat_threads.py.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request

from core.models import ChatMessage
from routes.session_routes import _verify_session_owner
from src import chat_threads

logger = logging.getLogger(__name__)

MERGE_HEADER = "[Side thread merged · {name}]"


def _meta(m) -> dict:
    return m.metadata if isinstance(getattr(m, "metadata", None), dict) else {}


def setup_thread_routes(session_manager) -> APIRouter:
    router = APIRouter(tags=["threads"])

    def _get(session_id: str):
        try:
            return session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found")

    @router.post("/api/session/{session_id}/threads")
    async def start_thread(request: Request, session_id: str):
        """Start a side thread at a message: {"anchor_msg_id": ...}."""
        _verify_session_owner(request, session_id)
        body = await request.json()
        anchor = str(body.get("anchor_msg_id") or "")
        parent = _get(session_id)
        if getattr(parent, "parent_session_id", None):
            raise HTTPException(400, "A side thread cannot have side threads of its own")
        hist = parent.history
        idx = next((i for i, m in enumerate(hist) if _meta(m).get("_db_id") == anchor), None)
        if idx is None:
            raise HTTPException(404, "Message not found")
        # Seed: the exchange the thread starts from (the question and its
        # reply), so the thread knows what it is about and nothing more.
        seed = [hist[idx]]
        if hist[idx].role == "assistant" and idx > 0 and hist[idx - 1].role == "user":
            seed.insert(0, hist[idx - 1])
        elif hist[idx].role == "user" and idx + 1 < len(hist) and hist[idx + 1].role == "assistant":
            seed.append(hist[idx + 1])
        title_src = next((m.content for m in seed if m.role == "user"), seed[0].content)
        name = "\U0001F9F5 " + chat_threads._label(title_src, 50)
        thread_id = str(uuid.uuid4())
        thread = session_manager.create_session(
            thread_id, name, parent.endpoint_url, parent.model, rag=False,
            owner=getattr(parent, "owner", None),
            parent_session_id=parent.id, thread_anchor_id=anchor)
        for m in seed:
            meta = {k: v for k, v in _meta(m).items()
                    if k not in ("_db_id", "timestamp", "excluded", "references")}
            meta["thread_seed"] = True
            thread.add_message(ChatMessage(m.role, m.content, metadata=meta))
        logger.info("[threads] %s started in %s at %s", thread_id[:8], session_id[:8], anchor[:8])
        return {"id": thread_id, "name": name, "parent_session_id": parent.id,
                "anchor_msg_id": anchor}

    def _merged_counts(parent) -> dict:
        """thread id -> how many of its messages were in its latest merge."""
        out = {}
        for m in parent.history:
            meta = _meta(m)
            if meta.get("source") == "thread_merge" and meta.get("thread_id"):
                out[meta["thread_id"]] = int(meta.get("merged_count") or 0)
        return out

    def _own_count(thread) -> int:
        return sum(1 for m in thread.history if not _meta(m).get("thread_seed") and m.role != "system")

    @router.get("/api/session/{session_id}/threads")
    async def list_threads(request: Request, session_id: str):
        """The side threads of a chat, for the cards under their messages."""
        _verify_session_owner(request, session_id)
        parent = _get(session_id)
        merged = _merged_counts(parent)
        out = []
        for sid, s in list(session_manager.sessions.items()):
            if getattr(s, "parent_session_id", None) != session_id or getattr(s, "archived", False):
                continue
            try:
                s = session_manager.get_session(sid)       # hydrate its messages
            except KeyError:
                continue
            count = _own_count(s)
            out.append({"id": sid, "name": s.name, "anchor_msg_id": s.thread_anchor_id,
                        "message_count": count,
                        "merged": sid in merged and merged[sid] >= count and count > 0,
                        "merged_count": merged.get(sid, 0)})
        return {"threads": out}

    @router.post("/api/session/{thread_id}/merge")
    async def merge_thread(request: Request, thread_id: str):
        """Post the thread's conversation into its parent chat as one
        message, which the main chat's model then reads like any other."""
        _verify_session_owner(request, thread_id)
        thread = _get(thread_id)
        parent_id = getattr(thread, "parent_session_id", None)
        if not parent_id:
            raise HTTPException(400, "This chat is not a side thread")
        _verify_session_owner(request, parent_id)
        parent = _get(parent_id)
        text = chat_threads.thread_transcript(thread)
        if not text:
            raise HTTPException(409, "Nothing has been said in this thread yet")
        count = _own_count(thread)
        content = MERGE_HEADER.format(name=thread.name) + "\n\n" + text
        parent.add_message(ChatMessage("user", content, metadata={
            "source": "thread_merge", "thread_id": thread_id, "merged_count": count}))
        try:
            session_manager.save_sessions()
        except Exception:
            pass
        logger.info("[threads] %s merged into %s (%d messages)", thread_id[:8], parent_id[:8], count)
        return {"ok": True, "parent_session_id": parent_id, "merged_count": count}

    return router
