# services/tts/tts_service.py
"""Multi-provider TTS service: local Kokoro (kokoro-onnx), an
OpenAI-compatible API (OpenAI, Kokoro-FastAPI, ...), an add-on voice (a mod
that provides `tts.voice`, like ElevenLabs), or the browser."""

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

import httpx

from src.constants import TTS_CACHE_DIR
from src.upload_limits import read_byte_limit_env
from services.tts.kokoro_local import (
    KOKORO_MODELS, PIP, VOICES, KokoroEngine, TTSError, cpu_has_vnni, cpu_threads,
    default_model, missing_packages, model_status, models_dir, resolve_model, resolve_voice,
)

logger = logging.getLogger(__name__)

# Total on-disk cache cap, single-sourced here like the upload limits in
# src/upload_limits.py. Applies to every provider that lands in cache_dir
# (local Kokoro and the OpenAI-compatible API path alike, since both go
# through _put_cache below), not just a hypothetical cloud-TTS cache.
DEFAULT_TTS_CACHE_MAX_BYTES = 500 * 1024 * 1024  # 500 MB
TTS_CACHE_MAX_BYTES_ENV = "ODYSSEUS_TTS_CACHE_MAX_BYTES"


def get_tts_cache_max_bytes() -> int:
    return read_byte_limit_env(TTS_CACHE_MAX_BYTES_ENV, DEFAULT_TTS_CACHE_MAX_BYTES)


