"""Buttons on Inbound mail for mail whose rule has a job runner.

Asked for on 2026-10-29: CleverNode submission emails should not start a chat;
"i can click a button and it does it", and the jobs a restart cut off need a
way to continue. A rule's `runner` is a script (CleverNode's
tools/fix_submission.py) that takes the batch ids written in the email:

    start <id> ... --json        status --batch <id> ... --json
    resume <job> --json          publish <job>          drop <job>

Odysseus only runs that script with ids read from the email (checked to be
UUIDs) and job ids from its own status output. No model reads the email, so
nothing in it can steer what runs. Publishing still waits for a click.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, List

logger = logging.getLogger(__name__)

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# "... fix_submission.py start <id> <id>" in the email CleverNode sends.
START_RE = re.compile(rf"\bstart((?:\s+{UUID})+)")
JOB_RE = re.compile(r"^[0-9a-f]{8}$")
WATCH_EVERY_S = 30
WATCH_FOR_S = 90 * 60
PUBLISH_TIMEOUT_S = 30 * 60

_publishing: Dict[str, Dict] = {}   # job -> {"state", "started", "finished", "output"}
_watching: set = set()


def _python() -> str:
    return shutil.which("python3") or sys.executable


def _rule_for(entry: Dict) -> Dict:
    from src import mail_listener
    cfg = mail_listener.load_config()
    for r in cfg.get("rules") or []:
        if r.get("address") == entry.get("rule"):
            return r
    return {}


def _setup(key: str):
    """The message, its rule's runner and the batch ids in it. Raises ValueError
    when this message has no job buttons."""
    from src import mail_listener
    entry = mail_listener._find(key)
    rule = _rule_for(entry)
    runner = rule.get("runner") or ""
    if not runner:
        raise ValueError("this email's rule has no job runner")
    if not os.path.isfile(runner):
        raise ValueError(f"the rule's runner is not there: {runner}")
    sender = (entry.get("from") or "")
    if not mail_listener.sender_allowed(rule, _addr(sender)):
        raise ValueError(f"{_addr(sender)} is not an allowed sender for this rule")
    with open(os.path.join(mail_listener._inbox_dir(), key), "rb") as f:
        m = mail_listener.parse(f.read(), {})
    ids: List[str] = []
    for found in START_RE.findall(m["body"] or ""):
        for i in found.split():
            if i not in ids:
                ids.append(i)
    if not ids:
        raise ValueError("no batch ids found in this email")
    return entry, runner, ids


def _addr(s: str) -> str:
    from email.utils import parseaddr
    return (parseaddr(s)[1] or s).strip().lower()


def _run(runner: str, *args: str, timeout: int = 120) -> str:
    out = subprocess.run([_python(), runner, *args], capture_output=True, text=True, timeout=timeout,
                         cwd=os.path.dirname(runner), stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise ValueError((out.stderr or out.stdout or f"exit {out.returncode}").strip()[-1500:])
    return out.stdout


def _json(text: str):
    import json
    return json.loads(text.strip().splitlines()[-1])


def _own_job(runner: str, ids: List[str], job: str) -> Dict:
    """A job of this email's batches (so one email's buttons cannot touch another's jobs)."""
    if not JOB_RE.match(job or ""):
        raise ValueError("bad job id")
    for j in _json(_run(runner, "status", "--batch", *ids, "--json")):
        if j.get("job") == job:
            return j
    raise ValueError(f"job {job} is not one of this email's")


def jobs(key: str) -> Dict:
    try:
        _, runner, ids = _setup(key)
    except ValueError as e:
        return {"runner": False, "reason": str(e)}
    rows = _json(_run(runner, "status", "--batch", *ids, "--json"))
    rows.sort(key=lambda j: j.get("started") or 0, reverse=True)
    for j in rows:
        p = _publishing.get(j["job"])
        if p:
            j["publishing"] = {k: p[k] for k in ("state", "started", "finished", "output")}
    failed = [j for j in rows if j["state"] == "failed"]
    keep = [j for j in rows if j["state"] != "failed"] + failed[:1]   # 148 identical failures: show the newest
    keep.sort(key=lambda j: j.get("started") or 0, reverse=True)
    return {"runner": True, "batches": ids, "jobs": keep, "hidden_failed": len(failed) - min(1, len(failed))}


def start(key: str) -> Dict:
    _, runner, ids = _setup(key)
    return _json(_run(runner, "start", *ids, "--json", timeout=300))


def resume(key: str, job: str) -> Dict:
    _, runner, ids = _setup(key)
    _own_job(runner, ids, job)
    return _json(_run(runner, "resume", job, "--json", timeout=300))


def drop(key: str, job: str) -> Dict:
    _, runner, ids = _setup(key)
    _own_job(runner, ids, job)
    _run(runner, "drop", job)
    return {"job": job, "state": "dropped"}


def publish(key: str, job: str) -> Dict:
    """Runs in the background: publishing pushes, deploys and can take minutes."""
    _, runner, ids = _setup(key)
    j = _own_job(runner, ids, job)
    if j["state"] != "done":
        raise ValueError(f"job {job} is {j['state']}, not ready to publish")
    if (_publishing.get(job) or {}).get("state") == "running":
        raise ValueError("already publishing")
    rec = {"state": "running", "started": time.time(), "finished": None, "output": ""}
    _publishing[job] = rec

    def _go():
        try:
            p = subprocess.Popen([_python(), runner, "publish", job], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, cwd=os.path.dirname(runner), stdin=subprocess.DEVNULL)
            deadline = time.time() + PUBLISH_TIMEOUT_S
            for line in p.stdout:
                rec["output"] = (rec["output"] + line)[-20000:]
                if time.time() > deadline:
                    p.kill()
                    rec["output"] += "\nStopped: publishing took over 30 minutes."
                    break
            rc = p.wait()
            rec["state"] = "done" if rc == 0 else "failed"
        except Exception as e:
            rec["output"] += f"\n{type(e).__name__}: {e}"
            rec["state"] = "failed"
        rec["finished"] = time.time()
    threading.Thread(target=_go, daemon=True, name=f"mail-publish-{job}").start()
    return {"job": job, "publishing": rec}


_tasks: set = set()


def spawn_watch(key: str, job: str) -> None:
    if not JOB_RE.match(job or ""):
        return
    t = asyncio.create_task(watch(key, job))
    _tasks.add(t)                     # a task nothing refers to can be collected mid-run
    t.add_done_callback(_tasks.discard)


async def watch(key: str, job: str, subject: str = "") -> None:
    """Tell the user when a job they started or resumed is finished."""
    if job in _watching:
        return
    _watching.add(job)
    try:
        _, runner, ids = await asyncio.to_thread(_setup, key)
        end = time.time() + WATCH_FOR_S
        while time.time() < end:
            await asyncio.sleep(WATCH_EVERY_S)
            try:
                j = await asyncio.to_thread(_own_job, runner, ids, job)
            except Exception:
                continue
            if j["state"] == "working":
                continue
            from src.chat_queue import send_notification
            heading = {"done": "Fix ready: publish?", "failed": "Fix failed",
                       "interrupted": "Fix interrupted"}.get(j["state"], f"Fix {j['state']}")
            body = (j.get("summary") or subject or f"job {job}")[:200]
            await send_notification("", {}, heading, body, kind="mail", anchor=f"email-inbound={key}")
            return
    except Exception as e:
        logger.warning("[mail jobs] watching %s: %s", job, e)
    finally:
        _watching.discard(job)
