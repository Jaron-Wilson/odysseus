"""Claude Code runs started from a chat, so they can be sent to the background,
watched, and outlive a server restart.

A foreground claude_code run holds the chat open until the CLI exits, which
for real work is many minutes. Every run is registered here, and the tool
card offers "Send to background": the process keeps running, the chat's turn
ends at once with a note, and the result is posted into the chat when the run
finishes. The Background tasks panel lists these runs with their live output
and a Stop button.

Runs survive a server restart. The CLI runs in its own session, writing to
files in its run directory (RUNS_DIR/<job id>) rather than to pipes into this
process, and each running job is recorded in JOBS_FILE. After a restart the
tool reattaches to every recorded run whose process is still alive, follows it
to the end, and posts the result into its chat (claude_code_tool.reattach_runs).
Seen live: a restart during an approved run left the chat asking again, and
approving again started another CLI doing the same work beside the first.

Finished runs are kept in memory for a while so their output can still be read.
"""

import asyncio
import collections
import json
import logging
import os
import signal
import tempfile
import time
import uuid
from typing import Deque, Dict, List, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

MAX_LINES = 2000            # full transcript kept per run
KEEP_FINISHED_S = 6 * 3600
MAX_FINISHED = 50
RUNS_DIR = os.path.join(DATA_DIR, "claude_code_runs")
JOBS_FILE = os.path.join(DATA_DIR, "claude_code_jobs.json")
# A record older than this is not reattached: nothing legitimate runs that long
# (the tool's own timeout caps a run at an hour).
MAX_REATTACH_AGE_S = 12 * 3600


class Job:
    def __init__(self, *, chat_session_id: str, owner: str, action: str,
                 cwd: str, model: str, engine: str, prompt: str, job_id: str = ""):
        self.id = job_id or uuid.uuid4().hex[:8]
        self.chat_session_id = chat_session_id or ""
        self.owner = owner or ""
        self.action = action
        self.cwd = cwd
        self.model = model
        self.engine = engine
        self.prompt = (prompt or "")[:300]
        self.cli_session_id = ""
        self.plan_id = ""                  # execute: the approved plan it carries out
        self.pid: Optional[int] = None
        self.proc = None
        self.task: Optional[asyncio.Task] = None   # the background continuation
        self.started = time.time()
        self.finished: Optional[float] = None
        self.status = "running"            # running | done | failed | stopped | timed_out
        self.detached = False              # sent to the background
        self.attached = False              # brought back: a chat turn is following it again
        self.reattached = False            # picked up again after a server restart
        self.detach_event = asyncio.Event()
        self.lines: Deque[str] = collections.deque(maxlen=MAX_LINES)
        self.banner = ""
        self.result: Optional[Dict] = None
        self.notify: Optional[Dict] = None
        self.spec: Dict = {}               # what the tool needs to finish the run later
        # What the agent says it is doing (it appends to status.jsonl in the
        # run directory): {"state", "detail", "at"}, and the last few of them.
        self.agent_status: Optional[Dict] = None
        self.timeline: List[Dict] = []

    @property
    def run_dir(self) -> str:
        return os.path.join(RUNS_DIR, self.id)

    def public(self, *, lines: int = 0) -> Dict:
        out = {
            "id": self.id,
            "chat_session_id": self.chat_session_id,
            "action": self.action,
            "cwd": self.cwd,
            "model": self.model,
            "engine": self.engine,
            "prompt": display_prompt(self.prompt),
            "cli_session_id": self.cli_session_id,
            "plan_id": self.plan_id,
            "pid": self.pid,
            "started": self.started,
            "finished": self.finished,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1),
            "status": self.status,
            # Brought back and followed in its chat again: not background. The
            # chip kept offering "Bring back" for a run already back.
            "background": self.detached and not self.attached,
            "attached": bool(self.attached),
            "reattached": self.reattached,
            "chat_name": _chat_name(self.chat_session_id),
            "agent_status": shown_status(self.status, self.agent_status),
        }
        if lines:
            out["timeline"] = self.timeline[-20:]
            out["banner"] = self.banner
            out["lines"] = list(self.lines)[-lines:]
            if self.result is not None:
                out["result"] = (self.result.get("output") or self.result.get("error") or "")[:4000]
        return out

    def record(self) -> Dict:
        """What is persisted for a running job."""
        return {
            "id": self.id, "chat_session_id": self.chat_session_id, "owner": self.owner,
            "action": self.action, "cwd": self.cwd, "model": self.model,
            "engine": self.engine, "prompt": self.prompt,
            "cli_session_id": self.cli_session_id, "plan_id": self.plan_id,
            "pid": self.pid, "started": self.started, "detached": self.detached,
            "banner": self.banner, "notify": self.notify, "spec": self.spec,
            "server_pid": os.getpid(),
            "agent_status": self.agent_status, "timeline": self.timeline[-20:],
        }


# The run's own progress instructions (claude_code_tool.STATUS_INSTRUCTIONS)
# go to the agent after the task. The panel showed them as the title.
_STATUS_MARK = "--- Status for the user ---"


