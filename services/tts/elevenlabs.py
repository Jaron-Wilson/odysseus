# services/tts/elevenlabs.py
"""ElevenLabs text to speech: the HTTP client, the encrypted API key, the
credit numbers from the account, and a local tally of what was sent.

ElevenLabs bills per character (credits), so this module is careful about
three things: the key never leaves the server (it is stored encrypted with
src/api_key_manager.py and only a masked hint is ever returned), every
character sent is counted locally as a backup to the account numbers, and
the remaining credits are known well enough for TTSService to stop using
ElevenLabs before the month's quota runs out.

Set ODYSSEUS_ELEVENLABS_BASE_URL to point the client at a fake server in
tests; it defaults to the real API.
"""

import io
import re
import json
import logging
import os
import threading
import time
import wave
from typing import Any, Dict, List, Optional

import httpx

from services.tts.kokoro_local import TTSError

logger = logging.getLogger(__name__)

BASE_URL_ENV = "ODYSSEUS_ELEVENLABS_BASE_URL"
DEFAULT_BASE_URL = "https://api.elevenlabs.io"
KEY_NAME = "elevenlabs"  # entry in data/api_keys.json (encrypted)

# The fast, cheap model is the default: Flash costs half the credits per
# character of Multilingual v2 / v3 on API generations.
DEFAULT_MODEL = "eleven_flash_v2_5"
# "George", the premade voice the ElevenLabs docs use in their examples.
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"

# credits_per_char is ElevenLabs' published rate for subscription API use
# (Flash: "50% lower price per character"; Multilingual v2: "1 text character
# equals 1 credit"). It only feeds the local estimate; the account's own
# numbers from /v1/user/subscription win whenever they can be read.
MODELS: List[Dict[str, Any]] = [
    {"id": "eleven_flash_v2_5", "label": "Flash v2.5: fastest, half the credits", "credits_per_char": 0.5,
     "speed": True, "max_chars": 40000},
    {"id": "eleven_v4_turbo", "label": "v4 Turbo: high quality, low latency", "credits_per_char": 1.0,
     "speed": False, "max_chars": 10000},
    {"id": "eleven_multilingual_v2", "label": "Multilingual v2: stable, 1 credit per character",
     "credits_per_char": 1.0, "speed": True, "max_chars": 10000},
    {"id": "eleven_v3", "label": "v3: most expressive, 1 credit per character", "credits_per_char": 1.0,
     "speed": True, "max_chars": 5000},
    {"id": "eleven_v4", "label": "v4: highest quality, slower", "credits_per_char": 1.0,
     "speed": False, "max_chars": 10000},
]
_MODEL_BY_ID = {m["id"]: m for m in MODELS}

# ElevenLabs accepts a speed of roughly 0.7 to 1.2; outside that it answers 400.
SPEED_MIN, SPEED_MAX = 0.7, 1.2

# PCM for a phone call: 16 kHz is plenty for an 8 kHz phone line or a 16 kHz
# Meet, works on every plan, and is a third of the bytes of 44.1 kHz.
PCM_RATE = 16000

SYNTH_TIMEOUT = httpx.Timeout(45.0, connect=10.0)
INFO_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
SUBSCRIPTION_TTL = 60.0     # seconds the account numbers are trusted as-is
VOICES_TTL = 300.0


