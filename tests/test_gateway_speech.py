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
        if voice not in ("af_heart", "af_nicole", "af_bella", "bf_emma"):
            from services.tts.kokoro_local import TTSError
            raise TTSError(f"Kokoro could not use the voice {voice!r}", 400)
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
    monkeypatch.setattr(gw, "voice_setting", lambda: (
        settings["tts_provider"].split(":", 1)[1] if settings["tts_provider"].startswith("endpoint:") else None,
        settings["tts_model"]))

    posted = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append({"url": url, "json": json, "headers": headers})
        if posted_mode.get("down"):
            raise httpx.ConnectError("refused")
        if posted_mode.get("status"):
            return httpx.Response(posted_mode["status"], text="nope", request=httpx.Request("POST", url))
        if json.get("voice") == posted_mode.get("reject_voice"):
            return httpx.Response(422, text="no such voice", request=httpx.Request("POST", url))
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

@pytest.mark.parametrize("name", ["tts", "tts-1", "tts-1-hd", "gpt-4o-mini-tts", "TTS-1", "tts:auto"])
def test_tts_the_openai_names_and_auto_are_the_same_model(env, name):
    r = say(env, name)
    # no add-on here, so the first speech endpoint (the voice endpoint, else by name) speaks
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == MP3
    assert r.headers["x-odysseus-voice"] == "tts:kokoro-ep-k"
    assert env.posted[-1]["url"].startswith("http://10.0.0.7")


def test_the_voice_endpoint_is_tried_first_by_auto(env):
    env.settings.update(tts_provider="endpoint:ep-speech")
    r = say(env, "tts")
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:speech-box"
    assert env.posted[-1]["url"].startswith("http://10.0.0.6")


def test_a_real_voice_in_the_request_is_passed_on(env):
    r = say(env, "tts:kokoro", voice="bf_emma", speed=1.5, response_format="wav")
    assert r.status_code == 200
    assert env.kok.calls[-1]["voice"] == "bf_emma" and env.kok.calls[-1]["speed"] == 1.5


def test_a_voice_object_is_read_like_a_string(env):
    assert say(env, "tts:kokoro", voice={"id": "bf_emma"}, response_format="wav").status_code == 200
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

def test_mp3_is_the_default_and_wav_can_be_asked_for(env):
    r = env.post("/v1/audio/speech", json={"model": "tts:kokoro", "input": "Hi"}, headers=H)   # no format, no voice
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert speech.sniff(r.content) == "mp3"
    assert "x-odysseus-audio-format" not in r.headers
    r = say(env, "tts:kokoro", response_format="wav")
    assert r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    r = say(env, "tts:kokoro", response_format="MP3")
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


def test_a_saved_add_on_voice_that_is_not_installed_does_not_stop_auto(env):
    env.settings.update(tts_provider="mod:elevenlabs")
    r = say(env, "tts")
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:kokoro-ep-k"


def test_only_the_off_switch_stops_the_server_voices(env):
    for saved in ("browser", "disabled"):             # the saved voice is the web app's business, not the gateway's
        env.settings["tts_provider"] = saved
        assert say(env, "tts:kokoro", response_format="wav").status_code == 200
        assert say(env, "tts").status_code == 200
    env.settings.update(tts_provider="local", tts_enabled=False)
    for name in ("tts:kokoro", "tts", "tts:speech-box"):
        r = say(env, name)
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

