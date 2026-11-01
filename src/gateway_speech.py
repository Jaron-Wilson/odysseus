"""Text to speech for the OpenAI-compatible gateway: ``POST /v1/audio/speech`` and the speech entries of ``GET /v1/models``.

It does not speak by itself. Every request goes through Odysseus's own TTS service (services/tts/tts_service.py), so the
voice settings, the credit-reserve fallback from an add-on voice to Kokoro, and the audio cache are the ones the web
app uses. The routes are in routes/openai_gateway_routes.py.

Models (ids as ``GET /v1/models`` lists them, "owned by" Odysseus voice):

  tts                 whatever Odysseus is set to speak with (Settings > Text to speech)
  tts:kokoro          local Kokoro on the server
  tts:<add-on id>     an add-on voice, e.g. tts:elevenlabs (listed only while the add-on is installed and ready)
  tts:<endpoint>      a registered model endpoint that has a speech model (a model whose name has "tts" in it),
                      named like chat endpoints are (the slug of its name)

``tts-1``, ``tts-1-hd`` and ``gpt-4o-mini-tts`` are accepted as other names for ``tts`` so OpenAI clients work
unchanged. They are not listed.

Voice: the OpenAI voice names (alloy, echo, nova ...) are not voices of any of these providers, so they are ignored and
the provider's own choice speaks (for ``tts``, the voice saved in Settings). Any other value is passed to the provider
as its voice id: a Kokoro voice (af_heart, bf_emma, a blend such as af_heart+af_bella), an add-on voice id. Kokoro
replaces a name it does not know with its default voice; an add-on or endpoint decides for itself.
"""
import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

OWNED_BY = "Odysseus voice"
MAX_INPUT_CHARS = 4096                  # OpenAI's limit for input
ALIASES = {"tts-1", "tts-1-hd", "gpt-4o-mini-tts"}
OPENAI_VOICES = {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse",
                 "marin", "cedar"}
FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav"}
UNSUPPORTED_FORMATS = {"opus", "aac", "flac", "pcm"}     # OpenAI has these; Odysseus's voices do not make them
SPEED_MIN, SPEED_MAX = 0.25, 4.0                         # OpenAI's range


class SpeechError(Exception):
    """A request that cannot be answered: shown to the caller as an OpenAI-style error."""

    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass
class Target:
    id: str                              # the id the client used or sees: "tts", "tts:kokoro", ...
    kind: str                            # default | local | mod | endpoint
    provider: Optional[str] = None       # the TTS service's provider string to force; None = the saved one
    model: Optional[str] = None          # an endpoint's speech model
    label: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ── what exists ─────────────────────────────────────────────────────────

def _service():
    from services.tts import get_tts_service
    return get_tts_service()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "endpoint"


def _speech_endpoints(owner: Optional[str]) -> List[Tuple[str, Any, str]]:
    """(slug, endpoint row, speech model) of every enabled endpoint this owner may use that has a speech model."""
    from core.database import ModelEndpoint
    from src import openai_gateway as gw
    from src.auth_helpers import owner_filter
    from src.endpoint_resolver import _endpoint_enabled_models

    db = gw.SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        q = owner_filter(q, ModelEndpoint, owner or "")
        rows = sorted(q.all(), key=lambda e: (str(e.name or "").lower(), str(getattr(e, "created_at", "") or ""), str(e.id)))
        reserved = {"kokoro"} | {str(m.get("id")) for m in _mods()}
        out, used = [], set(reserved)
        for ep in rows:
            if (getattr(ep, "model_type", None) or "llm") == "image":
                continue
            hidden = set(gw._json_list(getattr(ep, "hidden_models", None)))
            models = [str(m) for m in list(_endpoint_enabled_models(ep)) + gw._json_list(getattr(ep, "pinned_models", None))]
            speech = [m for m in models if m not in hidden and "tts" in m.lower()]
            if not speech:
                continue
            slug = _slug(ep.name)
            if slug in used:
                slug = f"{slug}-{str(ep.id)[:4]}"
            used.add(slug)
            db.expunge(ep)
            out.append((slug, ep, speech[0]))
        return out
    finally:
        db.close()


