"""The integrated IDE: a same-origin reverse proxy to code-server at /ide/,
and the admin API behind the Code panel (static/js/codePanel.js).

  /ide/...              HTTP and WebSocket, proxied to the editor (admin only)
  GET  /api/ide/config  the editor address and the project folders
  PUT  /api/ide/config
  GET  /api/ide/status  is the editor reachable, and which version
  GET  /api/ide/projects  git repos under the project folders
  GET  /api/ide/resolve?path=  check a folder for "Open folder"

The editor gives whoever uses it a shell on this server, so every route here
is admin only. AuthMiddleware only sees HTTP, so the WebSocket route checks the
session cookie itself. See src/ide.py for the header and cookie rewriting.
"""
import asyncio
import html
import logging
import os
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from starlette.background import BackgroundTask
from starlette.responses import RedirectResponse, Response, StreamingResponse

from core.middleware import require_admin
from src import ide

logger = logging.getLogger(__name__)

_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_BUFFER_BODY_MAX = 16 * 1024 * 1024

_client: Optional[httpx.AsyncClient] = None
_client_loop = None


def _http() -> httpx.AsyncClient:
    """One pooled client per event loop (the app has one; tests make several)."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client_loop = loop
        # No read timeout: responses stream as long as the editor sends them.
        # trust_env=False so an HTTP(S)_PROXY in the environment can't catch
        # a loopback upstream.
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0),
            follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=40))
    return _client


def _upstream_path(raw_path: str) -> str:
    """The path after /ide, still percent-encoded as the browser sent it."""
    rest = raw_path[len(ide.PREFIX):] if raw_path.startswith(ide.PREFIX) else raw_path
    return rest if rest.startswith("/") else "/" + rest


def _unreachable(upstream: str, err: str) -> Response:
    body = (
        "<!doctype html><meta charset=utf-8><title>Code editor unreachable</title>"
        "<body style=\"font:14px system-ui,sans-serif;padding:24px;max-width:640px\">"
        f"<h2 style=\"font-size:16px\">The code editor isn't reachable</h2>"
        f"<p>Odysseus couldn't connect to <code>{html.escape(upstream)}</code>: {html.escape(err)}.</p>"
        "<p>Check that code-server is running, or change the address in "
        "Settings &gt; System &gt; Code editor.</p></body>")
    return Response(body, status_code=502, media_type="text/html; charset=utf-8")


def _ws_allowed(ws: WebSocket) -> bool:
    """Admin check for a WebSocket. AuthMiddleware is HTTP-only, so resolve
    the session cookie here the same way it does, then ask require_admin."""
    if os.getenv("AUTH_ENABLED", "true").lower() != "false":
        auth_mgr = getattr(ws.app.state, "auth_manager", None)
        if auth_mgr is not None:
            try:
                from routes.auth_routes import SESSION_COOKIE
                token = ws.cookies.get(SESSION_COOKIE)
                if token and auth_mgr.validate_token(token):
                    ws.state.current_user = auth_mgr.get_username_for_token(token)
            except Exception:
                logger.debug("IDE websocket: session lookup failed", exc_info=True)
    try:
        require_admin(ws)
    except HTTPException:
        return False
    # Cross-site WebSocket hijacking: a browser always sends Origin, and it has
    # to be this site. (code-server checks it too, against X-Forwarded-Host.)
    origin = ws.headers.get("origin")
    if origin:
        host = ide.public_host(ws.headers).lower()
        if not host or urlsplit(origin).netloc.lower() != host:
            return False
    return True


def setup_ide_routes() -> APIRouter:
    router = APIRouter(tags=["ide"])

    # ── Admin API ──────────────────────────────────────────────────────────
    @router.get("/api/ide/config")
    def get_config(request: Request):
        require_admin(request)
        cfg = ide.load_config()
        return {**cfg, "default_upstream": ide.DEFAULT_UPSTREAM, "default_roots": ide.default_roots(),
                "odysseus_path": ide.odysseus_checkout()}

    @router.put("/api/ide/config")
    async def put_config(request: Request):
        require_admin(request)
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "Expected an object")
        try:
            return ide.save_config(data)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/api/ide/status")
    async def status(request: Request):
        require_admin(request)
        upstream = ide.upstream_url()
        out = {"upstream": upstream, "reachable": False, "version": None, "health": None, "error": None}
        try:
            async with httpx.AsyncClient(timeout=4.0, trust_env=False) as c:
                r = await c.get(upstream + "/healthz")
                out["reachable"] = True
                try:
                    out["health"] = (r.json() or {}).get("status")
                except ValueError:
                    pass
                # code-server puts its version in the page settings; the login
                # page (or the workbench when auth is off) carries it.
                try:
                    page = await c.get(upstream + "/login", follow_redirects=True)
                    out["version"] = ide.version_from_html(page.text)
                except httpx.HTTPError:
                    pass
        except httpx.HTTPError as e:
            out["error"] = str(e) or type(e).__name__
        return out

    @router.get("/api/ide/projects")
    async def projects(request: Request):
        require_admin(request)
        cfg = ide.load_config()
        return {"projects": await ide.list_projects(), "roots": cfg["roots"]}

    @router.get("/api/ide/resolve")
    def resolve(request: Request, path: str = ""):
        require_admin(request)
        try:
            real = ide.resolve_folder(path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"path": real, "name": os.path.basename(real) or real}

    # ── The proxy ──────────────────────────────────────────────────────────
    @router.api_route(ide.PREFIX, methods=_METHODS, include_in_schema=False)
    async def ide_root(request: Request):
        require_admin(request)
        q = request.url.query
        return RedirectResponse(ide.PREFIX + "/" + (f"?{q}" if q else ""), status_code=307)

    @router.api_route(ide.PREFIX + "/{path:path}", methods=_METHODS, include_in_schema=False)
    async def ide_http(request: Request, path: str):
        require_admin(request)
        upstream = ide.upstream_url()
        raw = request.scope.get("raw_path") or request.url.path.encode()
        url = upstream + _upstream_path(raw.decode("latin-1"))
        if request.url.query:
            url += "?" + request.url.query
        headers = ide.upstream_request_headers(
            request.headers, request.client.host if request.client else None, request.url.scheme)
        body = None
        if request.method not in ("GET", "HEAD"):
            try:
                length = int(request.headers.get("content-length") or -1)
            except ValueError:
                length = -1
            if 0 <= length <= _BUFFER_BODY_MAX:
                body = await request.body()
            else:
                body = request.stream()
        client = _http()
        req = client.build_request(request.method, url, headers=headers, content=body)
        try:
            r = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            return _unreachable(upstream, str(e) or type(e).__name__)
        resp = StreamingResponse(r.aiter_raw(), status_code=r.status_code, background=BackgroundTask(r.aclose))
        resp.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1"))
                            for k, v in ide.response_headers(r.headers, upstream)]
        return resp

    @router.websocket(ide.PREFIX + "/{path:path}")
    async def ide_ws(websocket: WebSocket, path: str):
        if not _ws_allowed(websocket):
            await websocket.close(code=1008)
            return
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import ConnectionClosed
            from websockets.typing import Subprotocol
        except ImportError:
            logger.warning("IDE websocket: the 'websockets' package is not installed")
            await websocket.close(code=1011)
            return
        upstream = ide.upstream_url()
        raw = websocket.scope.get("raw_path") or websocket.url.path.encode()
        target = urlsplit(upstream)
        scheme = "wss" if target.scheme == "https" else "ws"
        url = f"{scheme}://{target.netloc}{target.path.rstrip('/')}{_upstream_path(raw.decode('latin-1'))}"
        if websocket.url.query:
            url += "?" + websocket.url.query
        headers = [(k, v) for k, v in ide.upstream_request_headers(
            websocket.headers, websocket.client.host if websocket.client else None, websocket.url.scheme)
            if not k.lower().startswith("sec-websocket-")]
        protos = [p.strip() for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
        try:
            up = await connect(url, additional_headers=headers, user_agent_header=None,
                               subprotocols=[Subprotocol(p) for p in protos] or None,
                               max_size=None, ping_interval=None, open_timeout=15)
        except Exception as e:
            logger.info("IDE websocket: upstream refused %s: %s", url.split("?", 1)[0], e)
            await websocket.close(code=1011)
            return

        async def client_to_upstream():
            while True:
                m = await websocket.receive()
                if m["type"] == "websocket.disconnect":
                    return
                if m.get("bytes") is not None:
                    await up.send(m["bytes"])
                elif m.get("text") is not None:
                    await up.send(m["text"])

        async def upstream_to_client():
            try:
                async for m in up:
                    if isinstance(m, bytes):
                        await websocket.send_bytes(m)
                    else:
                        await websocket.send_text(m)
            except ConnectionClosed:
                pass

        try:
            await websocket.accept(subprotocol=up.subprotocol)
            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            for t in done:
                if t.exception() and not isinstance(t.exception(), ConnectionClosed):
                    logger.debug("IDE websocket pump ended: %r", t.exception())
        finally:
            code = up.close_code if up.close_code not in (None, 1005, 1006) else 1000
            try:
                await up.close()
            except Exception:
                pass
            try:
                await websocket.close(code=code)
            except Exception:
                pass

    return router
