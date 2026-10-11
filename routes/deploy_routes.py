"""One-click deploy: pull merged PRs into this checkout and restart.

Asked for: "yes I would love a one click" (deploying after a merge, instead
of asking for a pull and restart each time). The version badge (buildBadge.js)
shows when dev on GitHub is ahead of what this server runs and deploys it.

Safe by construction:
- admin only;
- only a fast-forward of the branch the checkout is on (dev), and only when
  the checkout has no local changes;
- every changed Python file of the new revision is compiled first, so a
  syntax error never takes the server down;
- the restart runs in a detached helper (its own session), which checks the
  new command can start, stops this process, starts the same command again
  with the same log, waits for the port to answer and falls back to
  `python -m uvicorn` if it does not. Claude Code
  runs survive it (they are reattached); a reply being written is cut off,
  and the page warns about that first.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FETCH_EVERY_S = 60
_state: Dict[str, float] = {"fetched": 0.0}


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", BASE_DIR, *args], capture_output=True, text=True, timeout=timeout)


def _branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _fetch(force: bool = False) -> str:
    """Fetch the branch from origin, at most once a minute unless forced."""
    if not force and time.time() - _state["fetched"] < FETCH_EVERY_S:
        return ""
    r = _git("fetch", "-q", "origin", _branch(), timeout=60)
    _state["fetched"] = time.time()
    return "" if r.returncode == 0 else (r.stderr.strip() or "git fetch failed")


def _merged_prs(rev_range: str) -> List[dict]:
    """The PR merges in `rev_range`, newest first, with the PR's title (GitHub
    puts it on the merge commit's first body line)."""
    out = []
    r = _git("log", "--first-parent", "--merges", "--format=%H%x1f%s%x1f%b%x1e", rev_range)
    for rec in r.stdout.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        sha, subject, body = (rec.split("\x1f") + ["", ""])[:3]
        m = re.search(r"#(\d+)", subject)
        title = (body.strip().splitlines() or [""])[0].strip()
        out.append({"pr": int(m.group(1)) if m else None, "commit": sha[:8],
                    "title": (title or subject)[:120]})
    return out


def status(force: bool = False) -> dict:
    from src import build_info
    branch = _branch()
    fetch_error = _fetch(force)
    upstream = f"origin/{branch}"
    head = _git("rev-parse", "--short=8", "HEAD").stdout.strip()
    behind = _git("rev-list", "--count", f"HEAD..{upstream}").stdout.strip() or "0"
    ahead = _git("rev-list", "--count", f"{upstream}..HEAD").stdout.strip() or "0"
    dirty = [ln[3:] for ln in _git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()]
    prs = _merged_prs(f"HEAD..{upstream}")
    changed = _git("diff", "--name-only", f"HEAD..{upstream}").stdout.split()
    from src import agent_runs
    running = sum(1 for sid in list(getattr(agent_runs, "_RUNS", {}) or {}) if agent_runs.is_active(sid))
    return {
        "branch": branch, "running_pr": build_info.INFO.get("pr"), "running_commit": build_info.INFO.get("commit"),
        "checkout_commit": head, "behind": int(behind), "ahead": int(ahead), "dirty": dirty,
        "new_prs": prs, "changed_files": changed[:200],
        "pc_files_changed": [f for f in changed if f.startswith("tools/")],
        # Pulled but not restarted: the checkout is newer than the running code.
        "restart_pending": head != (build_info.INFO.get("commit") or head),
        "replies_running": running, "fetch_error": fetch_error,
        "can_deploy": branch == "dev" and not dirty and int(ahead) == 0,
    }


def _syntax_errors(rev: str, files: List[str]) -> List[str]:
    errors = []
    for f in files:
        if not f.endswith(".py"):
            continue
        r = _git("show", f"{rev}:{f}")
        if r.returncode != 0:
            continue                                   # deleted in the new revision
        try:
            compile(r.stdout, f, "exec")
        except SyntaxError as e:
            errors.append(f"{f}:{e.lineno}: {e.msg}")
    return errors


