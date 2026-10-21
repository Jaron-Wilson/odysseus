"""Job folders and the background queue.

  DATA_DIR/print-helper/config.json        slicer settings and the model choice
  DATA_DIR/print-helper/jobs/<id>/
      meta.json        file name, owner, created
      input/<file>     the upload
      status.json      queued / running (progress, message) / done / failed / canceled
      analysis.json    pipeline.analyze
      options.json     the options of the last fix
      result.json      pipeline.fix
      renders/*.png    before_<i>.png per part, after_<piece>.png per piece
      out/*.stl, *_sliced.3mf
      chat.json        the conversation with the assistant
      decisions.jsonl  every options change and run: who decided (assistant, user) and what

Jobs run one at a time, each in its own niced worker process (worker.py), so a long 45 degree roof on a
big part never slows the web server and can be canceled.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^[0-9a-f]{16}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NICE = 10
MAX_JOBS_KEPT = 40

_queue: List[Dict] = []
_state: Dict = {"runner": None, "proc": None, "job": None}


def root() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "print-helper")


def jobs_dir() -> str:
    return os.path.join(root(), "jobs")


def job_dir(job_id: str) -> str:
    if not ID_RE.match(job_id or ""):
        raise KeyError("no such job")
    d = os.path.join(jobs_dir(), job_id)
    if not os.path.isdir(d):
        raise KeyError("no such job")
    return d


def read_json(path: str, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# ── Settings ────────────────────────────────────────────────────────────────

CONFIG_KEYS = ("pc_host", "ssh_user", "bambu_exe", "profiles_dir", "machine", "process", "filament",
               "endpoint_id", "model")


def load_config() -> Dict:
    raw = read_json(os.path.join(root(), "config.json"), {}) or {}
    return {k: str(raw.get(k) or "") for k in CONFIG_KEYS}


def save_config(data: Dict) -> Dict:
    cfg = load_config()
    for k in CONFIG_KEYS:
        if k in data:
            v = data[k]
            if v is not None and not isinstance(v, str):
                raise ValueError(f"{k} must be text")
            v = (v or "").strip()
            if len(v) > 300 or any(c in v for c in "\r\n\0"):
                raise ValueError(f"{k} is not a valid value")
            if k == "pc_host" and v and not re.match(r"^[A-Za-z0-9.\-:]+$", v):
                raise ValueError("the PC must be a host name or an address")
            if k == "ssh_user" and v and not re.match(r"^[A-Za-z0-9._\-]+$", v):
                raise ValueError("the SSH user is not a valid user name")
            cfg[k] = v
    os.makedirs(root(), exist_ok=True)
    write_json(os.path.join(root(), "config.json"), cfg)
    return cfg


# ── Jobs ────────────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in (".stl", ".3mf"):
        raise ValueError("upload an .stl or .3mf file")
    stem = re.sub(r"[^A-Za-z0-9_.\-]+", "_", stem).strip("._") or "model"
    return stem[:80] + ext


def new_job(filename: str, owner: str = "") -> Dict:
    """Make the folder for an upload; the caller writes input/<file> and then calls enqueue."""
    import secrets
    fname = safe_filename(filename)
    job_id = secrets.token_hex(8)
    d = os.path.join(jobs_dir(), job_id)
    os.makedirs(os.path.join(d, "input"))
    meta = {"id": job_id, "file": fname, "original_name": os.path.basename(filename or "")[:200],
            "owner": owner or "", "created": time.time()}
    write_json(os.path.join(d, "meta.json"), meta)
    set_status(job_id, "new", message="uploaded")
    _prune()
    return {**meta, "path": os.path.join(d, "input", fname)}


def _prune() -> None:
    """Keep the newest jobs only."""
    try:
        ids = [j["id"] for j in list_jobs()]
    except Exception:
        return
    for job_id in ids[MAX_JOBS_KEPT:]:
        if not any(q["job"] == job_id for q in _queue) and _state.get("job") != job_id:
            shutil.rmtree(os.path.join(jobs_dir(), job_id), ignore_errors=True)


def set_status(job_id: str, state: str, **extra) -> Dict:
    d = os.path.join(jobs_dir(), job_id)
    st = {"state": state, "updated": time.time(), **extra}
    write_json(os.path.join(d, "status.json"), st)
    return st


def status(job_id: str) -> Dict:
    st = read_json(os.path.join(job_dir(job_id), "status.json"), {}) or {}
    if st.get("state") in ("queued", "running") and not _known(job_id):
        st = {**st, "state": "interrupted", "message": "Odysseus restarted while this was running; run it again"}
    if st.get("state") == "queued":
        st["position"] = next((i + 1 for i, q in enumerate(_queue) if q["job"] == job_id), None)
    return st


def _known(job_id: str) -> bool:
    return _state.get("job") == job_id or any(q["job"] == job_id for q in _queue)


def list_jobs(owner: Optional[str] = None) -> List[Dict]:
    out = []
    try:
        names = os.listdir(jobs_dir())
    except OSError:
        return out
    for job_id in names:
        if not ID_RE.match(job_id):
            continue
        meta = read_json(os.path.join(jobs_dir(), job_id, "meta.json"))
        if not meta or (owner is not None and meta.get("owner", "") not in ("", owner)):
            continue
        out.append({**meta, "status": status(job_id)})
    out.sort(key=lambda j: -j.get("created", 0))
    return out


def get_job(job_id: str) -> Dict:
    d = job_dir(job_id)
    files = sorted(os.listdir(os.path.join(d, "out"))) if os.path.isdir(os.path.join(d, "out")) else []
    return {
        **(read_json(os.path.join(d, "meta.json"), {}) or {}),
        "status": status(job_id),
        "analysis": read_json(os.path.join(d, "analysis.json")),
        "result": read_json(os.path.join(d, "result.json")),
        "options": read_json(os.path.join(d, "options.json")),
        "chat": read_json(os.path.join(d, "chat.json"), []) or [],
        "decisions": decisions(job_id),
        "files": [f for f in files if FILE_RE.match(f) and not f.endswith(".tmp")],
    }


def delete_job(job_id: str) -> None:
    d = job_dir(job_id)
    if _state.get("job") == job_id:
        cancel(job_id)
    _queue[:] = [q for q in _queue if q["job"] != job_id]
    shutil.rmtree(d, ignore_errors=True)


def file_path(job_id: str, name: str) -> str:
    """A render or an output file of a job, by plain name only."""
    d = job_dir(job_id)
    if not FILE_RE.match(name or "") or name.startswith("."):
        raise KeyError("no such file")
    for sub in ("renders", "out"):
        p = os.path.join(d, sub, name)
        if os.path.isfile(p):
            return p
    raise KeyError("no such file")


# ── Decisions and chat ──────────────────────────────────────────────────────

def log_decision(job_id: str, source: str, what: str, options: Optional[Dict] = None, note: str = "") -> Dict:
    entry = {"at": time.time(), "source": source, "what": what}
    if options is not None:
        entry["options"] = options
    if note:
        entry["note"] = note[:1000]
    with open(os.path.join(job_dir(job_id), "decisions.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info("print helper %s: %s decided %s %s", job_id, source, what, json.dumps(options or {}))
    return entry


def decisions(job_id: str) -> List[Dict]:
    out = []
    try:
        with open(os.path.join(job_dir(job_id), "decisions.jsonl")) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out[-200:]


def save_chat(job_id: str, chat: List[Dict]) -> None:
    write_json(os.path.join(job_dir(job_id), "chat.json"), chat[-100:])


# ── The queue ───────────────────────────────────────────────────────────────

def enqueue(job_id: str, task: str, options: Optional[Dict] = None) -> Dict:
    if task not in ("analyze", "fix"):
        raise ValueError("unknown task")
    d = job_dir(job_id)
    if _known(job_id):
        raise RuntimeError("this model is already queued or running")
    if options is not None:
        write_json(os.path.join(d, "options.json"), options)
    _queue.append({"job": job_id, "task": task})
    st = set_status(job_id, "queued", task=task, message="waiting for the job before it" if _state.get("job") else "starting")
    runner = _state.get("runner")
    if runner is None or runner.done():
        _state["runner"] = asyncio.get_running_loop().create_task(_run_queue())
    return st


def queue_state() -> Dict:
    return {"running": _state.get("job"), "queued": [q["job"] for q in _queue]}


def cancel(job_id: str) -> bool:
    if any(q["job"] == job_id for q in _queue):
        _queue[:] = [q for q in _queue if q["job"] != job_id]
        set_status(job_id, "canceled", message="canceled before it started")
        return True
    if _state.get("job") == job_id and _state.get("proc") is not None:
        _state["canceled"] = True
        try:
            _state["proc"].kill()
        except ProcessLookupError:
            pass
        return True
    return False


def worker_argv(job_path: str, task: str) -> List[str]:
    python = os.environ.get("ODYSSEUS_PRINT_PYTHON") or sys.executable
    return [python, "-m", "src.print_helper.worker", job_path, task, str(NICE)]


async def _run_queue() -> None:
    while _queue:
        item = _queue.pop(0)
        job_id, task = item["job"], item["task"]
        try:
            d = job_dir(job_id)
        except KeyError:
            continue
        _state.update(job=job_id, canceled=False)
        set_status(job_id, "running", task=task, progress=0.0, message="starting", started=time.time())
        err = b""
        try:
            proc = await asyncio.create_subprocess_exec(
                *worker_argv(d, task), cwd=BASE_DIR, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            _state["proc"] = proc
            _, err = await proc.communicate()
            rc = proc.returncode
        except Exception as e:      # could not start the worker at all
            rc, err = -1, str(e).encode()
        finally:
            _state["proc"] = None
        st = read_json(os.path.join(d, "status.json"), {}) or {}
        if _state.get("canceled"):
            set_status(job_id, "canceled", task=task, message="canceled")
        elif rc == 0 and st.get("state") == "done":
            pass
        else:
            tail = (err or b"").decode(errors="replace").strip().splitlines()
            msg = st.get("error") or (tail[-1] if tail else f"the worker stopped (exit {rc})")
            set_status(job_id, "failed", task=task, message=msg[:500])
            logger.warning("print helper %s %s failed: %s", job_id, task, "\n".join(tail[-15:]))
        _state.update(job=None, canceled=False)
