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

# A plan the user never answered should not stay executable indefinitely, but
# an hour was far too short: seen live, a plan approved the next day had
# expired, so the agent planned it all over again. A week, and the user gets
# a notification when a plan is waiting (claude_code_tool._notify_plan).
APPROVAL_TTL_S = 7 * 24 * 60 * 60


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


def record_plan(session_id: str, *, cwd: str, plan: str, owner: Optional[str] = None,
                model: str = "", engine: str = "", chat_session_id: str = "") -> None:
    """Register a freshly produced plan as awaiting the user's answer. The
    model is kept so the approved run uses the one the user was shown, and the
    chat so approving can start the run there straight away."""
    data = _prune(_load())
    data[session_id] = {
        "status": "pending",
        "cwd": cwd,
        "plan": (plan or "")[:20000],
        "owner": owner or "",
        "model": model or "",
        "engine": engine or "",
        "chat_session_id": chat_session_id or "",
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


def assign_run_id(session_id: str) -> str:
    """The id the approved run will have, fixed at the moment of approval.

    Handed back to the page that clicked Approve, so the chat shows which run
    is carrying the plan out, and used as that run's job id: a second execute
    for the same approval finds it running instead of starting another."""
    import uuid
    data = _prune(_load())
    entry = data.get(session_id)
    if not entry:
        return ""
    if not entry.get("run_id"):
        entry["run_id"] = uuid.uuid4().hex[:8]
        _save(data)
    return entry["run_id"]


def set_run_choice(session_id: str, engine: str = "", model: str = "") -> None:
    """The engine and model picked in the Approve dialog for the run."""
    data = _prune(_load())
    if session_id in data:
        if engine:
            data[session_id]["run_engine"] = engine
        if model:
            data[session_id]["run_model"] = model
        _save(data)


def set_limits(session_id: str, limits: dict) -> None:
    """The run limits chosen when approving (turns, budget, take your time)."""
    data = _prune(_load())
    if session_id in data:
        data[session_id]["limits"] = limits
        _save(data)


def restore_approval(session_id: str) -> bool:
    """Put a spent approval back after a run that never really ran.

    An approval is consumed before the CLI starts, because that is the only
    point where refusing is still cheap. But a run that times out or dies has
    not used the user's consent for anything, and making them plan and approve
    again from scratch punishes them for our timeout.
    """
    data = _prune(_load())
    entry = data.get(session_id)
    if not entry or entry.get("status") != "used":
        return False
    entry["status"] = "approved"
    entry.pop("used_at", None)
    _save(data)
    return True


def pending_for(owner: str = "") -> list:
    """Plans waiting on this person's answer, newest first."""
    out = []
    for sid, e in _prune(_load()).items():
        if e.get("status") != "pending":
            continue
        if owner and e.get("owner") and e["owner"] != owner:
            continue
        out.append({"session_id": sid, "chat_session_id": e.get("chat_session_id", ""),
                    "cwd": e.get("cwd", ""), "engine": e.get("engine", ""),
                    "model": e.get("model", ""), "created": e.get("created", 0),
                    "plan": (e.get("plan") or "")[:600]})
    return sorted(out, key=lambda x: x["created"], reverse=True)


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
        # "plan again" read as an instruction: a second execute on the same
        # approval made the agent plan again, and the user was asked to
        # approve the same work once more (2026-09-29).
        run = entry.get("run_id") or ""
        return False, ("this approval was already used: its run " + (f"(job {run}) " if run else "")
                       + "has started, so do NOT plan again. Report that run's result, or call "
                       + "claude_code with action 'status'" + (f" and job_id '{run}'" if run else "")
                       + " to see how it is going. Only plan again if the user asks for new work.")
    if os.path.realpath(entry.get("cwd") or "") != os.path.realpath(cwd):
        return False, "cwd does not match the approved plan"
    entry["status"] = "used"
    entry["used_at"] = time.time()
    _save(data)
    return True, ""
