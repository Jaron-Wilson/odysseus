"""Text to speech for the OpenAI-compatible gateway: ``POST /v1/audio/speech`` and the speech entries of ``GET /v1/models``.

It does not speak by itself. Every request goes through Odysseus's own TTS service (services/tts/tts_service.py), so the
add-on's credit-reserve fallback and the audio cache are the ones the web app uses. The routes are in
routes/openai_gateway_routes.py.

Models (ids as ``GET /v1/models`` lists them, all "owned by" Odysseus voice), the speech side of the list the way the
chat side has the ``auto`` models:

  tts:auto            the first backend below that works speaks: the add-on voices (ElevenLabs) while they are ready
                      and not paused, then the speech endpoints (Kokoro on another machine), then Kokoro on this server.
                      A failure (quota, 401, 429, 5xx, timeout) goes on to the next one. All failing is one error.
  tts:elevenlabs      an add-on voice (tts:<add-on id>), listed only while the add-on is installed and ready
  tts:<endpoint>      a registered model endpoint that speaks (src/openai_gateway.py: speech_models), named like chat
                      endpoints are (the slug of its name), e.g. tts:kokoro-desktop
  tts:kokoro          local Kokoro on the Odysseus server

``tts``, ``tts-1``, ``tts-1-hd`` and ``gpt-4o-mini-tts`` are other names for ``tts:auto`` so OpenAI clients work
unchanged. They are not listed. The response says which backend spoke (``X-Odysseus-Voice``).

Voice: the OpenAI voice names (alloy, echo, nova ...) are not voices of any of these backends, so they are ignored and
the backend's own choice speaks. Any other value is passed on as the backend's voice id. A voice the backend that ends
up speaking does not have (af_heart on ElevenLabs, an ElevenLabs voice on Kokoro) is replaced by that backend's default
instead of failing the request, so falling through to another backend always works.
"""
import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src import openai_gateway as gw

logger = logging.getLogger(__name__)

OWNED_BY = "Odysseus voice"
MAX_INPUT_CHARS = 4096                  # OpenAI's limit for input
AUTO = "tts:auto"
ALIASES = {"tts", "tts-1", "tts-1-hd", "gpt-4o-mini-tts"}          # other names for tts:auto
OPENAI_VOICES = {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse",
                 "marin", "cedar"}
FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav"}
UNSUPPORTED_FORMATS = {"opus", "aac", "flac", "pcm"}     # OpenAI has these; Odysseus's voices do not make them
SPEED_MIN, SPEED_MAX = 0.25, 4.0                         # OpenAI's range
VOICE_LIST_TTL = 60.0


class SpeechError(Exception):
    """A request that cannot be answered: shown to the caller as an OpenAI-style error."""

    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass
class Target:
    id: str                              # the id the client used or sees: "tts:auto", "tts:kokoro", ...
    kind: str                            # auto | local | mod | endpoint
    provider: Optional[str] = None       # the TTS service's provider string to force
    model: Optional[str] = None          # an endpoint's speech model
    label: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ── what exists ─────────────────────────────────────────────────────────

def _service():
    from services.tts import get_tts_service
    return get_tts_service()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "endpoint"


def _mods() -> List[Dict[str, Any]]:
    """Enabled add-ons that provide a voice (tts.voice), elevenlabs first."""
    try:
        from src.mods import providers
        found = providers.providers("tts.voice")
    except Exception:
        logger.warning("gateway tts: could not list voice add-ons", exc_info=True)
        return []
    return sorted(found, key=lambda m: (str(m.get("id")) != "elevenlabs", str(m.get("id"))))


def _mod_of(provider: str) -> Optional[str]:
    from services.tts.tts_service import mod_id_of
    return mod_id_of(provider)


