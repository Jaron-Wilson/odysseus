"""Read-only transcript viewer routes -- /api/transcript/*.

Serves the parsed contents of a coder run's ``out.jsonl`` (see
``src/transcript_summaries.py``): a list of runs the caller owns, and each
run's detail -- thinking blocks (already capped server-side), tool calls with
their results, the parsed message/tool/result/turn counts, and a copy-able
resume command.

Read-only by design, which is what makes it safe to expose:

  * it opens only ``RUNS_DIR/<job_id>/out.jsonl`` (and, for a row's
    human-facing fields, the same history / live-job records the rest of the
    app already reads). It never reads ``subagents/`` or another run's files,
  * it never writes to a run directory, never touches a chat session, and
    never starts, resumes, edits or deletes anything,
  * the resume command it returns is a plain string for the user to run by
    hand. No route here sends a request to any agent engine.
"""

import json
import logging
import os
import re
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import claude_code_jobs as jobs
from src import transcript_summaries as summaries
from src.auth_helpers import _auth_disabled, get_current_user

logger = logging.getLogger(__name__)

# Same shape as routes/claude_code_routes.py: the job id is an 8-char hex
# token and is interpolated into a path, so it must stay a tight allowlist.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")

DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def _require_user(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        if _auth_disabled():
            return ""
        raise HTTPException(401, "Not authenticated")
    return user


def _record_for(run_id: str) -> Optional[Dict]:
    """The row's human-facing fields: prefer a live job (newest state), then
    the finished-run history line, then nothing. Mirrors what
    routes/claude_code_routes.py reads off the job and history file -- it adds
    no new source and writes nothing."""
    job = jobs.get(run_id)
    if job is not None:
        return {
            "id": job.id, "owner": job.owner, "action": job.action,
            "cwd": job.cwd, "model": job.model, "engine": job.engine,
            "started": job.started, "finished": job.finished,
            "status": job.status, "chat_session_id": job.chat_session_id,
            "chat_name": jobs._chat_name(job.chat_session_id),
        }
    try:
        with open(jobs.HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("id") == run_id:
                    return {
                        "id": rec["id"], "owner": rec.get("owner") or "",
                        "action": rec.get("action") or "",
                        "cwd": "", "model": rec.get("model") or "",
                        "engine": rec.get("engine") or "",
                        "started": rec.get("started"), "finished": rec.get("finished"),
                        "status": rec.get("status") or "done",
                        "chat_session_id": rec.get("chat_session_id") or "",
                        "chat_name": jobs._chat_name(rec.get("chat_session_id") or ""),
                    }
    except (OSError, ValueError):
        pass
    return None


def _owns(record: Optional[Dict], user: str) -> bool:
    """The caller may see a run when the run has no owner (a legacy local run)
    or the owner is them. Mirrors src/claude_code_jobs.py:visible_to."""
    if not record:
        return False
    owner = record.get("owner") or ""
    return not owner or not user or owner == user


def _run_dir(run_id: str) -> str:
    return os.path.join(jobs.RUNS_DIR, run_id)


def _row_for(run_id: str, record: Dict) -> Optional[Dict]:
    """A list row: the run's record plus, when available, the parsed summary's
    counts, preview and resume command (served from the on-disk summary cache)."""
    if record is None:
        return None
    detail = summaries.get_summary(_run_dir(run_id))
    out = {
        "id": record["id"],
        "engine": record.get("engine") or (detail or {}).get("engine") or "",
        "model": record.get("model") or (detail or {}).get("model") or "",
        "action": record.get("action") or "",
        "cwd": record.get("cwd") or "",
        "status": record.get("status") or (detail or {}).get("status") or "",
        "started": record.get("started") or (detail or {}).get("first"),
        "finished": record.get("finished") or (detail or {}).get("last"),
        "chat_session_id": record.get("chat_session_id") or "",
        "chat_name": record.get("chat_name") or "",
        "preview": (detail or {}).get("preview") or "",
        "resume_command": (detail or {}).get("resume_command") or "",
        "has_detail": detail is not None,
    }
    if detail is not None:
        out["counts"] = detail.get("counts") or {}
    return out


def _run_ids_in_listing() -> List[str]:
    """Every candidate run id, newest first: live jobs, then the finished-run
    history (read reversed so recent rows come first -- the file is append-
    only), then any run directory on disk (covers pre-history runs, as the
    DevOps page already does)."""
    ids: List[str] = []
    seen = set()
    for j in jobs.list_jobs(""):
        if j.id not in seen:
            ids.append(j.id)
            seen.add(j.id)
    try:
        with open(jobs.HISTORY_FILE, encoding="utf-8") as f:
            for line in reversed(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    rid = json.loads(line).get("id")
                except (ValueError, AttributeError):
                    continue
                if rid and rid not in seen:
                    ids.append(rid)
                    seen.add(rid)
    except OSError:
        pass
    try:
        for name in sorted(os.listdir(jobs.RUNS_DIR), reverse=True):
            if os.path.isdir(os.path.join(jobs.RUNS_DIR, name)) and name not in seen:
                ids.append(name)
                seen.add(name)
    except OSError:
        pass
    return ids


def setup_transcript_routes() -> APIRouter:
    router = APIRouter(tags=["transcript"])

    @router.get("/api/transcript/runs")
    async def list_transcripts(request: Request, limit: int = DEFAULT_LIMIT):
        """The runs the caller owns, newest first, with parsed counts. Cost is
        flat per row (one cached summary read plus the existing history /
        job lookup); the row carries just enough for the list, and the detail
        route is what carries the full block list."""
        user = _require_user(request)
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        rows = []
        for rid in _run_ids_in_listing():
            record = _record_for(rid)
            if not _owns(record, user):
                continue
            row = _row_for(rid, record)
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda r: (r.get("started") or 0, r.get("id")), reverse=True)
        return {"runs": rows[:limit]}

    @router.get("/api/transcript/runs/{job_id}")
    async def transcript_detail(request: Request, job_id: str):
        """One run, fully parsed: thinking (capped), tool calls with results,
        counts, preview and the copy-able resume command. Read-only; 404 when
        the caller does not own the run or its transcript is not (yet) a file."""
        user = _require_user(request)
        if not _JOB_ID_RE.fullmatch(job_id):
            raise HTTPException(400, "Invalid job id")
        record = _record_for(job_id)
        if not _owns(record, user):
            raise HTTPException(404, "No such transcript")
        detail = summaries.get_summary(_run_dir(job_id))
        if detail is None:
            raise HTTPException(404, "No transcript to show for this run yet")
        return {
            "id": record["id"],
            "engine": record.get("engine") or detail["engine"],
            "model": record.get("model") or detail.get("model") or "",
            "action": record.get("action") or "",
            "cwd": record.get("cwd") or "",
            "status": record.get("status") or detail.get("status") or "",
            "started": record.get("started") or detail.get("first"),
            "finished": record.get("finished") or detail.get("last"),
            "chat_session_id": record.get("chat_session_id") or "",
            "chat_name": record.get("chat_name") or "",
            "final_text": detail.get("final_text") or "",
            "counts": detail.get("counts") or {},
            "preview": detail.get("preview") or "",
            "truncated": bool(detail.get("truncated")),
            "blocks": detail.get("blocks") or [],
            "resume_command": detail.get("resume_command") or "",
        }

    return router
