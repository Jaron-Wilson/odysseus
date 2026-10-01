"""What's new (static/js/whatsNew.js): the merged PRs, and chats about them
(src/whats_new.py). Anyone logged in can read it and refresh it; "new since
you last looked" is kept per user."""
import asyncio
import logging
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from core.models import ChatMessage

logger = logging.getLogger(__name__)


def _owner(request: Request) -> Optional[str]:
    from src.auth_helpers import effective_user, require_authenticated_request
    require_authenticated_request(request)
    return effective_user(request) or None


def _is_admin(request: Request, user: Optional[str]) -> bool:
    try:
        auth_mgr = getattr(request.app.state, "auth_manager", None)
        return bool(user and auth_mgr is not None and auth_mgr.is_admin(user))
    except Exception:
        return False


def chat_name(prs, titles) -> str:
    if len(prs) == 1:
        return f"About #{prs[0]} {titles[0]}"[:80]
    nums = ", ".join(f"#{n}" for n in prs[:4])
    return f"About {nums}" + (f" and {len(prs) - 4} more" if len(prs) > 4 else "")


def placeholder(prs, titles) -> str:
    if len(prs) == 1:
        t = titles[0] if len(titles[0]) <= 48 else titles[0][:48].rsplit(" ", 1)[0]
        return f"Ask about #{prs[0]} {t}..."
    return f"Ask about {', '.join(f'#{n}' for n in prs[:5])}{', ...' if len(prs) > 5 else ''}..."


def default_chat(owner: Optional[str], is_admin: bool) -> dict:
    """The model a new chat starts on, the way the web UI's new chat picks it."""
    from routes.model_routes import resolve_default_chat
    return resolve_default_chat(owner or "", is_admin)


def new_chat(session_manager, owner: Optional[str], model: str, endpoint_id: str, name: str):
    from routes.session_routes import create_direct_chat
    return create_direct_chat(session_manager, owner, model, endpoint_id, name=name)


def setup_whats_new_routes(session_manager) -> APIRouter:
    router = APIRouter(tags=["whats_new"])
    from src import whats_new as wn

    def _kick() -> None:
        # Nothing cached from GitHub yet in this data dir: fetch it now, so the
        # first visit fills in the descriptions without waiting half an hour.
        if not wn._state["refreshed"] and not wn._state.get("kicked"):
            wn._state["kicked"] = True
            threading.Thread(target=wn.refresh, daemon=True, name="whats-new-refresh").start()

    @router.get("/api/whats-new")
    async def whats_new(request: Request):
        owner = _owner(request)
        _kick()
        return await asyncio.to_thread(wn.entries, owner)

    @router.post("/api/whats-new/refresh")
    async def refresh(request: Request):
        owner = _owner(request)
        res = await asyncio.to_thread(wn.refresh, True)
        out = await asyncio.to_thread(wn.entries, owner)
        out["refresh"] = res
        return out

    @router.get("/api/whats-new/unseen")
    async def unseen(request: Request):
        owner = _owner(request)
        return {"count": await asyncio.to_thread(wn.unseen_count, owner), "last_seen": wn.last_seen(owner)}

    @router.post("/api/whats-new/seen")
    async def seen(request: Request):
        owner = _owner(request)
        return {"last_seen": wn.mark_seen(owner)}

    @router.post("/api/whats-new/ask")
    async def ask(request: Request):
        """A new chat on the user's default model, about one or more PRs.
        Their details stay with the chat as context; nothing is sent yet."""
        owner = _owner(request)
        body = await request.json()
        try:
            prs = [int(str(n).lstrip("#")) for n in (body.get("prs") or [])][:wn.MAX_ASK_PRS]
        except (TypeError, ValueError):
            raise HTTPException(400, "prs must be PR numbers")
        if not prs:
            raise HTTPException(400, "Which PRs?")
        ctx = await asyncio.to_thread(wn.build_ask_context, prs)
        if not ctx["prs"]:
            raise HTTPException(404, "Those PRs are not in this checkout's history")
        dc = await asyncio.to_thread(default_chat, owner, _is_admin(request, owner))
        model, endpoint_id = dc.get("model") or "", dc.get("endpoint_id") or ""
        if not (model and endpoint_id):
            raise HTTPException(400, "No default model is set: pick one in Settings first")
        name = chat_name(ctx["prs"], ctx["titles"])
        sid, sess = new_chat(session_manager, owner, model, endpoint_id, name)
        wn.save_ask(sid, owner, ctx)
        if len(ctx["prs"]) == 1:
            what = f"**#{ctx['prs'][0]} {ctx['titles'][0]}**"
        else:
            what = ", ".join(f"**#{n}**" for n in ctx["prs"])
        one = len(ctx["prs"]) == 1
        sess.add_message(ChatMessage("assistant", (
            f"Ask me anything about {what}. I have {'its' if one else 'their'} description"
            f"{'' if one else 's'}, the files changed and the diff"
            f"{' (cut short, it is a big one)' if ctx['diff_cut'] else ''}, and they stay with this chat."),
            metadata={"source": "whats_new_intro", "prs": ctx["prs"]}))
        logger.info("[whats-new] chat %s about %s for %s", sid[:8], ctx["prs"], owner or "-")
        return {"id": sid, "name": name, "prs": ctx["prs"], "placeholder": placeholder(ctx["prs"], ctx["titles"]),
                "context_chars": len(ctx["text"])}

    @router.get("/api/whats-new/ask/{session_id}")
    async def ask_info(request: Request, session_id: str):
        """Which PRs a chat is about (for its composer's placeholder)."""
        owner = _owner(request)
        info = wn.ask_info(session_id)
        if not info:
            return {}
        try:
            sess = session_manager.get_session(session_id)
            if owner and getattr(sess, "owner", None) and sess.owner != owner:
                return {}
        except KeyError:
            return {}
        return dict(info, placeholder=placeholder(info["prs"], info["titles"]))

    return router
