"""Local text to speech: Kokoro-82M through kokoro-onnx (no torch).

Asked for 2026-10-01: a nicer-sounding voice than the browser's. The old
"local" provider needed torch + CUDA, which this server can't run. Kokoro is
mocked here; test_real_kokoro_* runs it and skips unless kokoro-onnx and the
model files are there (ODYSSEUS_TTS_REAL_MODELS=<data dir with models/tts>).
"""
import io
import os
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from services.tts import kokoro_local as kl
from services.tts.kokoro_local import TTSError
from services.tts.tts_service import TTSService


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def _installed(monkeypatch, on=True):
    real = kl._has_module
    monkeypatch.setattr(kl, "_has_module", lambda m: on if m in ("kokoro_onnx", "onnxruntime") else real(m))


def _fake_files(model="kokoro-v1.0"):
    d = kl.models_dir()
    d.mkdir(parents=True, exist_ok=True)
    for spec in (kl.KOKORO_MODELS[model], kl.VOICES_FILE):
        with open(d / spec["file"], "wb") as f:
            f.truncate(spec["bytes"])  # sparse: right size, no disk


class _FakeKokoro:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    def create(self, text, voice, speed=1.0, lang="en-us"):
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        if voice == "xx_bad":
            raise KeyError(f"Voice {voice} not found in available voices")
        time.sleep(self.delay)
        return np.zeros(int(24000 * 0.5), dtype=np.float32), 24000


def _service(monkeypatch, tmp_path, **settings):
    s = TTSService(cache_dir=str(tmp_path / "cache"))
    base = {"tts_enabled": True, "tts_provider": "local", "tts_model": "tts-1", "tts_voice": "alloy",
            "tts_speed": "1", "tts_kokoro_model": ""}
    base.update(settings)
    monkeypatch.setattr(s, "_load_settings", lambda: dict(base))
    return s


def _with_fake(monkeypatch, fake):
    loads = []
    monkeypatch.setattr(kl, "load_kokoro", lambda m, v: (loads.append((m, v)), (fake, "cpu"))[1])
    return loads


# --- Synthesis ------------------------------------------------------------------

def test_local_synthesizes_wav_with_kokoro_voice_and_native_speed(monkeypatch, tmp_path, data_dir):
    _installed(monkeypatch)
    _fake_files()
    monkeypatch.setattr(kl, "cpu_has_vnni", lambda: False)
    s = _service(monkeypatch, tmp_path, tts_voice="bf_emma", tts_speed="1.2")
    fake = _FakeKokoro()
    loads = _with_fake(monkeypatch, fake)
    assert s.available is True
    audio = s.synthesize_checked("Hello there.")
    with wave.open(io.BytesIO(audio)) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1 and w.getsampwidth() == 2
    assert fake.calls[0] == {"text": "Hello there.", "voice": "bf_emma", "speed": 1.2, "lang": "en-gb"}
    assert Path(loads[0][0]).name == "kokoro-v1.0.onnx"
    st = s.get_stats()
    assert st["engine"] == "kokoro" and st["model_loaded"] and st["speed_applied"] is True
    assert st["last"]["audio_seconds"] == 0.5
    # Cached: a second call does not synthesize again.
    s.synthesize_checked("Hello there.")
    assert len(fake.calls) == 1 and len(loads) == 1


def test_openai_default_voice_becomes_af_heart(monkeypatch, tmp_path, data_dir):
    _installed(monkeypatch)
    _fake_files()
    s = _service(monkeypatch, tmp_path, tts_voice="alloy")
    fake = _FakeKokoro()
    _with_fake(monkeypatch, fake)
    s.synthesize_checked("Hi.")
    assert fake.calls[0]["voice"] == "af_heart" and fake.calls[0]["lang"] == "en-us"


