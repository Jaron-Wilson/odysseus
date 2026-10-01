"""The Terminal (static/js/terminal.js): a live shell in the browser.

  WS     /api/terminal/ws                 start a session, or ?id= to reattach
  GET    /api/terminal/sessions           this user's sessions
  PATCH  /api/terminal/sessions/{id}      rename one
  DELETE /api/terminal/sessions/{id}      close one (kills its processes)
  GET    /api/terminal/info               shell, grace period, session cap

Admin only: a session is a full shell as the user the server runs as. The
auth middleware (app.py) is HTTP middleware and never sees WebSocket
connections, so the socket checks the session cookie and the admin role
itself, and checks the Origin header so another site can't open a socket
with the admin's cookie (cross-site WebSocket hijacking).

WebSocket messages: binary frames are raw terminal bytes both ways. Text
frames are JSON control messages. From the browser: {"type": "resize",
"rows", "cols"}, {"type": "title", "title"}, {"type": "ping"}. To the
browser: {"type": "hello", ...session info, "replay"}, {"type": "exit",
"code"}, {"type": "detached"} (opened in another tab), {"type": "gone"}
(no such session any more) and {"type": "error", "message"}.
"""
import asyncio
import json
import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel

from core.middleware import require_admin
from src import terminal
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

# Close codes the browser acts on (static/js/terminal.js).
CLOSE_TAKEN_OVER = 4000     # opened in another tab: don't reconnect
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_GONE = 4404
CLOSE_TOO_MANY = 4429


def _auth_disabled() -> bool:
    return os.getenv("AUTH_ENABLED", "true").lower() == "false"


def _owner(request: Request) -> str:
    return get_current_user(request) or ""


def _norm_host(netloc: str, scheme: str = "") -> str:
    netloc = (netloc or "").strip().lower()
    for port, schemes in ((":443", ("https", "wss", "")), (":80", ("http", "ws", ""))):
        if netloc.endswith(port) and scheme in schemes:
            return netloc[: -len(port)]
    return netloc


def origin_ok(ws: WebSocket) -> bool:
    """The page opening the socket has to be this server's own. Browsers
    always send Origin on a WebSocket; a missing one is refused too."""
    origin = (ws.headers.get("origin") or "").strip()
    if not origin or origin == "null":
        return False
    extra = {o.strip().rstrip("/").lower() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()}
    if origin.rstrip("/").lower() in extra:
        return True
    o = urlsplit(origin)
    got = _norm_host(o.netloc, o.scheme)
    if not got:
        return False
    hosts = {_norm_host(ws.headers.get("host", ""))}
    # Behind a proxy (Tailscale Serve, a tunnel) the public name can arrive
    # here instead. A browser can't set this header on a WebSocket, so it
    # doesn't let another site in.
    for h in (ws.headers.get("x-forwarded-host") or "").split(","):
        if h.strip():
            hosts.add(_norm_host(h))
    return got in hosts


def ws_admin(ws: WebSocket) -> Optional[str]:
    """The admin behind this socket ("" with auth off), or None to refuse.
    Same rules as core.middleware.require_admin, from the session cookie."""
    if _auth_disabled():
        return ""
    mgr = getattr(ws.app.state, "auth_manager", None)
    if not mgr or not getattr(mgr, "is_configured", False):
        return None
    try:
        from routes.auth_routes import SESSION_COOKIE
    except Exception:
        SESSION_COOKIE = "odysseus_session"
    token = ws.cookies.get(SESSION_COOKIE)
    if not token or not mgr.validate_token(token):
        return None
    user = mgr.get_username_for_token(token)
    if not user or not mgr.is_admin(user):
        return None
    return user


async def _refuse(ws: WebSocket, code: int, message: str) -> None:
    await ws.accept()
    try:
        await ws.send_text(json.dumps({"type": "error", "message": message}))
        await ws.close(code=code)
    except Exception:
        pass


