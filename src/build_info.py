"""Which code this server process is running, read once at startup.

Shown in the page's bottom-left corner, so a merge that has not been
deployed yet is visible at a glance. Asked for: "show the latest merged PR,
not the one that's not merged, so I can tell if the server's been
restarted". Read at import (startup), never again: pulling new code
without restarting keeps showing the old number, which is the point.
"""

import os
import re
import subprocess
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STARTED = time.time()


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", BASE_DIR, *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _read() -> dict:
    full = _git("rev-parse", "HEAD")
    commit = full[:8]
    # The newest "Merge pull request #N" on the branch's own line of history.
    subject = _git("log", "--first-parent", "--merges", "-1", "--format=%s")
    m = re.search(r"#(\d+)", subject or "")
    # The full hash too, for What's new to tell which merges are running.
    return {"pr": int(m.group(1)) if m else None, "commit": commit, "commit_full": full,
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"), "started": STARTED}


INFO = _read()
