"""The cloud browser (src/cloud_browser.py): watch it live, and take over.

Admin only: it is one browser for the whole server, with its logins."""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.middleware import require_admin

KEEPALIVE_S = 15


def _user(request: Request) -> str:
    require_admin(request)
    try:
        from src.auth_helpers import effective_user
        return effective_user(request) or ""
    except Exception:
        return ""


def setup_cloud_browser_routes() -> APIRouter:
    router = APIRouter(prefix="/api/cloud-browser", tags=["cloud_browser"])

    @router.get("/status")
    async def status(request: Request):
        _user(request)
        from src.cloud_browser import viewer
        return await viewer.status()

    @router.get("/stream")
    async def stream(request: Request):
        """Server-sent events: frames (JPEG, base64), the tab's address, the
        tab list, and who has taken over."""
        _user(request)
        from src.cloud_browser import viewer
        try:
            q = await viewer.subscribe()
        except Exception as e:
            raise HTTPException(503, str(e)[:300])

        async def events():
            try:
                yield f"event: control\ndata: {json.dumps({'taken_over_by': viewer.taken_over_by})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {msg.get('type', 'message')}\ndata: {json.dumps(msg)}\n\n"
            finally:
                viewer.unsubscribe(q)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/input")
    async def act(request: Request):
        _user(request)
        from src.cloud_browser import viewer
        try:
            return await viewer.act(await request.json())
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            # A navigation that times out or a closed tab: say so, keep going.
            return {"ok": False, "error": str(e).splitlines()[0][:300]}

    @router.post("/cookies")
    async def import_cookies(request: Request):
        """A login exported from an ordinary browser (cookies.txt or JSON),
        for sites that will not sign in a driven browser."""
        _user(request)
        from src.cloud_browser import viewer
        body = await request.json()
        text = str(body.get("text") or "")
        if len(text) > 2_000_000:
            raise HTTPException(413, "That file is too big for a cookie export.")
        try:
            return await viewer.import_cookies(text)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.post("/control")
    async def control(request: Request):
        user = _user(request)
        from src.cloud_browser import viewer
        body = await request.json()
        return await viewer.take_over(user, bool(body.get("on")))

    return router
