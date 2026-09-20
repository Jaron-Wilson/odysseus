"""Web Push subscription endpoints — /api/push/*."""

import logging

from fastapi import APIRouter, HTTPException, Request

from src import webpush
from src.auth_helpers import _auth_disabled, get_current_user

logger = logging.getLogger(__name__)


def setup_push_routes() -> APIRouter:
    router = APIRouter(tags=["push"])

    def _require_user(request: Request) -> str:
        user = get_current_user(request)
        if not user:
            if _auth_disabled():
                return ""
            raise HTTPException(401, "Not authenticated")
        return user

    @router.get("/api/push/key")
    async def get_key(request: Request):
        """The VAPID public key the browser needs to subscribe."""
        _require_user(request)
        return {"public_key": webpush.public_key()}

    @router.post("/api/push/subscribe")
    async def subscribe(request: Request):
        user = _require_user(request)
        body = await request.json()
        sub = body.get("subscription") or body
        try:
            rec = webpush.save_subscription(
                sub, device=(body.get("device") or "").strip(), owner=user)
        except ValueError as e:
            raise HTTPException(400, str(e))
        logger.info("[webpush] subscribed %s (%s)", rec.get("device") or "unnamed", user or "-")
        return {"ok": True, "device": rec.get("device"), "endpoint": rec["endpoint"][:60] + "…"}

    @router.post("/api/push/unsubscribe")
    async def unsubscribe(request: Request):
        _require_user(request)
        body = await request.json()
        endpoint = (body.get("endpoint") or "").strip()
        if not endpoint:
            raise HTTPException(400, "endpoint is required")
        return {"ok": webpush.remove_subscription(endpoint)}

    @router.get("/api/push/subscriptions")
    async def list_subs(request: Request):
        _require_user(request)
        return {
            "subscriptions": [
                {"device": s.get("device"), "owner": s.get("owner"),
                 "endpoint": (s.get("endpoint") or "")[:60] + "…"}
                for s in webpush.load_subscriptions()
            ]
        }

    @router.post("/api/push/test")
    async def test_push(request: Request):
        """Send a real notification, so setup can be confirmed from the UI."""
        _require_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        result = await webpush.send(
            body.get("title") or "Odysseus",
            body.get("body") or "Notifications are working.",
            device=(body.get("device") or "").strip(),
            url=body.get("url") or "/",
        )
        return result

    return router
