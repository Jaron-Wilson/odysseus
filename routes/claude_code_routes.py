"""Approval endpoints for claude_code plans — /api/claude_code/*.

These exist so the approve/deny decision belongs to the user rather than to the
agent. The claude_code tool can write a plan and can ask for approval, but only
an authenticated request from the user's own browser reaches here, and only
this module can move a plan to `approved`. The execute phase refuses to run
otherwise, so the gate holds even if a model ignores every instruction it was
given about waiting.
"""

import logging
import os
import re

from fastapi import APIRouter, HTTPException, Request

from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.auth_helpers import _auth_disabled, get_current_user

logger = logging.getLogger(__name__)

# Underscores are part of the real thing: the agent SDKs hand back ids like
# "ses_f3ba77bb0ffespZ8YzTJn2Ku8G". Leaving "_" out rejected every one of
# them, so both the plan PDF and the Approve button answered 400 -- the
# approval could not be given at all, by button or by typing it.
# Still an allowlist, and still no "/", "." or "\", which is what matters:
# the id is interpolated into a filename below.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


_JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")


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

    @router.get("/api/claude_code/plan/{session_id}/pdf")
    async def get_plan_pdf(request: Request, session_id: str):
        """Download the plan rendered in the house style."""
        _require_user(request)
        _validate(session_id)
        if not approvals.get(session_id):
            raise HTTPException(404, "No such plan (it may have expired)")
        from fastapi.responses import FileResponse
        from src.doc_pdf import PDF_DIR
        path = os.path.join(PDF_DIR, f"plan-{session_id}.pdf")
        if not os.path.isfile(path):
            raise HTTPException(404, "No PDF was rendered for this plan")
        return FileResponse(path, media_type="application/pdf",
                            filename=f"plan-{session_id[:8]}.pdf")

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

    # ------------------------------------------------------------------ #
    # Background tasks: Claude Code runs started from chats
    # (src/claude_code_jobs.py), plus the CLI's own `--bg` sessions.
    # ------------------------------------------------------------------ #
    def _job_or_404(request: Request, job_id: str):
        user = _require_user(request)
        if not _JOB_ID_RE.fullmatch(job_id):
            raise HTTPException(400, "Invalid job id")
        job = jobs.get(job_id)
        if job is None or not jobs.visible_to(job, user):
            raise HTTPException(404, "No such job")
        return job

    @router.get("/api/claude_code/jobs")
    async def list_jobs(request: Request, cli: int = 0):
        user = _require_user(request)
        out = {"jobs": [j.public() for j in jobs.list_jobs(user)]}
        if cli:
            # `claude agents --json` spawns the CLI, so only when asked.
            try:
                from src.agent_tools.claude_code_tool import ClaudeCodeTool
                listing = await ClaudeCodeTool()._list_agents()
                out["cli_sessions"] = [s for s in listing.get("sessions") or []
                                       if s.get("kind") == "background"]
                if listing.get("exit_code") != 0:
                    out["cli_error"] = listing.get("error")
            except Exception as e:
                out["cli_error"] = str(e)
        return out

    @router.get("/api/claude_code/jobs/{job_id}")
    async def get_job(request: Request, job_id: str, lines: int = 400):
        job = _job_or_404(request, job_id)
        return job.public(lines=max(1, min(int(lines or 400), jobs.MAX_LINES)))

    @router.post("/api/claude_code/jobs/{job_id}/background")
    async def background_job(request: Request, job_id: str):
        """Send a running run to the background: the chat carries on, the CLI
        keeps going, and its result is posted into the chat when it ends."""
        job = _job_or_404(request, job_id)
        if not jobs.detach(job_id):
            raise HTTPException(409, f"Job is {job.status}" + (" and already in the background" if job.detached else ""))
        logger.info("[claude_code] job %s sent to the background", job_id)
        return job.public()

    @router.post("/api/claude_code/jobs/{job_id}/stop")
    async def stop_job(request: Request, job_id: str):
        job = _job_or_404(request, job_id)
        if not jobs.stop(job_id):
            raise HTTPException(409, f"Job is {job.status}")
        logger.info("[claude_code] job %s stopped", job_id)
        return job.public()

    return router
