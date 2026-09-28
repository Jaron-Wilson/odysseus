"""Approval endpoints for claude_code plans — /api/claude_code/*.

These exist so the approve/deny decision belongs to the user rather than to the
agent. The claude_code tool can write a plan and can ask for approval, but only
an authenticated request from the user's own browser reaches here, and only
this module can move a plan to `approved`. The execute phase refuses to run
otherwise, so the gate holds even if a model ignores every instruction it was
given about waiting.
"""

import json
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

# Posted in the chat when a background run is brought back into it.
_BRING_BACK_PROMPT = (
    "[Brought back from the background · job {job_id} · {engine}]\n\n"
    "The user brought this run back into the chat. Call claude_code with exactly\n"
    "{args}\n"
    "and nothing else first: it follows the run here and returns its result when it finishes. "
    "Then report what it did and carry on with the conversation where the user left off."
)

# Posted in the chat when a plan is approved, starting the agent on it. The
# approval already exists server-side; this only saves the user typing "go".
_EXECUTE_PROMPT = (
    "[Plan approved · run {run_id} · {engine}]\n\n"
    "The user approved the plan. Carry it out now: call claude_code with exactly\n"
    "{args}\n"
    "and nothing else first (the engine and model the plan was shown with are used "
    "automatically). If it says the plan is already running, do not start it again: tell "
    "the user it is running as run {run_id} and its result will be posted here. When it "
    "finishes, report what was done."
)


def approve_plan(session_id: str, user: str, limits: dict = None) -> dict:
    """Approve a plan and start its run in the plan's chat. Shared by the
    chat's Approve link and the desktop overlay (routes/overlay_routes.py)."""
    entry = approvals.get(session_id)
    if not entry:
        raise HTTPException(404, "No such plan (it may have expired)")
    chat_id = entry.get("chat_session_id") or ""
    if entry.get("status") in ("approved", "used") and entry.get("run_id"):
        # A retry of an Approve whose answer was lost (the server was
        # restarting): same run, and never a second one.
        job = jobs.get(entry["run_id"])
        return {"session_id": session_id, "status": entry["status"], "already": True,
                "run_id": entry["run_id"], "chat_session_id": chat_id,
                "running": bool(job and job.status == "running"), "resuming": False}
    if not approvals.set_status(session_id, "approved", owner=user):
        raise HTTPException(409, f"Plan is already {entry.get('status')}")
    run_id = approvals.assign_run_id(session_id)
    if limits:
        # Turns / budget / take your time, chosen in the Approve dialog.
        from src.agent_tools.claude_code_tool import normalize_limits
        approvals.set_limits(session_id, normalize_limits(limits))
    logger.info("[claude_code] plan %s approved by %s (run %s)",
                session_id[:8], user or "(auth off)", run_id)
    # Carry the plan out now, in the chat it came from, rather than waiting
    # for the user to say "go". Registered before this returns so the page
    # can attach and show it running.
    from src.screen_control_resume import start_turn
    resuming = start_turn(chat_id, _EXECUTE_PROMPT.format(
        run_id=run_id, engine="Claude Code" if entry.get("engine") == "claude" else "OpenCode",
        args=json.dumps({
            "action": "execute", "session_id": session_id, "cwd": entry.get("cwd") or "",
            "prompt": "Carry out the approved plan."})),
        note_source="claude_code_plan_approved", reply_source="claude_code_plan_run")
    return {"session_id": session_id, "status": "approved", "run_id": run_id,
            "chat_session_id": chat_id, "resuming": resuming}


def deny_plan(session_id: str, user: str) -> dict:
    entry = approvals.get(session_id)
    if not entry:
        raise HTTPException(404, "No such plan (it may have expired)")
    if not approvals.set_status(session_id, "denied", owner=user):
        raise HTTPException(409, f"Plan is already {entry.get('status')}")
    logger.info("[claude_code] plan %s denied by %s", session_id[:8], user or "(auth off)")
    return {"session_id": session_id, "status": "denied"}


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
        limits = None
        try:
            if int(request.headers.get("content-length") or 0) > 0:
                limits = (await request.json()).get("limits")
        except Exception:
            limits = None
        return approve_plan(session_id, user, limits)

    @router.post("/api/claude_code/deny/{session_id}")
    async def deny(request: Request, session_id: str):
        user = _require_user(request)
        _validate(session_id)
        return deny_plan(session_id, user)

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

    @router.get("/api/claude_code/agents")
    async def list_agents_by_chat(request: Request):
        """The Claude Code agent each chat keeps (src/claude_code_agents.py)."""
        _require_user(request)
        from src import claude_code_agents
        busy = {j.cli_session_id for j in jobs.list_jobs() if j.status == "running"}
        return {"agents": [dict(a, busy=a.get("session_id") in busy)
                           for a in claude_code_agents.list_all()[:50]]}

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

    @router.post("/api/claude_code/jobs/{job_id}/foreground")
    async def foreground_job(request: Request, job_id: str):
        """Bring a background run back into its chat: a turn there follows it
        live and carries on from its result. Queued behind a reply that is
        already running in that chat."""
        job = _job_or_404(request, job_id)
        if job.status != "running":
            raise HTTPException(409, f"Job is {job.status}: its result is already in the chat")
        if job.attached:
            return {"ok": True, "already": True, "chat_session_id": job.chat_session_id}
        chat_id = job.chat_session_id
        if not chat_id:
            raise HTTPException(409, "This run has no chat to come back to")
        from src.agent_tools.claude_code_tool import engine_label
        prompt = _BRING_BACK_PROMPT.format(job_id=job.id, engine=engine_label(job.engine),
                                            args=json.dumps({"action": "attach", "job_id": job.id}))
        from src.screen_control_resume import start_turn
        started = start_turn(chat_id, prompt, note_source="claude_code_brought_back",
                             reply_source="claude_code_brought_back_run")
        queued = False
        reason = ""
        if not started:
            from src import agent_runs, chat_queue
            # Queued behind the reply that has the chat, once: seen live, two
            # clicks a second apart queued the prompt twice.
            reason = ("A reply is already running in that chat; this run comes back as soon as it ends."
                      if agent_runs.is_active(chat_id) else "The chat could not start a turn right now; it is queued.")
            try:
                chat_queue.add(chat_id, prompt, key=f"bring-back:{job.id}",
                               label=f"Bring back {engine_label(job.engine)} job {job.id}")
                queued = True
            except Exception as e:
                raise HTTPException(409, f"Could not bring it back: {e}")
        logger.info("[claude_code] job %s brought back into chat %s%s", job.id, chat_id[:8],
                    " (queued)" if queued else "")
        return {"ok": True, "resuming": started, "queued": queued, "reason": reason,
                "chat_session_id": chat_id}

    @router.post("/api/claude_code/jobs/{job_id}/stop")
    async def stop_job(request: Request, job_id: str):
        job = _job_or_404(request, job_id)
        if not jobs.stop(job_id):
            raise HTTPException(409, f"Job is {job.status}")
        logger.info("[claude_code] job %s stopped", job_id)
        return job.public()

    return router
