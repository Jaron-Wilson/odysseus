"""Native plans: plans a chat model writes itself, as Plans documents the user approves in the chat.

Asked for: "i want it so that the model never uses anything external to make
the plans, only using the native markdown planner, so instead of sending it out
it writes the md file in the chat so that i can approve or deny it in the chat."

The model calls `propose_plan` (src/tool_execution.py) with a title and a
Markdown body. That body is saved through src/plans.py as an ordinary Plans
document, DATA_DIR/plans/<owner>/<title>.md, so it opens in the Plans editor
like any plan the user wrote. This module keeps what a .md file can't hold:
which chat the plan belongs to, its status, who answered and when, the text
that was approved, and whether its coding run has started. That lives in
DATA_DIR/native_plans.json, keyed by a short random id.

States:
  pending     waiting for the user's Approve or Deny in the chat
  approved    the user approved it; `approved_content` is the text approved
  denied      the user denied it, with an optional note
  superseded  a newer plan in the same chat replaced it before it was answered

Only routes/native_plans_routes.py (an authenticated request from the user's
own page) can approve or deny a chat plan; nothing a model calls reaches
`approve` or `deny`. A coding run (claude_code action "execute") spends the
approval once through `consume_for_execute`, so a model cannot start the same
approved plan twice.

Company agents' plan approvals (company_request_approval type "plan") are
mirrored here too, with source "company", so every plan is in one place; their
approval itself stays in the Company flow.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Dict, List, Optional

from core.atomic_io import atomic_write_json
from src import plans

logger = logging.getLogger(__name__)

STATUSES = ("pending", "approved", "denied", "superseded")
MAX_TITLE = 100
MAX_NOTE = 2000
ID_RE = re.compile(r"^[0-9a-f]{12}$")

# Delegation tools: what a model could use to have something else write a plan
# (another model, a sub-agent, a coding agent). Refused while a plan in the
# chat is waiting for the user, so the answer comes from the user first.
DELEGATION_TOOLS = frozenset({
    "claude_code", "chat_with_model", "ask_teacher", "pipeline", "send_to_session",
    "create_session", "spawn_subagent", "branch_thread",
})

_lock = threading.RLock()


def _file() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "native_plans.json")


def _load() -> Dict[str, dict]:
    try:
        with open(_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _save(data: Dict[str, dict]) -> None:
    atomic_write_json(_file(), data, indent=1)


def _owner(owner: Optional[str]) -> str:
    return (owner or "").strip()


def valid_id(plan_id: str) -> bool:
    return bool(ID_RE.match(str(plan_id or "")))


# ── Reading ──────────────────────────────────────────────────────────────

def get(plan_id: str) -> Optional[dict]:
    if not valid_id(plan_id):
        return None
    with _lock:
        e = _load().get(plan_id)
    return dict(e) if e else None


def content_of(entry: dict) -> str:
    """The plan's current text: its Plans file, which the user may have
    edited, or the last copy kept here if the file is gone."""
    try:
        return plans.get_plan(entry.get("owner") or None, entry["name"])["content"]
    except Exception:
        return entry.get("content") or ""


def view(entry: dict) -> dict:
    """What the chat card and the API show."""
    text = content_of(entry)
    exists = True
    version = ""
    try:
        p = plans.get_plan(entry.get("owner") or None, entry["name"])
        version = p.get("version", "")
    except Exception:
        exists = False
    o = plans.outline(text)
    return {
        "id": entry["id"],
        "title": entry.get("title") or entry.get("name"),
        "name": entry.get("name"),
        "status": entry.get("status"),
        "source": entry.get("source") or "chat",
        "chat_session_id": entry.get("chat_session_id") or "",
        "revision": entry.get("revision") or 1,
        "revises": entry.get("revises") or "",
        "content": text,
        "file_exists": exists,
        "file_version": version,
        "tasks_total": o["tasks_total"],
        "tasks_done": o["tasks_done"],
        "engine": entry.get("engine") or "",
        "cwd": entry.get("cwd") or "",
        "created": entry.get("created"),
        "approved_at": entry.get("approved_at"),
        "denied_at": entry.get("denied_at"),
        "deny_note": entry.get("deny_note") or "",
        "executed_at": entry.get("executed_at"),
        "run_job_id": entry.get("run_job_id") or "",
        "edited_since_approval": bool(entry.get("status") == "approved"
                                      and entry.get("approved_content") is not None
                                      and text != entry.get("approved_content")),
    }


def for_chat(chat_id: str) -> List[dict]:
    """Every plan in a chat, oldest first."""
    if not chat_id:
        return []
    with _lock:
        rows = [dict(e) for e in _load().values() if e.get("chat_session_id") == chat_id]
    return sorted(rows, key=lambda e: e.get("created") or 0)


def pending_for_chat(chat_id: str) -> Optional[dict]:
    """The plan in this chat still waiting for the user, if any."""
    for e in reversed(for_chat(chat_id)):
        if e.get("status") == "pending":
            return e
    return None


def by_name(owner: Optional[str]) -> Dict[str, dict]:
    """The owner's plans that came from a chat or Company, by file name, for
    the Plans list's status badges."""
    own = _owner(owner)
    with _lock:
        rows = [e for e in _load().values() if _owner(e.get("owner")) == own]
    out: Dict[str, dict] = {}
    for e in sorted(rows, key=lambda e: e.get("created") or 0):
        out[e.get("name") or ""] = {"id": e["id"], "status": e.get("status"),
                                    "source": e.get("source") or "chat"}
    out.pop("", None)
    return out


