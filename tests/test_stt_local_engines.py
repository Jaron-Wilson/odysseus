"""Local speech to text: Whisper (faster-whisper) and Parakeet (onnx-asr).

Asked for 2026-10-01: "can we get whisper on the server to work? and then a
different vtt also? maybe a local model?" The engines are mocked here, so
the suite needs neither package nor a model. test_real_* at the bottom runs
the real engines and skips unless the packages and a downloaded model are
there (ODYSSEUS_STT_REAL_MODELS=<data dir with models/stt>).
"""
import io
import os
import struct
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from services.stt import local_models as lm
from services.stt import stt_service as svc_mod
from services.stt.audio import decode_to_16k_mono, resample
from services.stt.local_models import STTError
from services.stt.stt_service import STTService


def _wav(seconds=1.0, rate=16000, channels=1, freq=440.0, width=2):
    n = int(seconds * rate)
    t = np.arange(n) / rate
    x = 0.5 * np.sin(2 * np.pi * freq * t)
    if channels > 1:
        x = np.repeat(x[:, None], channels, axis=1).reshape(-1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            w.writeframes((x * 32767).astype("<i2").tobytes())
        else:
            w.writeframes(((x * 127) + 128).astype(np.uint8).tobytes())
    return buf.getvalue()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    # Not this machine's Hugging Face cache either.
    monkeypatch.setattr(lm, "_hf_cache_dir", lambda repo: None)
    return tmp_path


def _fake_model(data_dir, engine, model):
    folder = lm.model_dir(engine, model)
    folder.mkdir(parents=True, exist_ok=True)
    files = lm._WHISPER_REQUIRED if engine == "whisper" else lm.PARAKEET_MODELS[model]["files"]
    for f in files:
        (folder / f).write_bytes(b"x")
    return folder


def _service(monkeypatch, **settings):
    s = STTService()
    base = {"stt_enabled": True, "stt_provider": "local", "stt_model": "base.en",
            "stt_parakeet_model": "", "stt_language": ""}
    base.update(settings)
    monkeypatch.setattr(s, "_load_settings", lambda: dict(base))
    return s


def _installed(monkeypatch, *mods):
    real = lm._has_module
    monkeypatch.setattr(lm, "_has_module", lambda m: m in mods or (m not in
                        ("faster_whisper", "onnx_asr", "onnxruntime", "huggingface_hub") and real(m)))


class _Seg:
    def __init__(self, text):
        self.text = text


class _FakeWhisper:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        return iter([_Seg(" hello"), _Seg(" world ")]), type("I", (), {"language": "en"})()


class _FakeParakeet:
    def __init__(self):
        self.calls = []

    def recognize(self, audio, sample_rate=16000):
        self.calls.append((audio, sample_rate))
        return "Hello, world."


# --- Provider dispatch -------------------------------------------------------

def test_local_provider_runs_whisper_with_live_settings(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    _fake_model(data_dir, "whisper", "base.en")
    s = _service(monkeypatch, stt_provider="local")
    fake = _FakeWhisper()
    monkeypatch.setattr(s, "_load_whisper", lambda path: (fake, "cpu"))
    assert s.available is True
    assert s.transcribe_checked(_wav(1.0)) == "hello world"
    audio, kw = fake.calls[0]
    assert isinstance(audio, np.ndarray) and audio.dtype == np.float32 and audio.size == 16000
    assert kw["beam_size"] == 1
    assert "language" not in kw
    st = s.get_stats()
    assert st["engine"] == "whisper" and st["model"] == "base.en"
    assert st["model_loaded"] is True and st["device"] == "cpu"
    assert st["last_latency_ms"] >= 0 and st["last"]["audio_seconds"] == 1.0


def test_parakeet_provider_dispatches_to_parakeet(monkeypatch, data_dir):
    _installed(monkeypatch, "onnx_asr", "onnxruntime", "huggingface_hub")
    model = "parakeet-tdt-0.6b-v2-int8"
    _fake_model(data_dir, "parakeet", model)
    s = _service(monkeypatch, stt_provider="local:parakeet", stt_parakeet_model=model)
    fake = _FakeParakeet()
    seen = {}
    monkeypatch.setattr(s, "_load_parakeet", lambda m, path: (seen.setdefault("m", m), (fake, "cpu"))[1])
    monkeypatch.setattr(s, "_load_whisper", lambda path: pytest.fail("whisper must not load"))
    assert s.transcribe_checked(_wav(0.5)) == "Hello, world."
    assert seen["m"] == model
    assert fake.calls[0][1] == 16000
    assert s.get_stats()["engine"] == "parakeet"


def test_language_passthrough_and_en_model_switches_to_multilingual(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    _fake_model(data_dir, "whisper", "base")
    s = _service(monkeypatch, stt_model="base.en", stt_language="de")
    fake = _FakeWhisper()
    loaded = {}
    monkeypatch.setattr(s, "_load_whisper", lambda path: (loaded.setdefault("p", path), (fake, "cpu"))[1])
    s.transcribe_checked(_wav(0.5))
    assert Path(loaded["p"]).name == "base"
    assert fake.calls[0][1]["language"] == "de"
    assert lm.resolve_model("whisper", "small.en", "en-US") == "small.en"
    assert lm.resolve_model("whisper", "", "") == lm.WHISPER_DEFAULT


def test_endpoint_does_not_send_local_whisper_size_names(monkeypatch):
    s = _service(monkeypatch, stt_provider="endpoint:ep1", stt_model="base.en")
    seen = {}
    monkeypatch.setattr(s, "_transcribe_api", lambda audio, ep, model, lang: seen.update(ep=ep, model=model) or "hi")
    assert s.transcribe_checked(b"RIFF") == "hi"
    assert seen == {"ep": "ep1", "model": "whisper-1"}


def test_disabled_and_browser_are_reported_not_transcribed(monkeypatch):
    s = _service(monkeypatch, stt_enabled=False)
    with pytest.raises(STTError, match="off"):
        s.transcribe_checked(b"x")
    s = _service(monkeypatch, stt_provider="browser")
    with pytest.raises(STTError, match="browser"):
        s.transcribe_checked(b"x")
    assert s.transcribe(b"x") is None


# --- Missing packages and models --------------------------------------------

def test_missing_package_names_the_package_and_pip_command(monkeypatch, data_dir):
    _installed(monkeypatch)  # nothing
    s = _service(monkeypatch, stt_provider="local")
    assert s.available is False
    with pytest.raises(STTError) as e:
        s.transcribe_checked(_wav())
    assert "faster-whisper" in e.value.message
    assert "pip install faster-whisper 'av<16'" in e.value.message
    s = _service(monkeypatch, stt_provider="local:parakeet")
    with pytest.raises(STTError) as e:
        s.transcribe_checked(_wav())
    assert "onnx-asr" in e.value.message and "pip install 'onnx-asr[cpu,hub]'" in e.value.message
    eng = {x["id"]: x for x in s.engines()["engines"]}
    assert eng["whisper"]["installed"] is False and eng["whisper"]["pip"].startswith("pip install")
    assert "onnx-asr" in eng["parakeet"]["missing_packages"]


def test_model_not_downloaded_starts_the_download_and_says_so(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    s = _service(monkeypatch, stt_provider="local", stt_model="tiny.en")
    started = []
    monkeypatch.setattr(s.downloads, "start", lambda e, m, on_done=None: started.append((e, m)) or {"status": "downloading"})
    monkeypatch.setattr(s, "_load_whisper", lambda path: pytest.fail("must not load a missing model"))
    assert s.available is False
    with pytest.raises(STTError) as e:
        s.transcribe_checked(_wav())
    assert started == [("whisper", "tiny.en")]
    assert "tiny.en" in e.value.message and "MB" in e.value.message


def test_model_status_reports_size_place_and_progress(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    monkeypatch.setattr(lm, "_hf_cache_dir", lambda repo: None)
    s = _service(monkeypatch)
    eng = {x["id"]: x for x in s.engines()["engines"]}["whisper"]
    m = {x["id"]: x for x in eng["models"]}
    assert m["base.en"]["selected"] and m["base.en"]["recommended"]
    assert m["base.en"]["downloaded"] is False and m["base.en"]["size_mb"] == 148
    assert m["base.en"]["path"].startswith(str(data_dir / "models" / "stt" / "whisper"))
    # A download in flight reports the bytes on disk so far.
    folder = lm.model_dir("whisper", "small.en")
    folder.mkdir(parents=True)
    with s.downloads._lock:
        s.downloads._jobs[("whisper", "small.en")] = {"status": "downloading", "error": "", "base_bytes": 0}
    (folder / "model.bin.incomplete").write_bytes(b"\0" * 48_610_000)
    st = lm.model_status("whisper", "small.en", s.downloads)
    assert st["status"] == "downloading" and 0.09 < st["progress"] < 0.11
    _fake_model(data_dir, "whisper", "base.en")
    eng = {x["id"]: x for x in s.engines()["engines"]}["whisper"]
    assert eng["ready"] is True


def test_download_runs_in_background_into_the_data_dir(monkeypatch, data_dir):
    pytest.importorskip("huggingface_hub")
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    calls = []
    gate = threading.Event()

    def fake_snapshot(repo, local_dir=None, allow_patterns=None):
        calls.append((repo, local_dir, allow_patterns))
        gate.wait(5)
        for f in lm._WHISPER_REQUIRED:
            Path(local_dir, f).write_bytes(b"x")
        return local_dir

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    monkeypatch.setattr(lm, "_hf_cache_dir", lambda repo: None)
    d = lm.Downloads()
    done = threading.Event()
    assert d.start("whisper", "tiny.en", on_done=lambda e, m: done.set())["status"] == "downloading"
    assert d.start("whisper", "tiny.en")["status"] == "downloading"  # no second thread
    gate.set()
    assert done.wait(5)
    assert len(calls) == 1
    repo, local_dir, patterns = calls[0]
    assert repo == "Systran/faster-whisper-tiny.en" and "model.bin" in patterns
    assert Path(local_dir) == data_dir / "models" / "stt" / "whisper" / "tiny.en"
    assert lm.local_path("whisper", "tiny.en") == Path(local_dir)
    with pytest.raises(STTError):
        d.start("whisper", "no-such-model")


def test_parakeet_download_fetches_only_the_chosen_precision(monkeypatch, data_dir):
    pytest.importorskip("huggingface_hub")
    _installed(monkeypatch, "onnx_asr", "onnxruntime", "huggingface_hub")
    seen = {}
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download",
                        lambda repo, local_dir=None, allow_patterns=None: seen.update(p=allow_patterns))
    d = lm.Downloads()
    d.start("parakeet", "parakeet-tdt-0.6b-v2-int8")
    for _ in range(100):
        if d.state("parakeet", "parakeet-tdt-0.6b-v2-int8")["status"] != "downloading":
            break
        time.sleep(0.02)
    assert "encoder-model.int8.onnx" in seen["p"] and "encoder-model.onnx.data" not in seen["p"]
    # The fake wrote nothing, so the job says what went wrong.
    assert d.state("parakeet", "parakeet-tdt-0.6b-v2-int8")["status"] == "error"


# --- Audio --------------------------------------------------------------------

def test_wav_is_decoded_without_pyav_and_resampled_to_16k_mono(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_av(name, *a, **kw):
        if name == "av":
            raise ImportError("no av")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_av)
    x = decode_to_16k_mono(_wav(1.0, rate=48000, channels=2))
    assert x.dtype == np.float32 and abs(x.size - 16000) <= 1
    assert 0.4 < float(np.abs(x).max()) < 0.6
    x8 = decode_to_16k_mono(_wav(0.5, rate=8000, width=1))
    assert abs(x8.size - 8000) <= 1
    # Not a WAV and no PyAV: say which package.
    with pytest.raises(STTError, match=r"pip install 'av<16'"):
        decode_to_16k_mono(b"\x1aE\xdf\xa3" + b"\0" * 64)


def test_resample_keeps_speech_band_and_removes_aliases():
    rate = 48000
    t = np.arange(rate) / rate
    low = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    high = np.sin(2 * np.pi * 15000 * t).astype(np.float32)  # above 8 kHz: must not fold down
    assert np.abs(resample(low, rate)[200:-200]).max() > 0.9
    assert np.abs(resample(high, rate)[200:-200]).max() < 0.1
    assert resample(low, 16000) is not None and resample(low, 16000).size == low.size


def test_parakeet_gets_16k_samples_from_a_48k_upload(monkeypatch, data_dir):
    _installed(monkeypatch, "onnx_asr", "onnxruntime", "huggingface_hub")
    model = "parakeet-tdt-0.6b-v2"
    _fake_model(data_dir, "parakeet", model)
    s = _service(monkeypatch, stt_provider="local:parakeet", stt_parakeet_model=model)
    fake = _FakeParakeet()
    monkeypatch.setattr(s, "_load_parakeet", lambda m, path: (fake, "cpu"))
    s.transcribe_checked(_wav(2.0, rate=48000))
    audio, rate = fake.calls[0]
    assert rate == 16000 and abs(audio.size - 32000) <= 1


# --- Loading and concurrency ---------------------------------------------------

def test_model_loads_once_and_is_kept(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    _fake_model(data_dir, "whisper", "base.en")
    s = _service(monkeypatch)
    loads = []
    monkeypatch.setattr(s, "_load_whisper", lambda path: (loads.append(path), (_FakeWhisper(), "cpu"))[1])
    for _ in range(3):
        s.transcribe_checked(_wav(0.3))
    assert len(loads) == 1


def test_concurrent_transcriptions_are_bounded(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    _fake_model(data_dir, "whisper", "base.en")
    s = _service(monkeypatch)
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    class Slow(_FakeWhisper):
        def transcribe(self, audio, **kw):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return super().transcribe(audio, **kw)

    monkeypatch.setattr(s, "_load_whisper", lambda path: (Slow(), "cpu"))
    threads = [threading.Thread(target=s.transcribe_checked, args=(_wav(0.3),)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert state["max"] == 1


def test_a_full_queue_answers_busy(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    _fake_model(data_dir, "whisper", "base.en")
    s = _service(monkeypatch)
    s._busy_timeout = 0.05
    monkeypatch.setattr(s, "_load_whisper", lambda path: (_FakeWhisper(), "cpu"))
    s._sem.acquire()
    try:
        with pytest.raises(STTError, match="busy"):
            s.transcribe_checked(_wav(0.3))
    finally:
        s._sem.release()


def test_warm_loads_in_the_background(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    _fake_model(data_dir, "whisper", "base.en")
    s = _service(monkeypatch)
    gate = threading.Event()
    monkeypatch.setattr(s, "_load_whisper", lambda path: (gate.wait(5), (_FakeWhisper(), "cpu"))[1])
    assert s.warm() is True
    assert s.get_stats()["loading"] is True
    assert s.warm() is False  # already loading
    gate.set()
    for _ in range(100):
        if s.get_stats()["model_loaded"]:
            break
        time.sleep(0.02)
    assert s.get_stats()["model_loaded"] is True


def test_no_torch_import(monkeypatch, data_dir):
    """torch can SIGILL a CPU without AVX; CUDA is probed via CTranslate2."""
    src = Path(svc_mod.__file__).read_text()
    assert "import torch" not in src


# --- Routes -------------------------------------------------------------------

def _client(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.stt_routes import setup_stt_routes
    app = FastAPI()
    app.include_router(setup_stt_routes(service))
    return TestClient(app)


def test_transcribe_route_returns_the_specific_message(monkeypatch, data_dir):
    _installed(monkeypatch)
    s = _service(monkeypatch, stt_provider="local:parakeet")
    r = _client(s).post("/api/stt/transcribe", files={"file": ("u.wav", _wav(), "audio/wav")})
    assert r.status_code == 503
    assert "pip install 'onnx-asr[cpu,hub]'" in r.json()["detail"]["message"]
    assert "not available or set to browser mode" not in r.text


def test_routes_transcribe_engines_and_stats(monkeypatch, data_dir):
    _installed(monkeypatch, "faster_whisper", "huggingface_hub")
    _fake_model(data_dir, "whisper", "base.en")
    s = _service(monkeypatch)
    monkeypatch.setattr(s, "_load_whisper", lambda path: (_FakeWhisper(), "cpu"))
    c = _client(s)
    r = c.post("/api/stt/transcribe", files={"file": ("u.wav", _wav(), "audio/wav")})
    assert r.status_code == 200 and r.json() == {"text": "hello world"}
    st = c.get("/api/stt/stats").json()
    assert st["engine"] == "whisper" and st["model_loaded"] is True and "last_latency_ms" in st
    eng = c.get("/api/stt/engines").json()
    assert [e["provider"] for e in eng["engines"]] == ["local", "local:parakeet"]


def test_download_route_is_admin_only(monkeypatch, data_dir):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    s = _service(monkeypatch)
    r = _client(s).post("/api/stt/download", json={"engine": "whisper", "model": "tiny.en"})
    assert r.status_code == 403


# --- UI wiring -----------------------------------------------------------------

def test_settings_offer_both_local_engines_and_the_download_ui():
    root = Path(__file__).resolve().parent.parent / "static"
    html = (root / "index.html").read_text()
    assert '<option value="local:parakeet">' in html
    assert 'id="set-vcSttModel"' in html and 'id="set-vcSttDownload"' in html
    assert '<script type="module" src="/static/js/sttEngines.js"></script>' in html
    assert "'/static/js/sttEngines.js'" in (root / "sw.js").read_text()
    vc = (root / "js" / "voiceCall.js").read_text()
    assert "p.startsWith('local:')" in vc
    rec = (root / "js" / "voiceRecorder.js").read_text()
    assert "provider.startsWith('local:')" in rec


# --- Real engines (skipped unless installed and downloaded) ---------------------

_REAL = os.getenv("ODYSSEUS_STT_REAL_MODELS", "")
_CLIP = os.getenv("ODYSSEUS_STT_REAL_CLIP", "")


@pytest.mark.skipif(not (_REAL and _CLIP), reason="set ODYSSEUS_STT_REAL_MODELS and ODYSSEUS_STT_REAL_CLIP")
@pytest.mark.parametrize("engine,provider,key,model", [
    ("whisper", "local", "stt_model", "base.en"),
    ("parakeet", "local:parakeet", "stt_parakeet_model", "parakeet-tdt-0.6b-v2-int8"),
])
def test_real_engine_transcribes_a_clip(monkeypatch, engine, provider, key, model):
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", _REAL)
    if lm.missing_packages(engine):
        pytest.skip(f"{engine} packages not installed")
    if lm.local_path(engine, model) is None:
        pytest.skip(f"{model} not downloaded under {_REAL}")
    s = _service(monkeypatch, stt_provider=provider, **{key: model})
    text = s.transcribe_checked(Path(_CLIP).read_bytes())
    assert "country" in text.lower()
