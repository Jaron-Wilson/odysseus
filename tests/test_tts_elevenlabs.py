"""ElevenLabs text to speech (services/tts/elevenlabs.py + TTSService).

Every HTTP call is faked; no real key and no network. Covers the request
shape, WAV wrapping for calls, the cache (a repeat costs no credits), error
mapping, the credit guard and its fallback, the key never showing up in an
answer or a log line, and parsing of voices and the subscription.
"""
import io
import json
import logging
import wave

import httpx
import pytest

from services.tts import elevenlabs as el
from services.tts.kokoro_local import TTSError
from services.tts.tts_service import TTSService

KEY = "sk_test_0123456789abcdefSECRET"
MP3 = b"ID3" + b"\x00" * 64
PCM = b"\x01\x00\x02\x00" * 400  # 800 samples


class FakeHTTP:
    """Stands in for httpx.post / httpx.get; records each request."""

    def __init__(self):
        self.calls = []
        self.synth_status = 200
        self.synth_body = None
        self.sub = {"tier": "creator", "status": "active", "character_count": 10_000,
                    "character_limit": 131_000, "next_character_count_reset_unix": 1_790_000_000,
                    "voice_limit": 30}
        self.sub_status = 200
        self.voice_pages = [
            {"voices": [
                {"voice_id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "category": "premade",
                 "labels": {"accent": "british"}, "preview_url": "https://x/p.mp3"},
                {"voice_id": "myclone1", "name": "Jaron", "category": "cloned", "labels": {}},
            ], "has_more": True, "next_page_token": "t2"},
            {"voices": [{"voice_id": "gen1", "name": "Aria", "category": "generated"}],
             "has_more": False, "next_page_token": None},
        ]

    def _resp(self, status, url, content=b"", js=None, headers=None):
        req = httpx.Request("GET", url)
        if js is not None:
            return httpx.Response(status, json=js, request=req, headers=headers or {})
        return httpx.Response(status, content=content, request=req, headers=headers or {})

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, params, json, headers, timeout))
        if self.synth_status != 200:
            return self._resp(self.synth_status, url, js=self.synth_body or {})
        fmt = (params or {}).get("output_format", "")
        return self._resp(200, url, content=PCM if fmt.startswith("pcm_") else MP3)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, None, headers, timeout))
        if url.endswith("/v1/user/subscription"):
            if self.sub_status != 200:
                return self._resp(self.sub_status, url, js={"detail": {"code": "invalid_api_key"}})
            return self._resp(200, url, js=self.sub)
        if url.endswith("/v2/voices"):
            page = 1 if (params or {}).get("next_page_token") == "t2" else 0
            return self._resp(200, url, js=self.voice_pages[page])
        return self._resp(404, url, js={})

    def synth_calls(self):
        return [c for c in self.calls if c[0] == "POST"]


class FakeKokoro:
    def __init__(self, ready=True):
        self.ready = ready
        self.said = []

    def readiness(self, model):
        return None if self.ready else "Kokoro is not downloaded."

    def synthesize(self, text, model, voice, speed):
        self.said.append(text)
        return b"RIFF" + b"\x00" * 40


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(el.httpx, "post", fake.post)
    monkeypatch.setattr(el.httpx, "get", fake.get)
    monkeypatch.setenv(el.BASE_URL_ENV, "http://fake-eleven")
    return fake


def _service(tmp_path, monkeypatch, settings=None, key=KEY, kokoro_ready=True):
    svc = TTSService(cache_dir=str(tmp_path / "cache"))
    s = {"tts_enabled": True, "tts_provider": "elevenlabs", "tts_model": "tts-1", "tts_voice": "alloy",
         "tts_speed": "1", "tts_kokoro_model": "", "tts_elevenlabs_model": "eleven_flash_v2_5",
         "tts_elevenlabs_min_credits_pct": 5, "tts_elevenlabs_min_credits": 0,
         "tts_elevenlabs_max_reply_chars": 1500}
    s.update(settings or {})
    monkeypatch.setattr(svc, "_load_settings", lambda: dict(s))
    svc._eleven = el.ElevenLabsClient(key_getter=lambda: key, tally=el.UsageTally(str(tmp_path / "usage.json")))
    kok = FakeKokoro(kokoro_ready)
    monkeypatch.setattr(svc, "_get_kokoro", lambda: kok)
    monkeypatch.setattr(svc, "_kokoro_model", lambda settings: "kokoro-int8")
    svc.kok = kok
    return svc