# ── Writing ──────────────────────────────────────────────────────────────

def _body(title: str, content: str) -> str:
    content = (content or "").strip()
    if not re.match(r"^#\s", content):
        content = f"# {title}\n\n{content}"
    return content.rstrip() + "\n"


def propose(owner: Optional[str], chat_id: str, title: str, content: str, *,
            engine: str = "", cwd: str = "", revises: str = "",
            source: str = "chat", extra: Optional[dict] = None) -> dict:
    """Save a plan as a Plans document and record it as pending for the chat.

    A plan still pending in the same chat is marked superseded: only the
    newest one can be answered. A revision (after a denial, or `revises`)
    keeps the earlier plan and its file as they were."""
    title = re.sub(r"\s+", " ", str(title or "")).strip()[:MAX_TITLE] or "Plan"
    content = str(content or "")
    if not content.strip():
        raise plans.PlanError(400, "A plan needs a Markdown body")
    with _lock:
        data = _load()
        prev = data.get(revises) if revises else None
        if prev is not None and prev.get("chat_session_id") != chat_id:
            prev = None
        if prev is None and chat_id:
            mine = [e for e in data.values() if e.get("chat_session_id") == chat_id]
            if mine:
                prev = max(mine, key=lambda e: e.get("created") or 0)
                if prev.get("status") == "approved":
                    prev = None          # a new piece of work, not a revision
        revision = (int(prev.get("revision") or 1) + 1) if prev else 1
        file_title = title if revision == 1 else f"{title} (v{revision})"
        doc = plans.create_plan(owner, file_title, _body(title, content))
        for e in data.values():
            if e.get("chat_session_id") == chat_id and chat_id and e.get("status") == "pending":
                e["status"] = "superseded"
                e["superseded_at"] = time.time()
        pid = uuid.uuid4().hex[:12]
        entry = {
            "id": pid,
            "owner": _owner(owner),
            "name": doc["name"],
            "title": title,
            "chat_session_id": chat_id or "",
            "status": "pending",
            "source": source,
            "revision": revision,
            "revises": prev["id"] if prev else "",
            "engine": (engine or "").strip().lower()[:20],
            "cwd": (cwd or "").strip()[:500],
            "content": doc["content"],
            "created": time.time(),
        }
        if extra:
            entry.update(extra)
        data[pid] = entry
        _save(data)
    logger.info("[native_plans] plan %s proposed in chat %s (%s)", pid, (chat_id or "-")[:8], doc["name"])
    return dict(entry)


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _answerable(data: dict, plan_id: str, user: str) -> dict:
    e = data.get(plan_id)
    if not e:
        raise Refused(404, "No such plan")
    if _owner(e.get("owner")) != _owner(user):
        raise Refused(404, "No such plan")
    if (e.get("source") or "chat") != "chat":
        raise Refused(409, "This plan is answered in Company > Approvals")
    if e.get("status") == "superseded":
        raise Refused(409, "A newer plan in this chat replaced this one. Answer the newest plan.")
    if e.get("status") != "pending":
        raise Refused(409, f"This plan is already {e.get('status')}")
    return e