def _proc_cmdline() -> List[str]:
    """The exact argv this process was started with (the kernel's copy, so
    `python -m uvicorn` stays `-m uvicorn` and a console script keeps its
    launcher path), or [] where /proc is not available."""
    try:
        with open("/proc/self/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [a.decode("utf-8", "surrogateescape") for a in raw.split(b"\0") if a]


def _module_of(path: str) -> str:
    """'uvicorn' for .../site-packages/uvicorn/__main__.py (or any file in
    site-packages/<pkg>/), '' when the path is not inside an installed package."""
    parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            i = len(parts) - 1 - parts[::-1].index(marker)
            if i + 2 < len(parts):                             # inside a package dir
                if parts[-1] == "__main__.py":
                    return ".".join(parts[i + 1:-1])           # site-packages/a/b/__main__.py -> a.b
                return parts[i + 1]
    if os.path.basename(path) == "__main__.py":
        return os.path.basename(os.path.dirname(os.path.abspath(path)))
    return ""


def _relaunch_cmd(argv: List[str] = None, cmdline: List[str] = None, executable: str = None) -> List[str]:
    """The command that starts this server again.

    Seen live on 2026-10-11: the server ran as `python -m uvicorn app:app ...`,
    so sys.argv[0] was .../site-packages/uvicorn/__main__.py, and the old
    `[sys.executable, *sys.argv]` ran that file as a script. That puts the
    uvicorn package dir first on sys.path, `import logging` found
    uvicorn/logging.py, and the new server crashed at startup.
    """
    argv = list(sys.argv if argv is None else argv)
    cmdline = list(_proc_cmdline() if cmdline is None else cmdline)
    executable = executable or sys.executable
    # Best: exactly what was run, unless it was `python -c ...` (not repeatable).
    if cmdline and "-c" not in cmdline[1:3]:
        if not os.path.isabs(cmdline[0]) and not shutil.which(cmdline[0]) and not os.path.exists(cmdline[0]):
            cmdline[0] = executable
        return cmdline
    prog, rest = (argv[0] if argv else ""), argv[1:]
    pkg = _module_of(prog) if prog.endswith(".py") else ""
    if pkg:
        return [executable, "-m", pkg, *rest]
    if prog and not prog.endswith(".py"):
        # A console script (.venv/bin/uvicorn): run the launcher itself.
        launcher = (prog if os.path.exists(prog) else
                    os.path.join(os.path.dirname(executable), os.path.basename(prog)))
        if not os.path.exists(launcher):
            launcher = shutil.which(os.path.basename(prog)) or ""
        if launcher:
            return [launcher, *rest]
    return [executable, *argv]


def _uvicorn_args(cmd: List[str]) -> List[str]:
    """The arguments given to uvicorn in `cmd`, or [] if it is not a uvicorn command."""
    for i, a in enumerate(cmd):
        if a == "-m" and i + 1 < len(cmd) and cmd[i + 1] == "uvicorn":
            return cmd[i + 2:]
        if os.path.basename(a) == "uvicorn" or a.endswith(os.sep + os.path.join("uvicorn", "__main__.py")):
            return cmd[i + 1:]
    return []


def _port_of(cmd: List[str], default: int) -> int:
    for i, a in enumerate(cmd):
        if a == "--port" and i + 1 < len(cmd) and cmd[i + 1].isdigit():
            return int(cmd[i + 1])
        if a.startswith("--port=") and a[7:].isdigit():
            return int(a[7:])
    return default


def _import_check(cmd: List[str], cwd: str, executable: str) -> List[str]:
    """A quick `python -c` that imports what `cmd` imports first, with the
    same sys.path[0] it would get, so a command that cannot even start is
    caught before the running server is stopped."""
    py, path0, mod = executable, cwd, "uvicorn"
    if len(cmd) > 2 and cmd[1] == "-m":
        py, mod = cmd[0], cmd[2]
    elif len(cmd) > 1 and cmd[1].endswith(".py"):
        py, path0, mod = cmd[0], os.path.dirname(os.path.abspath(os.path.join(cwd, cmd[1]))), ""
    elif len(cmd) > 1 and os.path.basename(cmd[0]).startswith("python"):
        py, path0 = cmd[0], os.path.dirname(os.path.abspath(os.path.join(cwd, cmd[1])))   # python <launcher>
    else:
        path0 = os.path.dirname(os.path.abspath(cmd[0]))                                      # <launcher>
    code = f"import sys; sys.path[0] = {path0!r}; import asyncio, logging; logging.Formatter"
    return [py, "-c", code + (f"; import {mod}" if mod else "")]


def _restart_plan(port: int, log: str, cwd: str = None, cmd: List[str] = None, wait_s: int = 90) -> dict:
    """Everything the detached restart helper needs, worked out here so it can
    be tested without restarting anything."""
    cwd = cwd or os.getcwd()
    cmd = cmd or _relaunch_cmd()
    port = _port_of(cmd, port)
    args = _uvicorn_args(cmd) or ["app:app", "--host", "127.0.0.1", "--port", str(port)]
    fallback = [sys.executable, "-m", "uvicorn", *args]
    return {"pid": os.getpid(), "cwd": cwd, "log": log, "port": port, "wait_s": wait_s,
            "cmd": cmd, "check": _import_check(cmd, cwd, sys.executable),
            "fallback": fallback, "fallback_check": _import_check(fallback, cwd, sys.executable)}


# The detached helper. Runs with `python -I` (stdlib only, nothing from the
# cwd can shadow it) in its own session, so it outlives the server it stops.
RESTART_HELPER = r"""
import json, os, signal, subprocess, sys, time, urllib.error, urllib.request
plan = json.loads(sys.argv[1])
def log(msg):
    with open(plan["log"], "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " - deploy-restart - " + msg + "\n")
def ok(check):
    r = subprocess.run(check, cwd=plan["cwd"], capture_output=True, text=True, timeout=120)
    if r.returncode:
        log("ERROR: import check failed for this command: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    return r.returncode == 0
def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
def up(proc):
    end = time.time() + plan["wait_s"]
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/" % plan["port"], timeout=3)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(1)
    return False
def start(cmd):
    log("starting: " + " ".join(cmd))
    with open(plan["log"], "ab") as out:
        return subprocess.Popen(cmd, cwd=plan["cwd"], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
runs = [(plan["cmd"], plan["check"]), (plan["fallback"], plan["fallback_check"])]
runs = [r for r in runs if ok(r[1])]
if not runs:
    log("ERROR: NOT RESTARTING: neither the server's command nor python -m uvicorn would start. "
        "The old server keeps running the old code.")
    sys.exit(1)
time.sleep(1)
pid = plan["pid"]
if alive(pid):
    os.kill(pid, signal.SIGTERM)
for _ in range(60):
    if not alive(pid):
        break
    time.sleep(0.5)
if alive(pid):
    os.kill(pid, signal.SIGKILL)
time.sleep(1)
for i, (cmd, _) in enumerate(runs):
    proc = start(cmd)
    if up(proc):
        log("server is back up on port %d (pid %d)" % (plan["port"], proc.pid))
        sys.exit(0)
    log("ERROR: SERVER DID NOT COME BACK within %ds (exit code %s) after: %s"
        % (plan["wait_s"], proc.poll(), " ".join(cmd)))
    if proc.poll() is None:
        proc.kill()
    if i + 1 < len(runs):
        log("trying the fallback launch")
log("ERROR: THE SERVER IS DOWN: start it by hand.")
sys.exit(1)
"""


def _restart(port: int = 7000) -> None:
    """Stop this server and start the same command again, from a helper in
    its own session so it outlives this process. The helper checks the new
    command can start before stopping anything, waits for the port to answer,
    and falls back to `python -m uvicorn` if it does not."""
    try:
        log = os.readlink("/proc/self/fd/1")
        if not log.startswith("/") or log.startswith("/dev/"):
            raise OSError
    except OSError:
        from src.constants import DATA_DIR
        log = os.path.join(DATA_DIR, "logs", "odysseus.log")
    plan = _restart_plan(port, log)
    logger.info("Deploy restart: %s", " ".join(plan["cmd"]))
    subprocess.Popen([sys.executable, "-I", "-c", RESTART_HELPER, json.dumps(plan)],
                     cwd=plan["cwd"], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def setup_deploy_routes() -> APIRouter:
    router = APIRouter(tags=["deploy"])

    @router.get("/api/admin/deploy/status")
    async def deploy_status(request: Request, refresh: int = 0):
        require_admin(request)
        import asyncio
        return await asyncio.to_thread(status, bool(refresh))

    @router.post("/api/admin/deploy")
    async def deploy(request: Request):
        require_admin(request)
        import asyncio
        st = await asyncio.to_thread(status, True)
        if st["branch"] != "dev":
            raise HTTPException(409, f"The checkout is on {st['branch']!r}, not dev: deploy by hand.")
        if st["dirty"]:
            raise HTTPException(409, "The checkout has local changes: " + ", ".join(st["dirty"][:5]))
        if st["ahead"]:
            raise HTTPException(409, "The checkout has commits dev does not: deploy by hand.")
        if not st["behind"] and not st["restart_pending"]:
            raise HTTPException(409, "Already running the latest dev.")
        errors = _syntax_errors(f"origin/{st['branch']}", st["changed_files"])
        if errors:
            raise HTTPException(422, "Not deployed, the new code does not compile: " + "; ".join(errors[:5]))
        if st["behind"]:
            r = await asyncio.to_thread(_git, "merge", "--ff-only", "-q", f"origin/{st['branch']}")
            if r.returncode != 0:
                raise HTTPException(500, f"git merge --ff-only failed: {r.stderr.strip()[:300]}")
        # Stop replies being written so each is saved as far as it got,
        # rather than vanishing when this process exits.
        from src import agent_runs, restart_resume
        # ...and carried on once the new code is up (restart_resume).
        try:
            restart_resume.remember_active()
        except Exception as e:
            logger.warning("Could not note the replies to carry on: %s", e)
        stopped = await agent_runs.stop_all(timeout=10)
        server = request.scope.get("server") or ("127.0.0.1", 7000)
        _restart(int(server[1] or 7000))
        new_prs = [p["pr"] for p in st["new_prs"] if p["pr"]]
        return {"ok": True, "restarting": True, "replies_stopped": stopped,
                "to_pr": max(new_prs) if new_prs else st["running_pr"],
                "to_commit": _git("rev-parse", "--short=8", "HEAD").stdout.strip(),
                "pc_files_changed": st["pc_files_changed"]}

    return router
