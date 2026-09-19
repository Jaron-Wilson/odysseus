"""Server-side approval state for claude_code plans.

The point of this module is that the agent cannot approve its own plan. A plan
run records itself here as `pending`; the only thing that can move it to
`approved` is `POST /api/claude_code/approve/{id}`, which requires an
authenticated browser session. The execute phase refuses to run against
anything that is not approved, so the gate is enforced in code rather than by
asking the model nicely in a prompt.

Approvals are single-use and time-limited: an approval authorises one execute
run of one plan, not a standing permission for that directory.
"""

import json
import logging
import os
import tempfile
import time
from typing import Dict, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

APPROVALS_FILE = os.path.join(DATA_DIR, "claude_code_approvals.json")

# A plan the user never answered should not stay executable indefinitely.
APPROVAL_TTL_S = 60 * 60


def _load() -> Dict[str, dict]:
    try:
        with open(APPROVALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _save(data: Dict[str, dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    # Atomic replace: a torn write here would strand a plan mid-approval.
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".cc_appr_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, APPROVALS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune(data: Dict[str, dict]) -> Dict[str, dict]:
    now = time.time()
    return {k: v for k, v in data.items() if now - float(v.get("created", 0)) < APPROVAL_TTL_S}


def record_plan(session_id: str, *, cwd: str, plan: str, owner: Optional[str] = None) -> None:
    """Register a freshly produced plan as awaiting the user's answer."""
    data = _prune(_load())
    data[session_id] = {
        "status": "pending",
        "cwd": cwd,
        "plan": (plan or "")[:20000],
        "owner": owner or "",
        "created": time.time(),
    }
    _save(data)


def set_status(session_id: str, status: str, *, owner: Optional[str] = None) -> bool:
    """Move a plan to approved/denied. Returns False if there is no such plan."""
    if status not in ("approved", "denied"):
        raise ValueError("status must be approved or denied")
    data = _prune(_load())
    entry = data.get(session_id)
    if not entry:
        return False
    if entry.get("status") != "pending":
        return False
    entry["status"] = status
    entry["answered_at"] = time.time()
    if owner:
        entry["answered_by"] = owner
    _save(data)
    return True


def get(session_id: str) -> Optional[dict]:
    return _prune(_load()).get(session_id)


def consume_approval(session_id: str, cwd: str) -> tuple[bool, str]:
    """Spend a one-shot approval for an execute run.

    Returns (ok, reason). The cwd must match the plan's: approving a plan for
    one project must not authorise edits somewhere else.
    """
    data = _prune(_load())
    entry = data.get(session_id)
    if not entry:
        return False, "no plan found for this session_id (it may have expired)"
    status = entry.get("status")
    if status == "pending":
        return False, "this plan has not been approved by the user yet"
    if status == "denied":
        return False, "the user denied this plan"
    if status == "used":
        return False, "this approval was already used; plan again for a new run"
    if os.path.realpath(entry.get("cwd") or "") != os.path.realpath(cwd):
        return False, "cwd does not match the approved plan"
    entry["status"] = "used"
    entry["used_at"] = time.time()
    _save(data)
    return True, ""