def approve(plan_id: str, user: str) -> dict:
    """The user's Approve. The text approved is the plan file as it is now,
    including any edits the user made before approving."""
    with _lock:
        data = _load()
        e = _answerable(data, plan_id, user)
        e["approved_content"] = content_of(e)
        e["content"] = e["approved_content"]
        e["status"] = "approved"
        e["approved_at"] = time.time()
        e["approved_by"] = _owner(user)
        _save(data)
        return dict(e)


def deny(plan_id: str, user: str, note: str = "") -> dict:
    with _lock:
        data = _load()
        e = _answerable(data, plan_id, user)
        e["content"] = content_of(e)
        e["status"] = "denied"
        e["denied_at"] = time.time()
        e["denied_by"] = _owner(user)
        e["deny_note"] = str(note or "").strip()[:MAX_NOTE]
        _save(data)
        return dict(e)


def consume_for_execute(plan_id: str, chat_id: str, cwd: str) -> tuple:
    """Spend an approved plan's one coding run. Returns (entry, error)."""
    with _lock:
        data = _load()
        e = data.get(plan_id) if valid_id(plan_id) else None
        if not e or (e.get("source") or "chat") != "chat":
            return None, "there is no native plan with that plan_id"
        if (e.get("chat_session_id") or "") != (chat_id or ""):
            return None, "that plan belongs to another chat"
        status = e.get("status")
        if status == "pending":
            return None, "the user has not approved this plan yet: wait for their answer"
        if status in ("denied", "superseded"):
            return None, f"this plan was {status}: propose a new plan instead"
        if e.get("executed_at"):
            job = e.get("run_job_id") or ""
            return None, ("this approved plan's coding run already started"
                          + (f" (job {job})" if job else "")
                          + ". Do not start it again: report its result, or use action 'status'.")
        want = e.get("cwd") or ""
        if want and os.path.realpath(want) != os.path.realpath(cwd or ""):
            return None, f"cwd does not match the approved plan, which names {want}"
        e["executed_at"] = time.time()
        _save(data)
        return dict(e), ""


def set_run_job(plan_id: str, job_id: str) -> None:
    with _lock:
        data = _load()
        if plan_id in data:
            data[plan_id]["run_job_id"] = job_id
            _save(data)


def restore_execute(plan_id: str) -> bool:
    """Give the run back after one that never really ran (bad model, timeout)."""
    with _lock:
        data = _load()
        e = data.get(plan_id)
        if not e or not e.get("executed_at"):
            return False
        e.pop("executed_at", None)
        e.pop("run_job_id", None)
        _save(data)
        return True


def renamed(owner: Optional[str], old: str, new: str) -> None:
    """Keep the link when the user renames a plan in the Plans editor."""
    if old == new:
        return
    with _lock:
        data = _load()
        hit = False
        for e in data.values():
            if _owner(e.get("owner")) == _owner(owner) and e.get("name") == old:
                e["name"] = new
                hit = True
        if hit:
            _save(data)


# ── Company agents' plans ────────────────────────────────────────────────

def mirror_company_plan(owner: Optional[str], *, approval_id: str, title: str, plan: str,
                        agent_name: str = "", issue: str = "") -> Optional[dict]:
    """A Company agent's plan, kept as a Plans document too. Answered in
    Company > Approvals, never here."""
    head = f"_Company plan from {agent_name or 'an agent'}" + (f" for {issue}" if issue else "") \
        + ". Approve or deny it in Company > Approvals._\n\n"
    try:
        return propose(owner, "", title or "Company plan", head + (plan or ""),
                       source="company", extra={"company_approval_id": approval_id})
    except Exception as ex:
        logger.warning("[native_plans] could not mirror company plan %s: %s", approval_id, ex)
        return None


