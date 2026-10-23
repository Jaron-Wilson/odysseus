"""Capability providers (src/mods/providers.py): mods the core calls for a
feature (a TTS voice, a model provider, tailnet peers). Found by `provides`,
called at /mod/cap/<cap>/<action> with a signed identity, and never needed:
no provider is a normal state. Nothing leaves the machine."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import paths, providers, registry, signing
from tests.mods_helpers import add_mod, mark_running, mods_dir, run  # noqa: F401


def _write_raw(mod_id, raw):
    with open(os.path.join(paths.src_dir(mod_id), "odysseus-mod.json"), "w") as f:
        json.dump(raw, f)


@pytest.fixture
def fake_mod():
    """A mod server answering /mod/cap/... and recording what it got."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self, body):
            seen.append({"path": self.path, "method": self.command, "headers": dict(self.headers), "body": body})
            if self.path.startswith("/mod/cap/tts.voice/synthesize"):
                data = b"RIFF0000WAVEfake"
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
            else:
                data = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._answer(None)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self._answer(json.loads(self.rfile.read(n) or b"{}"))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], seen
    srv.shutdown()


def test_no_provider_is_normal(mods_dir):
    assert providers.providers("tts.voice") == []
    assert providers.provider("tts.voice") is None
    with pytest.raises(providers.ProviderUnavailable) as e:
        providers.call_sync("tts.voice", "status")
    assert e.value.status == 404
    assert providers.describe("tts.voice") == {"capability": "tts.voice", "providers": [], "installed_off": False}


def test_provides_from_the_raw_manifest_and_only_enabled(mods_dir):
    add_mod(mod_id="voice-b")
    _write_raw("voice-b", {"provides": ["tts.voice", "Bad Cap", 7]})
    add_mod(mod_id="voice-a")
    _write_raw("voice-a", {"provides": ["tts.voice"]})
    add_mod(mod_id="voice-off", enabled=False)
    _write_raw("voice-off", {"provides": ["tts.voice", "network.peers"]})
    add_mod(mod_id="plain")
    assert providers.provides(registry.get("voice-b")) == ["tts.voice"]
    assert [e["id"] for e in providers.providers("tts.voice")] == ["voice-a", "voice-b"]
    assert providers.provider("tts.voice")["id"] == "voice-a"
    assert providers.provider("tts.voice", "voice-b")["id"] == "voice-b"
    assert providers.provider("tts.voice", "voice-off") is None
    assert providers.providers("network.peers") == []
    assert providers.describe("network.peers")["installed_off"] is True


def test_validated_manifest_field_wins(mods_dir):
    entry, _ = add_mod(mod_id="voice")
    _write_raw("voice", {"provides": ["tts.voice"]})
    entry = dict(entry, manifest=dict(entry["manifest"], provides=["llm.provider"]))
    assert providers.provides(entry) == ["llm.provider"]


def test_path_for_refuses_odd_names():
    assert providers.path_for("tts.voice", "synthesize") == "/mod/cap/tts.voice/synthesize"
    assert providers.path_for("llm.provider", "v1/chat/completions") == "/mod/cap/llm.provider/v1/chat/completions"
    for cap, action in (("tts", "x"), ("tts.voice", "../x"), ("tts.voice", "a b"), ("../x.y", "z")):
        with pytest.raises(ValueError):
            providers.path_for(cap, action)


def test_calls_are_signed_for_the_user(mods_dir, fake_mod):
    port, seen = fake_mod
    _, token = add_mod(mod_id="voice")
    _write_raw("voice", {"provides": ["tts.voice"]})
    mark_running("voice", port)
    r = providers.call_sync("tts.voice", "synthesize", {"text": "hi"}, user="alice")
    assert r.status_code == 200 and r.content.startswith(b"RIFF")
    got = seen[-1]
    assert got["path"] == "/mod/cap/tts.voice/synthesize" and got["body"] == {"text": "hi"}
    assert signing.verify_identity(token, got["headers"]) == "alice"
    r = run(providers.call("tts.voice", "voices", {"refresh": "1"}, method="GET"))
    assert r.json()["path"] == "/mod/cap/tts.voice/voices?refresh=1"
    assert signing.verify_identity(token, seen[-1]["headers"]) == signing.CORE_USER


def test_a_stopped_provider_says_so(mods_dir):
    add_mod(mod_id="voice")
    _write_raw("voice", {"provides": ["tts.voice"]})
    # Not running, and not in an app worker thread: it cannot be started here.
    with pytest.raises(providers.ProviderUnavailable) as e:
        providers.call_sync("tts.voice", "status")
    assert e.value.status == 503 and "not running" in e.value.message


class _As(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.headers.get("x-test-user"):
            request.state.current_user = request.headers["x-test-user"]
            request.state.api_token = False
        return await call_next(request)


def test_providers_route(mods_dir, monkeypatch):
    from src.mods import routes

    class Auth:
        users, is_configured = {"bob": {}}, True

        def is_admin(self, u):
            return False

    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = Auth()
    app.include_router(routes.setup_mods_routes(Auth()))
    app.add_middleware(_As)
    c = TestClient(app)
    add_mod(mod_id="voice")
    _write_raw("voice", {"provides": ["tts.voice"]})
    assert c.get("/api/mods/providers?cap=tts.voice").status_code == 401
    got = c.get("/api/mods/providers?cap=tts.voice,llm.provider,bad", headers={"x-test-user": "bob"}).json()
    assert got["capabilities"]["tts.voice"]["providers"] == [{"id": "voice", "name": "Test Mod"}]
    assert got["capabilities"]["llm.provider"]["providers"] == []
    assert "bad" not in got["capabilities"]
