# services/stt/stt_service.py
"""Multi-provider Speech-to-Text service: local Whisper or Parakeet, an
OpenAI-compatible API, or the browser."""

import io
import logging
import os
import threading
import time
import httpx
from typing import Optional, Dict, Any

from services.stt.audio import SAMPLE_RATE, decode_to_16k_mono
from services.stt.local_models import (
    ENGINES, PARAKEET_MODELS, PROVIDER_TO_ENGINE, WHISPER_DEFAULT, WHISPER_MODELS,
    Downloads, STTError, cpu_has_avx2, cpu_threads, default_model, local_path,
    missing_package_message, missing_packages, model_status, models_for,
    models_root, resolve_model,
)

logger = logging.getLogger(__name__)


def audio_container(audio_bytes: bytes) -> tuple:
    """(file suffix, MIME type) for uploaded audio, from its magic bytes.

    The composer's recorder sends WebM; the voice call sends WAV; Safari's
    MediaRecorder makes MP4. OpenAI-compatible transcription APIs go by the
    file name, so a WAV labelled audio.webm is rejected. Unknown bytes keep
    the old WebM label.
    """
    head = bytes(audio_bytes[:12])
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav", "audio/wav"
    if head[:4] == b"OggS":
        return ".ogg", "audio/ogg"
    if head[4:8] == b"ftyp":
        return ".mp4", "audio/mp4"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return ".mp3", "audio/mpeg"
    return ".webm", "audio/webm"


