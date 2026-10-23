"""HTTP routes of mod styles (src/mods/style.py).

  GET  /api/mods/ui/styles             any signed-in user: the stylesheets to link now, {safe, styles: [{id, v, url}]}
  GET  /api/mods/ui/style/<id>.css     any signed-in user: one mod's checked stylesheet
  GET  /api/mods/ui/style-status       admin: each ui.style mod's state (in use, size, why refused)
  POST /api/mods/v1/ui/style/refresh   a mod granted ui.style: "my stylesheet changed, fetch it again"
"""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from src.mods import proxy, registry, style


def setup_style_routes(router: APIRouter) -> None:
    def _signed_in(request: Request) -> str:
        user = proxy.cookie_user(request)
        if user is None:
            raise HTTPException(401, "Not signed in")
        return user

    @router.get("/api/mods/ui/styles")
    async def styles(request: Request):
        _signed_in(request)
        if style.is_safe(request):
            return JSONResponse({"safe": True, "styles": []}, headers={"Cache-Control": "no-store"})
        try:
            await asyncio.wait_for(style.ensure_fresh(), 15)
        except asyncio.TimeoutError:
            pass
        return JSONResponse({"safe": False, "styles": style.listing()}, headers={"Cache-Control": "no-store"})

    @router.get("/api/mods/ui/style/{name}")
    async def stylesheet(request: Request, name: str):
        _signed_in(request)
        m = re.match(r"^([a-z][a-z0-9-]{1,31})\.css$", name)
        entry = registry.get(m.group(1)) if m else None
        css = style.read(entry["id"]) if entry and style.active(entry) else None
        if css is None:
            raise HTTPException(404, "No stylesheet")
        return Response(css, media_type="text/css; charset=utf-8",
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @router.get("/api/mods/ui/style-status")
    async def style_status(request: Request):
        from src.mods.routes import _require_admin
        _require_admin(request)
        return {"mods": {e["id"]: style.status(e["id"]) for e in registry.list_mods() if style.eligible(e)},
                "tokens": style.TOKEN_DOCS, "max_bytes": style.MAX_BYTES}

    @router.post("/api/mods/v1/ui/style/refresh")
    async def v1_style_refresh(request: Request):
        from src.mods import routes as mod_routes
        mod_routes.RATE_LIMITS.setdefault("ui.style", (30, 60))
        entry = mod_routes._mod(request, style.PERMISSION)
        return {"ok": True, "style": await style.refresh(entry["id"])}
