"""The desktop overlay's inbox and replies: /api/overlay/*.

The overlay (tools/music_overlay) runs on the PC with an API token scoped
"overlay", so it can read what the AI said and answer in the user's own
chats, and nothing else. A browser session works too.
"""

import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import effective_user, require_authenticated_request


def _owner(request: Request) -> str:
    require_authenticated_request(request)
    if getattr(request.state, "api_token", False):
        scopes = getattr(request.state, "api_token_scopes", None) or []
        if "overlay" not in scopes and "admin" not in scopes:
            raise HTTPException(403, "This token cannot use the overlay")
    return effective_user(request) or ""


def _pending_plans(owner: str) -> list:
    from src import claude_code_approvals as approvals
    from src.chat_queue import _session_title
    out = []
    for p in approvals.pending_for(owner)[:5]:
        from src.agent_tools.claude_code_tool import engine_label
        engine = engine_label(p.get("engine") or "claude")
        out.append({"id": p["session_id"], "session_id": p["chat_session_id"],
                    "chat": _session_title(p["chat_session_id"]) if p["chat_session_id"] else "",
                    "runs_on": f"{engine} · {p.get('model') or 'local default'}",
                    "plan": p["plan"], "created": p["created"]})
    return out


# "Open" in the overlay brings up the Odysseus tab already showing Odysseus
# on that machine, instead of opening a new one. The overlay asks here; the
# open pages on the same machine (same address) poll, switch to the chat, put
# a marker in their tab title and say so; the overlay then finds that tab
# through Windows' UI Automation and selects it. No page answering means
# none is open, and the overlay opens a new tab after all.
_OPEN_REQUESTS: Dict[str, Dict[str, Any]] = {}      # owner -> the latest request
OPEN_REQUEST_TTL_S = 10.0


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


# What the phones are playing, for the overlay when its own PC plays nothing.
# Asked for: "popup says nothing playing when it's from my phone". Cached a
# few seconds: the overlay polls every second or two.
_REMOTE_CACHE: Dict[str, Any] = {"at": 0.0, "item": None}
REMOTE_CACHE_S = 4.0


async def _remote_now_playing() -> Any:
    from src import device_routing, devices
    if time.time() - _REMOTE_CACHE["at"] < REMOTE_CACHE_S:
        return _REMOTE_CACHE["item"]
    best = None
    for ph in device_routing.phone_devices():
        dev = devices.get(ph["name"])
        if not dev:
            continue
        try:
            import asyncio
            r = await asyncio.wait_for(devices.send_command(dev, "now_playing", {}), 4)
        except Exception:
            continue
        np = (r or {}).get("result") or {}
        if not np.get("title"):
            continue
        item = {"server_id": ph["server_id"], "name": ph["name"], "title": np.get("title") or "",
                "artist": np.get("artist") or "", "playing": bool(np.get("playing")),
                "art_jpeg_b64": np.get("art_jpeg_b64") or ""}
        if item["playing"] or best is None:
            best = item
        if item["playing"]:
            break
    _REMOTE_CACHE.update(at=time.time(), item=best)
    return best


