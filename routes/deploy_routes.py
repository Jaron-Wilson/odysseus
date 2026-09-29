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
- the restart runs in a detached helper (its own session), which stops this
  process and starts the same command again with the same log. Claude Code
  runs survive it (they are reattached); a reply being written is cut off,
  and the page warns about that first.
"""

import logging
import os
import re
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


def _restart() -> None:
    """Stop this server and start the same command again, from a helper in
    its own session so it outlives this process."""
    pid = os.getpid()
    try:
        log = os.readlink("/proc/self/fd/1")
        if not log.startswith("/"):
            raise OSError
    except OSError:
        from src.constants import DATA_DIR
        log = os.path.join(DATA_DIR, "logs", "odysseus.log")
    cmd = [sys.executable, *sys.argv]
    script = (
        'sleep 1; kill "$1"; i=0; while kill -0 "$1" 2>/dev/null && [ $i -lt 60 ]; do sleep 0.5; i=$((i+1)); done; '
        'kill -9 "$1" 2>/dev/null; sleep 1; cd "$2" || exit 1; shift 3; exec "$@" >> "$LOG" 2>&1 < /dev/null'
    )
    env = dict(os.environ, LOG=log)
    subprocess.Popen(["/bin/sh", "-c", script, "deploy-restart", str(pid), os.getcwd(), "--", *cmd],
                     env=env, start_new_session=True, stdin=subprocess.DEVNULL,
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
        _restart()
        new_prs = [p["pr"] for p in st["new_prs"] if p["pr"]]
        return {"ok": True, "restarting": True, "replies_stopped": stopped,
                "to_pr": max(new_prs) if new_prs else st["running_pr"],
                "pc_files_changed": st["pc_files_changed"]}

    return router