def test_preview_overrides_voice_and_speed_and_bad_voice_is_400(monkeypatch, tmp_path, data_dir):
    _installed(monkeypatch)
    _fake_files()
    s = _service(monkeypatch, tmp_path)
    fake = _FakeKokoro()
    _with_fake(monkeypatch, fake)
    s.synthesize_checked("Sample.", voice="am_fenrir", speed=0.9)
    assert fake.calls[-1]["voice"] == "am_fenrir" and fake.calls[-1]["speed"] == 0.9
    with pytest.raises(TTSError) as e:
        s.synthesize_checked("Sample.", voice="xx_bad", use_cache=False)
    assert e.value.status == 400 and "xx_bad" in e.value.message


def test_concurrent_syntheses_are_bounded(monkeypatch, tmp_path, data_dir):
    _installed(monkeypatch)
    _fake_files()
    s = _service(monkeypatch, tmp_path)
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    class Counting(_FakeKokoro):
        def create(self, *a, **kw):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            try:
                return super().create(*a, **kw)
            finally:
                with lock:
                    state["now"] -= 1

    _with_fake(monkeypatch, Counting(delay=0.05))
    ts = [threading.Thread(target=s.synthesize_checked, args=(f"Sentence {i}.",)) for i in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert state["max"] == 1


# --- Readiness and downloads -----------------------------------------------------

def test_missing_package_names_the_pip_command(monkeypatch, tmp_path, data_dir):
    _installed(monkeypatch, on=False)
    s = _service(monkeypatch, tmp_path)
    assert s.available is False
    with pytest.raises(TTSError) as e:
        s.synthesize_checked("Hi.")
    assert "kokoro-onnx" in e.value.message and "pip install kokoro-onnx" in e.value.message
    eng = s.engines()["engines"][0]
    assert eng["installed"] is False and eng["pip"] == "pip install kokoro-onnx"


def test_missing_model_starts_the_download_and_says_so(monkeypatch, tmp_path, data_dir):
    _installed(monkeypatch)
    s = _service(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(s, "start_download", lambda m: started.append(m))
    monkeypatch.setattr(kl, "load_kokoro", lambda *a: pytest.fail("must not load"))
    with pytest.raises(TTSError) as e:
        s.synthesize_checked("Hi.")
    assert started == [kl.default_model()]
    assert "MB" in e.value.message


def test_default_model_follows_the_cpu(monkeypatch):
    monkeypatch.setattr(kl, "_cpu_flags", lambda: "flags : sse4_2 avx avx2")
    assert kl.default_model() == "kokoro-v1.0"
    monkeypatch.setattr(kl, "_cpu_flags", lambda: "flags : avx2 avx512f avx512_vnni")
    assert kl.default_model() == "kokoro-v1.0-int8"
    monkeypatch.setattr(kl, "_cpu_flags", lambda: "flags : avx2 avx_vnni")
    assert kl.default_model() == "kokoro-v1.0-int8"
    assert kl.resolve_model("nonsense") == kl.default_model()


def test_model_status_size_place_and_voices(monkeypatch, tmp_path, data_dir):
    _installed(monkeypatch)
    s = _service(monkeypatch, tmp_path)
    eng = s.engines()["engines"][0]
    m = {x["id"]: x for x in eng["models"]}
    assert m["kokoro-v1.0"]["size_mb"] == 354 and m["kokoro-v1.0-int8"]["size_mb"] == 121
    assert m["kokoro-v1.0"]["path"] == str(data_dir / "models" / "tts" / "kokoro")
    ids = [v["id"] for v in eng["voices"]]
    assert ids[0] == "af_heart" and {"af_bella", "af_nicole", "am_michael", "am_fenrir", "bf_emma", "bm_george"} <= set(ids)
    # A truncated file is not "downloaded".
    _fake_files()
    with open(kl.models_dir() / "voices-v1.0.bin", "r+b") as f:
        f.truncate(10)
    assert kl.model_paths("kokoro-v1.0") is None


def test_download_streams_both_files_with_progress(monkeypatch, tmp_path, data_dir):
    sizes = {spec["file"]: spec["bytes"] for spec in (*kl.KOKORO_MODELS.values(), kl.VOICES_FILE)}
    for k in sizes:
        sizes[k] = 3 << 20
    monkeypatch.setattr(kl, "VOICES_FILE", {**kl.VOICES_FILE, "bytes": sizes["voices-v1.0.bin"]})
    monkeypatch.setitem(kl.KOKORO_MODELS, "kokoro-v1.0-int8", {**kl.KOKORO_MODELS["kokoro-v1.0-int8"], "bytes": sizes["kokoro-v1.0.int8.onnx"]})
    urls = []
    gate = threading.Event()

    class _Resp:
        def __init__(self, url):
            self.url = url
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def raise_for_status(self):
            pass
        def iter_bytes(self, n):
            gate.wait(5)
            for _ in range(3):
                yield b"\0" * (1 << 20)

    import httpx
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: (urls.append(url), _Resp(url))[1])
    d = kl.Downloads()
    j = d.start("kokoro-v1.0-int8")
    assert j["status"] == "downloading" and j["total_bytes"] == 6 << 20
    assert d.start("kokoro-v1.0-int8")["status"] == "downloading"  # one thread only
    gate.set()
    for _ in range(200):
        if (d.state("kokoro-v1.0-int8") or {}).get("status") != "downloading":
            break
        time.sleep(0.01)
    assert d.state("kokoro-v1.0-int8")["status"] == "done"
    assert [u.rsplit("/", 1)[1] for u in urls] == ["kokoro-v1.0.int8.onnx", "voices-v1.0.bin"]
    assert all(u.startswith(kl.RELEASE) for u in urls)
    assert kl.model_paths("kokoro-v1.0-int8") is not None
    assert not list(kl.models_dir().glob("*.part"))
    with pytest.raises(TTSError):
        d.start("nope")


# --- OpenAI-compatible endpoint (Kokoro-FastAPI) ---------------------------------

def _endpoint(monkeypatch, base):
    class _Ep:
        base_url = base
        api_key = ""

    class _Q:
        def filter(self, *a): return self
        def first(self): return _Ep()

    class _Db:
        def query(self, *a): return _Q()
        def close(self): pass

    import src.database as db
    monkeypatch.setattr(db, "SessionLocal", lambda: _Db(), raising=False)
    monkeypatch.setattr(db, "ModelEndpoint", type("ME", (), {"id": 1}), raising=False)


def test_kokoro_fastapi_without_v1_is_found(monkeypatch, tmp_path):
    _endpoint(monkeypatch, "http://gpu-box:8880")
    s = _service(monkeypatch, tmp_path, tts_provider="endpoint:k", tts_model="kokoro", tts_voice="af_heart", tts_speed="1.1")
    seen = []

    class _R:
        def __init__(self, code):
            self.status_code = code
            self.content = b"ID3audio"
            self.text = "nf"

    def post(url, json=None, headers=None, timeout=None):
        seen.append((url, json))
        return _R(404 if url.endswith(":8880/audio/speech") else 200)

    import services.tts.tts_service as mod
    monkeypatch.setattr(mod.httpx, "post", post)
    assert s.synthesize_checked("Hi.") == b"ID3audio"
    assert [u for u, _ in seen] == ["http://gpu-box:8880/audio/speech", "http://gpu-box:8880/v1/audio/speech"]
    body = seen[-1][1]
    assert body == {"model": "kokoro", "input": "Hi.", "voice": "af_heart", "response_format": "mp3", "speed": 1.1}


def test_endpoint_error_is_reported(monkeypatch, tmp_path):
    _endpoint(monkeypatch, "http://gpu-box:8880/v1")
    s = _service(monkeypatch, tmp_path, tts_provider="endpoint:k")

    class _R:
        status_code = 400
        content = b""
        text = '{"detail":"invalid voice"}'

    import services.tts.tts_service as mod
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _R())
    with pytest.raises(TTSError, match="invalid voice"):
        s.synthesize_checked("Hi.")
    assert s.synthesize("Hi.") is None


