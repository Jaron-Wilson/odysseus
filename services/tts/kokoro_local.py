"""Kokoro-82M text to speech on this machine, through kokoro-onnx.

kokoro-onnx runs the Kokoro model on onnxruntime with espeak-ng for
phonemes, so there is no torch (a torch import can crash the whole server
on a CPU without AVX). Provider string "local", as before.

Model files come from the kokoro-onnx GitHub release and are kept in
DATA_DIR/models/tts/kokoro so they show up in Settings with their size:

  kokoro-v1.0.onnx       full precision, 326 MB
  kokoro-v1.0.int8.onnx  int8, 92 MB
  voices-v1.0.bin        every voice, 28 MB (needed by both)
"""

from __future__ import annotations

import importlib.util
import io
import logging
import os
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
VOICES_FILE = {"file": "voices-v1.0.bin", "bytes": 28_214_398}

KOKORO_MODELS: Dict[str, Dict[str, Any]] = {
    "kokoro-v1.0": {"label": "Kokoro v1.0, full precision", "file": "kokoro-v1.0.onnx", "bytes": 325_532_387},
    "kokoro-v1.0-int8": {"label": "Kokoro v1.0, int8", "file": "kokoro-v1.0.int8.onnx", "bytes": 92_361_271},
}

# The good English voices, best first (Kokoro's own grades: af_heart and
# af_bella are its A-grade voices). Every voice in voices-v1.0.bin works; this
# is what the picker offers.
VOICES: List[Dict[str, str]] = [
    {"id": "af_heart", "label": "Heart (US, female)"},
    {"id": "af_bella", "label": "Bella (US, female)"},
    {"id": "af_nicole", "label": "Nicole (US, female, soft)"},
    {"id": "af_aoede", "label": "Aoede (US, female)"},
    {"id": "af_kore", "label": "Kore (US, female)"},
    {"id": "af_sarah", "label": "Sarah (US, female)"},
    {"id": "am_michael", "label": "Michael (US, male)"},
    {"id": "am_fenrir", "label": "Fenrir (US, male)"},
    {"id": "am_puck", "label": "Puck (US, male)"},
    {"id": "am_echo", "label": "Echo (US, male)"},
    {"id": "bf_emma", "label": "Emma (UK, female)"},
    {"id": "bf_isabella", "label": "Isabella (UK, female)"},
    {"id": "bm_george", "label": "George (UK, male)"},
    {"id": "bm_fable", "label": "Fable (UK, male)"},
]
DEFAULT_VOICE = "af_heart"

PIP = "pip install kokoro-onnx"
SAMPLE_RATE = 24000


