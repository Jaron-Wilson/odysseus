"""Claude Code sessions on this host, to read in the browser
(static/js/claudeSessions.js, src/claude_code_sessions.py).

  GET /api/claude_sessions                                every session, live first
  GET /api/claude_sessions/{project}/{id}?before=&limit=  a page of turns, newest last
  GET /api/claude_sessions/{project}/{id}/updates?after=  turns written since `after`

Admin only, like the Terminal: the transcripts hold everything that was said
and done in those sessions. Read-only. `project` has to be one of the
directories under ~/.claude/projects and `id` a UUID; anything else is a 404.
"""
import asyncio
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import claude_code_sessions as ccs


def _path_or_404(project: str, session_id: str) -> str:
    path = ccs.transcript_path(project, session_id)
    if not path:
        raise HTTPException(404, "No such session")
    return path


def setup_claude_sessions_routes() -> APIRouter:
    router = APIRouter(tags=["claude-sessions"])

    @router.get("/api/claude_sessions")
    async def list_sessions(request: Request):
        require_admin(request)
        sessions = await asyncio.to_thread(ccs.list_sessions)
        return {"sessions": sessions, "live_window_s": ccs.LIVE_S, "now": time.time()}

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
