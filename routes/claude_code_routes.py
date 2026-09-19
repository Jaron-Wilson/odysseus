"""Approval endpoints for claude_code plans — /api/claude_code/*.

These exist so the approve/deny decision belongs to the user rather than to the
agent. The claude_code tool can write a plan and can ask for approval, but only
an authenticated request from the user's own browser reaches here, and only
this module can move a plan to `approved`. The execute phase refuses to run
otherwise, so the gate holds even if a model ignores every instruction it was
given about waiting.
"""

import logging
import re

from fastapi import APIRouter, HTTPException, Request

from src import claude_code_approvals as approvals
from src.auth_helpers import _auth_disabled, get_current_user

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,128}$")


def setup_claude_code_routes() -> APIRouter:
    router = APIRouter(tags=["claude_code"])

    def _require_user(request: Request) -> str:
        user = get_current_user(request)
        if not user:
            if _auth_disabled():
                return ""
            raise HTTPException(401, "Not authenticated")
        return user

    def _validate(session_id: str) -> None:
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise HTTPException(400, "Invalid session ID format")

    @router.get("/api/claude_code/plan/{session_id}")
    async def get_plan(request: Request, session_id: str):
        """Fetch a plan and its current status, for rendering the approval UI."""
        _require_user(request)
        _validate(session_id)
        entry = approvals.get(session_id)
        if not entry:
            raise HTTPException(404, "No such plan (it may have expired)")
        return {
            "session_id": session_id,
            "status": entry.get("status"),
            "cwd": entry.get("cwd"),
            "plan": entry.get("plan"),
        }

    @router.post("/api/claude_code/approve/{session_id}")
    async def approve(request: Request, session_id: str):
        """Authorise ONE execute run of this plan. Single-use, and the execute
        call must target the same directory the plan was made for."""
        user = _require_user(request)
        _validate(session_id)
        entry = approvals.get(session_id)
        if not entry:
            raise HTTPException(404, "No such plan (it may have expired)")
        if not approvals.set_status(session_id, "approved", owner=user):
            raise HTTPException(409, f"Plan is already {entry.get('status')}")
        logger.info("[claude_code] plan %s approved by %s", session_id[:8], user or "(auth off)")
        return {"session_id": session_id, "status": "approved"}

    @router.post("/api/claude_code/deny/{session_id}")
    async def deny(request: Request, session_id: str):
        user = _require_user(request)
        _validate(session_id)
        entry = approvals.get(session_id)
        if not entry:
            raise HTTPException(404, "No such plan (it may have expired)")
        if not approvals.set_status(session_id, "denied", owner=user):
            raise HTTPException(409, f"Plan is already {entry.get('status')}")
        logger.info("[claude_code] plan %s denied by %s", session_id[:8], user or "(auth off)")
        return {"session_id": session_id, "status": "denied"}

    return router
