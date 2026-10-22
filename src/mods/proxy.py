"""The reverse proxy at /mods/<id>/: HTTP (streamed both ways, so MJPEG and
SSE pass through as they arrive) and WebSocket.

The core authenticates; the mod never sees Odysseus's cookies or tokens.
Every request it gets has the client's own X-Odysseus-* and X-Forwarded-*
headers removed and the core's signed identity headers added
(src/mods/signing.py). Responses get a CSP `sandbox`, so a mod page runs
with an opaque origin: its scripts cannot read Odysseus's pages, cookies or
storage, and its requests to /api carry no session. That is also why pages
load under /mods/<id>/~<frame token>/: the token, not the cookie, is what
authenticates the page's own requests.
"""
from __future__ import annotations

import asyncio
import html
import logging
import posixpath
import re
from typing import List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import Request, WebSocket
from starlette.background import BackgroundTask
from starlette.responses import RedirectResponse, Response, StreamingResponse

from src.ide import HOP_BY_HOP, _connection_tokens, filter_cookie_header, public_host, public_proto
from src.mods import access, registry, runtime, signing

logger = logging.getLogger(__name__)

SANDBOX_CSP = ("sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox "
               "allow-downloads allow-modals")
_BUFFER_BODY_MAX = 16 * 1024 * 1024
_DROP_REQUEST = HOP_BY_HOP | {
    "host", "cookie", "authorization", "x-api-key", "x-auth-token", "forwarded", "x-forwarded-for",
    "x-forwarded-host", "x-forwarded-proto", "x-forwarded-prefix", "x-real-ip", "content-length",
}
_STREAM_TYPES = ("multipart/", "text/event-stream")

_client: Optional[httpx.AsyncClient] = None
_client_loop = None


def _http() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client_loop = loop
        # No read timeout: a camera stream or an SSE feed stays open for hours.
        _client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0),
                                    follow_redirects=False, trust_env=False,
                                    limits=httpx.Limits(max_connections=200, max_keepalive_connections=40))
    return _client


