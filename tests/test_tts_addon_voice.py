"""Add-on voices (services/tts/tts_service.py, provider "mod:<id>"): a mod that
provides tts.voice (like the ElevenLabs add-on) speaks for the core; without
it the core says to install it, and when it pauses itself Kokoro or the
browser speaks. A fake add-on answers on loopback; nothing leaves the machine."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.tts.kokoro_local import TTSError
from services.tts.tts_service import TTSService, mod_id_of
from src.mods import paths, registry
from tests.mods_helpers import add_mod, mark_running, mods_dir  # noqa: F401

MP3 = b"ID3" + b"\x00" * 64


class FakeAddon:
    def __init__(self):
        self.paused = ""
        self.calls = []
        self.status_code = 200
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.calls.append(("GET", self.path, None, self.headers.get("X-Odysseus-User")))
                if self.path.startswith("/mod/cap/tts.voice/status"):
                    return self._send(outer.status_code, {
                        "label": "ElevenLabs", "ready": True, "paused": bool(outer.paused),
                        "pause_reason": outer.paused, "max_reply_chars": 1500, "model": "eleven_flash_v2_5"})
                if self.path.startswith("/mod/cap/tts.voice/voices"):
                    return self._send(200, {"voices": [{"voice_id": "myclone1", "name": "Jaron", "category": "cloned"},
                                                       {"name": "no id"}]})
                return self._send(404, {"error": "no"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append(("POST", self.path, body, self.headers.get("X-Odysseus-User")))
                if outer.paused:
                    return self._send(409, {"paused": True, "reason": outer.paused})
                if body.get("text") == "bad key":
                    return self._send(401, {"error": "ElevenLabs rejected the API key."})
                return self._send(200, MP3, "audio/mpeg")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


class FakeKokoro:
    def __init__(self, ready=True):
        self.ready = ready
        self.said = []

    def readiness(self, model):
        return None if self.ready else "Kokoro is not downloaded."

    def synthesize(self, text, model, voice, speed):
        self.said.append(text)
        return b"RIFF" + b"\x00" * 40


def _write_raw(mod_id, raw):
    with open(os.path.join(paths.src_dir(mod_id), "odysseus-mod.json"), "w") as f:
        json.dump(raw, f)


@pytest.fixture
def addon(mods_dir):
    fake = FakeAddon()
    add_mod({"name": "ElevenLabs voice"}, mod_id="elevenlabs")
    _write_raw("elevenlabs", {"provides": ["tts.voice"]})
    mark_running("elevenlabs", fake.port)
    yield fake
    fake.srv.shutdown()


def _service(tmp_path, monkeypatch, provider="mod:elevenlabs", kokoro_ready=True, settings=None):
    svc = TTSService(cache_dir=str(tmp_path / "cache"))
    s = {"tts_enabled": True, "tts_provider": provider, "tts_model": "tts-1", "tts_voice": "myclone1",
         "tts_speed": "1", "tts_kokoro_model": ""}
    s.update(settings or {})
    from services.tts import tts_service as T
    monkeypatch.setattr(svc, "_load_settings", lambda: dict(s, tts_provider=T.LEGACY_PROVIDERS.get(
        s["tts_provider"], s["tts_provider"])))
    kok = FakeKokoro(kokoro_ready)
    monkeypatch.setattr(svc, "_get_kokoro", lambda: kok)
    monkeypatch.setattr(svc, "_kokoro_model", lambda settings: "kokoro-int8")
    svc.kok = kok
    return svc


def test_mod_id_of():
    assert mod_id_of("mod:elevenlabs") == "elevenlabs"
    assert mod_id_of("elevenlabs") == "elevenlabs"  # saved before ElevenLabs was an add-on
    assert mod_id_of("local") is None and mod_id_of("endpoint:3") is None and mod_id_of("mod:") is None


def test_without_the_add_on_it_says_to_install_it(mods_dir, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, provider="elevenlabs")
    assert svc.available is False
    stats = svc.get_stats()
    assert stats["provider"] == "mod:elevenlabs" and not stats["available"]
    assert "Install the ElevenLabs voice add-on" in stats["reason"]
    with pytest.raises(TTSError) as e:
        svc.synthesize_checked("Hello.")
    assert e.value.status == 400 and "Install the ElevenLabs voice add-on" in e.value.message
    # Local voice still works without any add-on.
    local = _service(tmp_path / "l", monkeypatch, provider="local")
    assert local.synthesize_checked("Hello.")[:4] == b"RIFF"


def test_a_turned_off_add_on_says_so(mods_dir, tmp_path, monkeypatch):
    add_mod({"name": "ElevenLabs voice"}, mod_id="elevenlabs", enabled=False)
    _write_raw("elevenlabs", {"provides": ["tts.voice"]})
    stats = _service(tmp_path, monkeypatch).get_stats()
    assert "turned off" in stats["reason"]


def test_speaks_through_the_add_on(addon, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, settings={"tts_speed": "1.1"})
    assert svc.available is True
    assert svc.synthesize_checked("Hello there.", response_format="wav") == MP3
    method, path, body, user = addon.calls[-1]
    assert path == "/mod/cap/tts.voice/synthesize" and user == "odysseus-core"
    assert body == {"text": "Hello there.", "voice": "myclone1", "speed": 1.1, "format": "wav", "use_cache": True}
    stats = svc.get_stats()
    assert stats["engine"] == "elevenlabs" and stats["available"] and stats["speed_applied"]
    assert stats["max_reply_chars"] == 1500 and stats["effective_provider"] == "mod:elevenlabs"
    assert stats["addon"] == {"id": "elevenlabs", "name": "ElevenLabs", "installed": True}
    assert not os.listdir(tmp_path / "cache")  # the add-on keeps its own cache


def test_paused_add_on_hands_over_to_kokoro(addon, tmp_path, monkeypatch):
    addon.paused = "ElevenLabs credits are below 5% (5,000 of 131,000 left)"
    svc = _service(tmp_path, monkeypatch)
    assert svc.synthesize_checked("Low on credits.")[:4] == b"RIFF"
    assert svc.kok.said == ["Low on credits."]
    assert svc.current_fallback()["to"] == "local" and "below 5%" in svc.current_fallback()["reason"]
    stats = svc.get_stats()
    assert stats["effective_provider"] == "local" and "below 5%" in stats["fallback_reason"]


def test_paused_without_kokoro_hands_to_the_browser(addon, tmp_path, monkeypatch):
    addon.paused = "ElevenLabs is out of credits for this month"
    svc = _service(tmp_path, monkeypatch, kokoro_ready=False)
    with pytest.raises(TTSError) as e:
        svc.synthesize_checked("No credits.")
    assert e.value.status == 409 and getattr(e.value, "fallback", "") == "browser"
    assert svc.get_stats()["effective_provider"] == "browser"
    # (The phone call engine check, which reports this, is the Phone & Messages mod's.)


def test_add_on_errors_are_never_401(addon, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    with pytest.raises(TTSError) as e:
        svc.synthesize_checked("bad key")
    assert e.value.status == 400 and "rejected" in e.value.message


def test_a_stopped_add_on(addon, tmp_path, monkeypatch):
    from src.mods import runtime
    runtime.state("elevenlabs").status = "stopped"
    svc = _service(tmp_path, monkeypatch)
    assert svc.available is False
    with pytest.raises(TTSError) as e:
        svc.synthesize_checked("Hello.")
    assert e.value.status == 503


def test_routes_voices_and_synthesize(addon, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import tts_routes
    svc = _service(tmp_path, monkeypatch)
    app = FastAPI()
    app.include_router(tts_routes.setup_tts_routes(svc))
    c = TestClient(app)
    r = c.get("/api/tts/voices?provider=mod:elevenlabs")
    assert r.status_code == 200 and r.json()["voices"] == [{"voice_id": "myclone1", "name": "Jaron",
                                                            "category": "cloned"}]
    assert c.get("/api/tts/voices?provider=local").json() == {"voices": []}
    r = c.post("/api/tts/synthesize", json={"text": "Route test."})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    addon.paused = "ElevenLabs credits are below 5%"
    r = c.post("/api/tts/synthesize", json={"text": "Now falls back."})
    assert r.headers.get("X-TTS-Fallback") == "local"
    svc.kok.ready = False
    r = c.post("/api/tts/synthesize", json={"text": "Browser now."})
    assert r.status_code == 409 and r.json()["detail"]["fallback"] == "browser"
    assert c.get("/api/tts/elevenlabs/status").status_code == 404  # moved to the add-on
    registry.set_enabled("elevenlabs", False)
    r = c.get("/api/tts/voices?provider=mod:elevenlabs")
    assert r.status_code == 400 and "turned off" in r.json()["detail"]["message"]
