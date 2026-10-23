"""A chat carrying on one of the Claude Code sessions on this host
(src/claude_attach.py, src/claude_code_sessions.py).

The page that listed and read those sessions moved to the Programming mod
(Jaron-Wilson/odysseus-programming): its tab replaces `claude-sessions`.

  GET /api/claude_sessions/resolve?prefix=7238cfa3        the one session an id prefix names

  GET    /api/claude_attach/{chat_id}                     the session attached to a chat
  POST   /api/claude_attach/{chat_id}  {"id": "7238cfa3"} attach one (src/claude_attach.py)
  POST   /api/claude_attach/{chat_id}/direct {"on": true}  replies straight from it (src/claude_direct.py)
  DELETE /api/claude_attach/{chat_id}                     detach it
  GET    /api/claude_direct/{chat_id}/live                the direct reply so far (src/claude_direct.py)

Admin only: the transcripts hold everything that was said and done in those
sessions. Attaching changes nothing under ~/.claude: it only tells the
chat's claude_code tool which session to carry on, and the chat has to be
the caller's own.
"""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import claude_attach
from src.auth_helpers import effective_user


def _own_chat_or_404(request: Request, chat_id: str) -> str:
    owner = effective_user(request) or ""
    try:
        from src.ai_interaction import get_session_manager
        sess = get_session_manager().get_session(chat_id)
    except Exception:
        sess = None
    if sess is None:
        raise HTTPException(404, "No such chat")
    if owner and getattr(sess, "owner", None) and sess.owner != owner:
        raise HTTPException(404, "No such chat")
    return owner


def _attach_error(e: "claude_attach.AttachError") -> HTTPException:
    return HTTPException(e.status, {"error": str(e), "candidates": e.candidates})


def setup_claude_sessions_routes() -> APIRouter:
    router = APIRouter(tags=["claude-sessions"])

    @router.get("/api/claude_sessions/resolve")
    async def resolve_prefix(request: Request, prefix: str = ""):
        require_admin(request)
        try:
            return {"session": await asyncio.to_thread(claude_attach.resolve, prefix)}
        except claude_attach.AttachError as e:
            raise _attach_error(e)

    @router.get("/api/claude_attach/{chat_id}")
    async def get_attached(request: Request, chat_id: str):
        require_admin(request)
        _own_chat_or_404(request, chat_id)
        return {"attached": await asyncio.to_thread(claude_attach.info, chat_id), "now": time.time()}

    @router.post("/api/claude_attach/{chat_id}")
    async def attach(request: Request, chat_id: str):
        require_admin(request)
        owner = _own_chat_or_404(request, chat_id)
        try:
            body = await request.json()
        except Exception:
            body = {}
        prefix = str((body or {}).get("id") or "")
        try:
            out = await asyncio.to_thread(claude_attach.attach, chat_id, prefix, by=owner)
        except claude_attach.AttachError as e:
            raise _attach_error(e)
        return {"attached": out, "warning": claude_attach.cwd_warning(out.get("cwd") or ""),
                "now": time.time()}

    @router.post("/api/claude_attach/{chat_id}/direct")
    async def set_direct(request: Request, chat_id: str):
        """{"on": true|false} - replies straight from the session (src/claude_direct.py)."""
        require_admin(request)
        _own_chat_or_404(request, chat_id)
        try:
            body = await request.json()
        except Exception:
            body = {}
        out = await asyncio.to_thread(claude_attach.set_direct, chat_id, bool((body or {}).get("on")))
        if out is None:
            raise HTTPException(409, {"error": "Attach a Claude Code session first: /claude attach <id>.",
                                      "candidates": []})
        return {"attached": out, "warning": claude_attach.cwd_warning(out.get("cwd") or ""), "now": time.time()}

    @router.delete("/api/claude_attach/{chat_id}")
    async def detach(request: Request, chat_id: str):
        require_admin(request)
        _own_chat_or_404(request, chat_id)
        was = await asyncio.to_thread(claude_attach.detach, chat_id)
        return {"detached": was}

    @router.get("/api/claude_direct/{chat_id}/live")
    async def direct_live(request: Request, chat_id: str):
        """The chat's direct reply so far: the one running, else the last
        saved. A page that opened the chat mid-turn polls this when it cannot
        reattach to the stream (src/claude_direct.py)."""
        require_admin(request)
        _own_chat_or_404(request, chat_id)
        from src import claude_direct
        from src.ai_interaction import get_session_manager
        try:
            sess = get_session_manager().get_session(chat_id)
        except Exception:
            sess = None
        return {**claude_direct.live_state(chat_id, sess), "now": time.time()}

    return router
