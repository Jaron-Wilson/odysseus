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

    # Inbound mail: read an email, and ask for help with it (which makes its chat).
    @router.get("/messages")
    def messages(request: Request):
        _admin(request)
        return mail_listener.list_messages()

    @router.get("/messages/{key}")
    def message(request: Request, key: str):
        _admin(request)
        try:
            return mail_listener.read_message(key)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "No such email")

    @router.delete("/messages/{key}")
    def delete(request: Request, key: str):
        _admin(request)
        try:
            mail_listener.delete_message(key)
        except KeyError:
            raise HTTPException(404, "No such email")
        return {"deleted": key}

    @router.post("/messages/{key}/ask")
    async def ask(request: Request, key: str):
        owner = _admin(request)
        body = await request.json()
        try:
            return mail_listener.ask_ai(key, endpoint_url=str(body.get("endpoint_url") or ""),
                                        model=str(body.get("model") or ""), owner=owner)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "No such email")
        except ValueError as e:
            raise HTTPException(400, str(e))

    return router
