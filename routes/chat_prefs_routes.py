"""Per-chat switches (src/chat_prefs.py): /api/chat-prefs/{session_id}."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import effective_user, require_authenticated_request


def setup_chat_prefs_routes() -> APIRouter:
    router = APIRouter(tags=["chat-prefs"])

    def _check(request: Request, session_id: str) -> None:
        require_authenticated_request(request)
        owner = effective_user(request) or ""
        try:
            from src.ai_interaction import get_session_manager
            sess = get_session_manager().get_session(session_id)
        except Exception:
            raise HTTPException(404, "No such chat")
        if owner and getattr(sess, "owner", None) and sess.owner != owner:
            raise HTTPException(404, "No such chat")

    @router.get("/api/chat-prefs/{session_id}")
    async def get_prefs(request: Request, session_id: str) -> Dict[str, Any]:
        from src import chat_prefs
        _check(request, session_id)
        return chat_prefs.get(session_id)

    @router.put("/api/chat-prefs/{session_id}")
    async def put_prefs(request: Request, session_id: str) -> Dict[str, Any]:
        from src import chat_prefs
        _check(request, session_id)
        try:
            body = await request.json()
        except Exception:
            body = {}
        out = chat_prefs.get(session_id)
        for key, value in (body or {}).items():
            try:
                out = chat_prefs.set_pref(session_id, key, value)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return out

    return router