# --- Routes and UI ------------------------------------------------------------

def _client(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.tts_routes import setup_tts_routes
    app = FastAPI()
    app.include_router(setup_tts_routes(service))
    return TestClient(app)


def test_routes_synthesize_preview_engines_and_errors(monkeypatch, tmp_path, data_dir):
    _installed(monkeypatch)
    _fake_files()
    s = _service(monkeypatch, tmp_path)
    fake = _FakeKokoro()
    _with_fake(monkeypatch, fake)
    c = _client(s)
    r = c.post("/api/tts/synthesize", json={"text": "Hello."})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    r = c.post("/api/tts/preview", json={"voice": "bm_george", "speed": 1.0})
    assert r.status_code == 200 and fake.calls[-1]["voice"] == "bm_george"
    assert c.get("/api/tts/engines").json()["engines"][0]["provider"] == "local"
    assert c.get("/api/tts/stats").json()["engine"] == "kokoro"
    _installed(monkeypatch, on=False)
    r = c.post("/api/tts/synthesize", json={"text": "Hello again."})
    assert r.status_code == 503 and "pip install kokoro-onnx" in r.json()["detail"]["message"]


def test_download_route_is_admin_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    s = _service(monkeypatch, tmp_path)
    assert _client(s).post("/api/tts/download", json={"model": "kokoro-v1.0"}).status_code == 403


def test_no_torch_and_ui_wiring():
    root = Path(__file__).resolve().parent.parent
    for f in ("services/tts/tts_service.py", "services/tts/kokoro_local.py"):
        assert "import torch" not in (root / f).read_text()
    html = (root / "static" / "index.html").read_text()
    for id_ in ("set-vcTtsModel", "set-vcTtsDownload", "set-vcVoicePreview", "set-vcSpeed"):
        assert f'id="{id_}"' in html
    assert '<script type="module" src="/static/js/ttsEngines.js"></script>' in html
    assert "'/static/js/ttsEngines.js'" in (root / "static" / "sw.js").read_text()
    vc = (root / "static" / "js" / "voiceCall.js").read_text()
    assert "const rate = 1;" in vc  # the server applies the speed now


def test_voice_is_a_dropdown_with_male_voices_first():
    """Reported 2026-10-01: the Voice box was a text field with a datalist, which
    Chrome filters by what is typed, so with a voice set it listed nothing. It is a
    real dropdown now (male voices first, by accent), with "Other..." for any name;
    the hidden #set-vcVoice text box stays the saved value."""
    root = Path(__file__).resolve().parent.parent
    html = (root / "static" / "index.html").read_text()
    assert '<select id="set-vcVoiceSelect"' in html
    assert 'id="set-vcVoice"' in html and 'list="set-vcVoiceList"' not in html
    vc = (root / "static" / "js" / "voiceCall.js").read_text()
    order = ["'Male, American'", "'Male, British'", "'Female, American'", "'Female, British'"]
    at = [vc.index(g) for g in order]
    assert at == sorted(at)
    assert "voice.dispatchEvent(new Event('change'))" in vc and "Other..." in vc
    kokoro = vc[vc.index('const KOKORO_VOICES'):]
    assert kokoro.index("'am_michael'") < kokoro.index("'af_heart'")


# --- Real Kokoro (skipped unless installed and downloaded) -----------------------

_REAL = os.getenv("ODYSSEUS_TTS_REAL_MODELS", "")


@pytest.mark.skipif(not _REAL, reason="set ODYSSEUS_TTS_REAL_MODELS to a data dir with models/tts/kokoro")
def test_real_kokoro_speaks(monkeypatch, tmp_path):
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", _REAL)
    if kl.missing_packages():
        pytest.skip("kokoro-onnx not installed")
    model = kl.default_model()
    if kl.model_paths(model) is None:
        pytest.skip(f"{model} not downloaded under {_REAL}")
    s = _service(monkeypatch, tmp_path, tts_voice="af_heart")
    audio = s.synthesize_checked("Hello, this is a test of the local voice.", use_cache=False)
    with wave.open(io.BytesIO(audio)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        assert w.getnframes() / w.getframerate() > 1.0
    assert float(np.sqrt((x ** 2).mean())) > 0.01
