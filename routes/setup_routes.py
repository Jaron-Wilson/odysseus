"""The guided setup checklist: Settings > Setup, the Home card, the first-run open.

    GET  /api/setup/status?here=on|off|unsupported&fresh=1   the checklist, computed from real state now
    POST /api/setup/dismiss  {id, dismissed}          hide (or bring back) one step, for this user
    POST /api/setup/seen                              the first-run open happened, do not repeat it
    POST /api/setup/card     {hidden, ids}            hide the Home card until a new step needs the person

`here` is what this browser knows about itself (the server cannot see whether
notifications are on in the browser that is asking). Everything else is read
from the server's own state in src/setup_guide.py, per user: a person who is
not an admin gets only the steps they can do.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from src import setup_guide
from src.auth_helpers import _auth_disabled, require_user

logger = logging.getLogger(__name__)


def _is_admin(request: Request, user: str) -> bool:
    if _auth_disabled() or not user:
        return True
    mgr = getattr(request.app.state, "auth_manager", None)
    try:
        return bool(mgr and mgr.is_admin(user))
    except Exception:
        return False


def setup_setup_routes(get_mcp_manager=None) -> APIRouter:
    router = APIRouter(prefix="/api/setup", tags=["setup"])

    def _mgr():
        if get_mcp_manager:
            return get_mcp_manager()
        from src.tool_utils import get_mcp_manager as g
        return g()

    async def _json(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    @router.get("/status")
    async def status(request: Request, here: str = "", fresh: bool = False):
        user = require_user(request)
        if fresh:
            setup_guide.forget_devices()
        return await setup_guide.build(request, user, _is_admin(request, user),
                                       here if here in ("on", "off", "unsupported") else None, _mgr())

    @router.post("/dismiss")
    async def dismiss(request: Request):
        user = require_user(request)
        body = await _json(request)
        sid = str(body.get("id") or "")
        if not setup_guide.ID_RE.match(sid):
            raise HTTPException(400, "id is not a setup step id")
        state = setup_guide.load_state(user)
        gone = [d for d in state["dismissed"] if d != sid]
        state["dismissed"] = gone + [sid] if body.get("dismissed", True) is not False else gone
        setup_guide.save_state(user, state)
        return {"ok": True, "dismissed": state["dismissed"]}

    @router.post("/seen")
    async def seen(request: Request):
        user = require_user(request)
        state = setup_guide.load_state(user)
        state["seen"] = True
        setup_guide.save_state(user, state)
        return {"ok": True}

    @router.post("/card")
    async def card(request: Request):
        user = require_user(request)
        body = await _json(request)
        state = setup_guide.load_state(user)
        if body.get("hidden"):
            ids = [i for i in (body.get("ids") or []) if isinstance(i, str) and setup_guide.ID_RE.match(i)]
            state["card_hidden"] = ids[:setup_guide.MAX_DISMISSED]
        else:
            state["card_hidden"] = None
        setup_guide.save_state(user, state)
        return {"ok": True}

    return router