def test_a_paused_add_on_falls_back_to_kokoro_and_says_so(env, addon):
    addon.paused = "Credit reserve reached"
    r = say(env, "tts:elevenlabs", response_format="mp3")                 # by name: the service's own fallback
    assert r.status_code == 200 and r.content[:3] != b"RIFF" and r.headers["content-type"] == "audio/mpeg"
    assert r.headers["x-tts-fallback"] == "local" and r.headers["x-tts-fallback-reason"] == "Credit reserve reached"
    assert r.headers["x-odysseus-voice"] == "tts:kokoro"
    assert env.kok.calls and env.kok.calls[-1]["voice"] == "af_heart"


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
    assert list(voice) == ["tts:auto", "tts:elevenlabs", "tts:kokoro-ep-k", "tts:local-ollama", "tts:speech-box", "tts:kokoro"]
    assert all(m["object"] == "model" and m["created"] == 0 and m["description"] for m in voice.values())
    assert "ElevenLabs" in voice["tts:auto"]["description"] and "Speech Box" in voice["tts:auto"]["description"]
    assert len(ids) == len(set(ids))
    chat = [m["id"] for m in data if m["owned_by"] != "Odysseus voice"]
    auto = [i for i in chat if i == "auto" or i.startswith("auto:")]
    assert chat[:len(auto)] == auto                                    # the router entries stay first, as before
    assert chat[len(auto):] == [i["id"] for i in gw.catalog("alice")]  # then the chat models, in their order
    assert chat[len(auto):] == ["local-ollama:qwen3:8b"]              # and no speech model among them
    assert data[-len(voice):] == list(voice.values())                  # the speech models come last
    assert not {"tts", "tts-1", "tts-1-hd", "gpt-4o-mini-tts"} & set(ids)   # the OpenAI names work but are not listed


def test_models_only_lists_what_can_speak(env):
    base = ["tts:auto", "tts:kokoro-ep-k", "tts:local-ollama", "tts:speech-box", "tts:kokoro"]      # no add-on installed
    assert _voice_ids_list(env) == base
    env.kok.ready = False
    assert _voice_ids_list(env) == ["tts:auto", "tts:kokoro-ep-k", "tts:local-ollama", "tts:speech-box"]
    env.settings.update(tts_provider="browser", tts_enabled=False)
    assert _voice_ids_list(env) == []


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
    say(env, "tts-1-hd", response_format="wav")                  # auto: recorded as the backend that spoke
    row = usage_rows(env)[-1]
    assert row[:4] == ("tts", "alice", "tts:elevenlabs", 12) and row[5] == "elevenlabs" and row[4] != "local_free"
    say(env, "tts:speech-box")
    row = usage_rows(env)[-1]
    assert row[:4] == ("tts", "alice", "tts:speech-box", 12)
    addon.paused = "Credit reserve reached"
    say(env, "tts", response_format="wav")                       # auto passes over the paused add-on
    assert usage_rows(env)[-1][:4] == ("tts", "alice", "tts:kokoro-ep-k", 12)


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


def _add_endpoint(env, **kw):
    s = env.factory()
    s.add(ModelEndpoint(**kw))
    s.commit()
    s.close()


def _voice_ids_list(env):
    return [m["id"] for m in env.get("/v1/models", headers=H).json()["data"] if m["owned_by"] == "Odysseus voice"]


def _voice_ids(env):
    return {m["id"] for m in env.get("/v1/models", headers=H).json()["data"] if m["owned_by"] == "Odysseus voice"}


def test_a_kokoro_server_whose_only_model_is_called_kokoro_is_a_speech_endpoint(env):
    _add_endpoint(env, id="ep-pc", name="PC Kokoro", base_url="http://100.102.86.125:8880/v1",
                  cached_models=json.dumps(["kokoro"]))
    assert "tts:pc-kokoro" in _voice_ids(env)
    r = say(env, "tts:pc-kokoro", voice="af_bella")
    assert r.status_code == 200
    p = env.posted[-1]
    assert p["url"] == "http://100.102.86.125:8880/v1/audio/speech" and p["json"]["model"] == "kokoro"
    assert p["json"]["voice"] == "af_bella"


def test_a_tts_name_is_preferred_when_an_endpoint_has_several_speech_models(env):
    say(env, "tts:speech-box")                       # models: kokoro, tts-1, whisper-1
    assert env.posted[-1]["json"]["model"] == "tts-1"


def test_the_endpoint_set_as_the_voice_counts_whatever_its_models_are_called(env):
    _add_endpoint(env, id="ep-odd", name="Odd voice", base_url="http://10.0.0.20/v1",
                  cached_models=json.dumps(["my-voice-v1"]))
    assert "tts:odd-voice" not in _voice_ids(env)    # nothing says it speaks
    assert say(env, "tts:odd-voice").status_code == 404
    assert "odd-voice:my-voice-v1" in [i["id"] for i in gw.catalog("alice")]      # until it is the voice, it is a chat model
    env.settings.update(tts_provider="endpoint:ep-odd", tts_model="my-voice-v1")
    assert "tts:odd-voice" in _voice_ids(env)
    assert say(env, "tts:odd-voice").status_code == 200
    assert env.posted[-1]["json"]["model"] == "my-voice-v1"
    assert "odd-voice:my-voice-v1" not in [i["id"] for i in gw.catalog("alice")]   # the saved speech model is not chat
    env.settings.update(tts_model="")
    say(env, "tts:odd-voice", input="Again.")
    assert env.posted[-1]["json"]["model"] == "tts-1"
    env.settings.update(tts_provider="local")        # no longer the voice: back to not listed
    assert "tts:odd-voice" not in _voice_ids(env)


