"""Claude Code runs started from a chat, so they can be sent to the background
and watched.

A foreground claude_code run holds the chat open until the CLI exits, which
for real work is many minutes. Every run is registered here, and the tool
card offers "Send to background": the process keeps running, the chat's turn
ends at once with a note, and the result is posted into the chat when the run
finishes. The Background tasks panel lists these runs with their live output
and a Stop button.

In memory: a run belongs to this server process (the CLI dies with it, see
_die_with_parent in the tool), so there is nothing to recover after a
restart. Finished runs are kept for a while so their output can still be read.
"""

import asyncio
import collections
import time
import uuid
from typing import Deque, Dict, List, Optional

MAX_LINES = 2000            # full transcript kept per run
KEEP_FINISHED_S = 6 * 3600
MAX_FINISHED = 50


class Job:
    def __init__(self, *, chat_session_id: str, owner: str, action: str,
                 cwd: str, model: str, engine: str, prompt: str):
        self.id = uuid.uuid4().hex[:8]
        self.chat_session_id = chat_session_id or ""
        self.owner = owner or ""
        self.action = action
        self.cwd = cwd
        self.model = model
        self.engine = engine
        self.prompt = (prompt or "")[:300]
        self.cli_session_id = ""
        self.pid: Optional[int] = None
        self.proc = None
        self.task: Optional[asyncio.Task] = None   # the background continuation
        self.started = time.time()
        self.finished: Optional[float] = None
        self.status = "running"            # running | done | failed | stopped | timed_out
        self.detached = False              # sent to the background
        self.detach_event = asyncio.Event()
        self.lines: Deque[str] = collections.deque(maxlen=MAX_LINES)
        self.banner = ""
        self.result: Optional[Dict] = None
        self.notify: Optional[Dict] = None

    def public(self, *, lines: int = 0) -> Dict:
        out = {
            "id": self.id,
            "chat_session_id": self.chat_session_id,
            "action": self.action,
            "cwd": self.cwd,
            "model": self.model,
            "engine": self.engine,
            "prompt": self.prompt,
            "cli_session_id": self.cli_session_id,
            "pid": self.pid,
            "started": self.started,
            "finished": self.finished,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1),
            "status": self.status,
            "background": self.detached,
        }
        if lines:
            out["banner"] = self.banner
            out["lines"] = list(self.lines)[-lines:]
            if self.result is not None:
                out["result"] = (self.result.get("output") or self.result.get("error") or "")[:4000]
        return out


_JOBS: Dict[str, Job] = {}


def _prune() -> None:
    now = time.time()
    done = [j for j in _JOBS.values() if j.finished]
    for j in done:
        if now - j.finished > KEEP_FINISHED_S:
            _JOBS.pop(j.id, None)
    done = sorted((j for j in _JOBS.values() if j.finished), key=lambda j: j.finished)
    for j in done[:-MAX_FINISHED]:
        _JOBS.pop(j.id, None)


def register(**kw) -> Job:
    _prune()
    job = Job(**kw)
    _JOBS[job.id] = job
    return job


def get(job_id: str) -> Optional[Job]:
    return _JOBS.get(job_id)


def visible_to(job: Job, owner: str) -> bool:
    return not job.owner or not owner or job.owner == owner


def list_jobs(owner: str = "") -> List[Job]:
    _prune()
    return sorted((j for j in _JOBS.values() if visible_to(j, owner)),
                  key=lambda j: j.started, reverse=True)


def detach(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job or job.status != "running" or job.detached:
        return False
    job.detached = True
    job.detach_event.set()
    return True


def stop(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job or job.status != "running" or not job.proc:
        return False
    try:
        job.proc.kill()
    except Exception:
        return False
    job.status = "stopped"
    return True


def finish(job: Job, status: str, result: Optional[Dict] = None) -> None:
    if job.status == "running":
        job.status = status
    job.finished = time.time()
    job.result = result