def page(status: int, title: str, text: str) -> Response:
    body = ("<!doctype html><meta charset=utf-8><title>" + html.escape(title) + "</title>"
            "<body style=\"font:14px system-ui,sans-serif;padding:24px;max-width:640px;color:#ccc;background:#111\">"
            f"<h2 style=\"font-size:16px\">{html.escape(title)}</h2><p>{html.escape(text)}</p></body>")
    return Response(body, status_code=status, media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": SANDBOX_CSP, "Cache-Control": "no-store"})


def upstream_headers(headers, client_host: Optional[str], scheme: str, mod_id: str, user: str,
                     prefix: str) -> List[Tuple[str, str]]:
    drop = _DROP_REQUEST | _connection_tokens(headers)
    out = [(k, v) for k, v in headers.items()
           if k.lower() not in drop and not k.lower().startswith("x-odysseus")]
    cookie = filter_cookie_header(headers.get("cookie") or "")
    if cookie:
        out.append(("cookie", cookie))
    host = public_host(headers)
    if host:
        out.append(("x-forwarded-host", host))
    out.append(("x-forwarded-proto", public_proto(headers, scheme)))
    out.append(("x-forwarded-prefix", prefix))
    if client_host:
        out.append(("x-forwarded-for", client_host))
    out.extend(signing.sign_identity(registry.token_of(mod_id), mod_id, user).items())
    return out


def _rewrite_location(loc: str, port: int, prefix: str) -> str:
    parts = urlsplit(loc)
    if parts.scheme and parts.netloc:
        if parts.hostname not in ("127.0.0.1", "localhost") or parts.port != port:
            return loc
        loc = parts.path or "/"
        if parts.query:
            loc += "?" + parts.query
    if loc.startswith("/") and not loc.startswith("//") and not loc.startswith(prefix.split("/~")[0] + "/"):
        return prefix + loc
    return loc


def _rewrite_set_cookie(value: str, cookie_path: str) -> Optional[str]:
    parts = [p.strip() for p in value.split(";")]
    if not parts or "=" not in parts[0]:
        return None
    if parts[0].split("=", 1)[0].strip().lower().startswith("odysseus"):
        return None
    attrs = [a for a in parts[1:] if a and a.split("=", 1)[0].strip().lower() not in ("domain", "path")]
    return "; ".join([parts[0], f"Path={cookie_path}", *attrs])


def response_headers(r: httpx.Response, port: int, prefix: str, mod_id: str) -> List[Tuple[str, str]]:
    drop = HOP_BY_HOP | _connection_tokens(r.headers) | {"x-frame-options"}
    out: List[Tuple[str, str]] = []
    for k, v in r.headers.multi_items():
        lk = k.lower()
        if lk in drop:
            continue
        if lk == "location":
            v = _rewrite_location(v, port, prefix)
        elif lk == "set-cookie":
            v = _rewrite_set_cookie(v, f"/mods/{mod_id}/")
            if v is None:
                continue
        out.append((k, v))
    ctype = (r.headers.get("content-type") or "").lower()
    if ctype.startswith(_STREAM_TYPES):
        # Streamed media: no buffering anywhere on the way. The gzip
        # middleware compresses anything without a Content-Encoding, and a
        # compressor holds frames back, so mark it as already encoded.
        if "content-encoding" not in r.headers:
            out.append(("Content-Encoding", "identity"))
        if "cache-control" not in r.headers:
            out.append(("Cache-Control", "no-cache"))
        out.append(("X-Accel-Buffering", "no"))
    out.append(("Content-Security-Policy", SANDBOX_CSP))
    out.append(("X-Frame-Options", "SAMEORIGIN"))
    out.append(("X-Content-Type-Options", "nosniff"))
    return out


def _is_navigation(request: Request) -> bool:
    dest = (request.headers.get("sec-fetch-dest") or "").lower()
    if dest:
        return dest in ("document", "iframe", "frame")
    return request.method == "GET" and "text/html" in (request.headers.get("accept") or "")


def _resolve(mod_id: str, rest: str, cookie_user: Optional[str]):
    """(entry, user, frame token, upstream path) or a Response to send."""
    entry = registry.get(mod_id)
    if not entry or not entry.get("enabled"):
        return page(404, "No such mod", f"There is no enabled mod called {mod_id}.")
    tok, upath = signing.split_frame_path(rest)
    user: Optional[str]
    if tok is not None:
        user = signing.check_frame_token(tok, mod_id)
        if user is None:
            return page(401, "This page has expired", "Reload the tab to open the mod again.")
    else:
        user = cookie_user
    if not access.may_use(entry, user):
        return page(403, "Not allowed", "This mod is only available to admins.")
    if core_only(upath):
        # A provider's capability endpoint takes calls the core brokered and
        # checked (src/mods/capabilities.py), never from a browser.
        return page(404, "Not found", "That path of the mod is only for Odysseus itself.")
    return entry, user, tok, upath


def core_only(upath: str) -> bool:
    """Whether a path under the mod is its capability endpoint (/mod/cap/...),
    however it is spelled (doubled slashes, percent-encoding)."""
    from src.mods.capabilities import PROVIDER_PATH
    p = posixpath.normpath(re.sub(r"/+", "/", "/" + unquote(unquote(upath or "")).replace("\\", "/")))
    return p == PROVIDER_PATH or p.startswith(PROVIDER_PATH + "/")


def cookie_user(request) -> Optional[str]:
    """The signed-in user from AuthMiddleware (HTTP) or the session cookie (WS)."""
    if getattr(request.state, "api_token", False):
        return None
    u = getattr(request.state, "current_user", None)
    if u and u not in ("api", "internal-tool"):
        return u
    if access.auth_disabled():
        return signing.LOCAL_USER
    return None


def _cors_headers(request: Request, preflight: bool) -> List[Tuple[str, str]]:
    out = [("Access-Control-Allow-Origin", "null"), ("Vary", "Origin")]
    if preflight:
        asked = request.headers.get("access-control-request-headers") or ""
        allowed = ", ".join(h.strip() for h in asked.split(",") if h.strip() and all(
            c.isalnum() or c in "-_" for c in h.strip()))
        out += [("Access-Control-Allow-Methods", ", ".join(m for m in _CORS_METHODS)),
                ("Access-Control-Max-Age", "600")]
        if allowed:
            out.append(("Access-Control-Allow-Headers", allowed))
    return out


_CORS_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


async def handle_http(request: Request, mod_id: str, rest: str) -> Response:
    """A mod page has an opaque origin, so its own fetch() calls are
    cross-origin (Origin: null). Under a frame token they may read the
    answer: the token in the URL is the credential, no cookie is involved."""
    tok, _ = signing.split_frame_path(rest)
    cors = tok is not None and request.headers.get("origin") == "null"
    if cors and request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
        if signing.check_frame_token(tok, mod_id) is None:
            return Response(status_code=401)
        return Response(status_code=204, headers=dict(_cors_headers(request, True)))
    resp = await _handle_http(request, mod_id, rest)
    if cors:
        resp.raw_headers = [(k, v) for k, v in resp.raw_headers if not k.startswith(b"access-control-")] + [(k.lower().encode("latin-1"), v.encode("latin-1"))
                                                 for k, v in _cors_headers(request, False)]
    return resp


async def _handle_http(request: Request, mod_id: str, rest: str) -> Response:
    res = _resolve(mod_id, rest, cookie_user(request))
    if isinstance(res, Response):
        return res
    entry, user, tok, upath = res
    if user is None:
        return page(401, "Not signed in", "Sign in to Odysseus first.")
    if tok is None and _is_navigation(request):
        # Pages always load under a frame token (see the module doc).
        q = request.url.query
        target = f"/mods/{mod_id}/~{signing.frame_token(mod_id, user)}{upath}" + (f"?{q}" if q else "")
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})
    port = await runtime.ensure_running(mod_id)
    if not port:
        st = runtime.state(mod_id)
        return page(503, f"{entry.get('name')} is not running",
                    f"Odysseus could not start it ({st.last_error or st.status}). See Settings > Mods for its log.")
    prefix = f"/mods/{mod_id}" + (f"/~{tok}" if tok else "")
    raw_s = (request.scope.get("raw_path") or request.url.path.encode()).decode("latin-1")
    # The path after the prefix, still percent-encoded as the browser sent it.
    base = f"/mods/{mod_id}" + (f"/~{tok}" if tok else "")
    tail = raw_s[len(base):] if raw_s.startswith(base) else "/" + quote(upath.lstrip("/"))
    url = f"http://127.0.0.1:{port}{tail or '/'}"
    if request.url.query:
        url += "?" + request.url.query
    headers = upstream_headers(request.headers, request.client.host if request.client else None,
                               request.url.scheme, mod_id, user, prefix)
    body = None
    if request.method not in ("GET", "HEAD"):
        try:
            length = int(request.headers.get("content-length") or -1)
        except ValueError:
            length = -1
        body = await request.body() if 0 <= length <= _BUFFER_BODY_MAX else request.stream()
    client = _http()
    req = client.build_request(request.method, url, headers=headers, content=body)
    try:
        r = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        return page(502, f"{entry.get('name')} did not answer", str(e) or type(e).__name__)
    runtime.touch(mod_id)
    resp = StreamingResponse(r.aiter_raw(), status_code=r.status_code, background=BackgroundTask(r.aclose))
    resp.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1"))
                        for k, v in response_headers(r, port, prefix, mod_id)]
    return resp


