"""The voice call's side of the opencode_chat tool (src/opencode_chat.py).

  GET    /api/opencode_chat/chats?chat=<id>      the opencode server's recent chats, and the one this chat is bound to
  PUT    /api/opencode_chat/binding              {"chat": <odysseus chat id>, "opencode": <id or title>}: the user's
                                                 own pick in the call menu. It binds AND arms that chat, so the agent
                                                 can send into it without asking before each message (ARM_TTL_S)
  DELETE /api/opencode_chat/binding?chat=<id>    unbind
  GET    /api/opencode_chat/updates?chat=&after= what the call should say next: short progress lines and the
                                                 summary of a finished run, plus a send still waiting for the
                                                 user's Approve (the call shows Approve and Deny for it). The call
                                                 page polls it; each poll also tells the server a call is open,
                                                 which is what switches spoken progress on

Only an authenticated request from the user's own page can arm a chat; nothing a model can call reaches these
routes (app_api refuses /api/opencode_chat).
"""
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src import opencode_chat as oc
from src.auth_helpers import effective_user, require_authenticated_request


def setup_opencode_chat_routes() -> APIRouter:
    router = APIRouter(tags=["opencode-chat"])

    def _check(request: Request, session_id: str) -> str:
        require_authenticated_request(request)
        owner = effective_user(request) or ""
        try:
            from src.ai_interaction import get_session_manager
            sess = get_session_manager().get_session(session_id)
        except Exception:
            raise HTTPException(404, "No such chat")
        if owner and getattr(sess, "owner", None) and sess.owner != owner:
            raise HTTPException(404, "No such chat")
        from src.tool_security import owner_is_admin_or_single_user
        if not owner_is_admin_or_single_user(owner or None):
            raise HTTPException(403, "Only an admin can use the opencode_chat tool.")
        return owner

    def _bound_view(b: Any) -> Any:
        if not b:
            return None
        import time
        return {"id": b["id"], "title": b["title"], "folder": b.get("folder") or "",
                "armed": time.time() < float(b.get("armed_until") or 0),
                "armed_until": b.get("armed_until") or 0}

    @router.get("/api/opencode_chat/chats")
    async def chats(request: Request, chat: str = "") -> Dict[str, Any]:
        _check(request, chat)
        try:
            rows = await oc.list_chats(25)
        except oc.OpenCodeError as e:
            raise HTTPException(502, str(e))
        return {"chats": rows, "bound": _bound_view(oc.get_binding(chat))}

    @router.put("/api/opencode_chat/binding")
    async def put_binding(request: Request) -> Dict[str, Any]:
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        sid = str(body.get("chat") or "")
        _check(request, sid)
        try:
            target = await oc.find(str(body.get("opencode") or ""))
        except oc.OpenCodeError as e:
            raise HTTPException(404, str(e))
        return {"bound": _bound_view(oc.bind(sid, target, armed=True))}

    @router.delete("/api/opencode_chat/binding")
    async def delete_binding(request: Request, chat: str = "") -> Dict[str, Any]:
        _check(request, chat)
        return {"unbound": oc.unbind(chat), "bound": None}

    @router.get("/api/opencode_chat/updates")
    async def updates(request: Request, chat: str = "", after: int = 0) -> Dict[str, Any]:
        owner = _check(request, chat)
        oc.note_call(chat)
        items, cursor = oc.updates_after(chat, max(0, after))
        ask = None
        try:
            from src import action_approvals as AA
            p = AA.pending_for_chat(chat)
            if p and p.get("kind") == "opencode_chat" and (p.get("owner") or "") == (owner or ""):
                ask = {"id": p["id"], "title": p.get("title") or "", "message": (p.get("body") or "")[:300]}
        except Exception:
            ask = None
        return {"items": items, "after": cursor, "working": bool(oc.working_chats(chat)),
                "bound": _bound_view(oc.get_binding(chat)), "approval": ask}

    return router
