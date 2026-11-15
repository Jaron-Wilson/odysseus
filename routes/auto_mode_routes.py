"""Approvals: Ask or Auto (src/auto_mode.py).

  GET  /api/auto_mode                 {mode, model, safety_model, gates}
  PUT  /api/auto_mode                 {"mode"?: "ask"|"auto", "model"?: "<model@Endpoint>" or ""}
  GET  /api/auto_mode/models          the models the safety check may use
  GET  /api/auto_mode/log?limit=N     the Auto mode log (newest first)
  GET  /api/auto_mode/chat/{id}       {override, effective, user_mode}
  PUT  /api/auto_mode/chat/{id}       {"mode": "ask"|"auto"|""}  ("" = use the default)

Only the user's own page changes these: a bearer API token is refused, and
app_api refuses the whole /api/auto_mode prefix (src/tool_implementations.py),
so the agent cannot switch itself to Auto.
"""
import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src import auto_mode
from src.auth_helpers import get_current_user, require_user


def _owner(request: Request) -> str:
    require_user(request)
    return get_current_user(request) or ""


def _no_token(request: Request) -> None:
    if getattr(request.state, "api_token", False):
        raise HTTPException(403, "Approvals can only be changed from your own page.")


def _check_chat(request: Request, session_id: str) -> str:
    owner = _owner(request)
    try:
        from src.ai_interaction import get_session_manager
        sess = get_session_manager().get_session(session_id)
    except Exception:
        raise HTTPException(404, "No such chat")
    if not sess or (owner and getattr(sess, "owner", None) and sess.owner != owner):
        raise HTTPException(404, "No such chat")
    return owner


def setup_auto_mode_routes() -> APIRouter:
    router = APIRouter(tags=["auto_mode"])

    @router.get("/api/auto_mode")
    async def get_settings(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        return await asyncio.to_thread(auto_mode.settings_view, owner or None)

    @router.put("/api/auto_mode")
    async def put_settings(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _no_token(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        try:
            auto_mode.set_user_settings(owner or None, mode=body.get("mode"), model=body.get("model"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return await asyncio.to_thread(auto_mode.settings_view, owner or None)

    @router.get("/api/auto_mode/models")
    async def models(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        try:
            from src.mods.llm import models as _models
            items = await asyncio.to_thread(_models, owner or None)
        except Exception:
            items = []
        return {"models": [{"spec": m["spec"], "name": m.get("name") or m["model"], "endpoint": m.get("endpoint"),
                            "local": bool(m.get("local"))} for m in items]}

    @router.get("/api/auto_mode/log")
    async def get_log(request: Request, limit: int = 100) -> Dict[str, Any]:
        owner = _owner(request)
        return {"log": auto_mode.recent_log(owner or None, min(max(1, limit), 500))}

    @router.get("/api/auto_mode/chat/{session_id}")
    async def get_chat(request: Request, session_id: str) -> Dict[str, Any]:
        owner = _check_chat(request, session_id)
        return {"override": auto_mode.chat_override(session_id),
                "effective": auto_mode.effective_mode(owner or None, session_id),
                "user_mode": auto_mode.user_mode(owner or None)}

    @router.put("/api/auto_mode/chat/{session_id}")
    async def put_chat(request: Request, session_id: str) -> Dict[str, Any]:
        owner = _check_chat(request, session_id)
        _no_token(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            auto_mode.set_chat_override(session_id, (body or {}).get("mode") or "")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"override": auto_mode.chat_override(session_id),
                "effective": auto_mode.effective_mode(owner or None, session_id),
                "user_mode": auto_mode.user_mode(owner or None)}

    return router