class STTService:
    """Multi-provider STT service.

    Reads provider config from data/settings.json on each call.
    Providers:
      "disabled"        - no STT
      "browser"         - client-side Web Speech API (no server transcription)
      "local"           - Whisper (faster-whisper) on this machine
      "local:parakeet"  - NVIDIA Parakeet TDT (onnx-asr) on this machine
      "endpoint:<id>"   - OpenAI-compatible /audio/transcriptions via ModelEndpoint

    Local models load once, in a worker thread, and stay loaded (one per
    engine). Transcriptions on this machine go through a bounded semaphore
    (ODYSSEUS_STT_CONCURRENCY, default 1): both engines already use every
    thread they are given, so two at once only makes both slow.
    """

    def __init__(self):
        self._whisper_model = None  # kept for callers that peeked at it
        self._loaded: Dict[str, Dict[str, Any]] = {}  # engine -> {model, obj, device, load_s}
        self._load_lock = threading.Lock()
        self._loading: set = set()
        try:
            n = int(os.getenv("ODYSSEUS_STT_CONCURRENCY", "1"))
        except ValueError:
            n = 1
        self._max_concurrent = max(1, n)
        self._sem = threading.BoundedSemaphore(self._max_concurrent)
        try:
            self._busy_timeout = float(os.getenv("ODYSSEUS_STT_QUEUE_TIMEOUT", "60"))
        except ValueError:
            self._busy_timeout = 60.0
        self.downloads = Downloads()
        self._last: Dict[str, Any] = {}

    # ── Settings ──

    def _load_settings(self) -> dict:
        from src.settings import load_settings
        saved = load_settings()
        return {
            "stt_enabled": saved.get("stt_enabled", False),
            "stt_provider": saved.get("stt_provider", "disabled"),
            "stt_model": saved.get("stt_model", WHISPER_DEFAULT),
            "stt_parakeet_model": saved.get("stt_parakeet_model", ""),
            "stt_language": saved.get("stt_language", ""),
        }

    def _selected(self, settings: dict, engine: str) -> str:
        key = ENGINES[engine]["setting"]
        return resolve_model(engine, settings.get(key, ""), settings.get("stt_language", ""))

    def readiness(self, engine: str, model: str) -> Optional[str]:
        """None when `engine`/`model` can run now, else what is missing."""
        missing = missing_packages(engine)
        if missing:
            return missing_package_message(engine, missing)
        if local_path(engine, model) is None:
            spec = models_for(engine).get(model) or {}
            size = spec.get("bytes")
            size_txt = f" ({round(size / 1_000_000)} MB)" if size else ""
            job = self.downloads.state(engine, model)
            label = ENGINES[engine]["label"]
            if job and job["status"] == "downloading":
                return (f"The {label} model {model}{size_txt} is downloading. "
                        "Settings > AI Defaults > Voice call shows the progress; try again when it is done.")
            if job and job["status"] == "error":
                return (f"Downloading the {label} model {model} failed: {job['error']}. "
                        "Try again from Settings > AI Defaults > Voice call.")
            return (f"The {label} model {model}{size_txt} is not downloaded yet. "
                    "Download it in Settings > AI Defaults > Voice call.")
        return None

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        if settings.get("stt_enabled") is False:
            return False
        provider = settings["stt_provider"]
        if provider == "disabled":
            return False
        if provider == "browser":
            return True  # handled client-side
        engine = PROVIDER_TO_ENGINE.get(provider)
        if engine:
            # Cheap: packages present and model on disk. Never loads a model
            # (this runs on the event loop).
            return self.readiness(engine, self._selected(settings, engine)) is None
        if provider.startswith("endpoint:"):
            return True  # assume reachable
        return False

    # ── Local engines ──

    def _load(self, engine: str, model: str):
        """The loaded model object for engine/model; loads it (blocking) when
        needed. Call from a worker thread, never the event loop."""
        cur = self._loaded.get(engine)
        if cur and cur["model"] == model:
            return cur["obj"]
        reason = self.readiness(engine, model)
        if reason:
            raise STTError(reason)
        with self._load_lock:
            cur = self._loaded.get(engine)
            if cur and cur["model"] == model:
                return cur["obj"]
            self._loading.add(engine)
            try:
                t0 = time.monotonic()
                path = str(local_path(engine, model))
                if engine == "whisper":
                    obj, device = self._load_whisper(path)
                else:
                    obj, device = self._load_parakeet(model, path)
                load_s = time.monotonic() - t0
                # Replacing the entry drops the previous model of this engine.
                self._loaded[engine] = {"model": model, "obj": obj, "device": device, "load_s": round(load_s, 2)}
                if engine == "whisper":
                    self._whisper_model = obj
                logger.info("STT: %s %s loaded on %s in %.1fs", engine, model, device, load_s)
                return obj
            except STTError:
                raise
            except Exception as e:
                logger.error("STT: loading %s %s failed: %s", engine, model, e, exc_info=True)
                raise STTError(f"Could not load the {ENGINES[engine]['label']} model {model}: {e}", 500)
            finally:
                self._loading.discard(engine)

    def _load_whisper(self, path: str):
        from faster_whisper import WhisperModel
        # CTranslate2 reports CUDA devices itself, so no torch import (torch
        # can crash the whole process on CPUs without AVX).
        device = "cpu"
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                device = "cuda"
        except Exception:
            device = "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        model = WhisperModel(path, device=device, compute_type=compute_type, cpu_threads=cpu_threads())
        return model, device

    def _load_parakeet(self, model: str, path: str):
        import onnx_asr
        import onnxruntime as ort
        spec = PARAKEET_MODELS[model]
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
        obj = onnx_asr.load_model(spec["onnx_name"], path, quantization=spec["quantization"],
                                  sess_options=opts, providers=providers)
        return obj, device

    def _get_whisper(self):
        """The loaded Whisper model (loads the selected one), or None."""
        settings = self._load_settings()
        try:
            return self._load("whisper", self._selected(settings, "whisper"))
        except STTError as e:
            logger.warning("Whisper unavailable: %s", e.message)
            return None

    def warm(self) -> bool:
        """Load the selected local model in the background (no-op when it is
        loaded, loading, not downloaded or the provider isn't local)."""
        settings = self._load_settings()
        if settings.get("stt_enabled") is False:
            return False
        engine = PROVIDER_TO_ENGINE.get(settings["stt_provider"])
        if not engine or engine in self._loading:
            return False
        model = self._selected(settings, engine)
        cur = self._loaded.get(engine)
        if (cur and cur["model"] == model) or self.readiness(engine, model):
            return False
        self._loading.add(engine)  # shows as loading right away

        def run():
            try:
                self._load(engine, model)
            except STTError as e:
                logger.warning("STT warm-up failed: %s", e.message)
            finally:
                self._loading.discard(engine)

        threading.Thread(target=run, name=f"stt-warm-{engine}", daemon=True).start()
        return True

    def _after_download(self, engine: str, model: str):
        settings = self._load_settings()
        if PROVIDER_TO_ENGINE.get(settings["stt_provider"]) == engine and self._selected(settings, engine) == model:
            self.warm()

    def start_download(self, engine: str, model: str) -> Dict[str, Any]:
        return self.downloads.start(engine, model, on_done=self._after_download)

    def _run_local(self, engine: str, model: str, samples, language: str) -> str:
        if not self._sem.acquire(timeout=self._busy_timeout):
            raise STTError("Speech to text is busy with other recordings. Try again in a moment.", 503)
        try:
            obj = self._load(engine, model)
            if engine == "whisper":
                kwargs = {"beam_size": 1, "condition_on_previous_text": False}
                if language:
                    kwargs["language"] = language
                segments, info = obj.transcribe(samples, **kwargs)
                text = " ".join(seg.text.strip() for seg in segments).strip()
                logger.info("Local STT (whisper %s): %d chars, lang=%s", model, len(text),
                            getattr(info, "language", "?"))
                return text
            result = obj.recognize(samples, sample_rate=SAMPLE_RATE)
            text = (result if isinstance(result, str) else getattr(result, "text", str(result))).strip()
            logger.info("Local STT (parakeet %s): %d chars", model, len(text))
            return text
        finally:
            self._sem.release()

    def _transcribe_engine(self, engine: str, audio_bytes: bytes, settings: dict) -> str:
        model = self._selected(settings, engine)
        language = (settings.get("stt_language") or "").strip()
        reason = self.readiness(engine, model)
        if reason:
            job = self.downloads.state(engine, model)
            if (local_path(engine, model) is None and not missing_packages(engine)
                    and not (job and job["status"] == "error")):
                # Start fetching it now, so the next try does not wait on a
                # download (it used to hang the first voice turn instead).
                try:
                    self.start_download(engine, model)
                    reason = self.readiness(engine, model) or reason
                except STTError:
                    pass
            raise STTError(reason)
        t0 = time.monotonic()
        samples = decode_to_16k_mono(audio_bytes)
        audio_s = samples.size / float(SAMPLE_RATE)
        if audio_s < 0.1:
            return ""
        text = self._run_local(engine, model, samples, language)
        took = time.monotonic() - t0
        self._last = {"engine": engine, "model": model, "latency_ms": round(took * 1000),
                      "audio_seconds": round(audio_s, 2), "at": time.time()}
        return text

    def _transcribe_local(self, audio_bytes: bytes, language: str = "") -> Optional[str]:
        """Whisper on this machine; None on any failure."""
        settings = dict(self._load_settings())
        if language:
            settings["stt_language"] = language
        try:
            return self._transcribe_engine("whisper", audio_bytes, settings)
        except Exception as e:
            logger.error(f"Local STT transcription failed: {e}")
            return None

    # ── API endpoint ──

    def _transcribe_api(self, audio_bytes: bytes, endpoint_id: str, model: str, language: str = "") -> Optional[str]:
        from src.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            if not ep:
                logger.error(f"STT endpoint {endpoint_id} not found")
                return None
            base_url = ep.base_url.rstrip("/")
            api_key = ep.api_key
        finally:
            db.close()

        url = base_url + "/audio/transcriptions"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        suffix, mime = audio_container(audio_bytes)
        files = {"file": ("audio" + suffix, io.BytesIO(audio_bytes), mime)}
        data = {"model": model or "whisper-1"}
        if language:
            data["language"] = language

        try:
            r = httpx.post(url, headers=headers, files=files, data=data, timeout=60)
            r.raise_for_status()
            result = r.json()
            text = result.get("text", "")
            logger.info(f"API STT: {len(text)} chars from {base_url}")
            return text
        except Exception as e:
            logger.error(f"API STT transcription failed: {e}")
            return None

    # ── Public interface ──

    def transcribe_checked(self, audio_bytes: bytes) -> str:
        """Transcribe with the configured provider. Raises STTError with a
        message for the user when it can't."""
        settings = self._load_settings()
        provider = settings["stt_provider"]
        if settings.get("stt_enabled") is False or provider == "disabled":
            raise STTError('Speech to text is off. Pick an engine for "Hears with" in Settings > AI Defaults > Voice call.')
        if provider == "browser":
            raise STTError("Speech to text is set to the browser's built-in recognition, so the server does not transcribe.")
        engine = PROVIDER_TO_ENGINE.get(provider)
        if engine:
            return self._transcribe_engine(engine, audio_bytes, settings)
        if provider.startswith("endpoint:"):
            model = settings["stt_model"]
            # The local Whisper size names mean nothing to an API.
            if not model or model in WHISPER_MODELS:
                model = "whisper-1"
            t0 = time.monotonic()
            text = self._transcribe_api(audio_bytes, provider.split(":", 1)[1], model, settings.get("stt_language", ""))
            if text is None:
                raise STTError("The speech to text endpoint failed. Check its URL, key and model.", 502)
            self._last = {"engine": "endpoint", "model": model,
                          "latency_ms": round((time.monotonic() - t0) * 1000), "at": time.time()}
            return text
        raise STTError(f"Unknown speech to text provider: {provider}", 400)

    def transcribe(self, audio_bytes: bytes) -> Optional[str]:
        try:
            return self.transcribe_checked(audio_bytes)
        except STTError as e:
            logger.warning("STT: %s", e.message)
            return None
        except Exception as e:
            logger.error("STT transcription failed: %s", e, exc_info=True)
            return None

    def engines(self) -> Dict[str, Any]:
        """Every local engine: can it run, its models and their downloads."""
        settings = self._load_settings()
        out = []
        for name, e in ENGINES.items():
            selected = self._selected(settings, name)
            missing = missing_packages(name)
            models = [model_status(name, m, self.downloads, selected) for m in models_for(name)]
            if not any(m["id"] == selected for m in models):
                models.append(model_status(name, selected, self.downloads, selected))
            loaded = self._loaded.get(name)
            reason = self.readiness(name, selected)
            out.append({
                "id": name,
                "provider": e["provider"],
                "label": e["label"],
                "installed": not missing,
                "missing_packages": missing,
                "pip": e["pip"] if missing else "",
                "setting": e["setting"],
                "selected_model": selected,
                "default_model": default_model(name),
                "ready": reason is None,
                "reason": reason or "",
                "loaded_model": loaded["model"] if loaded else "",
                "loading": name in self._loading,
                "models": models,
            })
        return {"engines": out, "models_dir": str(models_root()), "cpu_threads": cpu_threads(),
                "avx2": cpu_has_avx2()}

    def get_stats(self) -> Dict[str, Any]:
        settings = self._load_settings()
        provider = settings["stt_provider"]
        stt_enabled = settings.get("stt_enabled", False)
        # If toggle is off, report as disabled
        effective_provider = provider if stt_enabled else "disabled"

        stats = {
            "available": self.available and bool(stt_enabled),
            "provider": effective_provider,
            "model": settings["stt_model"],
            "language": settings.get("stt_language", ""),
        }

        engine = PROVIDER_TO_ENGINE.get(provider)
        if engine:
            model = self._selected(settings, engine)
            loaded = self._loaded.get(engine)
            reason = self.readiness(engine, model)
            stats.update({
                "engine": engine,
                "model": model,
                "ready": reason is None,
                "reason": reason or "",
                "model_loaded": bool(loaded and loaded["model"] == model),
                "loading": engine in self._loading,
                "device": loaded["device"] if loaded else "",
                "load_seconds": loaded["load_s"] if loaded else None,
                "cpu_threads": cpu_threads(),
                "max_concurrent": self._max_concurrent,
            })
        elif provider == "browser":
            stats["engine"] = "browser"
            stats["model"] = "Browser (Web Speech API)"
        elif provider.startswith("endpoint:"):
            stats["engine"] = "endpoint"
            stats["endpoint_id"] = provider.split(":", 1)[1]
        if self._last:
            stats["last"] = dict(self._last)
            stats["last_latency_ms"] = self._last.get("latency_ms")
        return stats


# Module-level singleton
_stt_service = None

def get_stt_service() -> STTService:
    global _stt_service
    if _stt_service is None:
        _stt_service = STTService()
    return _stt_service