# ── Request shape ──

def test_synth_request_shape(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, {"tts_voice": "myclone1", "tts_speed": "1.1"})
    audio = svc.synthesize_checked("Hello there.")
    assert audio == MP3
    method, url, params, body, headers, timeout = http.synth_calls()[0]
    assert url == "http://fake-eleven/v1/text-to-speech/myclone1"
    assert params == {"output_format": "mp3_44100_128"}
    assert body == {"text": "Hello there.", "model_id": "eleven_flash_v2_5", "voice_settings": {"speed": 1.1}}
    assert headers["xi-api-key"] == KEY
    assert timeout is not None


def test_default_voice_and_no_speed_at_1x(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)  # tts_voice "alloy" is another engine's default
    svc.synthesize_checked("Hi.")
    _, url, _, body, _, _ = http.synth_calls()[0]
    assert url.endswith("/" + el.DEFAULT_VOICE)
    assert "voice_settings" not in body


def test_speed_clamped_and_skipped_for_v4(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, {"tts_speed": "2"})
    svc.synthesize_checked("Fast talk.")
    assert http.synth_calls()[0][3]["voice_settings"]["speed"] == el.SPEED_MAX
    svc2 = _service(tmp_path / "b", monkeypatch, {"tts_speed": "1.2", "tts_elevenlabs_model": "eleven_v4"})
    svc2.synthesize_checked("Fast talk.")
    assert "voice_settings" not in http.synth_calls()[1][3]


