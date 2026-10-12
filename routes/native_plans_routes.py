"""Native plans in the chat: the card's state and the user's answer (src/native_plans.py).

  GET  /api/native_plans?chat=<id>      a chat's plans, oldest first
  GET  /api/native_plans/{id}           one plan, with its current text and status
  POST /api/native_plans/{id}/approve   approve it and start the chat on it
  POST /api/native_plans/{id}/deny      {"note"?: "..."}: deny it and tell the chat

Only an authenticated request from the user's own page reaches approve and
deny; nothing a model can call does. The plan's text is its Plans document,
so the editor's Save is how a plan is edited before it is answered.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src import native_plans
from src.auth_helpers import require_user


class DenyBody(BaseModel):
    note: Optional[str] = ""


def _entry(plan_id: str, user: str) -> dict:
    if not native_plans.valid_id(plan_id):
        raise HTTPException(400, "Invalid plan id")
    e = native_plans.get(plan_id)
    if not e or (e.get("owner") or "") != (user or ""):
        raise HTTPException(404, "No such plan")
    return e


def _resume(chat_id: str, prompt: str, source: str) -> bool:
    if not chat_id:
        return False
    from src.screen_control_resume import start_turn
    return start_turn(chat_id, prompt, note_source=source, reply_source=source + "_reply")


def setup_native_plans_routes() -> APIRouter:
    router = APIRouter(tags=["native_plans"])

    @router.get("/api/native_plans")
    async def list_for_chat(request: Request, chat: str = ""):
        user = require_user(request)
        rows = [e for e in native_plans.for_chat(chat[:100])
                if (e.get("owner") or "") == (user or "")]
        return {"plans": [native_plans.view(e) for e in rows]}

    @router.get("/api/native_plans/{plan_id}")
    async def get_plan(request: Request, plan_id: str):
        user = require_user(request)
        return native_plans.view(_entry(plan_id, user))

    @router.post("/api/native_plans/{plan_id}/approve")
    async def approve(request: Request, plan_id: str):
        user = require_user(request)
        _entry(plan_id, user)
        try:
            e = native_plans.approve(plan_id, user)
        except native_plans.Refused as r:
            raise HTTPException(r.status, r.message)
        resuming = _resume(e.get("chat_session_id") or "", native_plans.approved_prompt(e),
                           "native_plan_approved")
        return {**native_plans.view(e), "resuming": resuming}

    @router.post("/api/native_plans/{plan_id}/deny")
    async def deny(request: Request, plan_id: str, body: Optional[DenyBody] = None):
        user = require_user(request)
        _entry(plan_id, user)
        try:
            e = native_plans.deny(plan_id, user, (body.note if body else "") or "")
        except native_plans.Refused as r:
            raise HTTPException(r.status, r.message)
        resuming = _resume(e.get("chat_session_id") or "", native_plans.denied_prompt(e),
                           "native_plan_denied")
        return {**native_plans.view(e), "resuming": resuming}

    return router
