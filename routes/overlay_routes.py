"""The desktop overlay's inbox and replies: /api/overlay/*.

The overlay (tools/music_overlay) runs on the PC with an API token scoped
"overlay", so it can read what the AI said and answer in the user's own
chats, and nothing else. A browser session works too.
"""

import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import effective_user, require_authenticated_request


def _owner(request: Request) -> str:
    require_authenticated_request(request)
    if getattr(request.state, "api_token", False):
        scopes = getattr(request.state, "api_token_scopes", None) or []
        if "overlay" not in scopes and "admin" not in scopes:
            raise HTTPException(403, "This token cannot use the overlay")
    return effective_user(request) or ""


def _pending_plans(owner: str) -> list:
    from src import claude_code_approvals as approvals
    from src.chat_queue import _session_title
    out = []
    for p in approvals.pending_for(owner)[:5]:
        engine = "OpenCode" if p.get("engine") == "opencode" else "Claude Code"
        out.append({"id": p["session_id"], "session_id": p["chat_session_id"],
                    "chat": _session_title(p["chat_session_id"]) if p["chat_session_id"] else "",
                    "runs_on": f"{engine} · {p.get('model') or 'local default'}",
                    "plan": p["plan"], "created": p["created"]})
    return out


def setup_overlay_routes() -> APIRouter:
    router = APIRouter(tags=["overlay"])

    @router.get("/api/overlay/inbox")
    async def inbox(request: Request, since: float = 0.0) -> Dict[str, Any]:
        from src import overlay_inbox
        owner = _owner(request)
        now = time.time()
        # How long since a browser page on the overlay's own machine last
        # talked to us: the overlay closes itself when Odysseus is closed there.
        seen = (getattr(request.app.state, "browser_seen", {}) or {}).get(
            request.client.host if request.client else "")
        return {"now": now, "events": overlay_inbox.since(owner, since),
                "page_seen_ago": round(now - seen, 1) if seen else None,
                # Plans waiting on an answer: the overlay shows the newest
                # above the player while any is pending, and hides again after.
                "plans": _pending_plans(owner)}

    @router.post("/api/overlay/plan/{plan_id}/{verb}")
    async def answer_plan(request: Request, plan_id: str, verb: str) -> Dict[str, Any]:
        """Approve or deny a pending plan from the overlay, as the chat's
        links do (and with the same effect: approving starts the run)."""
        from src import claude_code_approvals as approvals
        from routes.claude_code_routes import _SESSION_ID_RE, approve_plan, deny_plan
        owner = _owner(request)
        if verb not in ("approve", "deny") or not _SESSION_ID_RE.fullmatch(plan_id):
            raise HTTPException(400, "Unknown plan or answer")
        entry = approvals.get(plan_id)
        if not entry or (owner and entry.get("owner") and entry["owner"] != owner):
            raise HTTPException(404, "No such plan (it may have expired)")
        return approve_plan(plan_id, owner) if verb == "approve" else deny_plan(plan_id, owner)

    @router.post("/api/overlay/reply")
    async def reply(request: Request) -> Dict[str, Any]:
        """Answer in a chat: queued behind any running reply, sent now if idle."""
        from src import agent_runs, chat_queue
        owner = _owner(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = str(body.get("session_id") or "").strip()
        text = str(body.get("text") or "").strip()
        if not sid or not text:
            raise HTTPException(400, "session_id and text are required")
        try:
            from src.ai_interaction import get_session_manager
            sess = get_session_manager().get_session(sid)
        except Exception:
            raise HTTPException(404, "No such chat")
        if owner and getattr(sess, "owner", None) and sess.owner != owner:
            raise HTTPException(404, "No such chat")
        try:
            chat_queue.add(sid, text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        running = agent_runs.is_active(sid)
        if not running:
            chat_queue.schedule_drain(sid)               # nothing to wait for: send it
        return {"ok": True, "queued_behind_reply": running}

    return router