class _Rename(BaseModel):
    title: str


def setup_terminal_routes() -> APIRouter:
    router = APIRouter(tags=["terminal"])

    @router.websocket("/api/terminal/ws")
    async def terminal_ws(ws: WebSocket):
        if not origin_ok(ws):
            await ws.close(code=CLOSE_FORBIDDEN)
            return
        owner = ws_admin(ws)
        if owner is None:
            await ws.close(code=CLOSE_UNAUTHORIZED)
            return
        q = ws.query_params
        rows, cols = q.get("rows"), q.get("cols")
        sid = q.get("id")
        if sid:
            s = terminal.manager.get(owner, sid)
            if not s:
                await ws.accept()
                try:
                    await ws.send_text(json.dumps({"type": "gone", "id": sid}))
                    await ws.close(code=CLOSE_GONE)
                except Exception:
                    pass
                return
            replay = True
        else:
            try:
                s = terminal.manager.create(owner, cwd=q.get("cwd"), rows=rows or 24, cols=cols or 80,
                                            title=q.get("title"))
            except terminal.TooManySessions as e:
                await _refuse(ws, CLOSE_TOO_MANY, str(e))
                return
            except ValueError as e:
                await _refuse(ws, CLOSE_BAD_REQUEST, str(e))
                return
            except OSError as e:
                logger.warning("Could not start a terminal: %s", e)
                await _refuse(ws, 1011, f"Could not start a shell: {e.strerror or e}")
                return
            replay = False
        await ws.accept()
        if rows and cols:
            s.resize(rows, cols)
        att = s.attach(ws)
        hello = {"type": "hello", "replay": replay, **s.info()}
        sender = asyncio.create_task(terminal.pump(s, att, hello))

        async def receive():
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    return
                data = m.get("bytes")
                if data is not None:
                    s.write(data)
                    continue
                text = m.get("text")
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "resize":
                    s.resize(msg.get("rows"), msg.get("cols"))
                elif kind == "input" and isinstance(msg.get("data"), str):
                    s.write(msg["data"].encode("utf-8", "replace"))
                elif kind == "title" and isinstance(msg.get("title"), str) and msg["title"].strip():
                    s.title = msg["title"].strip()[:60]

        receiver = asyncio.create_task(receive())
        try:
            await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # No awaiting in here: this also runs when the server cancels
            # the connection, and must not swallow that cancellation.
            taken_over = s.att is not att and not s.exited
            s.detach(att)
            for t in (sender, receiver):
                if not t.done():
                    t.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        try:
            if s.exited:
                await ws.close(code=1000)
            elif taken_over:
                await ws.send_text(json.dumps({"type": "detached"}))
                await ws.close(code=CLOSE_TAKEN_OVER)
        except Exception:
            pass

    @router.get("/api/terminal/sessions")
    async def list_sessions(request: Request):
        require_admin(request)
        return {"sessions": [s.info() for s in terminal.manager.list(_owner(request))]}

    @router.patch("/api/terminal/sessions/{sid}")
    async def rename_session(sid: str, body: _Rename, request: Request):
        require_admin(request)
        s = terminal.manager.get(_owner(request), sid)
        if not s:
            raise HTTPException(404, "No such terminal")
        if body.title.strip():
            s.title = body.title.strip()[:60]
        return s.info()

    @router.delete("/api/terminal/sessions/{sid}")
    async def close_session(sid: str, request: Request):
        require_admin(request)
        if not terminal.manager.close(_owner(request), sid):
            raise HTTPException(404, "No such terminal")
        return {"ok": True}

    @router.get("/api/terminal/info")
    async def info(request: Request):
        require_admin(request)
        return {
            "shell": terminal.login_shell(), "home": terminal.home_dir(),
            "grace_s": terminal.GRACE_S, "max_sessions": terminal.MAX_SESSIONS,
            "scrollback_bytes": terminal.SCROLLBACK_BYTES,
        }

    return router