def test_chat_and_speech_to_text_models_do_not_make_a_speech_endpoint(env):
    _add_endpoint(env, id="ep-llm", name="Plain LLM", base_url="http://10.0.0.21/v1",
                  cached_models=json.dumps(["qwen3:8b", "whisper-1", "text-embedding-3-small"]))
    assert "tts:plain-llm" not in _voice_ids(env)


def test_the_voice_endpoint_is_still_owner_scoped_and_needs_to_be_enabled(env):
    _add_endpoint(env, id="ep-priv", name="Private voice", base_url="http://10.0.0.22/v1", owner="bob",
                  cached_models=json.dumps(["x"]))
    env.settings.update(tts_provider="endpoint:ep-priv")
    assert "tts:private-voice" not in _voice_ids(env)           # alice does not see bob's endpoint
    got = {m["id"] for m in env.get("/v1/models", headers={"x-test-scopes": "chat", "x-test-owner": "bob"}).json()["data"]}
    assert "tts:private-voice" in got


# ---- tts:auto: ElevenLabs, then the speech endpoints, then Kokoro on the server ----

@pytest.fixture
def desktop(env):
    """The PC's Kokoro, as it is registered in the live setup: one model, "kokoro", no "tts" in its name."""
    s = env.factory()
    for ep in s.query(ModelEndpoint).filter(ModelEndpoint.id.in_(["ep-chat", "ep-speech", "ep-kok1", "ep-bob", "ep-off"])):
        s.delete(ep)
    s.add(ModelEndpoint(id="ep-desk", name="Kokoro (desktop)", base_url="http://100.102.86.125:8880/v1",
                        cached_models=json.dumps(["kokoro"])))
    s.commit()
    s.close()
    return env


def post_bodies(addon):
    return [c[2] for c in addon.calls if c[0] == "POST"]


def test_auto_tries_elevenlabs_first(desktop, addon):
    r = say(desktop, "tts:auto", voice="myclone1")
    assert r.status_code == 200 and r.content == MP3 and r.headers["x-odysseus-voice"] == "tts:elevenlabs"
    assert "x-odysseus-voice-skipped" not in r.headers
    assert len(post_bodies(addon)) == 1 and not desktop.posted and not desktop.kok.calls


def test_auto_goes_to_the_desktop_kokoro_then_to_the_server_kokoro(desktop):
    r = say(desktop, "tts:auto")                                  # no add-on installed
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:kokoro-desktop"
    assert desktop.posted[-1]["url"] == "http://100.102.86.125:8880/v1/audio/speech"
    assert desktop.posted[-1]["json"]["model"] == "kokoro" and not desktop.kok.calls
    desktop.mode["down"] = True                                   # the PC is off
    r = say(desktop, "tts:auto", response_format="wav")
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:kokoro" and r.content[:4] == b"RIFF"
    assert "kokoro-desktop" in r.headers["x-odysseus-voice-skipped"]
    assert len(desktop.kok.calls) == 1


