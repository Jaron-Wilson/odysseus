"""The integrated IDE (routes/ide_routes.py, src/ide.py).

Asked for: "can we implement a vs code or an integrated ide into it, one for
modifying the odysseus dev and also for other projects too please."

Odysseus proxies a code-server at /ide/ so the editor runs same-origin and can
sit in a tab beside a chat. What matters:

  * /ide/<path> reaches the editor at /<path>: every method, the body, the
    query, headers and cookies, with the response streamed rather than
    buffered;
  * WebSocket frames (code-server's whole protocol) go both ways;
  * redirects and cookies from the editor stay under /ide/, and never touch
    Odysseus's own session cookie, which is not sent to the editor;
  * only admins get in, over HTTP and WebSocket (the editor is a shell);
  * the project list finds git repos under the configured folders, with the
    Odysseus checkout first, and "Open folder" refuses paths outside them;
  * the status check reports whether the editor answers, and its version.

The editor is a small fake Starlette app on a random port, served by uvicorn.
"""
import asyncio
import json
import os
import socket
import subprocess
import threading
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware
from src import ide


# ── A fake code-server ─────────────────────────────────────────────────────

SEEN = []


async def _echo(request: Request):
    body = await request.body()
    SEEN.append({"method": request.method, "path": request.url.path, "raw_path": request.scope["raw_path"].decode(),
                 "query": request.url.query, "headers": dict(request.headers), "body": body.decode()})
    return JSONResponse({"method": request.method, "path": request.url.path, "query": request.url.query,
                         "body": body.decode(), "cookie": request.headers.get("cookie", ""),
                         "xfh": request.headers.get("x-forwarded-host"),
                         "xfp": request.headers.get("x-forwarded-prefix")})


async def _stream(request: Request):
    gate = request.app.state.gate

    async def gen():
        yield b"first\n"
        # The second chunk waits until the test has read the first one, so a
        # proxy that buffers the whole body would hang here.
        while not gate.is_set():
            await asyncio.sleep(0.01)
        yield b"second\n"
    return StreamingResponse(gen(), media_type="text/plain")


async def _redirect_abs(request: Request):
    return RedirectResponse(f"http://{request.url.netloc}/login?to=%2F", status_code=302)


async def _redirect_root(request: Request):
    return RedirectResponse("/welcome", status_code=302)


async def _redirect_rel(request: Request):
    return RedirectResponse("./login?folder=/x", status_code=302)


async def _redirect_other(request: Request):
    return RedirectResponse("https://example.org/elsewhere", status_code=302)


async def _cookies(request: Request):
    r = PlainTextResponse("ok")
    r.raw_headers.append((b"set-cookie", b"code-server-session=abc; Path=/; HttpOnly; SameSite=Lax"))
    r.raw_headers.append((b"set-cookie", b"other=1; Domain=127.0.0.1"))
    r.raw_headers.append((b"set-cookie", b"odysseus_session=evil; Path=/"))
    return r


async def _csp(request: Request):
    return Response("<html></html>", media_type="text/html",
                    headers={"Content-Security-Policy": "default-src 'self' 'unsafe-eval'"})


async def _healthz(request: Request):
    return JSONResponse({"status": "alive", "lastHeartbeat": 1})


async def _login(request: Request):
    return Response('<meta id="coder-options" data-settings="{&quot;base&quot;:&quot;.&quot;,'
                    '&quot;codeServerVersion&quot;:&quot;4.102.3&quot;}" />', media_type="text/html")


async def _ws(websocket):
    await websocket.accept(subprotocol=(websocket.scope.get("subprotocols") or [None])[0])
    await websocket.send_text("hello " + websocket.url.path + "?" + websocket.url.query
                              + " xfh=" + (websocket.headers.get("x-forwarded-host") or ""))
    while True:
        m = await websocket.receive()
        if m["type"] == "websocket.disconnect":
            return
        if m.get("bytes") is not None:
            await websocket.send_bytes(b"echo:" + m["bytes"])
        else:
            if m["text"] == "bye":
                await websocket.close(code=4001)
                return
            await websocket.send_text("echo:" + m["text"])


