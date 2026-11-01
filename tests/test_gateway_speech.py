"""POST /v1/audio/speech and the speech entries of GET /v1/models (src/gateway_speech.py), through the real TTS service
with fakes underneath: a fake Kokoro, a fake add-on on loopback, a fake speech endpoint. No network, no real voices."""
import io
import json

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ModelEndpoint, UsageEvent
from routes import openai_gateway_routes as routes
from services.tts import tts_service as T
from services.tts.kokoro_local import to_wav
from services.tts.tts_service import TTSService
from src import gateway_speech as speech
from src import openai_gateway as gw
from src import usage
from tests.mods_helpers import add_mod, mark_running, mods_dir  # noqa: F401
from tests.test_tts_addon_voice import FakeAddon, MP3, _write_raw

try:
    import av  # noqa: F401
    HAS_AV = True
except Exception:                                   # PyAV is an optional package
    HAS_AV = False
needs_av = pytest.mark.skipif(not HAS_AV, reason="PyAV is not installed")

WAV = to_wav(np.sin(np.linspace(0, 400, 12000)).astype("float32") * 0.3)
H = {"x-test-scopes": "chat", "x-test-owner": "alice"}


class FakeKokoro:
    def __init__(self, ready=True):
        self.ready = ready
        self.calls = []
        self.downloads = type("D", (), {"state": staticmethod(lambda m: None)})()

    def readiness(self, model):
        return None if self.ready else "Kokoro is not downloaded."

    def synthesize(self, text, model, voice, speed):
        self.calls.append({"text": text, "voice": voice, "speed": speed})
        return WAV


@pytest.fixture
def env(monkeypatch, tmp_path, mods_dir):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    import src.database as srcdb
    for mod, attr in ((gw, "SessionLocal"), (srcdb, "SessionLocal"), (usage, "_session_factory")):
        monkeypatch.setattr(mod, attr, factory, raising=False)
    monkeypatch.setattr(srcdb, "ModelEndpoint", ModelEndpoint, raising=False)   # tests/conftest.py stubs src.database
    s = factory()
    s.add_all([
        ModelEndpoint(id="ep-chat", name="Local Ollama", base_url="http://10.0.0.5:11434/v1",
                      cached_models=json.dumps(["qwen3:8b", "tts-1"])),
        ModelEndpoint(id="ep-speech", name="Speech Box", base_url="http://10.0.0.6:8880/v1", api_key="sk-speech",
                      cached_models=json.dumps(["kokoro", "tts-1", "whisper-1"])),
        ModelEndpoint(id="ep-kok1", name="Kokoro", base_url="http://10.0.0.7:8880/v1",
                      cached_models=json.dumps(["tts-1"])),
        ModelEndpoint(id="ep-bob", name="Bob voice", base_url="http://10.0.0.8/v1", owner="bob",
                      cached_models=json.dumps(["tts-1"])),
        ModelEndpoint(id="ep-off", name="Off voice", base_url="http://10.0.0.9/v1", is_enabled=False,
                      cached_models=json.dumps(["tts-1"])),
    ])
    s.commit()
    s.close()

    settings = {"tts_enabled": True, "tts_provider": "local", "tts_model": "tts-1", "tts_voice": "af_nicole",
                "tts_speed": "1.25", "tts_kokoro_model": ""}
    svc = TTSService(cache_dir=str(tmp_path / "cache"))
    monkeypatch.setattr(svc, "_load_settings", lambda: dict(settings, tts_provider=T.LEGACY_PROVIDERS.get(
        settings["tts_provider"], settings["tts_provider"])))
    kok = FakeKokoro()
    monkeypatch.setattr(svc, "_get_kokoro", lambda: kok)
    monkeypatch.setattr(svc, "_kokoro_model", lambda st: "kokoro-int8")
    monkeypatch.setattr(speech, "_service", lambda: svc)

    posted = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append({"url": url, "json": json, "headers": headers})
        if posted_mode.get("down"):
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=MP3, request=httpx.Request("POST", url))
    posted_mode = {}
    monkeypatch.setattr(T.httpx, "post", fake_post)

    app = FastAPI()
    app.include_router(routes.setup_openai_gateway_routes())

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        if scopes is not None:
            request.state.api_token = True
            request.state.api_token_scopes = [s for s in scopes.split(",") if s]
            request.state.api_token_owner = request.headers.get("x-test-owner") or None
        return await call_next(request)

    c = TestClient(app)
    c.settings, c.svc, c.kok, c.posted, c.mode, c.factory = settings, svc, kok, posted, posted_mode, factory
    return c


