"""Approve or deny screen control, from a signed-in browser.

The whole point of these routes is that they are NOT reachable by the agent.
They require a session cookie, so clicking approve is itself the proof of
identity: if the session has lapsed the browser is sent to /login and back,
which is the "sign in again to confirm" step rather than a failure.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from src import screen_control_approvals as approvals

logger = logging.getLogger(__name__)


def setup_screen_control_routes() -> APIRouter:
    router = APIRouter(prefix="/api/screen_control", tags=["screen-control"])

    def _require_user(request: Request) -> str:
        import os
        if os.getenv("AUTH_ENABLED", "true").lower() == "false":
            return ""
        user = getattr(request.state, "current_user", None)
        if not user:
            # 401 rather than a redirect: the caller is fetch(), and the page
            # handles sending the person to /login.
            raise HTTPException(401, "Sign in to approve screen control.")
        return user

    @router.get("/pending")
    async def pending(request: Request):
        """What is waiting on this person right now.

        The page calls this on load. Without it a reload loses the prompt
        entirely -- the request stays pending server-side and answerable,
        but nothing on screen says so, so the run just appears to hang.
        """
        user = _require_user(request)
        return {"pending": approvals.pending_for(user)}

    @router.get("/pending/{request_id}")
    async def get_request(request_id: str, request: Request):
        _require_user(request)
        from src.screen_control_approvals import _load, _prune
        rec = _prune(_load()).get(request_id)
        if not rec:
            raise HTTPException(404, "That request has expired or was already answered.")
        return {k: v for k, v in rec.items() if k != "owner"}

    @router.post("/approve/{request_id}")
    async def approve(request_id: str, request: Request):
        user = _require_user(request)
        rec = approvals.set_status(request_id, "approved", user)
        if not rec:
            raise HTTPException(
                404, "That request has expired or was already answered. Ask again.")
        # Carry on with the thing that was just approved, rather than leaving
        # the user to re-ask for it. Fired in the background so the click
        # returns immediately; a full agent turn can take a minute.
        resumed = False
        if rec.get("session_id"):
            from src.screen_control_resume import start_resume
            # Registered before this returns, so the approving browser can
            # attach to the run and show the agent carrying on, live.
            resumed = start_resume(rec["session_id"], rec.get("server_name", ""))
        return {
            "ok": True,
            "server": rec.get("server_name"),
            "minutes": approvals.GRANT_TTL_S // 60,
            "resuming": resumed,
            "session_id": rec.get("session_id") or "",
        }

    @router.post("/deny/{request_id}")
    async def deny(request_id: str, request: Request):
        user = _require_user(request)
        rec = approvals.set_status(request_id, "denied", user)
        if not rec:
            raise HTTPException(404, "That request has expired or was already answered.")
        return {"ok": True, "server": rec.get("server_name")}

    @router.get("/grants")
    async def grants(request: Request):
        """What is currently permitted, so a standing grant is visible."""
        _require_user(request)
        return {"grants": approvals.list_grants(),
                "ttl_minutes": approvals.GRANT_TTL_S // 60}

    @router.post("/revoke")
    async def revoke(request: Request, server_id: str = ""):
        """Hand back control early, without waiting for the window to lapse."""
        _require_user(request)
        return {"ok": True, "revoked": approvals.revoke(server_id)}

    return router
