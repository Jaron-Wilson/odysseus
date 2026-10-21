"""The /mods/<id>/ reverse proxy (src/mods/proxy.py): auth, signed identity,
header stripping, the frame-token redirect, MJPEG and SSE streamed as they
arrive (through the gzip middleware the app uses), and WebSocket."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from src.mods import routes, signing
from tests.mods_helpers import UvicornThread, add_mod, mark_running, mods_dir  # noqa: F401

BOUNDARY = b"frame"
FRAME = b"\xff\xd8" + b"J" * 3000 + b"\xff\xd9"


class Upstream:
    """A stand-in mod server: echoes headers, streams MJPEG and SSE slowly."""

    def __init__(self):
        outer = self
        self.seen = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append((self.path, dict(self.headers)))
                if self.path == "/health":
                    return self._send(200, b"ok", "text/plain")
                if self.path.startswith("/echo"):
                    return self._send(200, json.dumps({"path": self.path, "headers": dict(self.headers)}).encode(),
                                      "application/json", extra=[("Set-Cookie", "modc=1; Path=/"),
                                                                 ("Set-Cookie", "odysseus_session=stolen; Path=/")])
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/echo?from=redirect")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/page":
                    return self._send(200, b"<html><body>" + b"x" * 5000 + b"</body></html>", "text/html")
                if self.path == "/mjpeg":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for i in range(3):
                        part = (b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                + str(len(FRAME)).encode() + b"\r\n\r\n" + FRAME + b"\r\n")
                        self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                        self.wfile.flush()
                        time.sleep(1.0)
                    self.wfile.write(b"0\r\n\r\n")
                    return
                if self.path == "/sse":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for i in range(2):
                        ev = f"data: tick {i}\n\n".encode()
                        self.wfile.write(f"{len(ev):x}\r\n".encode() + ev + b"\r\n")
                        self.wfile.flush()
                        time.sleep(1.0)
                    self.wfile.write(b"0\r\n\r\n")
                    return
                self._send(404, b"nope", "text/plain")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                self._send(200, json.dumps({"got": body.decode(), "user": self.headers.get("X-Odysseus-User")}).encode(),
                           "application/json")

            def _send(self, code, body, ctype, extra=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


class FakeAuth:
    users = {"alice": {}, "bob": {}}

    def is_admin(self, u):
        return u == "alice"

    def validate_token(self, t):
        return t in ("alice-session", "bob-session")

    def get_username_for_token(self, t):
        return t.split("-")[0]


class CookieAuth(BaseHTTPMiddleware):
    """What AuthMiddleware does for these paths: the cookie names the user;
    frame-token paths are exempt and carry no cookie."""

    async def dispatch(self, request: Request, call_next):
        tok = request.cookies.get("odysseus_session")
        if tok and FakeAuth().validate_token(tok):
            request.state.current_user = tok.split("-")[0]
        return await call_next(request)


def make_app():
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(CookieAuth)
    app.add_middleware(GZipMiddleware, minimum_size=100)
    return app


@pytest.fixture
def stack(mods_dir):
    up = Upstream()
    entry, token = add_mod()
    mark_running("test-mod", up.port)
    with UvicornThread(make_app()) as srv:
        yield srv, up, token
    up.srv.shutdown()


def _tokpath(user="alice"):
    return f"/mods/test-mod/~{signing.frame_token('test-mod', user)}"


def test_navigation_without_token_redirects_to_a_frame_url(stack):
    srv, up, _ = stack
    r = httpx.get(srv.url + "/mods/test-mod/page?x=1", cookies={"odysseus_session": "alice-session"},
                  headers={"Sec-Fetch-Dest": "iframe"}, follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("/mods/test-mod/~") and loc.endswith("/page?x=1")
    tok = loc.split("~", 1)[1].split("/", 1)[0]
    assert signing.check_frame_token(tok, "test-mod") == "alice"


def test_not_signed_in_and_bad_tokens_are_refused(stack):
    srv, up, _ = stack
    assert httpx.get(srv.url + "/mods/test-mod/echo").status_code == 401
    assert httpx.get(srv.url + "/mods/test-mod/~bogus.token/echo").status_code == 401
    other = signing.frame_token("other-mod", "alice")
    assert httpx.get(srv.url + f"/mods/test-mod/~{other}/echo").status_code == 401
    assert httpx.get(srv.url + "/mods/nope-mod/echo", cookies={"odysseus_session": "alice-session"}).status_code == 404


def test_admin_only_mod_refuses_other_users(stack):
    srv, up, _ = stack
    r = httpx.get(srv.url + _tokpath("bob") + "/echo")
    assert r.status_code == 403


def test_identity_is_signed_and_client_headers_are_stripped(stack):
    srv, up, token = stack
    r = httpx.get(srv.url + _tokpath() + "/echo?q=1", headers={
        "X-Odysseus-User": "mallory", "X-Odysseus-Sig": "forged", "X-Forwarded-For": "6.6.6.6",
        "Authorization": "Bearer ody_secret", "Cookie": "odysseus_session=alice-session; modc=1"})
    assert r.status_code == 200
    seen = {k.lower(): v for k, v in r.json()["headers"].items()}
    assert r.json()["path"] == "/echo?q=1"
    assert seen["x-odysseus-user"] == "alice" and seen["x-odysseus-mod"] == "test-mod"
    assert signing.verify_identity(token, {"X-Odysseus-User": seen["x-odysseus-user"],
                                           "X-Odysseus-Mod": seen["x-odysseus-mod"],
                                           "X-Odysseus-Ts": seen["x-odysseus-ts"],
                                           "X-Odysseus-Sig": seen["x-odysseus-sig"]}) == "alice"
    assert "authorization" not in seen
    assert "odysseus_session" not in seen.get("cookie", "") and "modc=1" in seen.get("cookie", "")
    assert seen["x-forwarded-for"] == "127.0.0.1"
    assert seen["x-forwarded-prefix"].startswith("/mods/test-mod/~")
    # The response: a sandbox CSP, the mod's cookie scoped to it, Odysseus's cookie dropped.
    assert "sandbox" in r.headers["content-security-policy"]
    cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith("modc=1") and "Path=/mods/test-mod/" in c for c in cookies)
    assert not any(c.startswith("odysseus_session") for c in cookies)


def test_redirects_stay_under_the_prefix(stack):
    srv, up, _ = stack
    base = _tokpath()
    r = httpx.get(srv.url + base + "/redirect", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == base + "/echo?from=redirect"


def test_post_bodies_pass_through(stack):
    srv, up, _ = stack
    r = httpx.post(srv.url + _tokpath() + "/anything", content=b"hello body")
    assert r.json() == {"got": "hello body", "user": "alice"}


def test_html_is_still_compressed(stack):
    srv, up, _ = stack
    r = httpx.get(srv.url + _tokpath() + "/page", headers={"Accept-Encoding": "gzip", "Sec-Fetch-Dest": "empty"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"


def _first_chunk_time(url, path):
    async def go():
        async with httpx.AsyncClient(timeout=10) as c:
            t0 = time.monotonic()
            async with c.stream("GET", url + path, headers={"Accept-Encoding": "gzip"}) as r:
                assert r.status_code == 200
                got = b""
                first_at = None
                async for chunk in r.aiter_raw():
                    got += chunk
                    if first_at is None and len(got) > 0:
                        first_at = time.monotonic() - t0
                total = time.monotonic() - t0
                return r.headers, got, first_at, total
    return asyncio.run(go())


def test_mjpeg_streams_frame_by_frame(stack):
    """The printer camera: the first frame arrives while the mod is still
    sending, not when the stream ends, and nothing gzips it."""
    srv, up, _ = stack
    headers, body, first_at, total = _first_chunk_time(srv.url, _tokpath() + "/mjpeg")
    assert headers["content-type"].startswith("multipart/x-mixed-replace")
    assert headers.get("content-encoding") in (None, "identity")
    assert body.count(b"--frame") == 3 and body.count(FRAME) == 3
    assert total >= 2.0 and first_at < 0.9, (first_at, total)


def test_sse_streams_event_by_event(stack):
    srv, up, _ = stack
    headers, body, first_at, total = _first_chunk_time(srv.url, _tokpath() + "/sse")
    assert headers["content-type"].startswith("text/event-stream")
    assert b"data: tick 0" in body and b"data: tick 1" in body
    assert first_at < 0.9 and total >= 1.0


def test_websocket_pass_through(mods_dir):
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect

    seen = {}

    async def echo(ws):
        seen["user"] = ws.request.headers.get("X-Odysseus-User")
        seen["path"] = ws.request.path
        async for m in ws:
            await ws.send("echo:" + m)

    async def go():
        async with serve(echo, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            add_mod()
            mark_running("test-mod", port)
            with UvicornThread(make_app()) as srv:
                url = "ws://127.0.0.1:%d%s/live?x=1" % (srv.port, _tokpath())
                async with connect(url, origin="null") as ws:
                    await ws.send("ping")
                    assert await asyncio.wait_for(ws.recv(), 5) == "echo:ping"
                # Cookie-authenticated from another site: refused.
                with pytest.raises(Exception):
                    async with connect("ws://127.0.0.1:%d/mods/test-mod/live" % srv.port,
                                       origin="https://evil.example",
                                       additional_headers={"Cookie": "odysseus_session=alice-session"}) as ws:
                        await ws.recv()
    asyncio.run(go())
    assert seen["user"] == "alice" and seen["path"] == "/live?x=1"