def _speech_endpoints(owner: Optional[str]) -> List[Tuple[str, Any, str]]:
    """(slug, endpoint row, speech model) of every enabled endpoint this owner may use that speaks: it has a model that is
    known to be a speech model, or it is the voice chosen in Settings. The voice endpoint comes first."""
    from core.database import ModelEndpoint
    from src.auth_helpers import owner_filter

    voice = gw.voice_setting()
    db = gw.SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        q = owner_filter(q, ModelEndpoint, owner or "")
        rows = sorted(q.all(), key=lambda e: (str(e.name or "").lower(), str(getattr(e, "created_at", "") or ""), str(e.id)))
        used = {"auto", "kokoro"} | {str(m.get("id")) for m in _mods()}
        out = []
        for ep in rows:
            if (getattr(ep, "model_type", None) or "llm") == "image":
                continue
            hidden = set(gw._json_list(getattr(ep, "hidden_models", None)))
            speech = [m for m in gw.speech_models(ep, voice) if m not in hidden]
            slug = _slug(ep.name)
            if slug in used:
                slug = f"{slug}-{str(ep.id)[:4]}"
            used.add(slug)
            if not speech and str(ep.id) == voice[0]:
                speech = [voice[1] or "tts-1"]        # the voice in Settings, on a server whose models are named oddly
            if not speech:
                continue
            db.expunge(ep)
            out.append((slug, ep, speech[0]))
        out.sort(key=lambda t: str(t[1].id) != voice[0])        # stable: the voice endpoint first
        return out
    finally:
        db.close()


def _endpoint_target(slug: str, ep: Any, model: str) -> Target:
    return Target(id=f"tts:{slug}", kind="endpoint", provider=f"endpoint:{ep.id}", model=model,
                  label=ep.name or slug, extra={"endpoint_id": ep.id, "base_url": ep.base_url})


def _enabled() -> bool:
    return _service()._load_settings().get("tts_enabled") is not False


def _mod_state(mid: str) -> Tuple[bool, str]:
    """(can speak now, why not) for an add-on voice: ready and not paused."""
    try:
        st = _service().mod_status(mid)
    except Exception:
        return False, "did not answer"
    if not st.get("ready"):
        return False, str(st.get("reason") or "not ready")
    if st.get("paused"):
        return False, str(st.get("pause_reason") or "paused")
    return True, ""


def catalog(owner: Optional[str]) -> List[Dict[str, Any]]:
    """The speech models for GET /v1/models: only the ones that can speak right now."""
    svc = _service()
    if not _enabled():
        return []
    out: List[Dict[str, Any]] = []
    names: List[str] = []

    def add(mid: str, what: str):
        out.append({"id": mid, "object": "model", "created": 0, "owned_by": OWNED_BY, "description": what})

    for m in _mods():
        mid = str(m.get("id"))
        try:
            ready = bool(svc.mod_status(mid).get("ready"))
        except Exception:
            ready = False
        if ready:
            name = svc._addon_name(mid)
            names.append(name)
            add(f"tts:{mid}", f"{name} add-on voice (falls back to Kokoro on the server when it is paused)")
    for slug, ep, model in _speech_endpoints(owner):
        names.append(ep.name or slug)
        add(f"tts:{slug}", f"{ep.name or slug}: speech model {model}")
    if svc._kokoro_ready(svc._load_settings()):
        names.append("Kokoro on the server")
        add("tts:kokoro", "Kokoro, running on the Odysseus server")
    if out:
        out.insert(0, {"id": AUTO, "object": "model", "created": 0, "owned_by": OWNED_BY,
                       "description": "tries each voice in turn until one speaks: " + ", ".join(names)})
    return out


def resolve(model_id: str, owner: Optional[str]) -> Target:
    """The Target for a requested model. Raises SpeechError (404 for a name that does not exist)."""
    wanted = (model_id or "").strip()
    low = wanted.lower()
    if low in ALIASES or low == AUTO:
        return Target(id=AUTO, kind="auto", label="the first voice that works")
    if low.startswith("tts:"):
        name = low[4:]
        if name == "kokoro":
            return Target(id="tts:kokoro", kind="local", provider="local", label="Kokoro")
        for slug, ep, model in _speech_endpoints(owner):
            if slug == name:
                return _endpoint_target(slug, ep, model)
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
        raise _bad("'model' is required, for example 'tts:auto' (GET /v1/models lists them)", "missing_model")
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
        voice = ""                          # an OpenAI name: not a voice of ours, so the backend's own choice speaks
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


