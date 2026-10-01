"""The Terminal (src/terminal.py, routes/terminal_routes.py): a live shell.

Asked for 2026-09-30: "can i also get a command line in mine? ... a live
command line so that i can ssh and do stuff myself please."

What matters to the user, checked against a real PTY and a real shell:

  * what is typed runs, and its output comes back;
  * resizing the window resizes the terminal (`stty size` follows);
  * a reload or a dropped connection doesn't lose the shell: reattaching
    replays the recent output and the shell still has its state;
  * closing a terminal kills what runs in it;
  * the per-user session cap holds;
  * only an admin can open one, and only from this server's own page;
  * a terminal nobody reattaches to is closed after the grace period.
"""
import concurrent.futures
import contextlib
import json
import os
import re
import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from routes import terminal_routes
from src import terminal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIGIN = {"origin": "http://testserver"}

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a POSIX PTY")


class _Auth:
    """Stand-in AuthManager: token -> (user, is_admin)."""
    is_configured = True

    def __init__(self, users):
        self.users = users

    def validate_token(self, token):
        return token in self.users

    def get_username_for_token(self, token):
        return self.users[token][0] if token in self.users else None

    def is_admin(self, user):
        return any(u == user and a for u, a in self.users.values())


def _app(auth=None):
    app = FastAPI()
    app.state.auth_manager = auth
    app.include_router(terminal_routes.setup_terminal_routes())
    return app


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    # A plain bash, started in a throwaway home, so the test doesn't depend on
    # (or run) the real user's profile.
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(terminal, "home_dir", lambda: str(tmp_path))
    yield
    terminal.manager.close_all()


_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)


@contextlib.contextmanager
def _connect(client, url, **kw):
    """client.websocket_connect, but leaving it the way a browser does: close,
    then let the server finish with it. (TestClient cancels the app the
    moment it sends the close, which races the route's own cleanup.)"""
    with client.websocket_connect(url, **kw) as ws:
        try:
            yield ws
        finally:
            try:
                ws.close(1000)
            except Exception:
                pass
            time.sleep(0.15)


def _recv(ws, timeout=10):
    return _pool.submit(ws.receive).result(timeout=timeout)


def _hello(ws):
    m = _recv(ws)
    assert "text" in m, m
    msg = json.loads(m["text"])
    assert msg["type"] == "hello", msg
    return msg


def _read_until(ws, pattern, timeout=10):
    """Output until `pattern` (a regex) shows up; returns all of it."""
    out = b""
    end = time.time() + timeout
    while time.time() < end:
        m = _recv(ws, timeout=max(0.1, end - time.time()))
        if m["type"] == "websocket.close":
            break
        if m.get("bytes"):
            out += m["bytes"]
            if re.search(pattern, out.decode("utf-8", "replace")):
                return out.decode("utf-8", "replace")
        elif m.get("text"):
            out += b""       # control messages are checked by callers
    raise AssertionError(f"never saw {pattern!r} in {out[-2000:]!r}")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except OSError:
        return False


