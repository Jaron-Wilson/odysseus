"""Claude Code sessions on this host, to read in the browser
(static/js/claudeSessions.js, src/claude_code_sessions.py).

  GET /api/claude_sessions                                every session, live first
  GET /api/claude_sessions/{project}/{id}?before=&limit=  a page of turns, newest last
  GET /api/claude_sessions/{project}/{id}/updates?after=  turns written since `after`
  GET /api/claude_sessions/resolve?prefix=7238cfa3        the one session an id prefix names

  GET    /api/claude_attach/{chat_id}                     the session attached to a chat
  POST   /api/claude_attach/{chat_id}  {"id": "7238cfa3"} attach one (src/claude_attach.py)
  DELETE /api/claude_attach/{chat_id}                     detach it

Admin only, like the Terminal: the transcripts hold everything that was said
and done in those sessions. Read-only. `project` has to be one of the
directories under ~/.claude/projects and `id` a UUID; anything else is a 404.
Attaching changes nothing under ~/.claude: it only tells the chat's
claude_code tool which session to carry on, and the chat has to be the
caller's own.
"""
import asyncio
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import claude_attach
from src import claude_code_sessions as ccs
from src.auth_helpers import effective_user


def _path_or_404(project: str, session_id: str) -> str:
    path = ccs.transcript_path(project, session_id)
    if not path:
        raise HTTPException(404, "No such session")
    return path


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

    @router.get("/api/claude_sessions")
    async def list_sessions(request: Request):
        require_admin(request)
        sessions = await asyncio.to_thread(ccs.list_sessions)
        return {"sessions": sessions, "live_window_s": ccs.LIVE_S, "now": time.time()}

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

    @router.delete("/api/claude_attach/{chat_id}")
    async def detach(request: Request, chat_id: str):
        require_admin(request)
        _own_chat_or_404(request, chat_id)
        was = await asyncio.to_thread(claude_attach.detach, chat_id)
        return {"detached": was}

    @router.get("/api/claude_sessions/{project}/{session_id}")
    async def get_session(request: Request, project: str, session_id: str,
                          before: Optional[int] = None, limit: int = ccs.PAGE_TURNS):
        require_admin(request)
        path = _path_or_404(project, session_id)
        page = await asyncio.to_thread(ccs.read_page, path, before, limit)
        page["session"] = await asyncio.to_thread(ccs.session_info, project, path)
        return page

    @router.get("/api/claude_sessions/{project}/{session_id}/updates")
    async def get_updates(request: Request, project: str, session_id: str, after: int = 0):
        require_admin(request)
        path = _path_or_404(project, session_id)
        out = await asyncio.to_thread(ccs.read_after, path, after)
        out["session"] = await asyncio.to_thread(ccs.session_info, project, path)
        return out

    return router