# ── voices ──────────────────────────────────────────────────────────────

_voice_cache: Dict[str, Tuple[float, Optional[set]]] = {}


def _addon_voice_ids(mid: str) -> Optional[set]:
    """The voice ids (and lowercase names) an add-on has, or None when that cannot be told. Cached for a minute."""
    hit = _voice_cache.get(mid)
    if hit and time.time() - hit[0] < VOICE_LIST_TTL:
        return hit[1]
    ids: Optional[set]
    try:
        voices = _service().mod_voices(f"mod:{mid}")
        ids = set()
        for v in voices:
            ids.add(str(v.get("voice_id")))
            if v.get("name"):
                ids.add(str(v["name"]).lower())
    except Exception:
        ids = None
    _voice_cache[mid] = (time.time(), ids)
    return ids


def _voice_for(step: Target, voice: str) -> str:
    """The voice to ask this backend for: the one given if the backend can have it, else "" (its default). Only an
    add-on can say what it has up front; Kokoro and endpoints are tried and, if they refuse the voice, retried."""
    if not voice:
        return ""
    mod = _mod_of(step.provider or "")
    if mod:
        known = _addon_voice_ids(mod)
        if known is not None and voice not in known and voice.lower() not in known:
            return ""
    return voice


def _voice_refused(e) -> bool:
    """Did the backend turn down the voice (rather than being down)?"""
    status = int(getattr(e, "status", 0) or 0)
    return status in (400, 404, 422) or bool(re.search(r"answered (400|404|422)\b", str(getattr(e, "message", "") or e)))


# ── speaking ────────────────────────────────────────────────────────────

def _chain(owner: Optional[str]) -> Tuple[List[Target], List[str]]:
    """The backends tts:auto tries, in order, and the ones it skips without asking (and why)."""
    steps: List[Target] = []
    skipped: List[str] = []
    svc = _service()
    for m in _mods():
        mid = str(m.get("id"))
        ok, why = _mod_state(mid)
        if ok:
            steps.append(Target(id=f"tts:{mid}", kind="mod", provider=f"mod:{mid}", label=svc._addon_name(mid)))
        else:
            skipped.append(f"{mid}: {why}")
    for slug, ep, model in _speech_endpoints(owner):
        steps.append(_endpoint_target(slug, ep, model))
    steps.append(Target(id="tts:kokoro", kind="local", provider="local", label="Kokoro"))
    return steps, skipped


def _speak_one(step: Target, text: str, voice: str, speed: float, fmt: str, allow_fallback: bool) -> Tuple[bytes, Dict[str, Any]]:
    """One backend. Blocking. (audio, info) or SpeechError."""
    from services.tts.kokoro_local import TTSError
    svc = _service()
    mod = _mod_of(step.provider or "")
    if mod:
        from src.mods import providers
        if not providers.provider("tts.voice", mod):
            raise SpeechError(503, svc._missing_reason(mod), "voice_unavailable")
    started = time.time()

    def call(v: str) -> bytes:
        return svc.synthesize_checked(text, voice=v or None, speed=speed, response_format=fmt,
                                      provider=step.provider, model=step.model, allow_fallback=allow_fallback)
    use = _voice_for(step, voice)
    try:
        try:
            audio = call(use)
        except TTSError as e:
            if not (use and _voice_refused(e)):
                raise
            logger.info("gateway tts: %s does not have the voice %r; using its default", step.id, use)
            audio = call("")                    # a voice it does not have: its own default speaks instead
    except TTSError as e:
        raise _error_for(e)
    except SpeechError:
        raise
    except Exception:
        logger.error("gateway tts: synthesis failed", exc_info=True)
        raise SpeechError(502, "the voice failed to make the audio", "upstream_error")
    if not audio:
        raise SpeechError(502, "the voice sent no audio", "upstream_error")
    info: Dict[str, Any] = {"engine": step.provider, "fallback": None}
    fb = svc.current_fallback() if (mod and allow_fallback) else None
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


