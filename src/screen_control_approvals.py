"""Human approval for tools that can drive a machine's screen.

Most MCP tools answer questions. A few can move the mouse, type, and click
"confirm" on whatever happens to be in front of them, and those are worth a
different standard: the agent may ask, but only a person at a browser can
say yes.

The gate is enforced here rather than in the prompt, so a model that decides
to skip the asking still cannot act. Approval is granted by
`POST /api/screen_control/approve/{id}`, which requires an authenticated
session — clicking it is itself the proof of identity. An expired session
bounces to the login page and back, which is the "sign in again to confirm"
behaviour rather than an accident.

A grant covers one server for a short window, not forever. The window exists
because a real task is a sequence — screenshot, click, screenshot, type —
and asking per action would make the feature unusable while training the
habit of clicking approve without reading. Fifteen minutes is long enough to
finish something and short enough that a forgotten grant lapses on its own.
"""

import json
import logging
import os
import tempfile
import time
import uuid
from typing import Dict, List, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

APPROVALS_FILE = os.path.join(DATA_DIR, "screen_control_approvals.json")

# How long a granted window lasts. See the module docstring for why this is a
# window rather than a single action.
GRANT_TTL_S = 15 * 60

# How many sensitive actions one approval buys. A clock alone is not a
# bound on what can happen to someone's desktop: at a few seconds per
# round, fifteen minutes is hundreds of clicks, and an agent alternating
# screenshot and click never trips the loop-breaker because every call
# signature differs. Whichever runs out first -- the clock or the budget --
# sends the next action back to the approval gate.
MAX_ACTIONS_PER_GRANT = 25

# How long an unanswered request stays clickable. Answering a stale request
# should not silently hand out control.
REQUEST_TTL_S = 10 * 60

# Tool names, after the mcp__<server>__ prefix, that need a grant. Anything
# that moves the pointer, presses a key, or reads the screen. Reading is
# included deliberately: a screenshot of someone's desktop is not a lesser
# thing than a click on it.
SENSITIVE_TOOLS = {
    "screenshot", "click", "double_click", "move_mouse", "drag",
    "type_text", "press_keys", "scroll", "screen_record",
    # Annotating captures the screen to draw on it, so it is a screenshot
    # by another name and belongs behind the same gate.
    "annotate_screen",
    # Volume is not screen control: it changes a system setting, not what is
    # on screen, and gating it would put an approval click in front of
    # "turn it down" — the exact friction this was meant to remove.
}


def is_sensitive(qualified_name: str) -> bool:
    """True when this MCP tool needs a human grant before it runs."""
    parts = (qualified_name or "").split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return False
    return parts[2] in SENSITIVE_TOOLS


def server_of(qualified_name: str) -> str:
    parts = (qualified_name or "").split("__", 2)
    return parts[1] if len(parts) == 3 else ""


def _load() -> Dict[str, dict]:
    try:
        with open(APPROVALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _save(data: Dict[str, dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".sc_appr_", suffix=".json")
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
    keep = {}
    for key, rec in data.items():
        status = rec.get("status")
        age = now - rec.get("created", 0)
        if status == "approved" and now < rec.get("expires", 0):
            keep[key] = rec
        elif status == "pending" and age < REQUEST_TTL_S:
            keep[key] = rec
        # denied and expired records are dropped; the audit trail is the log.
    return keep


def request_grant(server_id: str, server_name: str, owner: str,
                  reason: str, session_id: str = "") -> dict:
    """Record a pending request and return it, including its id."""
    data = _prune(_load())
    rec = {
        "id": uuid.uuid4().hex[:12],
        "server_id": server_id,
        "server_name": server_name or server_id,
        "owner": owner or "",
        "reason": (reason or "")[:500],
        "session_id": session_id or "",
        "status": "pending",
        "created": time.time(),
    }
    data[rec["id"]] = rec
    _save(data)
    logger.info("Screen control requested for %s (%s): %s",
                server_name or server_id, rec["id"], rec["reason"][:120])
    return rec


def set_status(request_id: str, status: str, owner: str = "") -> Optional[dict]:
    """Approve or deny a pending request. Only a route with a session calls this."""
    if status not in ("approved", "denied"):
        return None
    data = _prune(_load())
    rec = data.get(request_id)
    if not rec or rec.get("status") != "pending":
        return None
    rec["status"] = status
    rec["decided_by"] = owner or ""
    rec["decided_at"] = time.time()
    if status == "approved":
        rec["expires"] = time.time() + GRANT_TTL_S
    data[request_id] = rec
    _save(data)
    logger.info("Screen control %s for %s by %s",
                status, rec.get("server_name"), owner or "?")
    return rec


def active_grant(server_id: str, owner: str = "") -> Optional[dict]:
    """A live grant for this server, or None.

    Owner-scoped: one person's approval is not another's, even though this
    install has a single user today.
    """
    now = time.time()
    for rec in _prune(_load()).values():
        if (rec.get("status") == "approved"
                and rec.get("server_id") == server_id
                and now < rec.get("expires", 0)
                and (not owner or not rec.get("owner") or rec.get("owner") == owner)):
            return rec
    return None


def revoke(server_id: str = "", owner: str = "") -> int:
    """Drop live grants, for a server or all of them. Returns how many.

    Owner-scoped when an owner is given, for the same reason active_grant
    is: stopping your own run must not quietly disarm someone else's.
    """
    data = _prune(_load())
    dropped = 0
    for key, rec in list(data.items()):
        if rec.get("status") != "approved":
            continue
        if server_id and rec.get("server_id") != server_id:
            continue
        if owner and rec.get("owner") and rec.get("owner") != owner:
            continue
        data.pop(key, None)
        dropped += 1
    if dropped:
        _save(data)
        logger.info("Revoked %d screen-control grant(s)", dropped)
    return dropped


def spend_action(server_id: str, owner: str = "") -> dict:
    """Charge one action to the live grant for this server.

    Charged before the call runs, not after: a call that hangs or crashes
    has still reached for the machine, and a budget that only counted
    clean returns would not bound the case that matters.

    Returns {"remaining": int, "exhausted": bool}. When the budget runs
    out the grant is dropped, so the next sensitive call re-prompts.
    """
    now = time.time()
    data = _prune(_load())
    for key, rec in list(data.items()):
        if (rec.get("status") == "approved"
                and rec.get("server_id") == server_id
                and now < rec.get("expires", 0)
                and (not owner or not rec.get("owner") or rec.get("owner") == owner)):
            used = int(rec.get("actions_used", 0)) + 1
            rec["actions_used"] = used
            remaining = max(MAX_ACTIONS_PER_GRANT - used, 0)
            if remaining <= 0:
                data.pop(key, None)
                _save(data)
                logger.info(
                    "Screen-control grant for %s spent its %d-action budget; "
                    "further actions need a new approval",
                    rec.get("server_name", server_id), MAX_ACTIONS_PER_GRANT)
                return {"remaining": 0, "exhausted": True}
            data[key] = rec
            _save(data)
            return {"remaining": remaining, "exhausted": False}
    return {"remaining": 0, "exhausted": True}


def list_grants() -> List[dict]:
    """Live grants, for showing what is currently permitted."""
    now = time.time()
    return [
        {
            "id": r["id"],
            "server_name": r.get("server_name", ""),
            "server_id": r.get("server_id", ""),
            "expires_in_s": int(r.get("expires", 0) - now),
        }
        for r in _prune(_load()).values()
        if r.get("status") == "approved" and now < r.get("expires", 0)
    ]
