"""The odysseus_mod helper: signed identity, the core client (also
standalone), and ModApp's built-in contract."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odysseus_mod import CoreClient, ModApp, sign_identity, verify_identity  # noqa: E402


def test_signature_round_trip_and_rejections():
    h = sign_identity("odm_k", "my-mod", "alice")
    assert verify_identity("odm_k", h) == "alice"
    assert verify_identity("odm_k", h, mod_id="other") is None
    assert verify_identity("odm_x", h) is None
    assert verify_identity("odm_k", dict(h, **{"X-Odysseus-User": "bob"})) is None
    assert verify_identity("odm_k", sign_identity("odm_k", "my-mod", "alice", ts=time.time() - 3600)) is None
    assert verify_identity("", h) is None
    # Header names are case-insensitive in practice.
    assert verify_identity("odm_k", {k.lower(): v for k, v in h.items()}) == "alice"


def test_standalone_client_uses_local_files(tmp_path, capsys):
    c = CoreClient(url="", token="", mod_id="my-mod", data_dir=str(tmp_path))
    assert c.standalone
    c.save_settings({"a": 1})
    assert c.settings() == {"a": 1}
    c.kv_put("k", [1, 2])
    assert c.kv_get("k") == [1, 2] and c.kv_get("missing", "d") == "d"
    c.notify("Hi", "there")
    assert "[notify:info] Hi: there" in capsys.readouterr().out
    assert c.emit("x.y")["standalone"] and c.devices() == []


def _serve(app):
    srv = app.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def under_core():
    core = CoreClient(url="http://127.0.0.1:9", token="odm_t", mod_id="my-mod")
    app = ModApp(core=core)

    @app.get("/hello/{name}")
    def hello(req):
        return {"user": req.user, "name": req.params["name"]}

    @app.tool("add", "Add two numbers", {"a": {"type": "number"}, "b": {"type": "number"}}, keywords=["sum"])
    def add(req, args):
        return {"output": str(args["a"] + args["b"]), "data": {"user": req.user}}

    got = []

    @app.on_event("print-hub/print.*")
    def on_print(req, ev):
        got.append(ev["type"])

    srv, base = _serve(app)
    yield base, got
    srv.shutdown()


def test_identity_is_required_under_the_core(under_core):
    base, _ = under_core
    assert _req(base + "/health")[0] == 200
    assert _req(base + "/hello/x")[0] == 401
    code, body = _req(base + "/hello/x", headers=sign_identity("odm_t", "my-mod", "alice"))
    assert code == 200 and json.loads(body) == {"user": "alice", "name": "x"}


def test_tools_and_events(under_core):
    base, got = under_core
    h = sign_identity("odm_t", "my-mod", "odysseus-core")
    tools = json.loads(_req(base + "/mod/tools", headers=h)[1])["tools"]
    assert tools[0]["function"]["name"] == "add" and tools[0]["x-odysseus"]["keywords"] == ["sum"]
    code, body = _req(base + "/mod/tools/add", "POST", {"a": 2, "b": 3}, dict(h, **{"Content-Type": "application/json"}))
    assert json.loads(body) == {"output": "5", "data": {"user": "odysseus-core"}}
    for t in ("print.finished", "game.finished"):
        _req(base + "/mod/events", "POST", {"source": "print-hub", "type": t}, h)
    _req(base + "/mod/events", "POST", {"source": "rook", "type": "print.finished"}, h)
    assert got == ["print.finished"]


def test_standalone_app_asks_for_a_password(monkeypatch):
    monkeypatch.setenv("MOD_PASSWORD", "pw")
    app = ModApp(core=CoreClient(url="", token="", mod_id="my-mod"))

    @app.get("/")
    def home(req):
        return "home"
    srv, base = _serve(app)
    try:
        code, body = _req(base + "/")
        assert code == 401 and b"password" in body
        # Signed headers mean nothing standalone.
        assert _req(base + "/", headers=sign_identity("x", "my-mod", "alice"))[0] == 401
    finally:
        srv.shutdown()


def test_chunked_request_bodies_are_read(monkeypatch):
    # The core's proxy forwards bodies over 16 MB chunked (no Content-Length).
    import http.client
    core = CoreClient(url="http://127.0.0.1:9", token="odm_t", mod_id="my-mod")
    app = ModApp(core=core)

    @app.post("/upload")
    def upload(req):
        return {"n": len(req.body), "head": req.body[:5].decode()}

    srv, base = _serve(app)
    try:
        host, port = srv.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        payload = b"hello" + b"x" * 70000
        conn.request("POST", "/upload", body=iter([payload[:30000], payload[30000:]]),
                     headers={**sign_identity("odm_t", "my-mod", "alice"), "Transfer-Encoding": "chunked"},
                     encode_chunked=True)
        r = conn.getresponse()
        assert r.status == 200 and json.loads(r.read()) == {"n": len(payload), "head": "hello"}
    finally:
        srv.shutdown()


def test_devcore_serves_machines_and_stats_with_their_permissions(tmp_path):
    from odysseus_mod import CoreError
    from odysseus_mod.devcore import DevCore
    (tmp_path / "odysseus-mod.json").write_text(json.dumps({
        "id": "probe", "name": "Probe", "version": "0.1.0", "permissions": ["machines.read"],
        "runtime": {"type": "process", "command": ["python", "-m", "nothing"]}}))
    core = DevCore(str(tmp_path), state_dir=str(tmp_path / ".devcore"))
    threading.Thread(target=core.server.serve_forever, daemon=True).start()
    try:
        c = CoreClient(url=core.url, token=core.token, mod_id="probe")
        assert c.machines()[0]["os"] == "windows"
        with pytest.raises(CoreError) as e:
            c.stats("devops")
        assert e.value.status == 403 and "stats.read" in e.value.detail
        core.perms.add("stats.read")
        core.transcripts["0123abcd"] = {"id": "0123abcd", "blocks": []}
        assert c.stats("devops", 24)["scope"] == "devops"
        assert c.stats_transcript("0123abcd")["id"] == "0123abcd"
        with pytest.raises(CoreError):
            c.stats_transcript("ffffffff")
    finally:
        core.server.shutdown()
