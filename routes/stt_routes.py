# routes/stt_routes.py
"""STT API routes: multi-provider (local Whisper or Parakeet, API endpoint, browser)."""

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from starlette.concurrency import run_in_threadpool
import logging

from core.middleware import require_admin
from services.stt.local_models import STTError
from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES

logger = logging.getLogger(__name__)


def setup_stt_routes(stt_service):
    """Setup STT routes with the provided STT service"""
    router = APIRouter(prefix="/api/stt", tags=["stt"])

    @router.get("/stats")
    async def get_stt_stats(warm: bool = True):
        """Engine, model, loaded, device and last latency. Also starts loading
        the selected local model in the background, so a voice call that
        checks this on start has it ready by the first turn."""
        try:
            stats = stt_service.get_stats()
            if warm and hasattr(stt_service, "warm") and stats.get("ready") and not stats.get("model_loaded"):
                stats["loading"] = stt_service.warm() or stats.get("loading", False)
            return stats
        except Exception as e:
            logger.error(f"Failed to get STT stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/engines")
    async def get_stt_engines():
        """Local engines: installed or not (and the pip command), and each
        model's size, download state and folder."""
        return await run_in_threadpool(stt_service.engines)

    @router.post("/download")
    async def download_stt_model(request: Request):
        """Admin: download a local model now (runs in the background; poll
        /api/stt/engines for progress)."""
        require_admin(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        engine = str((body or {}).get("engine") or "")
        model = str((body or {}).get("model") or "")
        if not engine or not model:
            raise HTTPException(status_code=400, detail={"message": "engine and model are required"})
        try:
            return stt_service.start_download(engine, model)
        except STTError as e:
            raise HTTPException(status_code=e.status, detail={"message": e.message})

    @router.post("/transcribe")
    async def transcribe_audio(file: UploadFile = File(...)):
        """Transcribe uploaded audio file to text"""
        audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
        if not audio_bytes:
            raise HTTPException(status_code=400, detail={"message": "Empty audio file"})
        try:
            # Decoding, model loading and inference all block: keep them off
            # the event loop so one voice turn does not stall the whole app.
            text = await run_in_threadpool(stt_service.transcribe_checked, audio_bytes)
            return {"text": text}
        except STTError as e:
            raise HTTPException(status_code=e.status, detail={"message": e.message})
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": f"Transcription failed: {str(e)}"}
            )

    return router