@pytest.fixture
def addon(env):
    fake = FakeAddon()
    add_mod({"name": "ElevenLabs voice"}, mod_id="elevenlabs")
    _write_raw("elevenlabs", {"provides": ["tts.voice"]})
    mark_running("elevenlabs", fake.port)
    yield fake
    fake.srv.shutdown()


def say(c, model="tts", **kw):
    body = {"model": model, "input": "Hello there.", "voice": "alloy"}
    body.update(kw)
    return c.post("/v1/audio/speech", json=body, headers=H)


def usage_rows(c):
    s = c.factory()
    try:
        return [(r.source, r.owner, r.model, r.input_tokens, r.billing_type, r.provider, r.cost_usd)
                for r in s.query(UsageEvent).all()]
    finally:
        s.close()


# ---- the models ----

@needs_av
@pytest.mark.parametrize("name", ["tts", "tts-1", "tts-1-hd", "gpt-4o-mini-tts", "TTS-1"])
def test_tts_and_the_openai_names_use_the_voice_odysseus_is_set_to(env, name):
    r = say(env, name)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and speech.sniff(r.content) == "mp3"
    # the saved voice and speed speak: "alloy" is an OpenAI name, not a voice of ours, so it is ignored
    assert env.kok.calls[-1]["voice"] == "af_nicole" and env.kok.calls[-1]["speed"] == 1.25


def test_tts_follows_the_saved_provider(env, addon):
    env.settings.update(tts_provider="mod:elevenlabs", tts_voice="myclone1")
    r = say(env, "tts", response_format="mp3")
    assert r.status_code == 200 and r.content == MP3 and r.headers["content-type"] == "audio/mpeg"
    body = [c for c in addon.calls if c[0] == "POST"][-1][2]
    assert body["voice"] == "myclone1" and body["speed"] == 1.25 and not env.kok.calls
    assert r.headers["x-odysseus-voice"] == "tts"


def test_a_real_voice_in_the_request_overrides_the_saved_one(env):
    r = say(env, "tts", voice="bf_emma", speed=1.5, response_format="wav")
    assert r.status_code == 200
    assert env.kok.calls[-1]["voice"] == "bf_emma" and env.kok.calls[-1]["speed"] == 1.5


def test_a_voice_object_is_read_like_a_string(env):
    assert say(env, "tts", voice={"id": "bf_emma"}, response_format="wav").status_code == 200
    assert env.kok.calls[-1]["voice"] == "bf_emma"


def test_kokoro_model_speaks_with_kokoro_whatever_the_saved_provider(env, addon):
    env.settings.update(tts_provider="mod:elevenlabs", tts_voice="myclone1")
    r = say(env, "tts:kokoro", response_format="wav")
    assert r.status_code == 200 and r.content[:4] == b"RIFF" and r.headers["content-type"] == "audio/wav"
    assert not [c for c in addon.calls if c[0] == "POST"]
    # the add-on's saved voice id is not a Kokoro voice, so it is not used; 1x unless asked
    assert env.kok.calls[-1]["voice"] == "af_heart"
    assert env.kok.calls[-1]["speed"] == 1.0


def test_kokoro_model_keeps_the_saved_voice_when_kokoro_is_the_saved_provider(env):
    assert say(env, "tts:kokoro", response_format="wav").status_code == 200
    assert env.kok.calls[-1]["voice"] == "af_nicole"


def test_elevenlabs_model_speaks_through_the_add_on_with_the_requested_voice(env, addon):
    r = say(env, "tts:elevenlabs", voice="myclone1", speed=0.9)
    assert r.status_code == 200 and r.content == MP3 and r.headers["content-type"] == "audio/mpeg"
    body = [c for c in addon.calls if c[0] == "POST"][-1][2]
    assert body["voice"] == "myclone1" and body["speed"] == 0.9 and body["format"] == "mp3"
    assert not env.kok.calls
    # an OpenAI voice name is not an ElevenLabs voice id: the add-on's own default speaks
    say(env, "tts:elevenlabs", voice="nova")
    assert [c for c in addon.calls if c[0] == "POST"][-1][2]["voice"] == ""


