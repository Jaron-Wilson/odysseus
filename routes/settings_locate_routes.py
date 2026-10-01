"""POST /api/settings/locate: the Utility model picks a place in Settings
(src/settings_locate.py, used by static/js/settingsNav.js)."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import effective_user, require_authenticated_request


def setup_settings_locate_routes() -> APIRouter:
    router = APIRouter(tags=["settings"])

    @router.post("/api/settings/locate")
    async def locate(request: Request) -> Dict[str, Any]:
        require_authenticated_request(request)
        owner = effective_user(request) or None
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "Send JSON: {query, candidates}")
        from src import settings_locate
        query = str(body.get("query") or "").strip()
        candidates = settings_locate.clean_candidates(body.get("candidates"))
        if not query or not candidates:
            raise HTTPException(400, "Both a query and candidates are needed")
        return await settings_locate.pick(query, candidates, owner)

    return router
