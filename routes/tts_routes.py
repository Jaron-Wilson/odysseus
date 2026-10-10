# routes/tts_routes.py
"""
TTS API routes: multi-provider (local Kokoro, API endpoint, browser).
"""

import base64
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core.middleware import require_admin
from services.tts.kokoro_local import TTSError

logger = logging.getLogger(__name__)

PREVIEW_TEXT = "Hi, I'm your assistant. This is how I sound when I read a reply out loud."


class TTSRequest(BaseModel):
    text: str
    format: str = "audio"  # "audio" or "base64"


class PreviewRequest(BaseModel):
    voice: Optional[str] = None
    speed: Optional[float] = None
    text: Optional[str] = None


class ElevenLabsKeyRequest(BaseModel):
    api_key: str


def _tts_error(e: TTSError) -> HTTPException:
    detail = {"message": e.message}
    if getattr(e, "fallback", None):
        detail["fallback"] = e.fallback
    return HTTPException(status_code=e.status, detail=detail)


def _fallback_headers(tts_service) -> dict:
    """A short note for the app when ElevenLabs was paused for this audio."""
    fb = tts_service.current_fallback() if hasattr(tts_service, "current_fallback") else None
    if not fb:
        return {}
    # Header values must be latin-1; the reason is plain ASCII text.
    reason = str(fb.get("reason", "")).encode("ascii", "replace").decode("ascii")[:200]
    return {"X-TTS-Fallback": str(fb.get("to", "")), "X-TTS-Fallback-Reason": reason}


def _audio_response(audio_data: bytes, headers: Optional[dict] = None) -> Response:
    # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
    is_mp3 = audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[0] == 0xff and (audio_data[1] & 0xe0) == 0xe0)
    mime = "audio/mpeg" if is_mp3 else "audio/wav"
    return Response(
        content=audio_data,
        media_type=mime,
        headers={"Content-Disposition": "inline; filename=speech.mp3" if is_mp3 else "inline; filename=speech.wav",
                 **(headers or {})},
    )


def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.get("/stats")
    async def get_tts_stats(warm: bool = True):
        """Provider, model, voice, speed, loaded, device and last latency.
        Also starts loading the local Kokoro model in the background, so a
        voice call that checks this on start speaks its first sentence sooner."""
        try:
            # The ElevenLabs credit numbers may need a (cached) API call.
            stats = await run_in_threadpool(tts_service.get_stats)
            if warm and hasattr(tts_service, "warm") and stats.get("available") and stats.get("engine") == "kokoro" \
                    and not stats.get("model_loaded"):
                stats["loading"] = tts_service.warm() or stats.get("loading", False)
            return stats
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/engines")
    async def get_tts_engines():
        """The local Kokoro engine: installed or not (and the pip command),
        each model's size and download state, and the voices."""
        return await run_in_threadpool(tts_service.engines)

    @router.post("/download")
    async def download_tts_model(request: Request):
        """Admin: download a Kokoro model now (background; poll /engines)."""
        require_admin(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = str((body or {}).get("model") or "")
        if not model:
            raise HTTPException(status_code=400, detail={"message": "model is required"})
        try:
            return tts_service.start_download(model)
        except TTSError as e:
            raise HTTPException(status_code=e.status, detail={"message": e.message})

    @router.post("/synthesize")
    async def synthesize_speech(request: TTSRequest):
        """Synthesize speech from text. The voice call sends one sentence per
        request, so it can play the first while the next is made."""
        try:
            # Synthesis and model loading block: keep them off the event loop.
            audio_data = await run_in_threadpool(tts_service.synthesize_checked, request.text)
        except TTSError as e:
            raise _tts_error(e)
        except Exception as e:
            logger.error(f"Synthesis error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail={"message": f"Synthesis failed: {str(e)}"})
        headers = _fallback_headers(tts_service)
        if request.format == "base64":
            out = {"audio": base64.b64encode(audio_data).decode("utf-8")}
            if headers:
                out["fallback"] = {"to": headers["X-TTS-Fallback"], "reason": headers["X-TTS-Fallback-Reason"]}
            return out
        return _audio_response(audio_data, headers)

    @router.post("/preview")
    async def preview_voice(request: PreviewRequest):
        """A short sample in `voice` at `speed` (the Settings preview button),
        without saving either."""
        text = (request.text or PREVIEW_TEXT)[:300]
        try:
            audio_data = await run_in_threadpool(
                lambda: tts_service.synthesize_checked(text, voice=request.voice or None, speed=request.speed))
        except TTSError as e:
            raise _tts_error(e)
        return _audio_response(audio_data, _fallback_headers(tts_service))

    # ── ElevenLabs ──
    # The key goes in through POST /elevenlabs/key and never comes back out:
    # status answers only whether one is set and a masked hint.

    @router.get("/elevenlabs/status")
    async def elevenlabs_status(refresh: bool = False):
        """Key set (masked hint), models, credits used/limit/reset, the
        local tally, the credit guard and the last fallback."""
        return await run_in_threadpool(tts_service.elevenlabs_status, refresh)

    @router.post("/elevenlabs/key")
    async def elevenlabs_set_key(request: Request, body: ElevenLabsKeyRequest):
        """Admin: check the key with ElevenLabs and store it encrypted."""
        require_admin(request)
        try:
            out = await run_in_threadpool(tts_service.set_elevenlabs_key, body.api_key)
        except TTSError as e:
            raise _tts_error(e)
        return out

    @router.delete("/elevenlabs/key")
    async def elevenlabs_delete_key(request: Request):
        """Admin: forget the stored ElevenLabs key."""
        require_admin(request)
        await run_in_threadpool(tts_service.delete_elevenlabs_key)
        return {"deleted": True}

    @router.get("/elevenlabs/voices")
    async def elevenlabs_voices(refresh: bool = False):
        """The account's voices (premade and the user's own clones): id,
        name, category, labels."""
        try:
            voices = await run_in_threadpool(tts_service.elevenlabs.voices, refresh)
        except TTSError as e:
            raise _tts_error(e)
        return {"voices": voices}

    @router.post("/clear-cache")
    async def clear_tts_cache():
        """Clear TTS cache"""
        try:
            tts_service.clear_cache()
            return {"success": True, "message": "Cache cleared"}
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