def _mods() -> List[Dict[str, Any]]:
    """Enabled add-ons that provide a voice (tts.voice)."""
    try:
        from src.mods import providers
        return providers.providers("tts.voice")
    except Exception:
        logger.warning("gateway tts: could not list voice add-ons", exc_info=True)
        return []


def _saved_provider() -> Tuple[str, bool]:
    s = _service()._load_settings()
    return s["tts_provider"], s.get("tts_enabled") is not False


def catalog(owner: Optional[str]) -> List[Dict[str, Any]]:
    """The speech models for GET /v1/models: only the ones that can speak right now."""
    svc = _service()
    out: List[Dict[str, Any]] = []

    def add(mid: str, what: str):
        out.append({"id": mid, "object": "model", "created": 0, "owned_by": OWNED_BY, "description": what})

    provider, enabled = _saved_provider()
    if enabled and (provider == "local" or provider.startswith("endpoint:") or _mod_of(provider)):
        add("tts", "the voice Odysseus is set to speak with (Settings > Text to speech)")
    if svc._kokoro_ready(svc._load_settings()):
        add("tts:kokoro", "Kokoro, running on the Odysseus server")
    for m in _mods():
        mid = str(m.get("id"))
        try:
            ready = bool(svc.mod_status(mid).get("ready"))
        except Exception:
            ready = False
        if ready:
            add(f"tts:{mid}", f"{svc._addon_name(mid)} add-on voice (falls back to Kokoro when it is paused)")
    for slug, ep, model in _speech_endpoints(owner):
        add(f"tts:{slug}", f"{ep.name or slug}: speech model {model}")
    return out


def _mod_of(provider: str) -> Optional[str]:
    from services.tts.tts_service import mod_id_of
    return mod_id_of(provider)


def resolve(model_id: str, owner: Optional[str]) -> Target:
    """The Target for a requested model. Raises SpeechError (404 for a name that does not exist)."""
    wanted = (model_id or "").strip()
    low = wanted.lower()
    if low == "tts" or low in ALIASES:
        return Target(id="tts", kind="default", label="the voice Odysseus is set to use")
    if low.startswith("tts:"):
        name = low[4:]
        if name == "kokoro":
            return Target(id="tts:kokoro", kind="local", provider="local", label="Kokoro")
        for slug, ep, model in _speech_endpoints(owner):
            if slug == name:
                return Target(id=f"tts:{slug}", kind="endpoint", provider=f"endpoint:{ep.id}", model=model,
                              label=ep.name or slug, extra={"endpoint_id": ep.id, "base_url": ep.base_url})
        for m in _mods():
            if str(m.get("id")).lower() == name:
                mid = str(m.get("id"))
                return Target(id=f"tts:{mid}", kind="mod", provider=f"mod:{mid}", label=_service()._addon_name(mid))
        if name == "elevenlabs":
            raise SpeechError(404, _service()._missing_reason("elevenlabs"), "model_not_found")
    raise SpeechError(404, f"no speech model '{wanted}'; GET /v1/models lists them (ids starting with 'tts')",
                      "model_not_found")


# ── the request ─────────────────────────────────────────────────────────

def _bad(message: str, code: str = "invalid_request") -> SpeechError:
    return SpeechError(400, message, code)


