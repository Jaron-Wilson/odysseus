"""Settings › Email › Inbound mail: the Cloudflare mail Worker (src/mail_listener.py)."""
from fastapi import APIRouter, HTTPException, Request

from src import mail_listener


def setup_mail_listener_routes() -> APIRouter:
    router = APIRouter(prefix="/api/mail-listener", tags=["mail_listener"])

    def _admin(request: Request) -> str:
        from core.middleware import require_admin
        require_admin(request)            # it can start agent turns
        try:
            from src.auth_helpers import effective_user
            return effective_user(request) or ""
        except Exception:
            return ""

    @router.get("/config")
    def get_config(request: Request):
        _admin(request)
        return mail_listener.public_config()

    @router.put("/config")
    async def put_config(request: Request):
        owner = _admin(request)
        try:
            return mail_listener.update_config(await request.json(), owner=owner)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.post("/check")
    async def check(request: Request):
        _admin(request)
        return await mail_listener.check_once()

    return router
