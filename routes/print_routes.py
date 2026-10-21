"""The 3D print helper page (static/js/printPanel.js, src/print_helper/).

  GET    /api/print/status                  packages, the local model, the queue, the settings
  GET    /api/print/config                  slicer settings and model choice
  PUT    /api/print/config                  change them
  GET    /api/print/jobs                    the uploads, newest first
  POST   /api/print/jobs                    upload an STL or 3MF (multipart "file"); starts the analysis
  GET    /api/print/jobs/{id}               analysis, options, result, chat, decisions, files
  DELETE /api/print/jobs/{id}
  POST   /api/print/jobs/{id}/chat          {message}: one turn with the local model
  PUT    /api/print/jobs/{id}/options       {options}: set by hand (logged as the user's decision)
  POST   /api/print/jobs/{id}/fix           {options?}: run the fixes (and the slice check) in the background
  POST   /api/print/jobs/{id}/analyze       analyze again
  POST   /api/print/jobs/{id}/cancel
  GET    /api/print/jobs/{id}/files/{name}  a render (PNG) or an output (STL, sliced 3MF)

Admin only: the slice check runs programs on the PC over SSH.
"""
import os

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from core.middleware import require_admin
from src.print_helper import jobs, missing_packages
from src.print_helper.pipeline import normalize_options
from src.upload_limits import format_byte_limit, read_byte_limit_env

UPLOAD_MAX_BYTES = read_byte_limit_env("ODYSSEUS_PRINT_UPLOAD_MAX_BYTES", 200 * 1024 * 1024)
MEDIA = {".png": "image/png", ".stl": "model/stl", ".3mf": "model/3mf"}


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Send JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Send a JSON object")
    return body


def _user(request: Request) -> str:
    return getattr(request.state, "current_user", None) or ""


def _job(job_id: str) -> dict:
    try:
        return jobs.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "No such model")


def _part_names(job: dict):
    parts = (job.get("analysis") or {}).get("parts") or []
    return [p["name"] for p in parts] or None


def _need_packages():
    miss = missing_packages()
    if miss:
        raise HTTPException(503, f"Install the 3D print helper's packages on the server first: pip install {' '.join(miss)}")


def setup_print_routes() -> APIRouter:
    router = APIRouter(tags=["print"])

    @router.get("/api/print/status")
    async def status(request: Request):
        require_admin(request)
        import asyncio
        from src.print_helper import assistant
        model = await asyncio.to_thread(assistant.model_info, _user(request) or None)
        return {"missing_packages": missing_packages(), "model": model, "queue": jobs.queue_state(),
                "config": jobs.load_config(), "upload_max": format_byte_limit(UPLOAD_MAX_BYTES)}

    @router.get("/api/print/config")
    async def get_config(request: Request):
        require_admin(request)
        return jobs.load_config()

    @router.put("/api/print/config")
    async def put_config(request: Request):
        require_admin(request)
        try:
            return jobs.save_config(await _body(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/api/print/jobs")
    async def list_jobs(request: Request):
        require_admin(request)
        return {"jobs": jobs.list_jobs()}

    @router.post("/api/print/jobs")
    async def upload(request: Request, file: UploadFile):
        require_admin(request)
        _need_packages()
        try:
            meta = jobs.new_job(file.filename or "", _user(request))
        except ValueError as e:
            raise HTTPException(400, str(e))
        size = 0
        try:
            with open(meta["path"], "wb") as f:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > UPLOAD_MAX_BYTES:
                        raise HTTPException(413, f"The file is bigger than {format_byte_limit(UPLOAD_MAX_BYTES)}")
                    f.write(chunk)
            if size < 84:
                raise HTTPException(400, "The file is empty")
        except HTTPException:
            jobs.delete_job(meta["id"])
            raise
        jobs.log_decision(meta["id"], "user", "upload", note=meta["file"])
        jobs.enqueue(meta["id"], "analyze")
        return jobs.get_job(meta["id"])

    @router.get("/api/print/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        require_admin(request)
        return _job(job_id)

    @router.delete("/api/print/jobs/{job_id}")
    async def delete_job(job_id: str, request: Request):
        require_admin(request)
        _job(job_id)
        jobs.delete_job(job_id)
        return {"ok": True}

    @router.post("/api/print/jobs/{job_id}/chat")
    async def chat(job_id: str, request: Request):
        require_admin(request)
        job = _job(job_id)
        body = await _body(request)
        msg = body.get("message", "")
        if not isinstance(msg, str):
            raise HTTPException(400, "message must be text")
        from src.print_helper import assistant
        out = await assistant.chat(job_id, msg, owner=_user(request) or None)
        if out.get("run"):
            if not job.get("analysis"):
                out["say"] += "\n\n(The analysis is still running; press Make it printable when it is done.)"
            else:
                try:
                    jobs.log_decision(job_id, "assistant", "run", out["options"], note=out.get("model") or "")
                    out["status"] = jobs.enqueue(job_id, "fix", out["options"])
                except RuntimeError as e:
                    out["say"] += f"\n\n({e})"
        return out

    @router.put("/api/print/jobs/{job_id}/options")
    async def put_options(job_id: str, request: Request):
        require_admin(request)
        job = _job(job_id)
        body = await _body(request)
        opts = normalize_options(body.get("options"), _part_names(job))
        old = job.get("options") or {}
        changed = {k: v for k, v in opts.items() if old.get(k) != v}
        jobs.write_json(os.path.join(jobs.job_dir(job_id), "options.json"), opts)
        if changed:
            jobs.log_decision(job_id, "user", "options", changed)
        return {"options": opts, "changed": changed}

    @router.post("/api/print/jobs/{job_id}/fix")
    async def fix(job_id: str, request: Request):
        require_admin(request)
        _need_packages()
        job = _job(job_id)
        if not job.get("analysis"):
            raise HTTPException(409, "Wait for the analysis to finish first")
        body = await _body(request)
        opts = normalize_options(body.get("options") if "options" in body else job.get("options"), _part_names(job))
        try:
            jobs.log_decision(job_id, "user", "run", opts)
            return {"status": jobs.enqueue(job_id, "fix", opts), "options": opts}
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @router.post("/api/print/jobs/{job_id}/analyze")
    async def analyze(job_id: str, request: Request):
        require_admin(request)
        _need_packages()
        _job(job_id)
        try:
            return {"status": jobs.enqueue(job_id, "analyze")}
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @router.post("/api/print/jobs/{job_id}/cancel")
    async def cancel(job_id: str, request: Request):
        require_admin(request)
        _job(job_id)
        ok = jobs.cancel(job_id)
        if ok:
            jobs.log_decision(job_id, "user", "cancel")
        return {"ok": ok}

    @router.get("/api/print/jobs/{job_id}/files/{name}")
    async def file(job_id: str, name: str, request: Request):
        require_admin(request)
        try:
            path = jobs.file_path(job_id, name)
        except KeyError:
            raise HTTPException(404, "No such file")
        ext = os.path.splitext(name)[1].lower()
        if ext not in MEDIA:
            raise HTTPException(404, "No such file")
        if ext == ".png":
            return FileResponse(path, media_type=MEDIA[ext], headers={"Cache-Control": "private, no-cache"})
        return FileResponse(path, media_type=MEDIA[ext], filename=name)

    return router