def validate(body: Any) -> Dict[str, Any]:
    """The checked request: model, text, voice, speed, fmt. Raises SpeechError(400)."""
    if not isinstance(body, dict):
        raise _bad("send a JSON object with 'model', 'input' and 'voice'")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _bad("'model' is required, for example 'tts' (GET /v1/models lists them)", "missing_model")
    text = body.get("input")
    if not isinstance(text, str):
        raise _bad("'input' is required and must be a string", "missing_input")
    if not text.strip():
        raise _bad("'input' is empty; there is nothing to say", "empty_input")
    if len(text) > MAX_INPUT_CHARS:
        raise _bad(f"'input' is {len(text)} characters; the limit is {MAX_INPUT_CHARS}. Send it in smaller pieces.",
                   "string_above_max_length")
    fmt = body.get("response_format")
    fmt = "mp3" if fmt in (None, "") else str(fmt).strip().lower()
    if fmt not in FORMATS:
        what = "Odysseus's voices do not make it" if fmt in UNSUPPORTED_FORMATS else "it is not a known format"
        raise _bad(f"response_format '{fmt}' is not supported ({what}). Use 'mp3' (the default) or 'wav'.",
                   "unsupported_response_format")
    speed = body.get("speed")
    if speed is not None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not (SPEED_MIN <= float(speed) <= SPEED_MAX):
            raise _bad(f"'speed' must be a number from {SPEED_MIN} to {SPEED_MAX}", "invalid_speed")
        speed = float(speed)
    voice = body.get("voice")
    if isinstance(voice, dict):             # OpenAI's custom voice form: {"id": "voice_..."}
        voice = voice.get("id")
    if voice is not None and not isinstance(voice, str):
        raise _bad("'voice' must be a string", "invalid_voice")
    voice = (voice or "").strip()
    if voice.lower() in OPENAI_VOICES:
        voice = ""                          # an OpenAI name: not a voice of ours, so the provider's own choice speaks
    ins = body.get("instructions")
    if ins is not None and not isinstance(ins, str):
        raise _bad("'instructions' must be a string", "invalid_instructions")
    sf = body.get("stream_format")
    if sf not in (None, "", "audio"):
        raise _bad("stream_format 'sse' is not supported; the audio comes back as one response", "unsupported_stream_format")
    return {"model": model.strip(), "text": text, "voice": voice, "speed": speed, "fmt": fmt}


def sniff(data: bytes) -> Optional[str]:
    """'wav' or 'mp3' from the first bytes, else None."""
    if data[:4] == b"RIFF":
        return "wav"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    return None


def transcode(data: bytes, to: str) -> Optional[bytes]:
    """The same audio as mp3 or wav, or None when this server cannot (PyAV is an optional package)."""
    try:
        import av
    except Exception:
        return None
    try:
        src = av.open(io.BytesIO(data))
        try:
            ins = src.streams.audio[0]
            rate = ins.rate or 24000
            layout = "mono" if (ins.channels or 1) == 1 else "stereo"
            out = io.BytesIO()
            dst = av.open(out, "w", format="mp3" if to == "mp3" else "wav")
            try:
                ost = dst.add_stream("libmp3lame" if to == "mp3" else "pcm_s16le", rate=rate)
                ost.layout = layout
                rs = av.AudioResampler(format="s16p" if to == "mp3" else "s16", layout=layout, rate=rate)
                for frame in src.decode(ins):
                    for f in rs.resample(frame):
                        for p in ost.encode(f):
                            dst.mux(p)
                for f in rs.resample(None) or []:
                    for p in ost.encode(f):
                        dst.mux(p)
                for p in ost.encode(None):
                    dst.mux(p)
            finally:
                dst.close()
        finally:
            src.close()
        return out.getvalue() or None
    except Exception:
        logger.warning("gateway tts: could not convert audio to %s", to, exc_info=True)
        return None


def _error_for(e) -> SpeechError:
    """A TTSError as the gateway's error: 409 paused, 429 and 503 as they are, other 5xx 502, the rest 400."""
    status = int(getattr(e, "status", 503) or 503)
    msg = str(getattr(e, "message", "") or e)
    if getattr(e, "fallback", None) or status == 409:
        return SpeechError(409, msg, "voice_paused")
    if status == 429:
        return SpeechError(429, msg, "rate_limit_exceeded")
    if status == 503:
        return SpeechError(503, msg, "voice_unavailable")
    if status >= 500:
        return SpeechError(502, msg, "upstream_error")
    return SpeechError(400, msg, "voice_rejected")