def test_an_endpoint_model_speaks_through_that_endpoint(env):
    r = say(env, "tts:speech-box", voice="af_bella", speed=1.1)
    assert r.status_code == 200 and r.content == MP3
    p = env.posted[-1]
    assert p["url"] == "http://10.0.0.6:8880/v1/audio/speech" and p["headers"]["Authorization"] == "Bearer sk-speech"
    assert p["json"]["model"] == "tts-1" and p["json"]["voice"] == "af_bella" and p["json"]["speed"] == 1.1
    assert p["json"]["input"] == "Hello there." and p["json"]["response_format"] == "mp3"
    say(env, "tts:speech-box")                         # no voice: the endpoint default, not the saved Kokoro voice
    assert env.posted[-1]["json"]["voice"] == "alloy" and env.posted[-1]["json"]["speed"] == 1.0
    say(env, "tts:speech-box", response_format="wav")
    assert env.posted[-1]["json"]["response_format"] == "wav"


def test_an_endpoint_named_like_a_reserved_id_gets_a_suffix_and_private_ones_stay_private(env):
    ids = [m["id"] for m in env.get("/v1/models", headers=H).json()["data"]]
    assert "tts:kokoro" in ids and "tts:kokoro-ep-k" in ids          # the endpoint named Kokoro does not take tts:kokoro
    assert say(env, "tts:kokoro-ep-k").status_code == 200
    assert env.posted[-1]["url"].startswith("http://10.0.0.7")
    assert say(env, "tts:bob-voice").status_code == 404               # Bob's own
    assert say(env, "tts:off-voice").status_code == 404               # disabled
    r = env.post("/v1/audio/speech", json={"model": "tts:bob-voice", "input": "hi"},
                 headers={"x-test-scopes": "chat", "x-test-owner": "bob"})
    assert r.status_code == 200


# ---- formats ----

@needs_av
def test_mp3_is_the_default_and_wav_can_be_asked_for(env):
    r = env.post("/v1/audio/speech", json={"model": "tts", "input": "Hi"}, headers=H)       # no format, no voice
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert speech.sniff(r.content) == "mp3"
    assert "x-odysseus-audio-format" not in r.headers
    r = say(env, "tts", response_format="wav")
    assert r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    r = say(env, "tts", response_format="MP3")
    assert r.headers["content-type"] == "audio/mpeg"


@needs_av
def test_mp3_from_an_endpoint_can_be_asked_for_as_wav(env):
    mp3 = speech.transcode(WAV, "mp3")
    assert mp3 and speech.sniff(mp3) == "mp3"
    env.settings["tts_provider"] = "endpoint:ep-speech"
    import httpx as _h
    orig = T.httpx.post
    T.httpx.post = lambda url, **kw: _h.Response(200, content=mp3, request=_h.Request("POST", url))
    try:
        r = say(env, "tts", response_format="wav")
    finally:
        T.httpx.post = orig
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"


def test_when_it_cannot_convert_the_bytes_keep_their_true_type_and_say_so(env, monkeypatch):
    monkeypatch.setattr(speech, "transcode", lambda data, to: None)
    r = say(env, "tts:kokoro", response_format="mp3")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    assert r.headers["x-odysseus-audio-format"] == "wav"


@pytest.mark.parametrize("fmt", ["opus", "aac", "flac", "pcm", "ogg"])
def test_formats_that_are_not_supported_say_what_is(env, fmt):
    r = say(env, "tts", response_format=fmt)
    e = r.json()["error"]
    assert r.status_code == 400 and e["code"] == "unsupported_response_format"
    assert f"'{fmt}'" in e["message"] and "'mp3'" in e["message"] and "'wav'" in e["message"]
    assert e["type"] == "invalid_request_error"
    assert not env.kok.calls


# ---- bad input ----

def test_input_is_capped_at_4096_characters(env):
    assert say(env, "tts", input="a" * 4096, response_format="wav").status_code == 200
    r = say(env, "tts", input="a" * 4097)
    assert r.status_code == 400 and r.json()["error"]["code"] == "string_above_max_length"
    assert "4096" in r.json()["error"]["message"] and "4097" in r.json()["error"]["message"]