class TTSError(Exception):
    """A synthesis problem the user can act on; `status` is the HTTP status."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.message = message
        self.status = status


# ── CPU and defaults ─────────────────────────────────────────────────────

_flags: Optional[str] = None


def _cpu_flags() -> str:
    global _flags
    if _flags is None:
        try:
            _flags = Path("/proc/cpuinfo").read_text(errors="ignore")
        except OSError:
            _flags = ""
    return _flags


def cpu_has_vnni() -> bool:
    """AVX-512 VNNI or AVX-VNNI: the int8 dot-product instructions that make
    the int8 model fast. Without them int8 Kokoro runs at 0.4-0.5x realtime
    (measured on a 16 core CPU with no AVX), full precision at about 2.5x."""
    f = _cpu_flags()
    if not f:
        return False  # not Linux: full precision is the safe choice
    return " avx512_vnni" in f or " avx_vnni" in f


def default_model() -> str:
    return "kokoro-v1.0-int8" if cpu_has_vnni() else "kokoro-v1.0"


def resolve_model(configured: str) -> str:
    configured = (configured or "").strip()
    return configured if configured in KOKORO_MODELS else default_model()


def resolve_voice(voice: str) -> str:
    """The saved voice when it is a Kokoro voice, else af_heart (the stored
    default is the OpenAI name "alloy"). Blends like "af_heart+af_bella"
    are left to kokoro-onnx to check."""
    v = (voice or "").strip()
    if not v or "_" not in v:
        return DEFAULT_VOICE
    return v


def cpu_threads() -> int:
    return max(1, min(8, os.cpu_count() or 1))


# ── Packages and files ───────────────────────────────────────────────────

def _has_module(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def missing_packages() -> List[str]:
    return [pkg for mod, pkg in (("kokoro_onnx", "kokoro-onnx"), ("onnxruntime", "onnxruntime")) if not _has_module(mod)]


def missing_package_message(missing: List[str]) -> str:
    return (f"The local Kokoro voice needs the Python package {', '.join(missing)}, which is not installed. "
            f"Install it in the Python environment Odysseus runs from, then restart Odysseus: {PIP}")


def models_dir() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "models" / "tts" / "kokoro"


def _files_for(model: str) -> List[Dict[str, Any]]:
    return [KOKORO_MODELS[model], VOICES_FILE]


def _present(spec: Dict[str, Any]) -> bool:
    p = models_dir() / spec["file"]
    try:
        return p.is_file() and p.stat().st_size == spec["bytes"]
    except OSError:
        return False


def model_paths(model: str) -> Optional[tuple]:
    """(model file, voices file) when both are downloaded, else None."""
    if model not in KOKORO_MODELS:
        return None
    if all(_present(s) for s in _files_for(model)):
        return models_dir() / KOKORO_MODELS[model]["file"], models_dir() / VOICES_FILE["file"]
    return None


def download_bytes(model: str) -> int:
    """Bytes still to fetch for `model` (the voices file is shared)."""
    return sum(s["bytes"] for s in _files_for(model) if not _present(s))


# ── Downloads ────────────────────────────────────────────────────────────

class Downloads:
    """One background download per model, with byte progress."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def state(self, model: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            j = self._jobs.get(model)
            return dict(j) if j else None

    def start(self, model: str, on_done=None) -> Dict[str, Any]:
        if model not in KOKORO_MODELS:
            raise TTSError(f"Unknown Kokoro model: {model}", 400)
        with self._lock:
            j = self._jobs.get(model)
            if j and j["status"] == "downloading":
                return dict(j)
            if model_paths(model):
                return {"status": "done"}
            j = {"status": "downloading", "error": "", "done_bytes": 0, "total_bytes": download_bytes(model)}
            self._jobs[model] = j
        threading.Thread(target=self._run, args=(model, on_done), name=f"tts-download-{model}", daemon=True).start()
        return dict(j)

    def _progress(self, model: str, n: int):
        with self._lock:
            self._jobs[model]["done_bytes"] += n

    def _fetch(self, model: str, spec: Dict[str, Any]):
        import httpx
        folder = models_dir()
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / spec["file"]
        part = dest.with_name(dest.name + ".part")
        with httpx.stream("GET", RELEASE + spec["file"], follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    self._progress(model, len(chunk))
        if part.stat().st_size != spec["bytes"]:
            got = part.stat().st_size
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{spec['file']} is {got} bytes, expected {spec['bytes']}")
        os.replace(part, dest)

    def _run(self, model: str, on_done):
        try:
            for spec in _files_for(model):
                if not _present(spec):
                    logger.info("TTS: downloading %s into %s", spec["file"], models_dir())
                    self._fetch(model, spec)
            with self._lock:
                self._jobs[model] = {"status": "done", "error": ""}
            if on_done:
                try:
                    on_done(model)
                except Exception:
                    logger.debug("TTS post-download hook failed", exc_info=True)
        except Exception as e:
            logger.error("Kokoro model download failed for %s: %s", model, e)
            with self._lock:
                self._jobs[model] = {"status": "error", "error": str(e) or e.__class__.__name__}


def model_status(model: str, downloads: Downloads, selected: str = "") -> Dict[str, Any]:
    spec = KOKORO_MODELS[model]
    size = spec["bytes"] + VOICES_FILE["bytes"]
    ready = model_paths(model) is not None
    out = {
        "id": model,
        "label": spec["label"],
        "size_bytes": size,
        "size_mb": round(size / 1_000_000),
        "downloaded": ready,
        "path": str(models_dir()),
        "selected": model == selected,
        "recommended": model == default_model(),
        "status": "downloaded" if ready else "not downloaded",
    }
    j = downloads.state(model)
    if j and j["status"] == "downloading" and not ready:
        total = j.get("total_bytes") or 0
        out.update(status="downloading", downloaded_bytes=j.get("done_bytes", 0),
                   progress=min(0.99, j.get("done_bytes", 0) / total) if total else None)
    elif j and j["status"] == "error" and not ready:
        out.update(status="error", error=j["error"])
    return out


# ── Synthesis ────────────────────────────────────────────────────────────

def to_wav(samples, rate: int = SAMPLE_RATE) -> bytes:
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def load_kokoro(model_path: Path, voices_path: Path):
    """A kokoro_onnx.Kokoro on CPU (or CUDA when onnxruntime has it), using
    at most 8 threads so a reply does not starve the rest of the app."""
    import onnxruntime as ort
    from kokoro_onnx import Kokoro
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = cpu_threads()
    providers = ["CPUExecutionProvider"]
    device = "cpu"
    try:
        if "CUDAExecutionProvider" in ort.get_available_providers():
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            device = "cuda"
    except Exception:
        pass
    session = ort.InferenceSession(str(model_path), sess_options=opts, providers=providers)
    return Kokoro.from_session(session, str(voices_path)), device


class KokoroEngine:
    """The loaded Kokoro model (one at a time), a load lock, and a bound on
    concurrent syntheses (ODYSSEUS_TTS_CONCURRENCY, default 1): Kokoro uses
    every thread it is given, so two at once only makes both slower, and the
    voice call's sentences then come back in the order they were asked for."""

    def __init__(self):
        self.downloads = Downloads()
        self._loaded: Optional[Dict[str, Any]] = None
        self._load_lock = threading.Lock()
        self.loading = False
        try:
            n = int(os.getenv("ODYSSEUS_TTS_CONCURRENCY", "1"))
        except ValueError:
            n = 1
        self.max_concurrent = max(1, n)
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        try:
            self._busy_timeout = float(os.getenv("ODYSSEUS_TTS_QUEUE_TIMEOUT", "60"))
        except ValueError:
            self._busy_timeout = 60.0
        self.last: Dict[str, Any] = {}

    def readiness(self, model: str) -> Optional[str]:
        missing = missing_packages()
        if missing:
            return missing_package_message(missing)
        if model_paths(model) is None:
            mb = round(download_bytes(model) / 1_000_000)
            j = self.downloads.state(model)
            if j and j["status"] == "downloading":
                return (f"The Kokoro voice model ({mb} MB) is downloading. "
                        "Settings > AI Defaults > Voice call shows the progress.")
            if j and j["status"] == "error":
                return (f"Downloading the Kokoro voice model failed: {j['error']}. "
                        "Try again from Settings > AI Defaults > Voice call.")
            return (f"The Kokoro voice model {model} ({mb} MB) is not downloaded yet. "
                    "Download it in Settings > AI Defaults > Voice call.")
        return None

    @property
    def loaded(self) -> Optional[Dict[str, Any]]:
        return self._loaded

    def load(self, model: str):
        cur = self._loaded
        if cur and cur["model"] == model:
            return cur["obj"]
        reason = self.readiness(model)
        if reason:
            raise TTSError(reason)
        with self._load_lock:
            cur = self._loaded
            if cur and cur["model"] == model:
                return cur["obj"]
            self.loading = True
            try:
                t0 = time.monotonic()
                obj, device = load_kokoro(*model_paths(model))
                self._loaded = {"model": model, "obj": obj, "device": device,
                                "load_s": round(time.monotonic() - t0, 2)}
                logger.info("TTS: Kokoro %s loaded on %s in %.1fs", model, device, self._loaded["load_s"])
                return obj
            except TTSError:
                raise
            except Exception as e:
                logger.error("TTS: loading Kokoro %s failed: %s", model, e, exc_info=True)
                raise TTSError(f"Could not load the Kokoro model {model}: {e}", 500)
            finally:
                self.loading = False

    def warm(self, model: str) -> bool:
        cur = self._loaded
        if self.loading or (cur and cur["model"] == model) or self.readiness(model):
            return False
        self.loading = True

        def run():
            try:
                self.load(model)
            except TTSError as e:
                logger.warning("Kokoro warm-up failed: %s", e.message)
            finally:
                self.loading = False

        threading.Thread(target=run, name="tts-warm-kokoro", daemon=True).start()
        return True

    def synthesize(self, text: str, model: str, voice: str, speed: float = 1.0) -> bytes:
        """WAV bytes (24 kHz mono 16-bit) for `text`. Blocks; call from a
        worker thread."""
        reason = self.readiness(model)
        if reason:
            raise TTSError(reason)
        if not self._sem.acquire(timeout=self._busy_timeout):
            raise TTSError("The voice is busy. Try again in a moment.", 503)
        try:
            obj = self.load(model)
            t0 = time.monotonic()
            speed = max(0.5, min(2.0, float(speed or 1.0)))
            try:
                samples, rate = obj.create(text, voice=voice, speed=speed, lang=_lang_for(voice))
            except (KeyError, ValueError, AssertionError) as e:
                raise TTSError(f"Kokoro could not use the voice {voice!r}: {e}", 400)
            took = time.monotonic() - t0
            audio_s = len(samples) / float(rate or SAMPLE_RATE)
            self.last = {"latency_ms": round(took * 1000), "audio_seconds": round(audio_s, 2),
                         "chars": len(text), "voice": voice, "model": model, "at": time.time()}
            logger.info("Kokoro: %d chars -> %.1fs of speech in %.2fs (%s)", len(text), audio_s, took, voice)
            return to_wav(samples, rate or SAMPLE_RATE)
        finally:
            self._sem.release()


def _lang_for(voice: str) -> str:
    """espeak language for a voice: the first letter of its name is the
    accent (a = US English, b = UK English; others as Kokoro defines)."""
    return {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it",
            "j": "ja", "p": "pt-br", "z": "cmn"}.get((voice or "a")[:1], "en-us")