def speak(target: Target, text: str, voice: str, speed: Optional[float], fmt: str) -> Tuple[bytes, Dict[str, Any]]:
    """Blocking. (audio in the format asked for when it can be, info) or SpeechError.
    info: format (what the bytes really are), fallback ({"to", "reason"} when an add-on was paused), engine."""
    from services.tts.kokoro_local import TTSError
    svc = _service()
    saved, _enabled = _saved_provider()
    provider = target.provider or saved
    mod = _mod_of(provider)
    if mod:
        from src.mods import providers
        if not providers.provider("tts.voice", mod):
            raise SpeechError(503, svc._missing_reason(mod), "voice_unavailable")
    if target.kind == "default":
        if provider == "browser":
            raise SpeechError(503, "Odysseus is set to the browser's voice, which the server cannot use. Choose a server "
                                   "voice in Settings > Text to speech, or ask for 'tts:kokoro'.", "voice_unavailable")
        if provider == "disabled":
            raise SpeechError(503, "Text to speech is off in Odysseus (Settings > Text to speech).", "voice_unavailable")
    # a model that names its own provider speaks at the speed asked (1x if none); 'tts' follows the saved speed
    if speed is None and target.kind != "default":
        speed = 1.0
    started = time.time()
    try:
        audio = svc.synthesize_checked(text, voice=voice or None, speed=speed, response_format=fmt,
                                       provider=target.provider, model=target.model)
    except TTSError as e:
        raise _error_for(e)
    except SpeechError:
        raise
    except Exception:
        logger.error("gateway tts: synthesis failed", exc_info=True)
        raise SpeechError(502, "the voice failed to make the audio", "upstream_error")
    if not audio:
        raise SpeechError(502, "the voice sent no audio", "upstream_error")
    info: Dict[str, Any] = {"engine": provider, "fallback": None}
    fb = svc.current_fallback() if mod else None
    if fb and float(fb.get("at") or 0) >= started - 1:
        info["fallback"] = {"to": str(fb.get("to", "")), "reason": str(fb.get("reason", ""))}
        info["engine"] = "local"
    have = sniff(audio)
    if have is None:
        raise SpeechError(502, "the voice sent audio in a format the gateway does not know", "upstream_error")
    if have != fmt:
        converted = transcode(audio, fmt)
        if converted:
            audio, have = converted, fmt
    info["format"] = have
    return audio, info


def fallback_headers(info: Dict[str, Any]) -> Dict[str, str]:
    """The same notes the web app's /api/tts routes send when an add-on voice was paused for this audio."""
    h: Dict[str, str] = {}
    fb = info.get("fallback")
    if fb:
        reason = str(fb.get("reason", "")).encode("ascii", "replace").decode("ascii")[:200]
        h["X-TTS-Fallback"] = str(fb.get("to", ""))
        h["X-TTS-Fallback-Reason"] = reason
    return h


# ── usage ───────────────────────────────────────────────────────────────

def record(owner: Optional[str], target: Target, chars: int, info: Dict[str, Any]) -> None:
    """One usage row with source "tts"; the text length in characters goes in the input-token column (a voice has no
    tokens). Local Kokoro is free; add-on voices are their own bill, so they stay unpriced. Never raises."""
    try:
        from src import pricing, usage
        engine = str(info.get("engine") or "")
        kw: Dict[str, Any] = {"model": target.id, "input_tokens": int(chars), "owner": owner, "source": "tts"}
        if engine == "local":
            kw.update(provider="kokoro", billing_type=pricing.BILLING_LOCAL_FREE, cost_usd=0.0)
        elif engine.startswith("endpoint:"):
            url = target.extra.get("base_url")
            if not url:
                url = _endpoint_url(engine.split(":", 1)[1])
            kw.update(endpoint_url=url, endpoint_id=engine.split(":", 1)[1])
        elif _mod_of(engine):
            kw.update(provider=_mod_of(engine))
        usage.record_usage(**kw)
    except Exception:
        logger.warning("gateway tts: could not record usage", exc_info=True)


def _endpoint_url(endpoint_id: str) -> Optional[str]:
    from core.database import ModelEndpoint
    from src import openai_gateway as gw
    db = gw.SessionLocal()
    try:
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
        return ep.base_url if ep else None
    finally:
        db.close()