def _safe_num(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


# An add-on voice is the provider "mod:<mod id>" (src/mods/providers.py,
# capability tts.voice). ElevenLabs was built in before it became an add-on,
# so a saved "elevenlabs" means that add-on.
MOD_PREFIX = "mod:"
LEGACY_PROVIDERS = {"elevenlabs": "mod:elevenlabs"}
# The add-ons the core knows by name, for "Install the ... add-on".
KNOWN_ADDONS = {"elevenlabs": "ElevenLabs voice"}
MOD_STATUS_TTL = 10.0


def mod_id_of(provider: str) -> Optional[str]:
    """The mod id of an add-on voice provider, else None."""
    provider = LEGACY_PROVIDERS.get(provider or "", provider or "")
    if not provider.startswith(MOD_PREFIX):
        return None
    return provider[len(MOD_PREFIX):] or None


def _safe_speed(value, default: float = 1.0) -> float:
    """Parse the stored tts_speed defensively. The settings layer tolerates
    corrupt/agent-written config, so a non-numeric or empty value (e.g. an agent
    setting "speech speed" = "fast", or a hand-edited settings.json) must not
    crash synthesis or the stats endpoint with a ValueError."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return speed if speed > 0 else default


class TTSService:
    """Multi-provider TTS service.

    Reads provider config from data/settings.json on each call.
    Providers:
      "disabled"        - no TTS
      "browser"         - client-side Web Speech API (no server synthesis)
      "local"           - Kokoro-82M on this machine via kokoro-onnx (no torch)
      "endpoint:<id>"   - OpenAI-compatible /audio/speech via ModelEndpoint
                          (also Kokoro-FastAPI on a GPU box: voice "af_heart")
      "mod:<id>"        - an add-on voice (a mod that provides tts.voice,
                          like ElevenLabs). When the add-on says it is
                          paused (out of credits), Kokoro speaks if it is
                          ready, else the browser voice. The add-on keeps
                          its own key, cache and limits.

    Speed is applied here for every server provider (Kokoro natively, APIs
    through their `speed` field), so the browser plays the audio at 1x.
    """

    def __init__(self, cache_dir: str = TTS_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._kokoro = None  # lazy-init
        self._fallback: Optional[Dict[str, Any]] = None  # last add-on hand-over
        self._last_fallback: Optional[Dict[str, Any]] = None
        self._mod_status_cache: Dict[str, Any] = {}  # mod id -> (at, status)

    # ── Settings ──

    def _load_settings(self) -> dict:
        from src.settings import load_settings
        saved = load_settings()
        return {
            "tts_enabled": saved.get("tts_enabled", True),
            "tts_provider": LEGACY_PROVIDERS.get(saved.get("tts_provider", "disabled"),
                                                 saved.get("tts_provider", "disabled")),
            "tts_model": saved.get("tts_model", "tts-1"),
            "tts_voice": saved.get("tts_voice", "alloy"),
            "tts_speed": saved.get("tts_speed", "1"),
            "tts_kokoro_model": saved.get("tts_kokoro_model", ""),
        }

    def _kokoro_model(self, settings: dict) -> str:
        return resolve_model(settings.get("tts_kokoro_model", ""))

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return False
        provider = settings["tts_provider"]
        if provider == "disabled":
            return False
        if provider == "browser":
            return True  # handled client-side
        if provider == "local":
            # Cheap: package present and files on disk; never loads the model.
            return self._get_kokoro().readiness(self._kokoro_model(settings)) is None
        if provider.startswith("endpoint:"):
            return True  # assume reachable; errors surface at synthesis time
        if mod_id_of(provider):
            return bool(self.mod_status(mod_id_of(provider)).get("ready"))
        return False

    # ── Add-on voices (mods that provide tts.voice) ──

    def _kokoro_ready(self, settings: dict) -> bool:
        try:
            return self._get_kokoro().readiness(self._kokoro_model(settings)) is None
        except Exception:
            return False

    @staticmethod
    def _addon_name(mod_id: str) -> str:
        from src.mods import registry
        e = registry.get(mod_id)
        return (e or {}).get("name") or KNOWN_ADDONS.get(mod_id) or mod_id

    def _missing_reason(self, mod_id: str) -> str:
        """Why an add-on voice cannot speak when no enabled mod provides it."""
        from src.mods import registry
        name = self._addon_name(mod_id)
        e = registry.get(mod_id)
        if e and not e.get("enabled"):
            return f"The {name} add-on is turned off. Turn it on in Settings > Mods."
        if e:
            return f"The {name} add-on does not provide a voice."
        return f"Install the {name} add-on (Settings > Mods > Browse) to speak with it."

    def mod_status(self, mod_id: str, refresh: bool = False) -> Dict[str, Any]:
        """The add-on's own status (ready, paused and why, max per reply ...),
        asked at most every MOD_STATUS_TTL seconds. Never raises."""
        from src.mods import providers
        hit = self._mod_status_cache.get(mod_id)
        if hit and not refresh and time.time() - hit[0] < MOD_STATUS_TTL:
            return hit[1]
        if not providers.provider("tts.voice", mod_id):
            out: Dict[str, Any] = {"ready": False, "installed": False, "reason": self._missing_reason(mod_id)}
        else:
            try:
                r = providers.call_sync("tts.voice", "status", {"refresh": "1"} if refresh else None,
                                        mod_id=mod_id, method="GET", timeout=10)
                d = _json(r)
                if r.status_code >= 400:
                    out = {"ready": False, "reason": f"The {self._addon_name(mod_id)} add-on answered {r.status_code}."}
                else:
                    out = d
            except providers.ProviderUnavailable as e:
                out = {"ready": False, "reason": e.message}
            out["installed"] = True
        out.setdefault("label", self._addon_name(mod_id))
        self._mod_status_cache[mod_id] = (time.time(), out)
        return out

    def mod_voices(self, provider: str, refresh: bool = False) -> List[Dict[str, Any]]:
        """The voices an add-on offers (an empty list for other providers)."""
        from src.mods import providers
        mod_id = mod_id_of(provider)
        if not mod_id:
            return []
        if not providers.provider("tts.voice", mod_id):
            raise TTSError(self._missing_reason(mod_id), 400)
        try:
            r = providers.call_sync("tts.voice", "voices", {"refresh": "1"} if refresh else None,
                                    mod_id=mod_id, method="GET", timeout=20)
        except providers.ProviderUnavailable as e:
            raise TTSError(e.message, 503)
        d = _json(r)
        if r.status_code >= 400:
            raise TTSError(str(d.get("error") or f"The add-on answered {r.status_code}."), _status_for(r.status_code))
        voices = d.get("voices") if isinstance(d.get("voices"), list) else []
        return [v for v in voices if isinstance(v, dict) and v.get("voice_id")]

    def _synthesize_mod(self, mod_id: str, text: str, voice: str, speed: float, response_format: str,
                        use_cache: bool, settings: dict) -> bytes:
        from src.mods import providers
        self._fallback = None
        if not providers.provider("tts.voice", mod_id):
            raise TTSError(self._missing_reason(mod_id), 400)
        t0 = time.monotonic()
        try:
            r = providers.call_sync("tts.voice", "synthesize",
                                    {"text": text, "voice": voice, "speed": speed, "format": response_format,
                                     "use_cache": use_cache}, mod_id=mod_id, timeout=60)
        except providers.ProviderUnavailable as e:
            raise TTSError(e.message, 503)
        if r.status_code == 409:
            d = _json(r)
            if d.get("paused"):
                # The add-on is paused (credit reserve, quota): Kokoro or the browser speaks.
                self._mod_status_cache.pop(mod_id, None)
                return self._synthesize_fallback(mod_id, str(d.get("reason") or ""), text, speed, use_cache, settings)
        if r.status_code >= 400:
            d = _json(r)
            raise TTSError(str(d.get("error") or f"The {self._addon_name(mod_id)} add-on answered {r.status_code}."),
                           _status_for(r.status_code))
        if not r.content:
            raise TTSError(f"The {self._addon_name(mod_id)} add-on sent no audio.", 502)
        self._last_api = {"latency_ms": round((time.monotonic() - t0) * 1000), "chars": len(text), "at": time.time()}
        return r.content

    # ── Cache ──

    def _cache_key(self, text: str, provider: str, model: str, voice: str, speed: float = 1.0) -> str:
        raw = f"{provider}|{model}|{voice}|{speed}|{text}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[bytes]:
        for ext in (".mp3", ".wav"):
            path = self.cache_dir / f"{key}{ext}"
            if path.exists():
                data = path.read_bytes()
                # Bump the access time (without touching mtime) so cache
                # eviction below evicts by actual last-use, not just write
                # order: a re-requested line stays warm longer.
                try:
                    st = path.stat()
                    os.utime(path, (time.time(), st.st_mtime))
                except OSError:
                    pass
                return data
        return None

    def _put_cache(self, key: str, data: bytes):
        ext = ".mp3" if (len(data) >= 3 and (data[:3] == b'ID3' or (data[0] == 0xff and (data[1] & 0xe0) == 0xe0))) else ".wav"
        (self.cache_dir / f"{key}{ext}").write_bytes(data)
        self._enforce_cache_limit()

    def _enforce_cache_limit(self):
        """Evict the least-recently-accessed cache files once the cache
        directory exceeds ODYSSEUS_TTS_CACHE_MAX_BYTES. Runs after every
        write, for every provider (local Kokoro included, since it has no
        cache of its own; synthesized audio always lands here via _put_cache)."""
        try:
            max_bytes = get_tts_cache_max_bytes()
        except ValueError as e:
            logger.warning(f"Skipping TTS cache eviction, bad {TTS_CACHE_MAX_BYTES_ENV}: {e}")
            return

        try:
            files = []
            total_size = 0
            for f in self.cache_dir.iterdir():
                try:
                    if f.is_file() and f.suffix.lower() in (".mp3", ".wav"):
                        files.append(f)
                        total_size += f.stat().st_size
                except OSError:
                    continue  # deleted mid-scan

            if total_size <= max_bytes:
                return

            logger.info(
                f"TTS cache ({total_size} bytes) exceeded limit ({max_bytes} bytes); evicting oldest-accessed files."
            )

            # Oldest-accessed first (atime), so frequently replayed lines
            # survive even if they were first synthesized long ago.
            files.sort(key=lambda f: f.stat().st_atime)

            # Trim down to 80% of the cap so we are not re-triggering this
            # on every single synthesis once the cache is near the limit.
            target_size = max_bytes * 0.8
            while files and total_size > target_size:
                f = files.pop(0)
                try:
                    size = f.stat().st_size
                    f.unlink()
                    total_size -= size
                except OSError as e:
                    logger.warning(f"Failed to evict TTS cache file {f}: {e}")
        except Exception as e:
            logger.warning(f"Error enforcing TTS cache limit: {e}", exc_info=True)

    def clear_cache(self):
        count = 0
        for f in self.cache_dir.glob("*.*"):
            f.unlink()
            count += 1
        logger.info(f"Cleared {count} cached TTS files")

    # ── Kokoro (local) ──

    def _get_kokoro(self) -> KokoroEngine:
        if self._kokoro is None:
            self._kokoro = KokoroEngine()
        return self._kokoro

    def warm(self) -> bool:
        """Load the Kokoro model in the background when it is the provider."""
        settings = self._load_settings()
        if settings.get("tts_enabled") is False or settings["tts_provider"] != "local":
            return False
        return self._get_kokoro().warm(self._kokoro_model(settings))

    def start_download(self, model: str) -> Dict[str, Any]:
        def after(m):
            if self._kokoro_model(self._load_settings()) == m:
                self.warm()
        return self._get_kokoro().downloads.start(model, on_done=after)

    # ── API endpoint ──

    def _synthesize_api(self, text: str, endpoint_id: str, model: str, voice: str, speed: float = 1.0) -> Optional[bytes]:
        try:
            return self._synthesize_api_checked(text, endpoint_id, model, voice, speed)
        except TTSError as e:
            logger.error(f"API TTS synthesis failed: {e.message}")
            return None

    def _synthesize_api_checked(self, text: str, endpoint_id: str, model: str, voice: str, speed: float = 1.0,
                                response_format: str = "mp3") -> bytes:
        from src.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            if not ep:
                raise TTSError(f"TTS endpoint {endpoint_id} not found", 404)
            base_url = ep.base_url.rstrip("/")
            api_key = ep.api_key
        finally:
            db.close()
        try:
            from src.endpoint_resolver import normalize_base, resolve_url
            base_url = resolve_url(normalize_base(base_url))
        except Exception:
            pass

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model or "tts-1",
            "input": text,
            "voice": voice,
            "response_format": response_format,
            "speed": speed,
        }

        # OpenAI-style bases end in /v1. Kokoro-FastAPI is often added as
        # just http://host:8880, so try /v1/audio/speech when that 404s.
        urls = [base_url + "/audio/speech"]
        if not base_url.endswith("/v1"):
            urls.append(base_url + "/v1/audio/speech")
        last = None
        for url in urls:
            try:
                r = httpx.post(url, json=payload, headers=headers, timeout=60)
            except Exception as e:
                raise TTSError(f"The text to speech endpoint did not answer: {e}", 502)
            if r.status_code == 404 and url != urls[-1]:
                last = r
                continue
            if r.status_code >= 400:
                detail = r.text[:300]
                raise TTSError(f"The text to speech endpoint answered {r.status_code}: {detail}", 502)
            logger.info(f"API TTS: {len(r.content)} bytes from {url}")
            return r.content
        raise TTSError(f"The text to speech endpoint answered {last.status_code if last else '?'}", 502)

    # ── Public interface ──

    def synthesize_checked(self, text: str, use_cache: bool = True, voice: Optional[str] = None,
                           speed: Optional[float] = None, response_format: str = "mp3") -> bytes:
        """Audio for `text` with the configured provider. `voice` and `speed`
        override the saved ones (the Settings preview). `response_format` is
        what an API engine is asked for: MP3 for the browser, WAV for a phone
        call (src/telephony), which has no MP3 decoder to count on. Kokoro
        always makes WAV. Raises TTSError."""
        settings = self._load_settings()
        provider = settings["tts_provider"]
        if settings.get("tts_enabled") is False or provider == "disabled":
            raise TTSError("Text to speech is off.")
        if provider == "browser":
            raise TTSError("Text to speech is set to the browser's voice, so the server does not speak.")
        model = settings["tts_model"]
        voice = voice or settings["tts_voice"]
        speed = _safe_speed(speed if speed is not None else settings.get("tts_speed", "1"))

        text = (text or "").strip()
        if not text:
            raise TTSError("Nothing to say.", 400)
        if len(text) > 5000:
            text = text[:5000]

        mod_id = mod_id_of(provider)
        if mod_id:
            # The add-on keeps its own cache (and limits, and voice defaults).
            return self._synthesize_mod(mod_id, text, voice or "", speed, response_format, use_cache, settings)
        if provider == "local":
            model = self._kokoro_model(settings)
            voice = resolve_voice(voice)
        elif not provider.startswith("endpoint:"):
            raise TTSError(f"Unknown TTS provider: {provider}", 400)

        # The format is in the key only when it is not the default, so the
        # cache the browser already filled stays valid.
        if response_format != "mp3" and provider != "local":
            cache_voice = f"{voice}|{response_format}"
        else:
            cache_voice = voice
        key = self._cache_key(text, provider, model, cache_voice, speed)
        if use_cache:
            cached = self._get_cached(key)
            if cached:
                logger.info(f"TTS cache hit ({len(text)} chars)")
                return cached

        if provider == "local":
            k = self._get_kokoro()
            reason = k.readiness(model)
            if reason:
                job = k.downloads.state(model)
                if not missing_packages() and not (job and job["status"] in ("downloading", "error")):
                    # Fetch it now so the next reply has a voice (STT does the same).
                    self.start_download(model)
                    reason = k.readiness(model) or reason
                raise TTSError(reason)
            audio_data = k.synthesize(text, model, voice, speed)
        else:
            t0 = time.monotonic()
            audio_data = self._synthesize_api_checked(text, provider.split(":", 1)[1], model, voice, speed,
                                                      response_format)
            self._last_api = {"latency_ms": round((time.monotonic() - t0) * 1000), "chars": len(text), "at": time.time()}

        if audio_data and use_cache:
            self._put_cache(key, audio_data)
        return audio_data

    def _synthesize_fallback(self, mod_id: str, reason: str, text: str, speed: float, use_cache: bool,
                             settings: dict) -> bytes:
        """The add-on voice is paused (credit reserve or quota): speak with
        Kokoro when it is ready, else raise so the browser voice takes over.
        The hand-over is recorded for the notice in the app."""
        to = "local" if self._kokoro_ready(settings) else "browser"
        reason = reason or f"{self._addon_name(mod_id)} is paused"
        if not self._last_fallback or self._last_fallback.get("reason") != reason:
            logger.warning("%s paused: %s; speaking with %s", mod_id, reason, to)
        self._fallback = {"reason": reason, "to": to, "at": time.time(), "addon": self._addon_name(mod_id)}
        self._last_fallback = self._fallback
        if to != "local":
            err = TTSError(reason + ". Kokoro is not ready, so the browser voice speaks instead.", 409)
            err.fallback = "browser"
            raise err
        model = self._kokoro_model(settings)
        voice = resolve_voice("")
        key = self._cache_key(text, "local", model, voice, speed)
        if use_cache:
            cached = self._get_cached(key)
            if cached:
                return cached
        audio = self._get_kokoro().synthesize(text, model, voice, speed)
        if audio and use_cache:
            self._put_cache(key, audio)
        return audio

    def current_fallback(self) -> Optional[Dict[str, Any]]:
        """The add-on hand-over that applied to the last synthesis, if any."""
        return self._fallback

    def synthesize(self, text: str, use_cache: bool = True, response_format: str = "mp3") -> Optional[bytes]:
        try:
            return self.synthesize_checked(text, use_cache=use_cache, response_format=response_format)
        except TTSError as e:
            logger.warning("TTS: %s", e.message)
            return None
        except Exception as e:
            logger.error("TTS synthesis failed: %s", e, exc_info=True)
            return None

    def synthesize_to_base64(self, text: str) -> Optional[str]:
        import base64
        audio = self.synthesize(text)
        if audio:
            return base64.b64encode(audio).decode("utf-8")
        return None

    def set_voice(self, voice: str):
        """Legacy no-op — voice is now managed via admin settings."""

    def engines(self) -> Dict[str, Any]:
        """The local Kokoro engine: installed, models, downloads, voices."""
        settings = self._load_settings()
        k = self._get_kokoro()
        selected = self._kokoro_model(settings)
        missing = missing_packages()
        reason = k.readiness(selected)
        loaded = k.loaded
        return {"engines": [{
            "id": "kokoro",
            "provider": "local",
            "label": "Kokoro (local)",
            "installed": not missing,
            "missing_packages": missing,
            "pip": PIP if missing else "",
            "setting": "tts_kokoro_model",
            "selected_model": selected,
            "default_model": default_model(),
            "ready": reason is None,
            "reason": reason or "",
            "loaded_model": loaded["model"] if loaded else "",
            "loading": k.loading,
            "models": [model_status(m, k.downloads, selected) for m in KOKORO_MODELS],
            "voices": VOICES,
            "voice": resolve_voice(settings["tts_voice"]),
        }], "models_dir": str(models_dir()), "cpu_threads": cpu_threads(), "vnni": cpu_has_vnni()}

    def get_stats(self) -> Dict[str, Any]:
        settings = self._load_settings()
        provider = settings["tts_provider"]
        tts_enabled = settings.get("tts_enabled", True)

        cache_files = list(self.cache_dir.glob("*.wav")) + list(self.cache_dir.glob("*.mp3"))
        cache_size = sum(f.stat().st_size for f in cache_files)

        is_available = self.available and tts_enabled
        stats = {
            "available": is_available,
            "ready": is_available,
            "provider": provider,
            "model": settings["tts_model"],
            "voice": settings["tts_voice"],
            "speed": _safe_speed(settings.get("tts_speed", "1")),
            # Server providers apply the speed themselves; play at 1x.
            "speed_applied": provider == "local" or provider.startswith(("endpoint:", MOD_PREFIX)),
            "cache_entries": len(cache_files),
            "cache_size_mb": round(cache_size / (1024 * 1024), 2),
        }

        if provider == "local":
            k = self._get_kokoro()
            model = self._kokoro_model(settings)
            reason = k.readiness(model)
            loaded = k.loaded
            stats.update({
                "engine": "kokoro",
                "model": model,
                "voice": resolve_voice(settings["tts_voice"]),
                "reason": reason or "",
                "model_loaded": bool(loaded and loaded["model"] == model),
                "loading": k.loading,
                "device": loaded["device"] if loaded else "",
                "load_seconds": loaded["load_s"] if loaded else None,
                "max_concurrent": k.max_concurrent,
            })
            if k.last:
                stats["last"] = dict(k.last)
                stats["last_latency_ms"] = k.last.get("latency_ms")
        elif provider == "browser":
            stats["model"] = "Browser (Web Speech API)"
        elif mod_id_of(provider):
            mod_id = mod_id_of(provider)
            st = self.mod_status(mod_id)
            paused = bool(st.get("paused")) and is_available
            fallback = ("local" if self._kokoro_ready(settings) else "browser") if paused else ""
            stats.update({
                "engine": mod_id,
                "addon": {"id": mod_id, "name": st.get("label") or self._addon_name(mod_id),
                          "installed": bool(st.get("installed"))},
                "model": str(st.get("model") or ""),
                "ready": is_available,
                "reason": "" if is_available else str(st.get("reason") or ""),
                "speed_applied": st.get("speed_applied", True) is not False,
                # The add-on's per-reply cap (0 = none): auto-read stops past it.
                "max_reply_chars": int(_safe_num(st.get("max_reply_chars"), 0)),
                "effective_provider": fallback or provider,
                "fallback_reason": str(st.get("pause_reason") or "") if paused else "",
            })
            last = getattr(self, "_last_api", None)
            if last:
                stats["last"] = dict(last)
                stats["last_latency_ms"] = last["latency_ms"]
        elif provider.startswith("endpoint:"):
            stats["endpoint_id"] = provider.split(":", 1)[1]
            last = getattr(self, "_last_api", None)
            if last:
                stats["last"] = dict(last)
                stats["last_latency_ms"] = last["latency_ms"]

        return stats


def _json(r) -> Dict[str, Any]:
    try:
        d = r.json()
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _status_for(code: int) -> int:
    """An add-on's error status as one for the app: never 401 (the app reads
    any 401 from /api as its own sign-out), 502 for the add-on's own errors."""
    if code in (401, 403):
        return 400
    return 502 if code >= 500 else code


# Module-level singleton
_tts_service = None

def get_tts_service() -> TTSService:
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service
