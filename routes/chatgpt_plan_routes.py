"""Sign in with ChatGPT routes (Settings > Services card).

Per Odysseus user: each browser user signs in to their own ChatGPT account,
and their tokens and "ChatGPT" endpoint row belong to them only.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src import chatgpt_plan
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def _owner(request: Request):
    return require_user(request) or None


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, chatgpt_plan.ChatGPTPlanError):
        return HTTPException(exc.status, chatgpt_plan.redact(exc))
    logger.error("ChatGPT plan route failed: %s", chatgpt_plan.redact(exc))
    return HTTPException(500, "ChatGPT sign-in hit an unexpected error.")


def setup_chatgpt_plan_routes() -> APIRouter:
    router = APIRouter(prefix="/api/chatgpt-plan", tags=["chatgpt-plan"])

    @router.get("/status")
    def get_status(request: Request) -> Dict[str, Any]:
        return chatgpt_plan.status(_owner(request))

    @router.post("/sign-in/start")
    def start(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        try:
            return chatgpt_plan.start_sign_in(owner)
        except Exception as exc:
            raise _http(exc)

    @router.post("/sign-in/complete")
    async def complete(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        pasted = (body or {}).get("redirect_url") or ""
        try:
            result = chatgpt_plan.complete_sign_in(pasted, owner=owner)
        except Exception as exc:
            raise _http(exc)
        try:
            result["models"] = chatgpt_plan.refresh_models(owner)["models"]
        except chatgpt_plan.ChatGPTPlanError as exc:
            result["models"] = []
            result["models_error"] = chatgpt_plan.redact(exc)
        return result

    @router.post("/models/refresh")
    def refresh(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        try:
            out = chatgpt_plan.refresh_models(owner)
        except Exception as exc:
            raise _http(exc)
        return {"models": out["models"], "status": chatgpt_plan.status(owner)}

    @router.post("/sign-out")
    def sign_out(request: Request) -> Dict[str, Any]:
        try:
            return chatgpt_plan.sign_out(_owner(request))
        except Exception as exc:
            raise _http(exc)

    return router