def company_decided(approval_id: str, decision: str, note: str = "") -> None:
    status = {"approved": "approved", "rejected": "denied", "denied": "denied"}.get(decision)
    if not approval_id or not status:
        return
    with _lock:
        data = _load()
        hit = False
        for e in data.values():
            if e.get("company_approval_id") == approval_id and e.get("status") == "pending":
                e["status"] = status
                e[f"{'approved' if status == 'approved' else 'denied'}_at"] = time.time()
                if status == "denied":
                    e["deny_note"] = str(note or "")[:MAX_NOTE]
                hit = True
        if hit:
            _save(data)


# ── What the model is told ───────────────────────────────────────────────

RULE = (
    "## PLANS ARE WRITTEN HERE, BY YOU\n"
    "When work needs a plan (anything that will change code, files or settings in more than a "
    "trivial way, or whenever the user asks for a plan), write it YOURSELF with the "
    "`propose_plan` tool: a title and a Markdown body with the steps as a `- [ ]` checklist, the "
    "files or areas affected, the risks, and what will run once it is approved (for code: which "
    "coding engine and the absolute project folder). It is saved in the user's Plans and shown "
    "in this chat with Approve, Deny and Edit, and calling it ends your turn. Never use anything "
    "else to make or draft a plan: not claude_code (its 'plan' action is gone), not "
    "chat_with_model, ask_teacher, pipeline, sub-agents, other chats or MCP tools. Read-only "
    "recon to ground the plan is fine (read_file, grep, claude_code action 'ask'). Do not start "
    "the work until the user approves; claude_code action 'execute' needs the approved plan's "
    "`plan_id`."
)


def context_note(chat_id: str) -> str:
    """A system note about this chat's newest plan, if it is waiting or
    approved and not yet run."""
    rows = for_chat(chat_id)
    if not rows:
        return ""
    e = rows[-1]
    if e.get("status") == "pending":
        return (f"## A PLAN IS WAITING FOR THE USER\nYour plan \"{e.get('title')}\" (plan_id "
                f"{e['id']}) is waiting for the user's Approve or Deny in this chat. Until they "
                "answer, delegation tools (claude_code, chat_with_model, ask_teacher, pipeline, "
                "sub-agents, other chats) are refused. If the user asks for changes, call "
                "propose_plan again with the revised plan (it replaces this one).")
    if e.get("status") == "approved" and not e.get("executed_at"):
        return (f"## APPROVED PLAN\nThe user approved the plan \"{e.get('title')}\" (plan_id "
                f"{e['id']}). Carry it out. If it needs a coding agent, call claude_code with "
                f"action 'execute', plan_id '{e['id']}' and the plan's cwd.")
    return ""


def approved_prompt(entry: dict) -> str:
    text = entry.get("approved_content") or entry.get("content") or ""
    run = ""
    if entry.get("cwd") or entry.get("engine"):
        run = ("\n\nIf it needs a coding agent, call claude_code with exactly "
               + json.dumps({"action": "execute", "plan_id": entry["id"],
                             "cwd": entry.get("cwd") or "<the project folder>",
                             **({"engine": entry["engine"]} if entry.get("engine") else {}),
                             "prompt": "Carry out the approved plan."})
               + " (no plan phase runs: the plan is already approved).")
    else:
        run = ("\n\nIf it needs a coding agent, call claude_code with action 'execute', "
               f"plan_id '{entry['id']}' and the project folder as cwd (no plan phase runs: the "
               "plan is already approved).")
    return (f"[Plan approved · {entry.get('title')}]\n\n"
            f"The user approved plan \"{entry.get('title')}\"; carry it out. The approved plan "
            f"is below; it is the spec. Work through it in order.{run}\n\n<approved-plan>\n{text.strip()}\n</approved-plan>")


def denied_prompt(entry: dict) -> str:
    note = (entry.get("deny_note") or "").strip()
    return (f"[Plan denied · {entry.get('title')}]\n\n"
            f"The user denied plan \"{entry.get('title')}\"."
            + (f" Their note: {note}" if note else " They left no note.")
            + "\n\nDo not carry it out. Either revise it by calling propose_plan with a new "
              f"version (pass revises '{entry['id']}'; the earlier plan is kept), or stop and ask "
              "what they would like instead. Do not use any other tool or model to write the plan.")