def _wait_dead(pid, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_what_is_typed_runs_and_the_output_comes_back():
    with TestClient(_app()) as c, _connect(c, "/api/terminal/ws?rows=24&cols=80", headers=ORIGIN) as ws:
        hello = _hello(ws)
        assert hello["replay"] is False and hello["id"] and hello["title"] == "Shell 1"
        ws.send_bytes(b"echo hello-$((1+1))\n")
        _read_until(ws, r"hello-2")
        ws.send_bytes(b"echo $TERM\n")
        _read_until(ws, r"xterm-256color")


def test_resizing_changes_stty_size():
    with TestClient(_app()) as c, _connect(c, "/api/terminal/ws?rows=24&cols=80", headers=ORIGIN) as ws:
        _hello(ws)
        ws.send_bytes(b"stty size\n")
        _read_until(ws, r"24 80")
        ws.send_text(json.dumps({"type": "resize", "rows": 40, "cols": 132}))
        ws.send_bytes(b"stty size\n")
        _read_until(ws, r"40 132")


def test_reattaching_replays_the_scrollback_and_the_shell_kept_its_state():
    with TestClient(_app()) as c:
        with _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
            sid = _hello(ws)["id"]
            ws.send_bytes(b"export X=kept-$((20+22)); echo marker-one\n")
            _read_until(ws, r"marker-one\r?\n")
        # The socket is gone (a reload), the session is only detached.
        listed = c.get("/api/terminal/sessions").json()["sessions"]
        assert [s["id"] for s in listed] == [sid]
        with _connect(c, f"/api/terminal/ws?id={sid}", headers=ORIGIN) as ws:
            hello = _hello(ws)
            assert hello["replay"] is True and hello["id"] == sid
            _read_until(ws, r"marker-one")                  # the replay
            ws.send_bytes(b"echo got-$X\n")
            _read_until(ws, r"got-kept-42")


def test_a_second_tab_takes_the_session_over():
    with TestClient(_app()) as c:
        with _connect(c, "/api/terminal/ws", headers=ORIGIN) as first:
            sid = _hello(first)["id"]
            with _connect(c, f"/api/terminal/ws?id={sid}", headers=ORIGIN) as second:
                _hello(second)
                msgs = []
                while True:
                    m = _recv(first)
                    msgs.append(m)
                    if m["type"] == "websocket.close":
                        break
                assert any(json.loads(m["text"]).get("type") == "detached" for m in msgs if m.get("text"))
                assert msgs[-1]["code"] == terminal_routes.CLOSE_TAKEN_OVER
                second.send_bytes(b"echo still-here\n")
                _read_until(second, r"still-here")


def test_closing_kills_the_shell_and_its_jobs():
    with TestClient(_app()) as c:
        with _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
            hello = _hello(ws)
            sid = hello["id"]
            ws.send_bytes(b"sleep 300 & echo JOB=$!\n")
            job = int(re.search(r"JOB=(\d+)", _read_until(ws, r"JOB=\d+")).group(1))
            shell = terminal.manager.get("", sid).pid
            assert _alive(job) and _alive(shell)
            assert c.delete(f"/api/terminal/sessions/{sid}").json() == {"ok": True}
            # The browser hears that it ended.
            end = time.time() + 10
            saw_exit = False
            while time.time() < end:
                m = _recv(ws)
                if m.get("text") and json.loads(m["text"])["type"] == "exit":
                    saw_exit = True
                if m["type"] == "websocket.close":
                    break
            assert saw_exit
        assert _wait_dead(shell) and _wait_dead(job)
        assert c.get("/api/terminal/sessions").json()["sessions"] == []
        assert c.delete(f"/api/terminal/sessions/{sid}").status_code == 404
        # Reattaching to it says it is gone rather than starting a new one.
        with _connect(c, f"/api/terminal/ws?id={sid}", headers=ORIGIN) as ws:
            assert json.loads(_recv(ws)["text"])["type"] == "gone"


def test_exit_in_the_shell_ends_the_session():
    with TestClient(_app()) as c, _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
        _hello(ws)
        ws.send_bytes(b"exit 3\n")
        end = time.time() + 10
        while time.time() < end:
            m = _recv(ws)
            if m.get("text"):
                msg = json.loads(m["text"])
                if msg["type"] == "exit":
                    assert msg["code"] == 3
                    break
        else:
            raise AssertionError("no exit message")
        assert c.get("/api/terminal/sessions").json()["sessions"] == []


def test_the_session_cap_holds(monkeypatch):
    monkeypatch.setattr(terminal, "MAX_SESSIONS", 2)
    with TestClient(_app()) as c:
        for _ in range(2):
            with _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
                _hello(ws)
        with _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
            m = json.loads(_recv(ws)["text"])
            assert m["type"] == "error" and "Close one" in m["message"]
            close = _recv(ws)
            assert close["type"] == "websocket.close" and close["code"] == terminal_routes.CLOSE_TOO_MANY
        titles = sorted(s["title"] for s in c.get("/api/terminal/sessions").json()["sessions"])
        assert titles == ["Shell 1", "Shell 2"]


def test_the_start_folder_has_to_be_inside_home(tmp_path):
    (tmp_path / "proj").mkdir()
    with TestClient(_app()) as c:
        with _connect(c, "/api/terminal/ws?cwd=" + str(tmp_path / "proj"), headers=ORIGIN) as ws:
            _hello(ws)
            ws.send_bytes(b"pwd\n")
            _read_until(ws, re.escape(str(tmp_path / "proj")))
        with _connect(c, "/api/terminal/ws?cwd=/etc", headers=ORIGIN) as ws:
            m = json.loads(_recv(ws)["text"])
            assert m["type"] == "error" and "home" in m["message"]


def test_only_an_admin_can_open_one(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    auth = _Auth({"admintok": ("jaron", True), "usertok": ("guest", False)})
    with TestClient(_app(auth)) as c:
        for cookie in (None, "odysseus_session=usertok", "odysseus_session=nonsense"):
            headers = dict(ORIGIN, **({"cookie": cookie} if cookie else {}))
            with pytest.raises(WebSocketDisconnect) as e:
                with _connect(c, "/api/terminal/ws", headers=headers) as ws:
                    ws.receive()
            assert e.value.code == terminal_routes.CLOSE_UNAUTHORIZED
        assert terminal.manager.list("guest") == [] and terminal.manager.list("") == []
        with _connect(c, "/api/terminal/ws", headers=dict(ORIGIN, cookie="odysseus_session=admintok")) as ws:
            _hello(ws)
            ws.send_bytes(b"echo admin-ok\n")
            _read_until(ws, "admin-ok")
        assert len(terminal.manager.list("jaron")) == 1


def test_another_sites_page_is_refused():
    with TestClient(_app()) as c:
        for headers in ({"origin": "https://evil.example"}, {"origin": "null"}, {},
                        {"origin": "http://testserver.evil.example"}):
            with pytest.raises(WebSocketDisconnect) as e:
                with _connect(c, "/api/terminal/ws", headers=headers) as ws:
                    ws.receive()
            assert e.value.code == terminal_routes.CLOSE_FORBIDDEN
        assert terminal.manager.list("") == []
        # Behind Tailscale Serve the public name comes as X-Forwarded-Host.
        with _connect(c, "/api/terminal/ws", headers={
                "origin": "https://box.tail.ts.net", "x-forwarded-host": "box.tail.ts.net"}) as ws:
            _hello(ws)


def test_a_detached_session_is_closed_after_the_grace_period(monkeypatch):
    monkeypatch.setattr(terminal, "GRACE_S", 0.5)
    with TestClient(_app()) as c:
        with _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
            sid = _hello(ws)["id"]
            pid = terminal.manager.get("", sid).pid
        assert [s["attached"] for s in c.get("/api/terminal/sessions").json()["sessions"]] == [False]
        assert _wait_dead(pid)
        assert c.get("/api/terminal/sessions").json()["sessions"] == []


def test_a_reattach_within_the_grace_period_keeps_it():
    with TestClient(_app()) as c:
        with _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
            sid = _hello(ws)["id"]
        terminal.manager.get("", sid)._reap_timer.cancel()      # (stands in for "not yet")
        with _connect(c, f"/api/terminal/ws?id={sid}", headers=ORIGIN) as ws:
            _hello(ws)
            assert terminal.manager.get("", sid)._reap_timer is None


def test_rename_and_info():
    with TestClient(_app()) as c:
        with _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
            sid = _hello(ws)["id"]
        assert c.patch(f"/api/terminal/sessions/{sid}", json={"title": "ssh pi"}).json()["title"] == "ssh pi"
        assert c.get("/api/terminal/sessions").json()["sessions"][0]["title"] == "ssh pi"
        info = c.get("/api/terminal/info").json()
        assert info["shell"] == "/bin/bash" and info["max_sessions"] == terminal.MAX_SESSIONS


def test_the_list_is_admin_only(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with TestClient(_app(_Auth({}))) as c:
        assert c.get("/api/terminal/sessions").status_code == 403
        assert c.delete("/api/terminal/sessions/x").status_code == 403


def test_the_shell_does_not_get_the_servers_secrets(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "sk-should-not-leak")
    with TestClient(_app()) as c, _connect(c, "/api/terminal/ws", headers=ORIGIN) as ws:
        _hello(ws)
        ws.send_bytes(b"printf 'k%s=[%s]\\n' ey \"$SOME_API_KEY\"\n")
        out = _read_until(ws, r"key=\[[^\]]*\]")
        assert "key=[]" in out


def test_it_is_wired_into_the_app_and_the_page():
    app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "setup_terminal_routes()" in app
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert 'id="tool-terminal-btn"' in html and "/static/js/terminal.js" in html
    sw = open(os.path.join(ROOT, "static", "sw.js"), encoding="utf-8").read()
    for f in ("/static/js/terminal.js", "/static/css/terminal.css", "/static/lib/xterm/xterm.mjs"):
        assert f in sw
    src = open(terminal.__file__, encoding="utf-8").read()
    # Output and keystrokes never go to the log.
    assert not re.search(r"logger\.\w+\([^)]*(data|scrollback|chunk)", src)
