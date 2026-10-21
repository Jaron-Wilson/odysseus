"""Hello Mod on its own: standalone login, and under a stand-in core that
records the mod API calls. Run from examples/hello-mod: python -m pytest -q"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hello_mod import server as hello  # noqa: E402

TOKEN = "odm_test_token"


def sign(user="jaron", mod="hello-mod", token=TOKEN, ts=None):
    ts = str(int(ts if ts is not None else time.time()))
    sig = hmac.new(token.encode(), f"v1\n{mod}\n{user}\n{ts}".encode(), hashlib.sha256).hexdigest()
    return {"X-Odysseus-User": user, "X-Odysseus-Mod": mod, "X-Odysseus-Ts": ts, "X-Odysseus-Sig": sig}


def request(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                         **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class FakeCore:
    def __init__(self):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                outer.calls.append(("GET", self.path, self.headers.get("Authorization"), None))
                self._reply({"settings": {"greeting": "Howdy"}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append(("POST", self.path, self.headers.get("Authorization"), body))
                self._reply({"ok": True, "id": "e1", "delivered_to": []})

            do_PUT = do_POST

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture
def under_core(tmp_path, monkeypatch):
    core = FakeCore()
    monkeypatch.setenv("ODYSSEUS_URL", core.url)
    monkeypatch.setenv("ODYSSEUS_MOD_TOKEN", TOKEN)
    monkeypatch.setenv("ODYSSEUS_MOD_ID", "hello-mod")
    monkeypatch.setenv("MOD_DATA_DIR", str(tmp_path))
    srv = hello.build_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", core
    srv.shutdown()
    core.srv.shutdown()


def test_health_needs_no_identity(under_core):
    base, _ = under_core
    assert request(base + "/health")[0] == 200


def test_unsigned_requests_are_refused(under_core):
    base, _ = under_core
    assert request(base + "/")[0] == 401
    assert request(base + "/", headers=sign(token="wrong"))[0] == 401
    assert request(base + "/", headers=sign(ts=time.time() - 3600))[0] == 401


def test_page_and_card_with_identity(under_core):
    base, _ = under_core
    code, body = request(base + "/", headers=sign())
    assert code == 200 and b"jaron" in body
    code, body = request(base + "/cards/status", headers=sign())
    assert code == 200 and b"waves" in body


def test_tool_list_and_call(under_core):
    base, _ = under_core
    code, body = request(base + "/mod/tools", headers=sign("odysseus-core"))
    tools = json.loads(body)["tools"]
    assert code == 200 and tools[0]["function"]["name"] == "hello_greet"
    code, body = request(base + "/mod/tools/hello_greet", "POST", {"name": "Ada"}, sign())
    out = json.loads(body)
    assert code == 200 and out["output"].startswith("Howdy, Ada!") and out["data"]["count"] == 1


def test_wave_emits_event_and_notifies(under_core):
    base, core = under_core
    code, body = request(base + "/api/wave", "POST", {}, sign())
    assert code == 200
    paths = [(c[0], c[1]) for c in core.calls]
    assert ("POST", "/api/mods/v1/events") in paths and ("POST", "/api/mods/v1/notify") in paths
    assert all(c[2] == f"Bearer {TOKEN}" for c in core.calls)
    ev = next(c[3] for c in core.calls if c[1] == "/api/mods/v1/events")
    assert ev["type"] == "hello.waved" and ev["data"]["waves"] == 1


def test_events_are_recorded(under_core):
    base, _ = under_core
    ev = {"id": "x", "source": "print-hub", "type": "print.finished", "ts": time.time(), "data": {}}
    assert request(base + "/mod/events", "POST", ev, sign("odysseus-core"))[0] == 200
    code, body = request(base + "/api/state", headers=sign())
    assert json.loads(body)["events"][0]["type"] == "print.finished"


def test_standalone_login(tmp_path, monkeypatch):
    for k in ("ODYSSEUS_URL", "ODYSSEUS_MOD_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HELLO_PASSWORD", "pw")
    monkeypatch.setenv("MOD_DATA_DIR", str(tmp_path))
    srv = hello.build_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, body = request(base + "/")
        assert code == 200 and b"password" in body.lower()
        # Signed headers mean nothing without the core.
        assert b"Wave" not in request(base + "/", headers=sign())[1]
    finally:
        srv.shutdown()