def setup_overlay_routes() -> APIRouter:
    router = APIRouter(tags=["overlay"])

    @router.get("/api/overlay/remote_media")
    async def remote_media(request: Request):
        """A phone's song, for the overlay to show when its PC plays nothing."""
        _owner(request)
        return {"item": await _remote_now_playing()}

    @router.post("/api/overlay/remote_media/control")
    async def remote_media_control(request: Request):
        """Play/pause, next or previous on that phone, from the overlay."""
        _owner(request)
        body = await request.json()
        action = str(body.get("action") or "")
        sid = str(body.get("server_id") or "")
        if action not in ("play_pause", "next", "previous") or not sid.startswith("device:"):
            raise HTTPException(400, "action must be play_pause, next or previous, on a phone")
        from src import devices
        dev = devices.get(sid[len("device:"):])
        if not dev:
            raise HTTPException(404, "No such phone")
        r = await devices.send_command(dev, "media_control", {"action": action})
        _REMOTE_CACHE["at"] = 0.0                         # show the change at the next poll
        if not (r or {}).get("ok"):
            raise HTTPException(502, (r or {}).get("error") or "The phone did not answer")
        return {"ok": True}

    @router.get("/api/overlay/inbox")
    async def inbox(request: Request, since: float = 0.0) -> Dict[str, Any]:
        from src import overlay_inbox
        owner = _owner(request)
        now = time.time()
        # How long since a browser page on the overlay's own machine last
        # talked to us: the overlay closes itself when Odysseus is closed there.
        seen = (getattr(request.app.state, "browser_seen", {}) or {}).get(
            request.client.host if request.client else "")
        return {"now": now, "events": overlay_inbox.since(owner, since),
                "page_seen_ago": round(now - seen, 1) if seen else None,
                # Plans waiting on an answer: the overlay shows the newest
                # above the player while any is pending, and hides again after.
                "plans": _pending_plans(owner)}

    def _own_plan(request: Request, plan_id: str) -> dict:
        from src import claude_code_approvals as approvals
        from routes.claude_code_routes import _SESSION_ID_RE
        owner = _owner(request)
        if not _SESSION_ID_RE.fullmatch(plan_id):
            raise HTTPException(400, "Unknown plan")
        entry = approvals.get(plan_id)
        if not entry or (owner and entry.get("owner") and entry["owner"] != owner):
            raise HTTPException(404, "No such plan (it may have expired)")
        return entry

    def _plan_pdf(plan_id: str) -> str:
        import os
        from src.doc_pdf import PDF_DIR
        path = os.path.join(PDF_DIR, f"plan-{plan_id}.pdf")
        return path if os.path.isfile(path) else ""

    @router.get("/api/overlay/plan/{plan_id}")
    async def plan_doc(request: Request, plan_id: str) -> Dict[str, Any]:
        """The whole plan, for reading it in the overlay (over a game)."""
        from src.chat_queue import _session_title
        entry = _own_plan(request, plan_id)
        pages = 0
        pdf = _plan_pdf(plan_id)
        if pdf:
            try:
                import pymupdf as fitz
                with fitz.open(pdf) as doc:
                    pages = doc.page_count
            except Exception:
                pages = 0
        chat = entry.get("chat_session_id") or ""
        from src.agent_tools.claude_code_tool import engine_label
        engine = engine_label(entry.get("engine") or "claude")
        return {"id": plan_id, "status": entry.get("status"), "plan": entry.get("plan") or "",
                "chat": _session_title(chat) if chat else "", "session_id": chat,
                "runs_on": f"{engine} · {entry.get('model') or 'local default'}",
                "cwd": entry.get("cwd") or "", "pages": pages,
                "pdf_url": f"/api/claude_code/plan/{plan_id}/pdf?inline=1" if pdf else ""}

    @router.get("/api/overlay/plan/{plan_id}/page/{n}.png")
    async def plan_page(request: Request, plan_id: str, n: int, w: int = 620):
        """One page of the plan's PDF as an image."""
        from fastapi.responses import Response
        _own_plan(request, plan_id)
        pdf = _plan_pdf(plan_id)
        if not pdf:
            raise HTTPException(404, "No PDF was rendered for this plan")
        import pymupdf as fitz
        w = max(300, min(int(w or 620), 1600))
        with fitz.open(pdf) as doc:
            if not 1 <= n <= doc.page_count:
                raise HTTPException(404, "No such page")
            pg = doc[n - 1]
            zoom = w / max(pg.rect.width, 1)
            png = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    @router.post("/api/overlay/plan/{plan_id}/{verb}")
    async def answer_plan(request: Request, plan_id: str, verb: str) -> Dict[str, Any]:
        """Approve or deny a pending plan from the overlay, as the chat's
        links do (and with the same effect: approving starts the run)."""
        from src import claude_code_approvals as approvals
        from routes.claude_code_routes import _SESSION_ID_RE, approve_plan, deny_plan
        owner = _owner(request)
        if verb not in ("approve", "deny") or not _SESSION_ID_RE.fullmatch(plan_id):
            raise HTTPException(400, "Unknown plan or answer")
        entry = approvals.get(plan_id)
        if not entry or (owner and entry.get("owner") and entry["owner"] != owner):
            raise HTTPException(404, "No such plan (it may have expired)")
        return approve_plan(plan_id, owner) if verb == "approve" else deny_plan(plan_id, owner)

    @router.post("/api/overlay/open")
    async def open_chat(request: Request) -> Dict[str, Any]:
        """The overlay wants `session_id` on screen: ask this machine's pages."""
        import secrets
        owner = _owner(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            raise HTTPException(400, "session_id is required")
        nonce = secrets.token_hex(3)
        _OPEN_REQUESTS[owner] = {"nonce": nonce, "session_id": sid, "ip": _client_ip(request),
                                 "ts": time.time(), "acks": []}
        return {"nonce": nonce, "marker": f"[{nonce}]"}

    @router.get("/api/overlay/open/{nonce}")
    async def open_status(request: Request, nonce: str) -> Dict[str, Any]:
        owner = _owner(request)
        req = _OPEN_REQUESTS.get(owner)
        if not req or req["nonce"] != nonce:
            raise HTTPException(404, "No such request")
        return {"acks": req["acks"]}

    @router.get("/api/overlay/page/open-request")
    async def page_open_request(request: Request) -> Dict[str, Any]:
        """Polled by the Odysseus page: is the overlay on this machine asking
        for a chat to be brought up?"""
        owner = _owner(request)
        req = _OPEN_REQUESTS.get(owner)
        if (not req or time.time() - req["ts"] > OPEN_REQUEST_TTL_S
                or req["ip"] != _client_ip(request)):
            return {}
        return {"nonce": req["nonce"], "session_id": req["session_id"]}

    @router.post("/api/overlay/page/open-request/{nonce}/ack")
    async def page_open_ack(request: Request, nonce: str) -> Dict[str, Any]:
        owner = _owner(request)
        req = _OPEN_REQUESTS.get(owner)
        if not req or req["nonce"] != nonce:
            raise HTTPException(404, "No such request")
        try:
            body = await request.json()
        except Exception:
            body = {}
        req["acks"].append({"visible": bool(body.get("visible")), "ts": time.time()})
        return {"ok": True}

    @router.post("/api/overlay/reply")
    async def reply(request: Request) -> Dict[str, Any]:
        """Answer in a chat: queued behind any running reply, sent now if idle."""
        from src import agent_runs, chat_queue
        owner = _owner(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = str(body.get("session_id") or "").strip()
        text = str(body.get("text") or "").strip()
        if not sid or not text:
            raise HTTPException(400, "session_id and text are required")
        try:
            from src.ai_interaction import get_session_manager
            sess = get_session_manager().get_session(sid)
        except Exception:
            raise HTTPException(404, "No such chat")
        if owner and getattr(sess, "owner", None) and sess.owner != owner:
            raise HTTPException(404, "No such chat")
        try:
            chat_queue.add(sid, text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        running = agent_runs.is_active(sid)
        if not running:
            chat_queue.schedule_drain(sid)               # nothing to wait for: send it
        return {"ok": True, "queued_behind_reply": running}

    return router