def display_prompt(prompt: str) -> str:
    """The task as the user would read it, without our instructions to the agent."""
    text = prompt or ""
    i = text.find(_STATUS_MARK)
    return text[:i].rstrip() if i >= 0 else text


def shown_status(status: str, agent_status: Optional[Dict]) -> Optional[Dict]:
    """The agent's last progress line, while it still means something. A
    finished run kept showing "working · Creating the worktree" next to
    "done"; its last step is not what it is doing any more."""
    if not agent_status:
        return None
    if status != "running" and agent_status.get("state") in ("working", "needs_input", None):
        return None
    return agent_status


def _chat_name(session_id: str) -> str:
    """The chat's title, so a run is shown with the chat it belongs to."""
    if not session_id:
        return ""
    try:
        from src.chat_queue import _session_title
        return _session_title(session_id) or ""
    except Exception:
        return ""


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


def save() -> None:
    """Persist the running jobs (atomic replace). Never raises: losing the
    record only costs the reattach after a restart, not the run itself."""
    try:
        recs = [j.record() for j in _JOBS.values() if j.status == "running" and j.pid]
        # Keep what another server process still follows (an old one finishing
        # its shutdown during a restart), so neither erases the other's runs.
        mine = {r["id"] for r in recs} | set(_JOBS)
        for r in load_records():
            sp = r.get("server_pid")
            if r["id"] not in mine and sp and sp != os.getpid() and pid_alive(sp):
                recs.append(r)
        os.makedirs(os.path.dirname(JOBS_FILE) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(JOBS_FILE) or ".",
                                   prefix=".cc_jobs_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(recs, f, indent=1)
        os.replace(tmp, JOBS_FILE)
    except Exception as e:
        logger.warning("Could not save Claude Code job records: %s", e)


def load_records() -> List[Dict]:
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [r for r in data if isinstance(r, dict) and r.get("id")] if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return []


def register(job_id: str = "", **kw) -> Job:
    _prune()
    if job_id and job_id in _JOBS and _JOBS[job_id].status != "running":
        _JOBS.pop(job_id)                  # a retried run of the same approval
    job = Job(job_id=job_id if job_id not in _JOBS else "", **kw)
    _JOBS[job.id] = job
    return job


def adopt(rec: Dict) -> Job:
    """A running job from before a restart, back in the registry."""
    job = Job(job_id=rec["id"], chat_session_id=rec.get("chat_session_id", ""),
              owner=rec.get("owner", ""), action=rec.get("action", ""),
              cwd=rec.get("cwd", ""), model=rec.get("model", ""),
              engine=rec.get("engine", ""), prompt=rec.get("prompt", ""))
    job.cli_session_id = rec.get("cli_session_id", "")
    job.plan_id = rec.get("plan_id", "")
    job.pid = rec.get("pid")
    job.started = float(rec.get("started") or time.time())
    job.detached = True                    # nothing holds a chat turn for it any more
    job.reattached = True
    job.banner = rec.get("banner", "")
    job.notify = rec.get("notify")
    job.spec = rec.get("spec") or {}
    job.agent_status = rec.get("agent_status")
    job.timeline = list(rec.get("timeline") or [])
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


def running_for(cli_session_id: str = "", plan_id: str = "", job_id: str = "") -> Optional[Job]:
    """The live run already doing this: the same CLI session, the same
    approved plan, or the run id the approval was given."""
    for j in _JOBS.values():
        if j.status != "running":
            continue
        if ((cli_session_id and j.cli_session_id == cli_session_id)
                or (plan_id and j.plan_id == plan_id)
                or (job_id and j.id == job_id)):
            return j
    return None


def detach(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if job and job.status == "running" and job.detached and job.attached:
        job.attached = False               # brought back, now sent away again
        return True
    if not job or job.status != "running" or job.detached:
        return False
    job.detached = True
    job.detach_event.set()
    save()
    return True


def pid_alive(pid: Optional[int], marker: str = "") -> bool:
    """Whether `pid` is still the run's process. `marker` (the run directory,
    which is on the wrapper's command line) guards against a reused pid."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except Exception:
        return False
    if marker:
        try:
            with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
            if marker not in cmd:
                return False
            with open(f"/proc/{int(pid)}/stat", "rb") as f:
                if f.read().split(b")")[-1].split()[0] == b"Z":
                    return False           # exited, not yet reaped
        except FileNotFoundError:
            return False
        except Exception:
            pass
    return True


def kill_run(job: Job) -> bool:
    """Kill the run's whole process group (the wrapper and the CLI under it)."""
    if job.pid:
        try:
            os.killpg(int(job.pid), signal.SIGKILL)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            pass
    if job.proc is not None:
        try:
            job.proc.kill()
            return True
        except Exception:
            return False
    return False


def stop(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job or job.status != "running" or not (job.proc or job.pid):
        return False
    if not kill_run(job):
        return False
    job.status = "stopped"
    return True


def finish(job: Job, status: str, result: Optional[Dict] = None) -> None:
    if job.status == "running":
        job.status = status
    job.finished = time.time()
    job.result = result
    save()
