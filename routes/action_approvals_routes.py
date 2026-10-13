"""Action approvals in the chat: a held Google write and the user's answer
(src/action_approvals.py, src/google_write_gate.py).

  GET  /api/action_approvals?chat=<id>     a chat's held calls, oldest first
  GET  /api/action_approvals/pending       the user's calls still waiting (any chat)
  GET  /api/action_approvals/{id}          one, with what it would do and its state
  POST /api/action_approvals/{id}/approve  run the pinned call once, then carry on in the chat
  POST /api/action_approvals/{id}/deny     {"note"?: "..."}: tell the chat it was denied

  GET  /api/google/write-approval          the switch, waiting calls and the recent log
  PUT  /api/google/write-approval          {"enabled": bool}; turning it off is admin only

Only an authenticated request from the user's own page reaches approve and
deny; nothing a model can call does (app_api refuses these paths).
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src import action_approvals as AA
from src.auth_helpers import require_user


class DenyBody(BaseModel):
    note: Optional[str] = ""


def _entry(aid: str, user: str) -> dict:
    if not AA.valid_id(aid):
        raise HTTPException(400, "Invalid approval id")
    e = AA.get(aid)
    if not e or (e.get("owner") or "") != (user or ""):
        raise HTTPException(404, "No such approval")
    return e


def _resume(chat_id: str, prompt: str, source: str) -> bool:
    if not chat_id:
        return False
    from src.screen_control_resume import start_turn
    return start_turn(chat_id, prompt, note_source=source, reply_source=source + "_reply")


def _is_admin(request: Request, user: Optional[str]) -> bool:
    try:
        from routes.google_routes import _is_admin as g_admin
        return bool(g_admin(request, user))
    except Exception:
        return False


def setup_action_approvals_routes() -> APIRouter:
    router = APIRouter(tags=["action_approvals"])

    @router.get("/api/action_approvals")
    async def list_for_chat(request: Request, chat: str = ""):
        user = require_user(request)
        rows = [e for e in AA.for_chat(chat[:100]) if (e.get("owner") or "") == (user or "")]
        return {"approvals": [AA.view(e) for e in rows]}

    @router.get("/api/action_approvals/pending")
    async def pending(request: Request):
        user = require_user(request)
        return {"approvals": [AA.view(e) for e in AA.pending_for_owner(user)]}

    @router.get("/api/action_approvals/{aid}")
    async def get_one(request: Request, aid: str):
        user = require_user(request)
        return AA.view(_entry(aid, user))

    @router.post("/api/action_approvals/{aid}/approve")
    async def approve(request: Request, aid: str):
        user = require_user(request)
        _entry(aid, user)
        try:
            e = await AA.approve_and_run(aid, user)
        except AA.Refused as r:
            raise HTTPException(r.status, r.message)
        resuming = _resume(e.get("chat_session_id") or "", AA.approved_prompt(e), "action_approved")
        return {**AA.view(e), "resuming": resuming}

    @router.post("/api/action_approvals/{aid}/deny")
    async def deny(request: Request, aid: str, body: Optional[DenyBody] = None):
        user = require_user(request)
        _entry(aid, user)
        try:
            e = AA.deny(aid, user, (body.note if body else "") or "")
        except AA.Refused as r:
            raise HTTPException(r.status, r.message)
        resuming = _resume(e.get("chat_session_id") or "", AA.denied_prompt(e), "action_denied")
        return {**AA.view(e), "resuming": resuming}

    def _settings_view(request: Request, user: Optional[str]) -> dict:
        from src import google_write_gate as G
        admin = _is_admin(request, user)
        st = G.settings()
        return {
            "enabled": st["enabled"],
            "changed_by": st["changed_by"],
            "changed_at": st["changed_at"],
            "can_turn_off": admin,
            "pending": [AA.view(e) for e in AA.pending_for_owner(user)],
            "recent": AA.recent_audit(user, limit=50),
        }

    @router.get("/api/google/write-approval")
    async def get_setting(request: Request):
        user = require_user(request)
        return _settings_view(request, user)

    @router.put("/api/google/write-approval")
    async def put_setting(request: Request):
        user = require_user(request)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            raise HTTPException(400, "Expected {\"enabled\": true|false}.")
        from src import google_write_gate as G
        if not body["enabled"] and not _is_admin(request, user):
            raise HTTPException(403, "Only an admin can stop Odysseus asking before Google writes.")
        G.set_enabled(body["enabled"], by=user or "")
        return _settings_view(request, user)

    return router