def test_wav_request_is_pcm_wrapped(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    audio = svc.synthesize_checked("Call audio.", response_format="wav")
    assert http.synth_calls()[0][2] == {"output_format": "pcm_16000"}
    assert audio[:4] == b"RIFF"
    with wave.open(io.BytesIO(audio)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 800)
    # The phone codec can read it.
    from src.telephony import codec
    samples, rate = codec.decode_audio(audio)
    assert rate == 16000 and len(samples) == 800


def test_per_reply_cap(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, {"tts_elevenlabs_max_reply_chars": 40})
    svc.synthesize_checked("First sentence is here. Second sentence goes past the cap for sure.")
    assert http.synth_calls()[0][3]["text"] == "First sentence is here."


# ── Cache ──

def test_cache_hit_costs_nothing(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    a = svc.synthesize_checked("Same phrase.")
    b = svc.synthesize_checked("Same phrase.")
    assert a == b and len(http.synth_calls()) == 1
    tally = svc.elevenlabs.tally.snapshot()
    assert tally["chars"] == len("Same phrase.") and tally["requests"] == 1
    assert tally["credits"] == pytest.approx(len("Same phrase.") * 0.5)
    # WAV and MP3 of one phrase are cached apart.
    svc.synthesize_checked("Same phrase.", response_format="wav")
    svc.synthesize_checked("Same phrase.", response_format="wav")
    assert len(http.synth_calls()) == 2


def test_cache_hit_even_when_guard_tripped(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc.synthesize_checked("Cached line.")
    http.sub["character_count"] = 130_000
    svc.elevenlabs.forget()
    assert svc.synthesize_checked("Cached line.") == MP3
    assert len(http.synth_calls()) == 1 and not svc.kok.said


# ── Errors ──

@pytest.mark.parametrize("status,body,want_status,want_text", [
    (401, {"detail": {"type": "authentication_error", "code": "invalid_api_key", "message": "bad"}}, 400, "rejected the API key"),
    (429, {"detail": {"type": "rate_limit_error", "code": "rate_limit_exceeded"}}, 429, "busy"),
    (404, {"detail": {"type": "not_found", "code": "voice_not_found"}}, 404, "no voice with id"),
    (400, {"detail": {"type": "validation_error", "code": "invalid_voice_id"}}, 404, "no voice with id"),
    (422, {"detail": [{"loc": ["body", "text"], "msg": "field required", "type": "missing"}]}, 400, "field required"),
    (500, {"detail": {"message": "boom"}}, 502, "answered 500"),
])
def test_error_mapping(http, tmp_path, monkeypatch, status, body, want_status, want_text):
    svc = _service(tmp_path, monkeypatch)
    http.synth_status, http.synth_body = status, body
    with pytest.raises(TTSError) as ei:
        svc.synthesize_checked("Hello.")
    assert ei.value.status == want_status
    assert want_text in ei.value.message
    assert KEY not in ei.value.message


def test_timeout_maps_to_tts_error(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(el.httpx, "post", boom)
    monkeypatch.setattr(el.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    svc = _service(tmp_path, monkeypatch)
    with pytest.raises(TTSError) as ei:
        svc.synthesize_checked("Hello.")
    assert ei.value.status == 504


def test_no_key_is_a_clear_error(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, key="")
    with pytest.raises(TTSError) as ei:
        svc.synthesize_checked("Hello.")
    assert "API key" in ei.value.message
    assert svc.available is False
    assert not http.synth_calls()


# ── Credit guard and fallback ──

def test_guard_falls_back_to_kokoro(http, tmp_path, monkeypatch):
    http.sub["character_count"] = 126_000  # 5,000 of 131,000 left = 3.8%
    svc = _service(tmp_path, monkeypatch)
    audio = svc.synthesize_checked("Low on credits.")
    assert audio[:4] == b"RIFF" and svc.kok.said == ["Low on credits."]
    assert not http.synth_calls()
    fb = svc.current_fallback()
    assert fb["to"] == "local" and "below 5%" in fb["reason"]
    stats = svc.get_stats()
    assert stats["effective_provider"] == "local" and stats["guard"]["tripped"]


def test_guard_without_kokoro_hands_to_browser(http, tmp_path, monkeypatch):
    http.sub["character_count"] = 126_000
    svc = _service(tmp_path, monkeypatch, kokoro_ready=False)
    with pytest.raises(TTSError) as ei:
        svc.synthesize_checked("Low on credits.")
    assert ei.value.status == 409 and getattr(ei.value, "fallback", "") == "browser"
    assert svc.get_stats()["effective_provider"] == "browser"
    from src.telephony import call
    monkeypatch.setattr("services.tts.get_tts_service", lambda: svc)
    assert any("Kokoro is not ready" in p for p in call.engines_ready())


def test_guard_absolute_floor(http, tmp_path, monkeypatch):
    http.sub["character_count"] = 100_000  # 31,000 left, above 5%
    svc = _service(tmp_path, monkeypatch, {"tts_elevenlabs_min_credits": 50_000})
    svc.synthesize_checked("Floor.")
    assert svc.kok.said == ["Floor."]


def test_guard_off_when_plenty(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc.synthesize_checked("Plenty.")
    assert len(http.synth_calls()) == 1 and svc.current_fallback() is None


def test_local_spend_moves_cached_credits(http, tmp_path, monkeypatch):
    http.sub["character_count"] = 124_000  # 7,000 left = 5.3%
    svc = _service(tmp_path, monkeypatch)
    svc.synthesize_checked("x" * 1400 + ".")  # 0.5 credits/char -> 700 credits
    c = svc.elevenlabs.credits()
    assert c["estimated"] and c["remaining"] == 7000 - 700
    svc.synthesize_checked("Next one.")  # now below 5%: Kokoro
    assert svc.kok.said == ["Next one."]


def test_quota_exceeded_falls_back_and_pauses(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    http.synth_status = 401
    http.synth_body = {"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}
    audio = svc.synthesize_checked("Out of credits.")
    assert audio[:4] == b"RIFF" and svc.current_fallback()["to"] == "local"
    http.synth_status = 200
    svc.synthesize_checked("Still paused.")
    assert len(http.synth_calls()) == 1  # no second paid try within the pause


# ── Key handling ──

def test_key_never_in_answers_or_logs(http, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    svc = _service(tmp_path, monkeypatch)
    saved = {}
    monkeypatch.setattr(el, "save_api_key", lambda k: saved.setdefault("k", k))
    out = svc.set_elevenlabs_key("  " + KEY + "  ")
    assert saved["k"] == KEY
    svc.synthesize_checked("Log check.")
    status = svc.elevenlabs_status(refresh=True)
    stats = svc.get_stats()
    blob = json.dumps([out, status, stats], default=str)
    assert KEY not in blob and KEY[-12:] not in blob
    assert status["key_hint"] == "..." + KEY[-4:]
    assert KEY not in caplog.text


def test_bad_key_is_not_saved(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    http.sub_status = 401
    monkeypatch.setattr(el, "save_api_key", lambda k: pytest.fail("saved a rejected key"))
    with pytest.raises(TTSError) as ei:
        svc.set_elevenlabs_key("sk_wrong_key_123456")
    assert ei.value.status == 400 and isinstance(ei.value, el.InvalidKey)


def test_key_round_trip_encrypted(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    el.save_api_key(KEY)
    raw = (tmp_path / "api_keys.json").read_text()
    assert KEY not in raw
    assert el.get_api_key() == KEY
    el.delete_api_key()
    assert el.get_api_key() == ""


def test_routes_never_return_key(http, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import tts_routes
    monkeypatch.setattr(tts_routes, "require_admin", lambda request: None)
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(el, "save_api_key", lambda k: None)
    app = FastAPI()
    app.include_router(tts_routes.setup_tts_routes(svc))
    c = TestClient(app)
    http.sub_status = 401
    r = c.post("/api/tts/elevenlabs/key", json={"api_key": "sk_wrong_key_123456"})
    assert r.status_code == 400  # never 401: the app signs out on any 401
    http.sub_status = 200
    r = c.post("/api/tts/elevenlabs/key", json={"api_key": KEY})
    assert r.status_code == 200 and KEY not in r.text
    for path in ("/api/tts/elevenlabs/status?refresh=1", "/api/tts/stats", "/api/tts/elevenlabs/voices"):
        r = c.get(path)
        assert r.status_code == 200 and KEY not in r.text, path
    r = c.post("/api/tts/synthesize", json={"text": "Route test."})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    http.sub["character_count"] = 130_000
    svc.elevenlabs.forget()
    r = c.post("/api/tts/synthesize", json={"text": "Now falls back."})
    assert r.headers.get("X-TTS-Fallback") == "local"
    svc.kok.ready = False
    r = c.post("/api/tts/synthesize", json={"text": "Browser now."})
    assert r.status_code == 409 and r.json()["detail"]["fallback"] == "browser"


# ── Parsing ──

def test_voices_parsed_paged_and_sorted(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    voices = svc.elevenlabs.voices()
    assert [v["name"] for v in voices] == ["Jaron", "Aria", "George"]  # cloned, generated, premade
    assert voices[2] == {"voice_id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "category": "premade",
                         "labels": {"accent": "british"}, "preview_url": "https://x/p.mp3"}
    gets = [c for c in http.calls if c[1].endswith("/v2/voices")]
    assert gets[0][2]["page_size"] == 100 and gets[1][2]["next_page_token"] == "t2"
    svc.elevenlabs.voices()
    assert len([c for c in http.calls if c[1].endswith("/v2/voices")]) == 2  # cached


def test_subscription_parsed(http, tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    status = svc.elevenlabs_status(refresh=True)
    c = status["credits"]
    assert c["known"] and c["used"] == 10_000 and c["limit"] == 131_000 and c["remaining"] == 121_000
    assert c["remaining_pct"] == pytest.approx(92.4) and c["reset_unix"] == 1_790_000_000
    assert c["tier"] == "creator" and not c["estimated"]
    assert status["model"] == "eleven_flash_v2_5"
    assert any(m["id"] == "eleven_flash_v2_5" and m["credits_per_char"] == 0.5 for m in status["models"])


def test_subscription_unreadable_is_not_fatal(http, tmp_path, monkeypatch):
    http.sub_status = 401
    svc = _service(tmp_path, monkeypatch)
    assert svc.elevenlabs.credits()["known"] is False
    assert svc.synthesize_checked("Still speaks.") == MP3


def test_pcm_to_wav_odd_length():
    w = el.pcm_to_wav(b"\x01\x02\x03", 24000)
    with wave.open(io.BytesIO(w)) as f:
        assert f.getframerate() == 24000 and f.getnframes() == 1


def test_resolve_voice_keeps_ids_with_underscores():
    assert el.resolve_voice("clone_jaron") == "clone_jaron"
    assert el.resolve_voice("af_heart") == el.DEFAULT_VOICE
    assert el.resolve_voice("bm_george+af_bella") == el.DEFAULT_VOICE
    assert el.resolve_voice("alloy") == el.DEFAULT_VOICE
    assert el.resolve_voice("") == el.DEFAULT_VOICE