def _ws_cookie_user(ws: WebSocket) -> Optional[str]:
    if access.auth_disabled():
        return signing.LOCAL_USER
    mgr = getattr(ws.app.state, "auth_manager", None)
    if mgr is None:
        return None
    try:
        from routes.auth_routes import SESSION_COOKIE
        token = ws.cookies.get(SESSION_COOKIE)
        if token and mgr.validate_token(token):
            return mgr.get_username_for_token(token)
    except Exception:
        logger.debug("mod websocket: session lookup failed", exc_info=True)
    return None


async def handle_ws(websocket: WebSocket, mod_id: str, rest: str) -> None:
    res = _resolve(mod_id, rest, _ws_cookie_user(websocket))
    if isinstance(res, Response):
        await websocket.close(code=1008)
        return
    entry, user, tok, upath = res
    origin = websocket.headers.get("origin")
    if user is None:
        await websocket.close(code=1008)
        return
    if tok is None:
        # Cookie-authenticated: block cross-site WebSocket hijacking. (A mod
        # page has an opaque origin and sends "null"; it uses its frame token.)
        host = public_host(websocket.headers).lower()
        if origin and (not host or urlsplit(origin).netloc.lower() != host):
            await websocket.close(code=1008)
            return
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        from websockets.typing import Subprotocol
    except ImportError:
        await websocket.close(code=1011)
        return
    port = await runtime.ensure_running(mod_id)
    if not port:
        await websocket.close(code=1013)
        return
    prefix = f"/mods/{mod_id}" + (f"/~{tok}" if tok else "")
    url = f"ws://127.0.0.1:{port}{upath}"
    if websocket.url.query:
        url += "?" + websocket.url.query
    headers = [(k, v) for k, v in upstream_headers(
        websocket.headers, websocket.client.host if websocket.client else None, websocket.url.scheme,
        mod_id, user, prefix) if not k.lower().startswith("sec-websocket-") and k.lower() != "origin"]
    protos = [p.strip() for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
    try:
        up = await connect(url, additional_headers=headers, user_agent_header=None,
                           subprotocols=[Subprotocol(p) for p in protos] or None,
                           max_size=None, ping_interval=None, open_timeout=15)
    except Exception as e:
        runtime.log_line(mod_id, f"[odysseus] websocket {upath} refused: {type(e).__name__}")
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
                runtime.touch(mod_id)
                if isinstance(m, bytes):
                    await websocket.send_bytes(m)
                else:
                    await websocket.send_text(m)
        except ConnectionClosed:
            pass

    try:
        await websocket.accept(subprotocol=up.subprotocol)
        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
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


def frame_base(mod_id: str, user: str) -> str:
    return f"/mods/{mod_id}/~{signing.frame_token(mod_id, user)}"

