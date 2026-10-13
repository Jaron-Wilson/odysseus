"""Action approvals: a tool call held until the user says yes in the chat.

Asked for: "if its trying to use my google account other than reading please
make it ask me".

src/google_write_gate.py decides which calls need asking (anything that
writes to the user's Google account or a Gmail mailbox). When one does, the
call does not run. Instead it is saved here, exactly as the model made it
(tool name and argument text, "pinned"), and the chat shows a card saying in
plain words what would happen, with Approve and Deny. The model's turn ends,
like ask_user.

Approve (routes/action_approvals_routes.py, an authenticated request from the
user's own page; nothing a model can call) runs the pinned call once, through
the same tool path as any other call, and starts the chat again with its
result. Deny tells the model "the user denied <action>" with the user's note.

States:
  pending     waiting for the user's answer
  running     approved; the pinned call is running right now
  approved    approved, and the call ran once (result kept)
  queued      approved in Company > Approvals; runs when the agent wakes
  denied      the user said no (with an optional note)
  expired     nobody answered within EXPIRY_S, so it can no longer run
  superseded  a newer request in the same chat replaced it unanswered

Kept in DATA_DIR/action_approvals.json. Every answer (approved, denied,
expired, failed) is also appended to DATA_DIR/google_write_audit.jsonl, which
Settings > Google lists.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

EXPIRY_S = 30 * 60
MAX_NOTE = 2000
MAX_RESULT = 6000
KEEP_ANSWERED_S = 30 * 24 * 3600     # answered entries kept a month, for the cards
ID_RE = re.compile(r"^[0-9a-f]{12}$")
STATUSES = ("pending", "running", "approved", "queued", "denied", "expired", "superseded")

_lock = threading.RLock()

# The one call an approval lets through: (tool, content, approval id). Set
# only by run_pinned for the duration of that call; the gate clears it the
# moment it matches, so it covers exactly one execution.
_approved_call: contextvars.ContextVar[Optional[Tuple[str, str, str]]] = contextvars.ContextVar(
    "odysseus_approved_call", default=None)


# Where agent work comes from when it is not an interactive chat turn: "mail"
# for the mail listener (src/mail_listener.py). Scheduled tasks and Company
# runs are told apart by their own context (usage_scope, company_tools).
run_context: contextvars.ContextVar[str] = contextvars.ContextVar("odysseus_run_context", default="")


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ── Storage ──────────────────────────────────────────────────────────────

def _file() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "action_approvals.json")


def _audit_file() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "google_write_audit.jsonl")


def _load() -> Dict[str, dict]:
    try:
        with open(_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _save(data: Dict[str, dict]) -> None:
    now = time.time()
    # Old answered entries go; pending ones always stay until answered or expired.
    for k in [k for k, e in data.items()
              if e.get("status") not in ("pending", "running", "queued")
              and now - float(e.get("answered_at") or e.get("created") or now) > KEEP_ANSWERED_S]:
        data.pop(k, None)
    atomic_write_json(_file(), data, indent=1)


def _owner(owner: Optional[str]) -> str:
    return (owner or "").strip()


def valid_id(aid: str) -> bool:
    return bool(ID_RE.match(str(aid or "")))


def _expire_in(data: Dict[str, dict], now: Optional[float] = None) -> List[dict]:
    """Mark pending entries past their time as expired. Returns those changed."""
    now = now if now is not None else time.time()
    out = []
    for e in data.values():
        if e.get("status") == "pending" and now >= float(e.get("expires_at") or 0):
            e["status"] = "expired"
            e["answered_at"] = now
            out.append(e)
            audit(e, "expired")
    return out


def _sweep() -> List[dict]:
    with _lock:
        data = _load()
        gone = _expire_in(data)
        if gone:
            _save(data)
    return gone


# ── Reading ──────────────────────────────────────────────────────────────

def get(aid: str) -> Optional[dict]:
    if not valid_id(aid):
        return None
    _sweep()
    with _lock:
        e = _load().get(aid)
    return dict(e) if e else None


def for_chat(chat_id: str) -> List[dict]:
    if not chat_id:
        return []
    _sweep()
    with _lock:
        rows = [dict(e) for e in _load().values() if e.get("chat_session_id") == chat_id]
    return sorted(rows, key=lambda e: e.get("created") or 0)


def pending_for_chat(chat_id: str) -> Optional[dict]:
    for e in reversed(for_chat(chat_id)):
        if e.get("status") == "pending":
            return e
    return None


def pending_for_owner(owner: Optional[str]) -> List[dict]:
    _sweep()
    own = _owner(owner)
    with _lock:
        rows = [dict(e) for e in _load().values()
                if _owner(e.get("owner")) == own and e.get("status") in ("pending", "queued")]
    return sorted(rows, key=lambda e: e.get("created") or 0, reverse=True)


def view(e: dict) -> dict:
    """What the chat card and the API show. Never the raw pinned args."""
    now = time.time()
    left = max(0, int(float(e.get("expires_at") or 0) - now)) if e.get("status") == "pending" else 0
    return {
        "id": e["id"],
        "kind": e.get("kind") or "google_write",
        "status": e.get("status"),
        "source": e.get("source") or "chat",
        "chat_session_id": e.get("chat_session_id") or "",
        "tool": e.get("tool") or "",
        "action": e.get("action") or "",
        "title": e.get("title") or "",
        "details": e.get("details") or [],
        "body": e.get("body") or "",
        "body_label": e.get("body_label") or "",
        "target": e.get("target") or {},
        "account": e.get("account") or "",
        "created": e.get("created"),
        "expires_at": e.get("expires_at"),
        "expires_in": left,
        "answered_at": e.get("answered_at"),
        "deny_note": e.get("deny_note") or "",
        "result": e.get("result") or None,
        "company_approval_id": e.get("company_approval_id") or "",
    }


# ── Creating ─────────────────────────────────────────────────────────────

def create(owner: Optional[str], chat_id: str, tool: str, content: str, info: Dict[str, Any], *,
           source: str = "chat", extra: Optional[dict] = None) -> dict:
    """Hold `tool` with `content` (pinned exactly) for the user's answer.

    The same call already waiting in this chat is reused rather than shown
    twice. Any other request still waiting in the chat is marked superseded:
    the model asked for something else since, and only the newest can run."""
    content = content if isinstance(content, str) else json.dumps(content)
    now = time.time()
    with _lock:
        data = _load()
        _expire_in(data, now)
        if chat_id:
            for e in data.values():
                if (e.get("chat_session_id") == chat_id and e.get("status") == "pending"
                        and e.get("tool") == tool and e.get("content") == content):
                    _save(data)
                    return dict(e, reused=True)
            for e in data.values():
                if e.get("chat_session_id") == chat_id and e.get("status") == "pending":
                    e["status"] = "superseded"
                    e["answered_at"] = now
        aid = uuid.uuid4().hex[:12]
        entry = {
            "id": aid,
            "kind": info.get("kind") or "google_write",
            "owner": _owner(owner),
            "chat_session_id": chat_id or "",
            "source": source,
            "tool": tool,
            "content": content,
            "action": str(info.get("action") or tool)[:80],
            "title": str(info.get("title") or tool)[:300],
            "details": [[str(k)[:60], str(v)[:1000]] for k, v in (info.get("details") or [])][:20],
            "body": str(info.get("body") or "")[:20000],
            "body_label": str(info.get("body_label") or "")[:60],
            "target": {k: str(v)[:500] for k, v in (info.get("target") or {}).items() if k in ("name", "url")},
            "account": str(info.get("account") or "")[:200],
            "status": "pending",
            "created": now,
            "expires_at": now + EXPIRY_S,
        }
        if extra:
            entry.update(extra)
        data[aid] = entry
        _save(data)
    logger.info("[approvals] %s held for approval in chat %s (%s)", tool, (chat_id or "-")[:8], aid)
    return dict(entry)


def set_fields(aid: str, **fields) -> None:
    with _lock:
        data = _load()
        if aid in data:
            data[aid].update(fields)
            _save(data)


# ── Answering ────────────────────────────────────────────────────────────

def _answerable(data: dict, aid: str, user: Optional[str]) -> dict:
    e = data.get(aid)
    if not e or _owner(e.get("owner")) != _owner(user):
        raise Refused(404, "No such approval")
    if e.get("source") == "company":
        raise Refused(409, "This change is answered in Company > Approvals")
    st = e.get("status")
    if st == "superseded":
        raise Refused(409, "A newer request in this chat replaced this one.")
    if st == "expired" or (st == "pending" and time.time() >= float(e.get("expires_at") or 0)):
        raise Refused(409, "This request expired before it was answered. Ask again if you still want it.")
    if st != "pending":
        raise Refused(409, f"This request is already {st}")
    return e


def claim(aid: str, user: Optional[str]) -> dict:
    """The user's Approve: from pending to running, once. A second Approve
    (double click, another tab) gets 409 and never runs the call again."""
    with _lock:
        data = _load()
        try:
            e = _answerable(data, aid, user)
        except Refused:
            if _expire_in(data):
                _save(data)
            raise
        e["status"] = "running"
        e["answered_at"] = time.time()
        e["answered_by"] = _owner(user)
        _save(data)
        return dict(e)


def deny(aid: str, user: Optional[str], note: str = "") -> dict:
    with _lock:
        data = _load()
        e = _answerable(data, aid, user)
        e["status"] = "denied"
        e["answered_at"] = time.time()
        e["answered_by"] = _owner(user)
        e["deny_note"] = str(note or "").strip()[:MAX_NOTE]
        _save(data)
        out = dict(e)
    audit(out, "denied")
    return out


def _result_text(result: Dict) -> Tuple[int, str]:
    code = result.get("exit_code")
    try:
        code = int(code) if code is not None else (1 if result.get("error") else 0)
    except (TypeError, ValueError):
        code = 1
    parts = []
    for k in ("output", "response", "stdout", "results", "content"):
        v = result.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
            break
    if result.get("error"):
        parts.append(f"Error: {result['error']}")
    text = "\n".join(parts) or ("Done." if code == 0 else "It failed with no message.")
    link = result.get("google_link")
    if isinstance(link, dict) and link.get("url"):
        text += f"\nLink: {link['url']}"
    return code, text[:MAX_RESULT]


async def run_pinned(e: dict) -> Dict:
    """Run the approved call exactly as it was pinned, once, through the
    normal tool path (src/tool_execution.py). Only the gate is let past."""
    from src.tool_execution import execute_tool_block
    from src.agent_tools import ToolBlock
    token = _approved_call.set((e["tool"], e["content"], e["id"]))
    try:
        _desc, result = await execute_tool_block(
            ToolBlock(e["tool"], e["content"]),
            session_id=e.get("chat_session_id") or None,
            owner=e.get("owner") or None,
        )
    except Exception as ex:
        logger.warning("[approvals] approved call %s failed: %s", e["id"], ex, exc_info=True)
        result = {"error": f"{type(ex).__name__}: {ex}"[:500], "exit_code": 1}
    finally:
        try:
            _approved_call.reset(token)
        except ValueError:
            _approved_call.set(None)
    return result if isinstance(result, dict) else {"output": str(result), "exit_code": 0}


def take_approved(tool: str, content: str) -> bool:
    """For the gate: is this the one call an approval is running right now?
    Spends it, so it lets exactly one execution through."""
    cur = _approved_call.get()
    if cur and cur[0] == tool and cur[1] == content:
        _approved_call.set(None)
        return True
    return False


def finish(aid: str, result: Dict) -> dict:
    code, text = _result_text(result or {})
    with _lock:
        data = _load()
        e = data.get(aid)
        if not e:
            return {}
        e["status"] = "approved"
        e["ran_at"] = time.time()
        e["result"] = {"exit_code": code, "output": text}
        _save(data)
        out = dict(e)
    audit(out, "approved" if code == 0 else "approved (failed)")
    return out


async def approve_and_run(aid: str, user: Optional[str]) -> dict:
    e = claim(aid, user)
    result = await run_pinned(e)
    return finish(aid, result)


# ── Company agents: answered in Company > Approvals ──────────────────────

def company_decided(company_approval_id: str, decision: str, note: str = "", by: str = "") -> None:
    """Mirror a Company > Approvals decision. Approved: queued, and it runs
    once when the agent wakes (run_company_queued). Anything else: denied."""
    if not company_approval_id:
        return
    now = time.time()
    changed = []
    with _lock:
        data = _load()
        _expire_in(data, now)
        for e in data.values():
            if e.get("company_approval_id") != company_approval_id:
                continue
            if e.get("status") != "pending":
                continue
            e["answered_at"] = now
            e["answered_by"] = by
            if decision == "approved":
                e["status"] = "queued"
            else:
                e["status"] = "denied"
                e["deny_note"] = str(note or "")[:MAX_NOTE]
                changed.append(dict(e))
        _save(data)
    for e in changed:
        audit(e, "denied")


async def run_company_queued(company_approval_id: str) -> Optional[dict]:
    """Run a Company-approved change once, as the agent wakes for it."""
    with _lock:
        data = _load()
        hit = next((e for e in data.values() if e.get("company_approval_id") == company_approval_id), None)
        if not hit:
            return None
        if hit.get("status") != "queued":
            return dict(hit)
        hit["status"] = "running"
        _save(data)
        e = dict(hit)
    result = await run_pinned(e)
    return finish(e["id"], result)


def for_company_approval(company_approval_id: str) -> Optional[dict]:
    with _lock:
        for e in _load().values():
            if e.get("company_approval_id") == company_approval_id:
                return dict(e)
    return None


# ── Audit log ────────────────────────────────────────────────────────────

def audit(e: dict, outcome: str) -> None:
    row = {
        "time": time.time(),
        "owner": _owner(e.get("owner")),
        "chat_id": e.get("chat_session_id") or "",
        "source": e.get("source") or "chat",
        "tool": e.get("tool") or "",
        "action": e.get("title") or e.get("action") or "",
        "target": (e.get("target") or {}).get("name") or (e.get("target") or {}).get("url") or "",
        "account": e.get("account") or "",
        "outcome": outcome,
        "note": e.get("deny_note") or "",
        "id": e.get("id") or "",
    }
    try:
        os.makedirs(os.path.dirname(_audit_file()), exist_ok=True)
        with _lock, open(_audit_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as ex:
        logger.warning("[approvals] could not write the audit log: %s", ex)


def recent_audit(owner: Optional[str], limit: int = 50, all_owners: bool = False) -> List[dict]:
    own = _owner(owner)
    rows: List[dict] = []
    try:
        with open(_audit_file(), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and (all_owners or _owner(r.get("owner")) == own):
                    rows.append(r)
    except FileNotFoundError:
        return []
    rows = rows[-max(1, int(limit)):]
    titles = {}
    try:
        from src.ai_interaction import get_session_manager
        sm = get_session_manager()
        for r in rows:
            cid = r.get("chat_id")
            if cid and cid not in titles:
                try:
                    s = sm.get_session(cid) if sm else None
                    titles[cid] = (getattr(s, "name", "") or "") if s else ""
                except Exception:
                    titles[cid] = ""
    except Exception:
        pass
    for r in rows:
        r["chat_title"] = titles.get(r.get("chat_id"), "")
    return list(reversed(rows))


# ── What the model is told ───────────────────────────────────────────────

def held_output(e: dict) -> str:
    where = ("in Company > Approvals" if e.get("source") == "company"
             else "in the chat with Approve and Deny")
    return (f"Not run yet: \"{e.get('title')}\" writes to the user's Google account, so it needs their "
            f"approval first (approval id {e['id']}). It is shown {where}. Your turn ends here: do not "
            "call it again, do not try another way to make the change, and do not say it is done. If they "
            "approve, it runs exactly as you asked and you get its result.")


def approved_prompt(e: dict) -> str:
    r = e.get("result") or {}
    ok = r.get("exit_code") == 0
    return (f"[Google change approved · {e.get('title')}]\n\n"
            f"The user approved \"{e.get('title')}\" and it ran once, exactly as requested"
            + ("." if ok else ", but it failed.")
            + f" Its result:\n\n<result>\n{(r.get('output') or '').strip()}\n</result>\n\n"
            "Do not run it again. Carry on with the task from here and tell the user the outcome.")


def denied_prompt(e: dict) -> str:
    note = (e.get("deny_note") or "").strip()
    return (f"[Google change denied · {e.get('title')}]\n\n"
            f"The user denied {e.get('title')}"
            + (f": {note}" if note else ". They left no note.")
            + "\n\nIt did not run. Do not try it again or make the change another way. Ask the user what "
              "they would like instead, or carry on without it.")