@pytest.mark.parametrize("patch,code", [
    ({"input": ""}, "empty_input"), ({"input": "   "}, "empty_input"), ({"input": None}, "missing_input"),
    ({"input": 5}, "missing_input"), ({"model": ""}, "missing_model"), ({"speed": 5}, "invalid_speed"),
    ({"speed": 0.1}, "invalid_speed"), ({"speed": "fast"}, "invalid_speed"), ({"speed": True}, "invalid_speed"),
    ({"voice": 7}, "invalid_voice"), ({"instructions": 3}, "invalid_instructions"),
    ({"stream_format": "sse"}, "unsupported_stream_format"),
])
def test_bad_requests_are_400s(env, patch, code):
    r = env.post("/v1/audio/speech", json=dict({"model": "tts", "input": "Hi"}, **patch), headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == code
    assert not env.kok.calls


def test_the_body_must_be_a_json_object(env):
    assert env.post("/v1/audio/speech", content=b"nope", headers=H).status_code == 400
    r = env.post("/v1/audio/speech", json=["x"], headers=H)
    assert r.status_code == 400 and r.json()["error"]["message"]


def test_instructions_are_accepted_and_ignored(env):
    assert say(env, "tts", instructions="speak warmly", response_format="wav").status_code == 200
    assert say(env, "tts", speed=4, response_format="wav").status_code == 200
    assert say(env, "tts", speed=0.25, response_format="wav").status_code == 200


# ---- unknown or unavailable ----

def test_an_unknown_model_is_404(env):
    for name in ("gpt-4o", "tts:nope", "tts:", "local-ollama:qwen3:8b"):
        r = say(env, name)
        assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found", name


def test_elevenlabs_without_the_add_on_is_404_and_says_to_install_it(env):
    r = say(env, "tts:elevenlabs")
    assert r.status_code == 404 and "Install the ElevenLabs voice add-on" in r.json()["error"]["message"]


def test_a_turned_off_add_on_is_unavailable_for_tts(env):
    env.settings.update(tts_provider="mod:elevenlabs")
    r = say(env, "tts")
    assert r.status_code == 503 and r.json()["error"]["code"] == "voice_unavailable"
    assert "Install the ElevenLabs voice add-on" in r.json()["error"]["message"]


def test_the_browser_voice_and_off_cannot_speak_on_the_server(env):
    env.settings["tts_provider"] = "browser"
    r = say(env, "tts")
    assert r.status_code == 503 and "browser" in r.json()["error"]["message"]
    assert say(env, "tts:kokoro", response_format="wav").status_code == 200       # but a named voice still works
    env.settings["tts_provider"] = "disabled"
    r = say(env, "tts")
    assert r.status_code == 503 and "off" in r.json()["error"]["message"]
    env.settings.update(tts_provider="local", tts_enabled=False)
    r = say(env, "tts:kokoro")
    assert r.status_code == 503 and r.json()["error"]["message"] == "Text to speech is off."


def test_kokoro_not_ready_is_503_with_the_reason(env):
    env.kok.ready = False
    env.svc.start_download = lambda m: None
    r = say(env, "tts:kokoro")
    assert r.status_code == 503 and r.json()["error"]["code"] == "voice_unavailable"
    assert "not downloaded" in r.json()["error"]["message"] and r.json()["error"]["type"] == "api_error"


def test_a_dead_endpoint_is_502(env):
    env.mode["down"] = True
    r = say(env, "tts:speech-box")
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"
    assert "did not answer" in r.json()["error"]["message"]


def test_an_add_on_that_is_over_quota_is_429(env, addon, monkeypatch):
    env.settings["tts_provider"] = "mod:elevenlabs"
    import threading
    real = addon.srv.RequestHandlerClass.do_POST

    def post(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        return self._send(429, {"error": "ElevenLabs says slow down."})
    monkeypatch.setattr(addon.srv.RequestHandlerClass, "do_POST", post)
    r = say(env, "tts:elevenlabs")
    assert r.status_code == 429 and "slow down" in r.json()["error"]["message"]
    monkeypatch.setattr(addon.srv.RequestHandlerClass, "do_POST", real)


# ---- a paused add-on ----

@needs_av
def test_a_paused_add_on_falls_back_to_kokoro_and_says_so(env, addon):
    addon.paused = "Credit reserve reached"
    r = say(env, "tts:elevenlabs", response_format="mp3")
    assert r.status_code == 200 and r.content[:3] != b"RIFF" and r.headers["content-type"] == "audio/mpeg"
    assert r.headers["x-tts-fallback"] == "local" and r.headers["x-tts-fallback-reason"] == "Credit reserve reached"
    assert env.kok.calls and env.kok.calls[-1]["voice"] == "af_heart"
    env.settings.update(tts_provider="mod:elevenlabs")
    r = say(env, "tts", response_format="wav")
    assert r.status_code == 200 and r.content[:4] == b"RIFF" and r.headers["x-tts-fallback"] == "local"


def test_a_paused_add_on_without_kokoro_is_409(env, addon):
    addon.paused = "Credit reserve reached"
    env.kok.ready = False
    r = say(env, "tts:elevenlabs")
    e = r.json()["error"]
    assert r.status_code == 409 and e["code"] == "voice_paused" and "Credit reserve reached" in e["message"]


def test_no_fallback_header_when_nothing_was_paused(env, addon):
    addon.paused = ""
    assert "x-tts-fallback" not in say(env, "tts:elevenlabs").headers
    addon.paused = "Credit reserve reached"
    say(env, "tts:elevenlabs", response_format="wav")
    addon.paused = ""
    r = say(env, "tts:elevenlabs")
    assert "x-tts-fallback" not in r.headers


# ---- caching ----

def test_the_tts_service_cache_applies(env):
    for _ in range(3):
        assert say(env, "tts:kokoro", response_format="wav").status_code == 200
    assert len(env.kok.calls) == 1
    say(env, "tts:kokoro", input="Something else.", response_format="wav")
    assert len(env.kok.calls) == 2


# ---- scope ----

def test_speech_needs_the_chat_scope_like_chat_does(env):
    body = {"model": "tts", "input": "Hi"}
    r = env.post("/v1/audio/speech", json=body, headers={"x-test-scopes": "memory:read"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
    assert not env.kok.calls
    assert env.post("/v1/audio/speech", json=body, headers={"x-test-scopes": ""}).status_code == 403
    assert env.post("/v1/audio/speech", json=dict(body, response_format="wav"),
                    headers={"x-test-scopes": "chat"}).status_code == 200
    assert env.post("/v1/audio/speech", json=dict(body, response_format="wav")).status_code == 200   # browser session


# ---- GET /v1/models ----

def test_models_lists_the_speech_models_and_leaves_the_chat_ones_alone(env, addon):
    r = env.get("/v1/models", headers=H)
    data = r.json()["data"]
    ids = [m["id"] for m in data]
    voice = {m["id"]: m for m in data if m["owned_by"] == "Odysseus voice"}
    assert set(voice) == {"tts", "tts:kokoro", "tts:elevenlabs", "tts:speech-box", "tts:kokoro-ep-k", "tts:local-ollama"}
    assert all(m["object"] == "model" and m["created"] == 0 for m in voice.values())
    assert len(ids) == len(set(ids))
    chat = [m["id"] for m in data if m["owned_by"] != "Odysseus voice"]
    auto = [i for i in chat if i == "auto" or i.startswith("auto:")]
    assert chat[:len(auto)] == auto                                    # the router entries stay first, as before
    assert chat[len(auto):] == [i["id"] for i in gw.catalog("alice")]  # then the chat models, in their order
    assert data[-len(voice):] == list(voice.values())                  # the speech models come last
    assert "tts-1" not in ids and "gpt-4o-mini-tts" not in ids          # the OpenAI names work but are not listed


def test_models_only_lists_what_can_speak(env):
    base = {"tts", "tts:kokoro", "tts:speech-box", "tts:kokoro-ep-k", "tts:local-ollama"}          # no add-on installed
    got = {m["id"] for m in env.get("/v1/models", headers=H).json()["data"] if m["owned_by"] == "Odysseus voice"}
    assert got == base
    env.kok.ready = False
    env.settings["tts_provider"] = "browser"
    got = {m["id"] for m in env.get("/v1/models", headers=H).json()["data"] if m["owned_by"] == "Odysseus voice"}
    assert got == {"tts:speech-box", "tts:kokoro-ep-k", "tts:local-ollama"}
    env.settings.update(tts_provider="local", tts_enabled=False)
    got = {m["id"] for m in env.get("/v1/models", headers=H).json()["data"] if m["owned_by"] == "Odysseus voice"}
    assert "tts" not in got


def test_an_add_on_that_is_not_ready_is_not_listed(env, addon, monkeypatch):
    monkeypatch.setattr(env.svc, "mod_status", lambda mod_id, refresh=False: {"ready": False})
    got = {m["id"] for m in env.get("/v1/models", headers=H).json()["data"]}
    assert "tts:elevenlabs" not in got


def test_models_list_needs_the_scope_and_survives_a_speech_failure(env, monkeypatch):
    assert env.get("/v1/models", headers={"x-test-scopes": "todos:read"}).status_code == 403

    def boom(owner):
        raise RuntimeError("x")
    monkeypatch.setattr(speech, "catalog", boom)
    r = env.get("/v1/models", headers=H)
    assert r.status_code == 200 and any(m["id"].startswith("local-ollama:") for m in r.json()["data"])


# ---- usage ----

def test_usage_is_recorded_as_tts_in_characters(env, addon):
    say(env, "tts:kokoro", input="  Hello there.  ", response_format="wav")
    assert usage_rows(env) == [("tts", "alice", "tts:kokoro", 12, "local_free", "kokoro", 0.0)]
    say(env, "tts-1-hd", response_format="wav")                    # an OpenAI name is recorded as the model it is for
    rows = usage_rows(env)
    assert rows[-1][:4] == ("tts", "alice", "tts", 12) and rows[-1][5] == "kokoro"
    say(env, "tts:elevenlabs")
    row = usage_rows(env)[-1]
    assert row[:4] == ("tts", "alice", "tts:elevenlabs", 12) and row[5] == "elevenlabs" and row[4] != "local_free"
    say(env, "tts:speech-box")
    row = usage_rows(env)[-1]
    assert row[:4] == ("tts", "alice", "tts:speech-box", 12)


def test_a_fallback_is_recorded_as_the_kokoro_that_spoke(env, addon):
    addon.paused = "Credit reserve reached"
    say(env, "tts:elevenlabs", response_format="wav")
    row = usage_rows(env)[-1]
    assert row[0] == "tts" and row[4] == "local_free" and row[5] == "kokoro"


def test_failed_and_rejected_requests_record_nothing(env):
    say(env, "tts", response_format="opus")
    say(env, "tts:nope")
    env.kok.ready = False
    env.svc.start_download = lambda m: None
    say(env, "tts:kokoro")
    assert usage_rows(env) == []


def test_a_request_without_an_owner_records_none(env):
    env.post("/v1/audio/speech", json={"model": "tts:kokoro", "input": "Hi", "response_format": "wav"})
    assert usage_rows(env)[0][:4] == ("tts", None, "tts:kokoro", 2)


# ---- the web app's own path is unchanged ----

def test_the_service_without_an_override_behaves_as_before(env):
    assert env.svc.synthesize_checked("Hello.") == WAV
    assert env.kok.calls[-1]["voice"] == "af_nicole"
    env.settings["tts_provider"] = "mod:elevenlabs"
    from services.tts.kokoro_local import TTSError
    with pytest.raises(TTSError):
        env.svc.synthesize_checked("Hello.")                         # no add-on, as always


@needs_av
def test_the_conversion_keeps_the_audio_playable_and_its_length():
    def info(data):
        c = av.open(io.BytesIO(data))
        try:
            s = c.streams.audio[0]
            return s.codec_context.name, s.rate, c.duration / 1e6
        finally:
            c.close()
    mp3 = speech.transcode(WAV, "mp3")
    codec, rate, secs = info(mp3)
    assert speech.sniff(mp3) == "mp3" and codec.startswith("mp3") and rate == 24000 and abs(secs - 0.5) < 0.1
    wav = speech.transcode(mp3, "wav")
    codec, rate, secs = info(wav)
    assert speech.sniff(wav) == "wav" and codec.startswith("pcm") and rate == 24000 and abs(secs - 0.5) < 0.1
    assert speech.transcode(b"not audio", "mp3") is None