@pytest.mark.parametrize("how", ["quota", "key", "limit", "down"])
def test_auto_falls_through_when_elevenlabs_fails(desktop, addon, monkeypatch, how):
    status = {"quota": 402, "key": 401, "limit": 429, "down": 500}[how]

    def post(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return self._send(status, {"error": f"ElevenLabs answered {status}"})
    monkeypatch.setattr(addon.srv.RequestHandlerClass, "do_POST", post)
    r = say(desktop, "tts:auto")
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:kokoro-desktop"
    assert r.headers["x-odysseus-voice-skipped"].startswith("elevenlabs:")
    assert not desktop.kok.calls


def test_auto_falls_through_when_the_add_on_is_not_running(desktop, addon):
    from src.mods import runtime
    runtime.state("elevenlabs").status = "stopped"
    desktop.svc._mod_status_cache.clear()
    r = say(desktop, "tts:auto")
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:kokoro-desktop"
    assert "x-odysseus-voice-skipped" in r.headers


def test_auto_skips_a_paused_add_on_without_calling_it_and_does_not_use_the_server_kokoro_for_it(desktop, addon):
    addon.paused = "Credit reserve reached"
    r = say(desktop, "tts:auto")
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:kokoro-desktop"
    assert "Credit reserve reached" in r.headers["x-odysseus-voice-skipped"]
    assert not post_bodies(addon) and not desktop.kok.calls
    assert "x-tts-fallback" not in r.headers


def test_auto_handles_a_pause_that_starts_between_the_status_check_and_the_call(desktop, addon, monkeypatch):
    monkeypatch.setattr(speech, "_mod_state", lambda mid: (True, ""))       # the status said fine
    addon.paused = "Credit reserve reached"                               # the call says paused
    r = say(desktop, "tts:auto")
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:kokoro-desktop"
    assert len(post_bodies(addon)) == 1 and not desktop.kok.calls          # the add-on's own Kokoro fallback is not used


def test_auto_with_everything_failing_is_one_openai_error(desktop, addon, monkeypatch):
    desktop.mode["down"] = True
    desktop.kok.ready = False
    desktop.svc.start_download = lambda m: None
    addon.paused = "Credit reserve reached"
    r = say(desktop, "tts:auto")
    e = r.json()["error"]
    assert r.status_code == 502 and e["code"] == "upstream_error" and e["type"] == "api_error"
    for part in ("elevenlabs: Credit reserve reached", "kokoro-desktop:", "kokoro: Kokoro is not downloaded"):
        assert part in e["message"]
    assert usage_rows(desktop) == []


def test_auto_with_nothing_available_is_503(desktop):
    desktop.kok.ready = False
    desktop.svc.start_download = lambda m: None
    s = desktop.factory()
    s.query(ModelEndpoint).delete()
    s.commit()
    s.close()
    r = say(desktop, "tts:auto")
    assert r.status_code == 503 and r.json()["error"]["code"] == "voice_unavailable"
    assert _voice_ids_list(desktop) == []                               # and nothing is listed, auto included


def test_auto_follows_the_order_and_names_who_spoke_in_usage(desktop, addon):
    say(desktop, "tts-1")
    say(desktop, "tts-1", input="Second one.")
    assert [r[2] for r in usage_rows(desktop)] == ["tts:elevenlabs", "tts:elevenlabs"]
    addon.paused = "Credit reserve reached"
    say(desktop, "tts-1", input="Third one.")
    assert usage_rows(desktop)[-1][2] == "tts:kokoro-desktop"


# ---- voices that the backend that speaks does not have ----

def test_a_voice_the_add_on_does_not_have_is_replaced_by_its_default(desktop, addon):
    r = say(desktop, "tts:auto", voice="af_heart")                # a Kokoro voice, asked of ElevenLabs
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:elevenlabs"
    assert post_bodies(addon)[-1]["voice"] == ""
    say(desktop, "tts:elevenlabs", voice="Jaron", input="By name.")        # a voice it has, by its name
    assert post_bodies(addon)[-1]["voice"] == "Jaron"
    say(desktop, "tts:elevenlabs", voice="myclone1", input="By id.")
    assert post_bodies(addon)[-1]["voice"] == "myclone1"


def test_an_elevenlabs_voice_on_kokoro_uses_the_kokoro_default(desktop):
    r = say(desktop, "tts:kokoro", voice="myclone1", response_format="wav")
    assert r.status_code == 200 and desktop.kok.calls[-1]["voice"] == "af_heart"
    r = say(desktop, "tts:kokoro", voice="zz_unknown", response_format="wav", input="Another.")   # looks like Kokoro, is not
    # it refused that one, so Kokoro's default speaks (here the saved Kokoro voice)
    assert r.status_code == 200 and [c["voice"] for c in desktop.kok.calls][-2:] == ["zz_unknown", "af_nicole"]


def test_an_endpoint_that_refuses_the_voice_is_asked_again_with_its_default(desktop):
    desktop.mode["reject_voice"] = "af_heart"
    r = say(desktop, "tts:kokoro-desktop", voice="af_heart")
    assert r.status_code == 200 and [p["json"]["voice"] for p in desktop.posted] == ["af_heart", "alloy"]


def test_falling_through_never_fails_over_a_voice(desktop, addon, monkeypatch):
    # ElevenLabs is down and the desktop does not know the voice: the desktop's default speaks, not an error
    def post(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return self._send(500, {"error": "boom"})
    monkeypatch.setattr(addon.srv.RequestHandlerClass, "do_POST", post)
    desktop.mode["reject_voice"] = "myclone1"
    r = say(desktop, "tts:auto", voice="myclone1")
    assert r.status_code == 200 and r.headers["x-odysseus-voice"] == "tts:kokoro-desktop"
    assert [p["json"]["voice"] for p in desktop.posted] == ["myclone1", "alloy"]


def test_a_server_that_is_down_is_not_retried_as_a_voice_problem(desktop):
    desktop.mode["status"] = 500
    r = say(desktop, "tts:kokoro-desktop", voice="af_heart")
    assert r.status_code == 502 and len(desktop.posted) == 1


# ---- speech models are not chat models ----

def test_the_desktop_kokoro_is_a_speech_model_and_not_a_chat_model(desktop):
    data = desktop.get("/v1/models", headers=H).json()["data"]
    ids = {m["id"]: m for m in data}
    assert "kokoro-desktop:kokoro" not in ids                      # it was listed as a chat model before
    assert ids["tts:kokoro-desktop"]["owned_by"] == "Odysseus voice" and ids["tts:auto"]["owned_by"] == "Odysseus voice"
    assert "Kokoro (desktop)" in ids["tts:auto"]["description"]
    assert gw.catalog("alice") == []                               # the only endpoint left is a speech one
    with pytest.raises(LookupError):
        gw.resolve("kokoro-desktop:kokoro", "alice")               # the auto router resolves through this
    with pytest.raises(LookupError):
        gw.resolve("kokoro", "alice")


def test_a_chat_completion_for_a_speech_model_is_a_404(desktop):
    r = desktop.post("/v1/chat/completions", headers=H, json={"model": "kokoro-desktop:kokoro", "messages": []})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"


def test_the_auto_router_never_picks_a_speech_model(env, monkeypatch):
    """The router names its candidates in gateway_router.json and reaches them through gw.resolve. A speech model named
    there (strong model gone wrong, or the only endpoint added) is "not available", so the next candidate is used."""
    import asyncio
    from src import gateway_router as ar
    _add_endpoint(env, id="ep-desk", name="Kokoro (desktop)", base_url="http://100.102.86.125:8880/v1",
                  cached_models=json.dumps(["kokoro"]))
    cfg = dict(ar.config(), strong="kokoro-desktop:kokoro", fast="kokoro-desktop:kokoro", chat=None,
               fallback=["local-ollama:qwen3:8b"])
    monkeypatch.setattr(ar, "config", lambda: cfg)
    monkeypatch.setattr(ar, "_log", lambda entry: None)
    monkeypatch.setattr(ar, "_reachable", lambda url: True)
    payload = {"model": "auto:hard", "messages": [{"role": "user", "content": "fix the crash"}]}
    out, decision = asyncio.run(ar.route(payload, "alice", complete=None, supports_images=None))
    assert out["model"] == "local-ollama:qwen3:8b"
    assert "kokoro-desktop:kokoro is not available" in decision["reason"]


def test_chat_models_on_a_mixed_endpoint_stay_chat_models(env):
    _add_endpoint(env, id="ep-mix", name="Mixed", base_url="http://10.0.0.30/v1",
                  cached_models=json.dumps(["gpt-4o", "tts-1", "gpt-4o-mini-tts", "kokoro"]))
    assert [i["id"] for i in gw.catalog("alice") if i["id"].startswith("mixed:")] == ["mixed:gpt-4o"]
    assert "tts:mixed" in _voice_ids(env)
    say(env, "tts:mixed")
    assert env.posted[-1]["json"]["model"] == "tts-1"              # a tts-* name first