def _fake_app():
    app = Starlette(routes=[
        Route("/stream", _stream),
        Route("/redir-abs", _redirect_abs),
        Route("/redir-root", _redirect_root),
        Route("/redir-rel", _redirect_rel),
        Route("/redir-other", _redirect_other),
        Route("/cookies", _cookies),
        Route("/csp", _csp),
        Route("/healthz", _healthz),
        Route("/login", _login),
        WebSocketRoute("/{rest:path}", _ws),
        Route("/{rest:path}", _echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    ])
    app.state.gate = threading.Event()
    return app


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Served:
    """An ASGI app on a random loopback port, served by uvicorn in a thread."""

    def __init__(self, app):
        import uvicorn
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        ws_impl = "auto" if _has_websockets() else "none"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", ws=ws_impl, lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(250):
            if self.server.started:
                break
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture(scope="module")
def upstream():
    app = _fake_app()
    with _Served(app) as srv:
        yield {"url": srv.url, "app": app}


def _has_websockets():
    try:
        import websockets  # noqa: F401
        return True
    except ImportError:
        return False


# ── Odysseus side ──────────────────────────────────────────────────────────

class _FakeAuth:
    is_configured = True
    users = {"root": {}, "bob": {}}

    def validate_token(self, t):
        return t in ("tok-root", "tok-bob")

    def get_username_for_token(self, t):
        return {"tok-root": "root", "tok-bob": "bob"}.get(t)

    def is_admin(self, u):
        return u == "root"


def _make_app(auth=False):
    from routes.ide_routes import setup_ide_routes
    app = FastAPI()
    app.include_router(setup_ide_routes())
    app.add_middleware(SecurityHeadersMiddleware)
    if auth:
        app.state.auth_manager = _FakeAuth()

        @app.middleware("http")
        async def _session(request, call_next):        # what AuthMiddleware does
            tok = request.cookies.get("odysseus_session")
            if not app.state.auth_manager.validate_token(tok):
                return JSONResponse({"error": "Not authenticated"}, status_code=401)
            request.state.current_user = app.state.auth_manager.get_username_for_token(tok)
            return await call_next(request)
    return app


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    return tmp_path


@pytest.fixture
def client(upstream, data_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    ide.save_config({"upstream": upstream["url"]})
    with TestClient(_make_app(), base_url="http://odysseus.test") as c:
        yield c


# ── HTTP ───────────────────────────────────────────────────────────────────

def test_http_strips_the_prefix_and_forwards_method_query_body(client):
    r = client.post("/ide/some/deep%20path/file.js?folder=/home/x&a=1", content=b"hello body",
                    headers={"content-type": "text/plain"})
    assert r.status_code == 200
    d = r.json()
    assert d["method"] == "POST"
    assert d["path"] == "/some/deep path/file.js"
    assert d["query"] == "folder=/home/x&a=1"
    assert d["body"] == "hello body"
    assert SEEN[-1]["raw_path"] == "/some/deep%20path/file.js"    # encoding kept
    for m in ("PUT", "PATCH", "DELETE"):
        assert client.request(m, "/ide/x").json()["method"] == m


def test_http_root_and_bare_prefix(client):
    assert client.get("/ide/?folder=/a").json()["path"] == "/"
    r = client.get("/ide?folder=/a", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ide/?folder=/a"


def test_odysseus_cookies_and_credentials_stay_home(client):
    client.cookies.set("odysseus_session", "secret-session")
    client.cookies.set("code-server-session", "cs")
    r = client.get("/ide/echo", headers={"authorization": "Bearer ody_x", "connection": "keep-alive, x-private",
                                         "x-private": "1", "x-odysseus-internal-token": "t"})
    d = r.json()
    assert "code-server-session=cs" in d["cookie"]
    assert "odysseus_session" not in d["cookie"]
    h = SEEN[-1]["headers"]
    assert "authorization" not in h and "x-private" not in h and "x-odysseus-internal-token" not in h
    assert d["xfh"] == "odysseus.test" and d["xfp"] == "/ide"
    client.cookies.clear()


def test_response_streams(client, upstream):
    # A real server in front: the test client collects whole bodies.
    gate = upstream["app"].state.gate
    gate.clear()
    with _Served(_make_app()) as ody, httpx.Client(timeout=10) as c:
        with c.stream("GET", ody.url + "/ide/stream") as r:
            it = r.iter_raw()
            first = next(it)
            assert first.startswith(b"first")     # arrived before the upstream finished
            gate.set()
            rest = b"".join(it)
    assert (first + rest) == b"first\nsecond\n"


def test_redirects_are_rewritten_under_the_prefix(client):
    loc = lambda p: client.get(p, follow_redirects=False).headers["location"]
    assert loc("/ide/redir-abs") == "/ide/login?to=%2F"
    assert loc("/ide/redir-root") == "/ide/welcome"
    assert loc("/ide/redir-rel") == "./login?folder=/x"            # already resolves under /ide/
    assert loc("/ide/redir-other") == "https://example.org/elsewhere"


def test_upstream_cookies_are_scoped_to_ide_and_cannot_clobber_the_session(client):
    r = client.get("/ide/cookies")
    cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith("code-server-session=abc") and "Path=/ide/" in c and "HttpOnly" in c for c in cookies)
    other = next(c for c in cookies if c.startswith("other="))
    assert "Domain" not in other and "Path=/ide/" in other
    assert not any(c.startswith("odysseus_session") for c in cookies)


def test_editor_pages_keep_their_own_csp_and_can_be_framed_by_the_app(client):
    r = client.get("/ide/csp")
    assert r.headers["content-security-policy"] == "default-src 'self' 'unsafe-eval'"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    r = client.get("/ide/plain")
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]


def test_unreachable_upstream_is_a_502_page(data_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    ide.save_config({"upstream": f"http://127.0.0.1:{_free_port()}"})
    with TestClient(_make_app()) as c:
        r = c.get("/ide/")
    assert r.status_code == 502 and "isn't reachable" in r.text


# ── WebSocket ──────────────────────────────────────────────────────────────

# The test client's WebSocket requests always say Host: testserver.
WS_ORIGIN = "http://testserver"


def test_websocket_frames_both_ways(client):
    pytest.importorskip("websockets")
    with client.websocket_connect("/ide/stable-abc/?reconnectionToken=r1&skipWebSocketFrames=false",
                                  headers={"origin": WS_ORIGIN}, subprotocols=["vscode"]) as ws:
        hello = ws.receive_text()
        assert hello.startswith("hello /stable-abc/?reconnectionToken=r1&skipWebSocketFrames=false")
        assert "xfh=testserver" in hello
        ws.send_text("ping")
        assert ws.receive_text() == "echo:ping"
        ws.send_bytes(b"\x00\x01binary")
        assert ws.receive_bytes() == b"echo:\x00\x01binary"
        ws.send_text("bye")                       # the editor closes; so do we
        with pytest.raises(Exception):
            ws.receive_text()


def test_websocket_from_another_site_is_refused(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ide/x", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()


# ── Admin only ─────────────────────────────────────────────────────────────

def test_non_admins_are_refused(upstream, data_dir, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setenv("AUTH_ENABLED", "true")
    ide.save_config({"upstream": upstream["url"]})
    with TestClient(_make_app(auth=True), base_url="http://odysseus.test") as c:
        c.cookies.set("odysseus_session", "tok-bob")
        for path in ("/ide/", "/ide/x.js", "/api/ide/projects", "/api/ide/status", "/api/ide/config",
                     "/api/ide/resolve?path=/tmp"):
            assert c.get(path).status_code == 403, path
        assert c.put("/api/ide/config", json={"upstream": "http://x"}).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ide/x", headers={"origin": WS_ORIGIN}) as ws:
                ws.receive_text()
        c.cookies.clear()
        with pytest.raises(WebSocketDisconnect):       # no session at all
            with c.websocket_connect("/ide/x", headers={"origin": WS_ORIGIN}) as ws:
                ws.receive_text()
        c.cookies.set("odysseus_session", "tok-root")
        assert c.get("/ide/hi").json()["path"] == "/hi"
        if _has_websockets():
            with c.websocket_connect("/ide/x", headers={"origin": WS_ORIGIN}) as ws:
                assert ws.receive_text().startswith("hello /x")


# ── Projects and folders ───────────────────────────────────────────────────

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repos(tmp_path):
    root = tmp_path / "home"
    for name, dirty in (("alpha", False), ("beta", True)):
        d = root / name
        d.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=d)
        (d / "f.txt").write_text("x")
        _git("add", ".", cwd=d)
        _git("commit", "-q", "-m", "init", cwd=d)
        if dirty:
            (d / "f.txt").write_text("changed")
    (root / "not-a-repo").mkdir()
    (root / ".hidden-repo" / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    return root


def test_projects_lists_repos_with_branch_and_dirty_and_pins_odysseus(client, repos):
    assert client.put("/api/ide/config", json={"roots": [str(repos)]}).status_code == 200
    d = client.get("/api/ide/projects").json()
    names = [p["name"] for p in d["projects"]]
    ody = ide.odysseus_checkout()
    if ody:
        assert names[0] == "Odysseus dev" and d["projects"][0]["pinned"]
    by = {p["name"]: p for p in d["projects"]}
    assert by["alpha"]["branch"] == "main" and by["alpha"]["dirty"] is False
    assert by["beta"]["branch"] == "main" and by["beta"]["dirty"] is True
    assert "not-a-repo" not in by and ".hidden-repo" not in by
    assert d["roots"] == [str(repos)]


def test_open_folder_accepts_folders_under_the_roots_only(client, repos):
    client.put("/api/ide/config", json={"roots": [str(repos)]})
    ok = client.get("/api/ide/resolve", params={"path": str(repos / "alpha")})
    assert ok.status_code == 200 and ok.json()["path"] == os.path.realpath(repos / "alpha")
    assert client.get("/api/ide/resolve", params={"path": str(repos / "not-a-repo")}).status_code == 200
    for bad in (str(repos / ".." / "outside"), str(repos / "alpha" / ".." / ".." / "outside"),
                str(repos / "escape"),                      # a symlink out of the root
                "relative/path", "/etc", str(repos / "missing"), str(repos / "alpha" / "f.txt"), ""):
        r = client.get("/api/ide/resolve", params={"path": bad})
        assert r.status_code == 400, bad


def test_config_validation(client):
    assert client.put("/api/ide/config", json={"upstream": "file:///etc/passwd"}).status_code == 400
    assert client.put("/api/ide/config", json={"roots": ["relative"]}).status_code == 400
    assert client.put("/api/ide/config", json={"roots": ["/definitely/not/here"]}).status_code == 400
    r = client.put("/api/ide/config", json={"roots": []})
    assert r.json()["roots"] == ide.default_roots()


def test_parse_status():
    assert ide.parse_status("## dev...origin/dev [ahead 1]\n") == {"branch": "dev", "dirty": False}
    assert ide.parse_status("## No commits yet on main\n?? a\n") == {"branch": "main", "dirty": True}
    assert ide.parse_status("## HEAD (no branch)\n M x\n") == {"branch": "detached", "dirty": True}


# ── Status ─────────────────────────────────────────────────────────────────

def test_status_reports_reachable_and_version(client, upstream):
    d = client.get("/api/ide/status").json()
    assert d == {"upstream": upstream["url"], "reachable": True, "version": "4.102.3",
                 "health": "alive", "error": None}


def test_status_when_the_editor_is_down(data_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    ide.save_config({"upstream": f"http://127.0.0.1:{_free_port()}"})
    with TestClient(_make_app()) as c:
        d = c.get("/api/ide/status").json()
    assert d["reachable"] is False and d["version"] is None and d["error"]