def base_url() -> str:
    return (os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")


def model_info(model: str) -> Dict[str, Any]:
    return _MODEL_BY_ID.get(model or "", _MODEL_BY_ID[DEFAULT_MODEL])


def resolve_model(model: str) -> str:
    """A model id ElevenLabs knows. Unknown ids (a hand-typed newer model)
    pass through; empty or an OpenAI name falls back to the default."""
    m = (model or "").strip()
    if not m or not m.startswith("eleven_"):
        return DEFAULT_MODEL
    return m


# A Kokoro voice name ("af_heart", "bm_george+af_bella"), left over from that engine.
_KOKORO_VOICE = re.compile(r"^[a-z][fm]_[a-z]+(\+[a-z][fm]_[a-z]+)*$")


def resolve_voice(voice: str) -> str:
    """The saved voice id, or the default when it is empty or one of the
    other engines' defaults ("alloy", a Kokoro "af_heart")."""
    v = (voice or "").strip()
    if not v or v in ("alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer") or _KOKORO_VOICE.match(v):
        return DEFAULT_VOICE
    return v


def clamp_speed(speed: float) -> float:
    return round(max(SPEED_MIN, min(SPEED_MAX, float(speed or 1.0))), 2)


def pcm_to_wav(pcm: bytes, rate: int = PCM_RATE) -> bytes:
    """Raw 16-bit little-endian mono PCM as a WAV file."""
    pcm = pcm[: len(pcm) // 2 * 2]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def mask_key(key: str) -> str:
    """A hint that tells two keys apart without revealing either."""
    key = key or ""
    if len(key) < 8:
        return "set" if key else ""
    return "..." + key[-4:]


# ── Key storage ──

def _key_manager():
    from src.api_key_manager import APIKeyManager
    from src.constants import DATA_DIR
    return APIKeyManager(DATA_DIR)


def get_api_key() -> str:
    try:
        return _key_manager().load().get(KEY_NAME, "") or ""
    except Exception as e:  # never let a key file problem crash TTS
        logger.warning("Could not read the ElevenLabs key: %s", type(e).__name__)
        return ""


def save_api_key(key: str) -> None:
    _key_manager().save(KEY_NAME, (key or "").strip())


def delete_api_key() -> None:
    mgr = _key_manager()
    raw = mgr._load_raw()
    if KEY_NAME in raw:
        raw.pop(KEY_NAME, None)
        with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
            json.dump(raw, f)


# ── Errors ──

def _detail(r: httpx.Response) -> Dict[str, Any]:
    try:
        body = r.json()
    except Exception:
        return {}
    d = body.get("detail") if isinstance(body, dict) else None
    if isinstance(d, dict):
        return d
    if isinstance(d, str):
        return {"message": d}
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return {"message": d[0].get("msg", ""), "code": "validation_error"}
    return {}


class QuotaExceeded(TTSError):
    """The account is out of credits (TTSService falls back when it sees this)."""


class InvalidKey(TTSError):
    """ElevenLabs rejected the key. Answered to the app as 400, not 401: the
    app treats any 401 from /api as its own session ending and signs out."""


def raise_for_status(r: httpx.Response, voice: str = "") -> None:
    """Map an ElevenLabs error answer onto a TTSError a person can act on.
    The message never includes request headers, so the key cannot leak."""
    if r.status_code < 400:
        return
    d = _detail(r)
    code = str(d.get("code") or d.get("status") or "")
    msg = str(d.get("message") or "")[:200]
    if code == "quota_exceeded" or r.status_code == 402 or "quota" in code or "credit" in msg.lower() and r.status_code in (401, 402, 403):
        raise QuotaExceeded("ElevenLabs is out of credits for this month" + (f" ({msg})" if msg else "") + ".", 402)
    if r.status_code == 401:
        if code in ("missing_permissions", "unauthorized") or "permission" in msg.lower():
            raise TTSError("The ElevenLabs API key does not have permission for this" + (f": {msg}" if msg else "") + ".", 403)
        raise InvalidKey("ElevenLabs rejected the API key. Check it in Settings > AI Defaults > Voice call.", 400)
    if r.status_code == 403:
        raise TTSError("The ElevenLabs API key does not have permission for this" + (f": {msg}" if msg else "") + ".", 403)
    if r.status_code == 404 or code in ("voice_not_found", "invalid_voice_id"):
        if code == "model_not_found":
            raise TTSError("ElevenLabs does not know that model. Pick another one in Settings.", 400)
        raise TTSError(f"ElevenLabs has no voice with id {voice or '?'}. Pick another voice in Settings.", 404)
    if r.status_code == 429:
        what = "too many requests at once" if code == "concurrent_limit_exceeded" else "rate limited"
        raise TTSError(f"ElevenLabs is busy ({what}). Try again in a moment.", 429)
    if r.status_code in (400, 422):
        raise TTSError("ElevenLabs could not use that request" + (f": {msg}" if msg else "") + ".", 400)
    raise TTSError(f"ElevenLabs answered {r.status_code}" + (f": {msg}" if msg else "") + ".", 502)


# ── Local tally ──

class UsageTally:
    """Characters sent to ElevenLabs, per calendar month and since the last
    time the account numbers were read. Kept in DATA_DIR so a restart does
    not forget what was spent."""

    def __init__(self, path: Optional[str] = None):
        if path is None:
            from src.constants import DATA_DIR
            path = os.path.join(DATA_DIR, "elevenlabs_usage.json")
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("Could not save the ElevenLabs usage tally: %s", e)

    def add(self, chars: int, credits: float) -> None:
        month = time.strftime("%Y-%m")
        with self._lock:
            d = self._load()
            m = d.setdefault("months", {}).setdefault(month, {"chars": 0, "credits": 0.0, "requests": 0})
            m["chars"] += int(chars)
            m["credits"] = round(m["credits"] + float(credits), 2)
            m["requests"] += 1
            d["since_sync_credits"] = round(float(d.get("since_sync_credits", 0)) + float(credits), 2)
            d["total_chars"] = int(d.get("total_chars", 0)) + int(chars)
            self._save(d)

    def synced(self) -> None:
        """The account numbers were just read: they include everything so far."""
        with self._lock:
            d = self._load()
            d["since_sync_credits"] = 0.0
            self._save(d)

    def snapshot(self) -> Dict[str, Any]:
        d = self._load()
        month = time.strftime("%Y-%m")
        m = (d.get("months") or {}).get(month) or {"chars": 0, "credits": 0.0, "requests": 0}
        return {"month": month, "chars": m["chars"], "credits": m["credits"], "requests": m["requests"],
                "since_sync_credits": float(d.get("since_sync_credits", 0)),
                "total_chars": int(d.get("total_chars", 0))}


# ── Client ──

class ElevenLabsClient:
    def __init__(self, key_getter=get_api_key, tally: Optional[UsageTally] = None):
        self._key_getter = key_getter
        self._tally = tally
        self._sub: Optional[Dict[str, Any]] = None
        self._sub_at = 0.0
        self._sub_error = ""
        self._sub_try_at = 0.0      # last attempt, so an outage is not retried on every reply
        self._sub_adjusted = False  # cached numbers moved on by local synthesis since the read
        self._voices: Optional[List[Dict[str, Any]]] = None
        self._voices_at = 0.0
        self._lock = threading.Lock()

    @property
    def tally(self) -> UsageTally:
        if self._tally is None:
            self._tally = UsageTally()
        return self._tally

    def key(self) -> str:
        return self._key_getter() or ""

    def _headers(self, key: Optional[str] = None) -> Dict[str, str]:
        key = key if key is not None else self.key()
        if not key:
            raise TTSError("No ElevenLabs API key yet. Paste one in Settings > AI Defaults > Voice call.", 400)
        return {"xi-api-key": key, "Content-Type": "application/json"}

    def forget(self) -> None:
        """Drop cached account data (after the key changes)."""
        self._sub, self._sub_at, self._sub_error, self._sub_try_at = None, 0.0, "", 0.0
        self._sub_adjusted = False
        self._voices, self._voices_at = None, 0.0

    # Synthesis

    def synthesize(self, text: str, voice: str, model: str, speed: float = 1.0, wav: bool = False) -> bytes:
        """MP3 for the app, or WAV (PCM wrapped in a header) for a call."""
        voice = resolve_voice(voice)
        model = resolve_model(model)
        info = model_info(model)
        fmt = f"pcm_{PCM_RATE}" if wav else "mp3_44100_128"
        payload: Dict[str, Any] = {"text": text, "model_id": model}
        if info.get("speed", True) and abs(float(speed) - 1.0) > 1e-3:
            payload["voice_settings"] = {"speed": clamp_speed(speed)}
        url = f"{base_url()}/v1/text-to-speech/{voice}"
        headers = self._headers()
        try:
            r = httpx.post(url, params={"output_format": fmt}, json=payload, headers=headers, timeout=SYNTH_TIMEOUT)
        except httpx.TimeoutException:
            raise TTSError("ElevenLabs did not answer in time.", 504)
        except httpx.HTTPError as e:
            raise TTSError(f"Could not reach ElevenLabs ({type(e).__name__}).", 502)
        raise_for_status(r, voice)
        audio = r.content
        if not audio:
            raise TTSError("ElevenLabs sent no audio.", 502)
        # Count what was billed: the header when ElevenLabs sends one, else
        # the characters times the model's published rate.
        credits = len(text) * float(info.get("credits_per_char", 1.0))
        hdr = r.headers.get("character-cost") or r.headers.get("x-character-count")
        if hdr:
            try:
                credits = float(hdr)
            except ValueError:
                pass
        try:
            self.tally.add(len(text), credits)
        except Exception as e:
            logger.warning("ElevenLabs tally failed: %s", e)
        if self._sub is not None:
            # Keep the cached remaining in step until the next refresh.
            self._sub["character_count"] = float(self._sub.get("character_count") or 0) + credits
            self._sub_adjusted = True
        logger.info("ElevenLabs TTS: %d chars, model %s, %s, %d bytes", len(text), model, fmt, len(audio))
        return pcm_to_wav(audio, PCM_RATE) if wav else audio

    # Account

    def subscription(self, refresh: bool = False, key: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The account's credit numbers, cached for a minute. None when they
        cannot be read (no key, offline, a key without user_read)."""
        with self._lock:
            now = time.time()
            fresh = self._sub is not None and now - self._sub_at < SUBSCRIPTION_TTL
            tried = now - self._sub_try_at < SUBSCRIPTION_TTL
            if key is None and not refresh and (fresh or tried):
                return self._sub
            if key is None:
                self._sub_try_at = now
            try:
                r = httpx.get(f"{base_url()}/v1/user/subscription", headers=self._headers(key), timeout=INFO_TIMEOUT)
                raise_for_status(r)
                d = r.json()
            except TTSError as e:
                if key is not None:
                    raise
                self._sub_error = e.message
                return self._sub
            except (httpx.HTTPError, ValueError) as e:
                if key is not None:
                    raise TTSError(f"Could not reach ElevenLabs ({type(e).__name__}).", 502)
                self._sub_error = f"Could not reach ElevenLabs ({type(e).__name__})."
                return self._sub
            self._sub = parse_subscription(d)
            self._sub_at = time.time()
            self._sub_error = ""
            self._sub_adjusted = False
            try:
                self.tally.synced()
            except Exception:
                pass
            return self._sub

    def credits(self) -> Dict[str, Any]:
        """Used, limit, remaining and reset, from the account when it could be
        read; `estimated` when the cached numbers were advanced by the tally."""
        sub = self.subscription()
        out: Dict[str, Any] = {"known": False, "error": self._sub_error}
        if sub:
            limit = int(sub.get("character_limit") or 0)
            used = int(round(float(sub.get("character_count") or 0)))
            remaining = max(0, limit - used)
            out.update({
                "known": True, "used": used, "limit": limit, "remaining": remaining,
                "remaining_pct": round(remaining * 100.0 / limit, 1) if limit else 0.0,
                "reset_unix": sub.get("next_character_count_reset_unix"),
                "tier": sub.get("tier", ""), "status": sub.get("status", ""),
                "fetched_at": self._sub_at, "estimated": self._sub_adjusted,
            })
        return out

    def voices(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Every voice the account can use (premade, cloned, generated,
        professional), name and category; cached for five minutes."""
        if self._voices is not None and not refresh and time.time() - self._voices_at < VOICES_TTL:
            return self._voices
        headers = self._headers()
        out: List[Dict[str, Any]] = []
        token = None
        for _ in range(10):  # 100 per page, 1000 voices is plenty
            params: Dict[str, Any] = {"page_size": 100, "include_total_count": "false"}
            if token:
                params["next_page_token"] = token
            try:
                r = httpx.get(f"{base_url()}/v2/voices", params=params, headers=headers, timeout=INFO_TIMEOUT)
            except httpx.HTTPError as e:
                raise TTSError(f"Could not reach ElevenLabs ({type(e).__name__}).", 502)
            raise_for_status(r)
            d = r.json()
            out.extend(parse_voices(d))
            token = d.get("next_page_token")
            if not d.get("has_more") or not token:
                break
        out.sort(key=_voice_order)
        self._voices, self._voices_at = out, time.time()
        return out


def parse_subscription(d: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("tier", "status", "character_count", "character_limit", "next_character_count_reset_unix",
            "can_extend_character_limit", "character_refresh_period")
    return {k: d.get(k) for k in keep if k in d}


_CATEGORY_ORDER = {"cloned": 0, "professional": 1, "generated": 2, "premade": 3}


def parse_voices(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for v in (d.get("voices") or []):
        if not isinstance(v, dict) or not v.get("voice_id"):
            continue
        labels = v.get("labels") if isinstance(v.get("labels"), dict) else {}
        out.append({
            "voice_id": str(v["voice_id"]),
            "name": str(v.get("name") or v["voice_id"]),
            "category": str(v.get("category") or ""),
            "labels": {k: str(val) for k, val in labels.items() if isinstance(val, (str, int, float))},
            "preview_url": v.get("preview_url") or "",
        })
    out.sort(key=_voice_order)
    return out


def _voice_order(v: Dict[str, Any]):
    """The user's own voices first, then the rest by name."""
    return (_CATEGORY_ORDER.get(v["category"], 9), v["name"].lower())