def speak(target: Target, text: str, voice: str, speed: Optional[float], fmt: str,
          owner: Optional[str] = None) -> Tuple[bytes, Dict[str, Any]]:
    """Blocking. (audio in the format asked for when it can be, info) or SpeechError.
    info: backend (the Target that spoke), format (what the bytes really are), fallback ({"to", "reason"} when an add-on
    was paused and Kokoro spoke for it), engine, skipped (what tts:auto passed over, and why)."""
    if not _enabled():
        raise SpeechError(503, "Text to speech is off.", "voice_unavailable")
    speed = 1.0 if speed is None else speed        # every model speaks at the speed asked, 1x if none
    if target.kind != "auto":
        audio, info = _speak_one(target, text, voice, speed, fmt, allow_fallback=True)
        info.update(backend=target, skipped=[])
        return audio, info
    steps, skipped = _chain(owner)
    errors: List[SpeechError] = []
    for step in steps:
        try:
            audio, info = _speak_one(step, text, voice, speed, fmt, allow_fallback=False)
        except SpeechError as e:
            logger.info("gateway tts: auto: %s did not speak (%s)", step.id, e.message)
            errors.append(e)
            skipped.append(f"{step.id[4:]}: {e.message}")
            continue
        info.update(backend=step, skipped=skipped)
        return audio, info
    msg = "no voice could speak: " + "; ".join(skipped)
    if all(e.status == 503 for e in errors):
        raise SpeechError(503, msg, "voice_unavailable")
    raise SpeechError(502, msg, "upstream_error")


def _ascii(s: str, n: int) -> str:
    return str(s).encode("ascii", "replace").decode("ascii").replace("\r", " ").replace("\n", " ")[:n]


def response_headers(info: Dict[str, Any]) -> Dict[str, str]:
    """Which backend spoke, what tts:auto passed over, and the notes the web app's /api/tts routes send when an add-on
    voice was paused for this audio."""
    h = {"X-Odysseus-Voice": info["backend"].id}
    if info.get("skipped"):
        h["X-Odysseus-Voice-Skipped"] = _ascii("; ".join(info["skipped"]), 300)
    fb = info.get("fallback")
    if fb:
        h["X-TTS-Fallback"] = str(fb.get("to", ""))
        h["X-TTS-Fallback-Reason"] = _ascii(fb.get("reason", ""), 200)
        h["X-Odysseus-Voice"] = "tts:kokoro"
    return h


# ── usage ───────────────────────────────────────────────────────────────

def record(owner: Optional[str], info: Dict[str, Any], chars: int) -> None:
    """One usage row with source "tts" for the backend that spoke (model = its id); the text length in characters goes
    in the input-token column (a voice has no tokens). Local Kokoro is free; add-on voices are their own bill, so they
    stay unpriced. Never raises."""
    try:
        from src import pricing, usage
        target = info["backend"]
        engine = str(info.get("engine") or "")
        model = "tts:kokoro" if engine == "local" else target.id
        kw: Dict[str, Any] = {"model": model, "input_tokens": int(chars), "owner": owner, "source": "tts"}
        if engine == "local":
            kw.update(provider="kokoro", billing_type=pricing.BILLING_LOCAL_FREE, cost_usd=0.0)
        elif engine.startswith("endpoint:"):
            eid = engine.split(":", 1)[1]
            kw.update(endpoint_url=target.extra.get("base_url") or _endpoint_url(eid), endpoint_id=eid)
        elif _mod_of(engine):
            kw.update(provider=_mod_of(engine))
        usage.record_usage(**kw)
    except Exception:
        logger.warning("gateway tts: could not record usage", exc_info=True)


def _endpoint_url(endpoint_id: str) -> Optional[str]:
    from core.database import ModelEndpoint
    db = gw.SessionLocal()
    try:
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
        return ep.base_url if ep else None
    finally:
        db.close()
