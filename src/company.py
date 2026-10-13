"""Company: an organization of AI agents inside Odysseus (docs/company.md).

The data rules live here, not in the routes, so the executor that will run
agents (PR C) can call the same functions as the HTTP API and get the same
validation and the same activity log. Nothing in this module runs a model;
src/company_executor.py does, and src/company_tools.py gives agents tools
that call back into these functions as Actor("agent", agent_id, run_id).

Every function takes ``owner`` (the username, or "" in single-user mode) and
only ever touches rows of that owner. Errors are raised as ``CompanyError``
with an HTTP-style status so the routes can pass them straight through.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import case, or_, select, update

from core.database import (
    CompanyActivity, CompanyAgent, CompanyApproval, CompanyCompany, CompanyGoal,
    CompanyIssue, CompanyIssueComment, CompanyIssueLabel, CompanyIssueRelation,
    CompanyLabel, CompanyProject, CompanyRun, CompanyWakeup, utcnow_naive,
)
from src.auth_helpers import owner_filter

# ── Vocabulary ───────────────────────────────────────────────────────────

COMPANY_STATUSES = ("active", "paused", "archived")
GOAL_LEVELS = ("company", "team", "agent", "task")
GOAL_STATUSES = ("planned", "active", "achieved", "cancelled")
PROJECT_STATUSES = ("backlog", "planned", "in_progress", "completed", "cancelled")
AGENT_ROLES = ("ceo", "cto", "cmo", "cfo", "security", "engineer", "designer",
               "pm", "qa", "devops", "researcher", "general")
AGENT_STATUSES = ("active", "paused", "idle", "running", "error", "pending_approval", "terminated")
# What a person may set by hand. running/error belong to the executor and
# pending_approval to the hire flow.
USER_AGENT_STATUSES = ("active", "paused", "idle", "terminated")
ISSUE_STATUSES = ("backlog", "todo", "in_progress", "in_review", "done", "blocked", "cancelled")
ISSUE_TRANSITIONS: Dict[str, tuple] = {
    "backlog":     ("todo", "in_progress", "blocked", "cancelled"),
    "todo":        ("backlog", "in_progress", "blocked", "cancelled"),
    "in_progress": ("todo", "in_review", "done", "blocked", "cancelled"),
    "in_review":   ("in_progress", "todo", "done", "blocked", "cancelled"),
    "blocked":     ("backlog", "todo", "in_progress", "cancelled"),
    "done":        ("todo",),                  # reopen
    "cancelled":   ("backlog", "todo"),        # reopen
}
PRIORITIES = ("critical", "high", "medium", "low")
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
RUN_SOURCES = ("timer", "assignment", "mention", "on_demand", "approval")
# no_progress: the run ended normally but never worked on the issues it was
# woken for (src/company_executor._outcome).
RUN_STATUSES = ("queued", "running", "succeeded", "no_progress", "failed", "cancelled", "timed_out", "interrupted")
RUN_TERMINAL = ("succeeded", "no_progress", "failed", "cancelled", "timed_out", "interrupted")
RUN_LIVE = ("queued", "running")
# google_write: an agent's change to the person's Google account, held by
# src/google_write_gate.py and run once when approved (src/action_approvals.py).
APPROVAL_TYPES = ("hire_agent", "strategy", "plan", "budget_override", "google_write")
APPROVAL_STATUSES = ("pending", "revision_requested", "approved", "rejected", "cancelled")
APPROVAL_DECISIONS = ("approved", "rejected", "revision_requested")
RELATION_TYPES = ("blocks",)
DEFAULT_HEARTBEAT = {"enabled": False, "interval_sec": 3600, "wake_on_assignment": True,
                     "wake_on_mention": True, "wake_on_demand": True}
# A checkout lock older than this can be taken over by another run even if
# its run never reported back (crashed worker, restarted server).
STALE_LOCK_SECONDS = 30 * 60
CHECKOUT_STATUSES = ("backlog", "todo", "blocked", "in_review", "in_progress")

_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CompanyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Actor:
    """Who is making a change: the person (board) or an agent in a run."""
    type: str = "user"            # user | agent | system
    id: Optional[str] = None
    run_id: Optional[str] = None

    @property
    def agent_id(self) -> Optional[str]:
        return self.id if self.type == "agent" else None


def user_actor(user: str) -> Actor:
    return Actor("user", user or "local")


def _say(status: str) -> str:
    """A stored value as words for a message: "in_progress" -> "in progress".
    Values keep Paperclip's "cancelled"; messages use American spelling."""
    return str(status).replace("_", " ").replace("cancelled", "canceled")


def _id() -> str:
    return uuid.uuid4().hex


def month_key(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow_naive()).strftime("%Y-%m")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def _owner_val(owner: str) -> Optional[str]:
    return owner or None


def _scoped(db, model, owner: str):
    return owner_filter(db.query(model), model, owner, include_shared=False)


def _get(db, model, ident: Optional[str], owner: str, what: str):
    row = _scoped(db, model, owner).filter(model.id == ident).first() if ident else None
    if row is None:
        raise CompanyError(404, f"{what} not found")
    return row


def _text(v: Any, field: str, *, required: bool = False, max_len: int = 200) -> Optional[str]:
    if v is None:
        if required:
            raise CompanyError(422, f"{field} is required")
        return None
    if not isinstance(v, str):
        raise CompanyError(422, f"{field} must be text")
    v = v.strip()
    if required and not v:
        raise CompanyError(422, f"{field} is required")
    if len(v) > max_len:
        raise CompanyError(422, f"{field} is too long (max {max_len})")
    return v or None


def _choice(v: Any, field: str, allowed: Iterable[str]) -> str:
    if v not in allowed:
        raise CompanyError(422, f"{field} must be one of: {', '.join(allowed)}")
    return v


def _cents(v: Any, field: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise CompanyError(422, f"{field} must be a whole number of cents, 0 or more")
    return v


def _budget(spent: int, month: Optional[str], limit: int) -> Dict[str, Any]:
    # A new month starts at zero; the executor (PR C) rolls the stored value.
    cur = month_key()
    spent_now = spent if month == cur else 0
    return {"monthly_budget_cents": limit, "spent_cents": spent_now, "budget_month": cur,
            "over_budget": bool(limit) and spent_now >= limit}


# ── Activity ─────────────────────────────────────────────────────────────

def log_activity(db, owner: str, company_id: str, actor: Actor, action: str, entity_type: str,
                 entity_id: Optional[str], details: Optional[Dict] = None,
                 agent_id: Optional[str] = None, run_id: Optional[str] = None) -> CompanyActivity:
    row = CompanyActivity(
        id=_id(), owner=_owner_val(owner), company_id=company_id, actor_type=actor.type,
        actor_id=actor.id, action=action, entity_type=entity_type, entity_id=entity_id,
        agent_id=agent_id or actor.agent_id, run_id=run_id or actor.run_id, details=details or None,
    )
    db.add(row)
    return row


def ser_activity(a: CompanyActivity) -> Dict:
    return {"id": a.id, "company_id": a.company_id, "actor_type": a.actor_type, "actor_id": a.actor_id,
            "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id,
            "agent_id": a.agent_id, "run_id": a.run_id, "details": a.details or {},
            "created_at": _iso(a.created_at)}


def list_activity(db, owner: str, company_id: str, *, limit: int = 50, before: Optional[str] = None,
                  entity_type: Optional[str] = None, entity_id: Optional[str] = None,
                  agent_id: Optional[str] = None) -> List[Dict]:
    get_company(db, owner, company_id)
    q = _scoped(db, CompanyActivity, owner).filter(CompanyActivity.company_id == company_id)
    if entity_type:
        q = q.filter(CompanyActivity.entity_type == entity_type)
    if entity_id:
        q = q.filter(CompanyActivity.entity_id == entity_id)
    if agent_id:
        q = q.filter(CompanyActivity.agent_id == agent_id)
    if before:
        try:
            q = q.filter(CompanyActivity.created_at < datetime.fromisoformat(before.rstrip("Z")))
        except ValueError:
            raise CompanyError(422, "before must be an ISO timestamp")
    rows = q.order_by(CompanyActivity.created_at.desc()).limit(max(1, min(limit, 200))).all()
    return [ser_activity(a) for a in rows]


# ── Companies ────────────────────────────────────────────────────────────

def ser_company(c: CompanyCompany) -> Dict:
    return {"id": c.id, "name": c.name, "description": c.description or "", "status": c.status,
            "issue_prefix": c.issue_prefix, "issue_counter": c.issue_counter,
            "require_approval_for_new_agents": bool(c.require_approval_for_new_agents),
            **_budget(c.spent_cents or 0, c.budget_month, c.monthly_budget_cents or 0),
            "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at)}


def get_company(db, owner: str, company_id: str) -> CompanyCompany:
    return _get(db, CompanyCompany, company_id, owner, "Company")


def list_companies(db, owner: str) -> List[Dict]:
    rows = _scoped(db, CompanyCompany, owner).order_by(CompanyCompany.created_at).all()
    return [ser_company(c) for c in rows]


def _derive_prefix(name: str) -> str:
    """"Odysseus Labs" -> "OL", "Acme" -> "ACM"."""
    words = re.findall(r"[A-Za-z0-9]+", name)
    p = "".join(w[0] for w in words[:4]) if len(words) >= 2 else (words[0][:3] if words else "")
    p = p.upper()
    if not p or not p[0].isalpha():
        p = "C" + p
    return (p + "O")[:10] if len(p) < 2 else p[:10]


def _prefix_taken(db, owner: str, prefix: str, exclude_id: Optional[str] = None) -> bool:
    q = _scoped(db, CompanyCompany, owner).filter(CompanyCompany.issue_prefix == prefix)
    if exclude_id:
        q = q.filter(CompanyCompany.id != exclude_id)
    return db.query(q.exists()).scalar()


def create_company(db, owner: str, actor: Actor, data: Dict) -> Dict:
    name = _text(data.get("name"), "name", required=True, max_len=120)
    prefix = data.get("issue_prefix")
    if prefix:
        prefix = str(prefix).strip().upper()
        if not _PREFIX_RE.match(prefix):
            raise CompanyError(422, "issue_prefix must be 2 to 10 letters or digits, starting with a letter")
        if _prefix_taken(db, owner, prefix):
            raise CompanyError(409, f"You already have a company using the prefix {prefix}")
    else:
        base = _derive_prefix(name)
        prefix, n = base, 2
        while _prefix_taken(db, owner, prefix):
            prefix = f"{base[:8]}{n}"
            n += 1
    c = CompanyCompany(
        id=_id(), owner=_owner_val(owner), name=name,
        description=_text(data.get("description"), "description", max_len=4000),
        status=_choice(data.get("status", "active"), "status", COMPANY_STATUSES),
        issue_prefix=prefix, issue_counter=0,
        monthly_budget_cents=_cents(data.get("monthly_budget_cents", 0), "monthly_budget_cents"),
        spent_cents=0, budget_month=month_key(),
        require_approval_for_new_agents=bool(data.get("require_approval_for_new_agents", False)),
    )
    db.add(c)
    db.flush()
    log_activity(db, owner, c.id, actor, "company.created", "company", c.id, {"name": name, "issue_prefix": prefix})
    db.commit()
    return ser_company(c)


def update_company(db, owner: str, actor: Actor, company_id: str, data: Dict) -> Dict:
    c = get_company(db, owner, company_id)
    changes: Dict[str, Any] = {}

    def setf(field, value):
        old = getattr(c, field)
        if old != value:
            changes[field] = {"from": old, "to": value}
            setattr(c, field, value)

    if "name" in data:
        setf("name", _text(data["name"], "name", required=True, max_len=120))
    if "description" in data:
        setf("description", _text(data["description"], "description", max_len=4000))
    if "status" in data:
        setf("status", _choice(data["status"], "status", COMPANY_STATUSES))
    if "monthly_budget_cents" in data:
        setf("monthly_budget_cents", _cents(data["monthly_budget_cents"], "monthly_budget_cents"))
    if "require_approval_for_new_agents" in data:
        setf("require_approval_for_new_agents", bool(data["require_approval_for_new_agents"]))
    if "issue_prefix" in data:
        prefix = str(data["issue_prefix"] or "").strip().upper()
        if prefix != c.issue_prefix:
            if not _PREFIX_RE.match(prefix):
                raise CompanyError(422, "issue_prefix must be 2 to 10 letters or digits, starting with a letter")
            if c.issue_counter:
                raise CompanyError(409, "The prefix can't change once the company has issues")
            if _prefix_taken(db, owner, prefix, exclude_id=c.id):
                raise CompanyError(409, f"You already have a company using the prefix {prefix}")
            setf("issue_prefix", prefix)
    if changes:
        log_activity(db, owner, c.id, actor, "company.updated", "company", c.id, {"changes": changes})
    db.commit()
    return ser_company(c)


def delete_company(db, owner: str, actor: Actor, company_id: str) -> None:
    c = get_company(db, owner, company_id)
    # SQLite FKs cascade (PRAGMA foreign_keys=ON), but delete the children
    # explicitly too so a backend without enforced FKs leaves nothing behind.
    for model in (CompanyIssueLabel,):
        ids = [i for (i,) in db.query(CompanyIssue.id).filter(CompanyIssue.company_id == c.id)]
        if ids:
            db.query(model).filter(model.issue_id.in_(ids)).delete(synchronize_session=False)
    for model in (CompanyActivity, CompanyIssueComment, CompanyIssueRelation, CompanyWakeup, CompanyRun,
                  CompanyApproval, CompanyLabel):
        db.query(model).filter(model.company_id == c.id).delete(synchronize_session=False)
    # Self-referencing rows: clear the links first so the order doesn't matter.
    db.query(CompanyIssue).filter(CompanyIssue.company_id == c.id).update(
        {CompanyIssue.parent_id: None}, synchronize_session=False)
    db.query(CompanyIssue).filter(CompanyIssue.company_id == c.id).delete(synchronize_session=False)
    db.query(CompanyProject).filter(CompanyProject.company_id == c.id).delete(synchronize_session=False)
    db.query(CompanyGoal).filter(CompanyGoal.company_id == c.id).update(
        {CompanyGoal.parent_id: None, CompanyGoal.owner_agent_id: None}, synchronize_session=False)
    db.query(CompanyGoal).filter(CompanyGoal.company_id == c.id).delete(synchronize_session=False)
    db.query(CompanyAgent).filter(CompanyAgent.company_id == c.id).update(
        {CompanyAgent.reports_to: None}, synchronize_session=False)
    db.query(CompanyAgent).filter(CompanyAgent.company_id == c.id).delete(synchronize_session=False)
    db.delete(c)
    db.commit()


# ── Agents and the org tree ──────────────────────────────────────────────

def heartbeat_policy(raw: Any) -> Dict[str, Any]:
    pol = dict(DEFAULT_HEARTBEAT)
    if raw is None:
        return pol
    if not isinstance(raw, dict):
        raise CompanyError(422, "heartbeat must be an object")
    for k, v in raw.items():
        if k not in DEFAULT_HEARTBEAT:
            raise CompanyError(422, f"Unknown heartbeat setting: {k}")
        if k == "interval_sec":
            if isinstance(v, bool) or not isinstance(v, int) or not 60 <= v <= 7 * 86400:
                raise CompanyError(422, "heartbeat.interval_sec must be between 60 and 604800")
        elif not isinstance(v, bool):
            raise CompanyError(422, f"heartbeat.{k} must be true or false")
        pol[k] = v
    return pol


def _tool_list(v: Any, field: str) -> Optional[List[str]]:
    if v is None:
        return None
    if isinstance(v, str):
        v = [t for t in re.split(r"[\s,]+", v) if t]
    if not isinstance(v, list) or not all(isinstance(t, str) and t.strip() for t in v):
        raise CompanyError(422, f"{field} must be a list of tool names")
    out = []
    for t in v:
        t = t.strip()
        if len(t) > 80:
            raise CompanyError(422, f"{field}: tool name too long")
        if t not in out:
            out.append(t)
    return out


CODING_AGENTS = ("opencode", "claude", "antigravity")


def _coding_agent(v: Any) -> Optional[str]:
    if v in (None, "", "off"):
        return None
    return _choice(v, "coding_agent", CODING_AGENTS)


def _workdir(v: Any) -> Optional[str]:
    """The folder an agent's coding agent works in: an absolute path, and
    never inside the running Odysseus install (the tool refuses that too)."""
    import os
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if not isinstance(v, str) or len(v) > 1000:
        raise CompanyError(422, "workdir must be a folder path")
    path = os.path.realpath(os.path.expanduser(v.strip()))
    if not os.path.isabs(path):
        raise CompanyError(422, "workdir must be an absolute path")
    root = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    if path == root or path.startswith(root + os.sep):
        raise CompanyError(422, "workdir can't be inside the running Odysseus install")
    if os.path.exists(path) and not os.path.isdir(path):
        raise CompanyError(422, "workdir is a file, not a folder")
    return path


def _cached_model_match(spec: str, owner: str) -> bool:
    """True when `spec` names a model the owner's endpoint last listed. Lets an
    agent be saved while a local endpoint is briefly offline."""
    if "@" not in spec:
        return False
    model, ep_name = (s.strip() for s in spec.rsplit("@", 1))
    try:
        from core.database import ModelEndpoint, SessionLocal
        db = SessionLocal()
        try:
            q = owner_filter(db.query(ModelEndpoint), ModelEndpoint, owner)
            for ep in q.filter(ModelEndpoint.is_enabled == True).all():  # noqa: E712
                if (ep.name or "").strip().lower() != ep_name.lower():
                    continue
                try:
                    ids = json.loads(ep.cached_models or "[]")
                except Exception:
                    ids = []
                if any(isinstance(m, str) and m.lower() == model.lower() for m in ids):
                    return True
        finally:
            db.close()
    except Exception:
        return False
    return False


def validate_model_spec(spec: Any, owner: str) -> Optional[str]:
    """Check that `spec` ("model@Endpoint Name") is a model this owner can use.
    Returns the cleaned spec, None for "no model", raises 422 otherwise."""
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return None
    if not isinstance(spec, str) or len(spec) > 300:
        raise CompanyError(422, "model must be a model spec like 'model@Endpoint Name'")
    spec = spec.strip()
    from src import ai_interaction
    from src.endpoint_resolver import invalid_port_error
    try:
        url, _model, _headers = ai_interaction._resolve_model(spec, owner=owner or None)
    except Exception as e:
        if _cached_model_match(spec, owner):
            return spec
        raise CompanyError(422, f"Model '{spec}' is not available to you: {e}")
    bad = invalid_port_error(url)
    if bad:
        raise CompanyError(422, f"Model '{spec}' can't be reached: its endpoint URL {url} is invalid ({bad})")
    return spec


def ser_agent(a: CompanyAgent) -> Dict:
    return {"id": a.id, "company_id": a.company_id, "name": a.name, "role": a.role, "title": a.title or "",
            "capabilities": a.capabilities or "", "reports_to": a.reports_to, "status": a.status,
            "model": a.model or "", "instructions": a.instructions or "",
            "enabled_tools": a.enabled_tools, "disabled_tools": a.disabled_tools,
            "heartbeat": heartbeat_policy(a.heartbeat or None),
            **_budget(a.spent_cents or 0, a.budget_month, a.monthly_budget_cents or 0),
            "last_heartbeat_at": _iso(a.last_heartbeat_at), "session_id": a.session_id,
            "pause_reason": a.pause_reason or "", "auto_paused": bool(a.auto_paused),
            "coding_agent": a.coding_agent or "", "workdir": a.workdir or "",
            "created_at": _iso(a.created_at), "updated_at": _iso(a.updated_at)}


def get_agent(db, owner: str, agent_id: str) -> CompanyAgent:
    return _get(db, CompanyAgent, agent_id, owner, "Agent")


def _company_agents(db, owner: str, company_id: str) -> List[CompanyAgent]:
    return _scoped(db, CompanyAgent, owner).filter(CompanyAgent.company_id == company_id).all()


def list_agents(db, owner: str, company_id: str, status: Optional[str] = None) -> List[Dict]:
    get_company(db, owner, company_id)
    q = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.company_id == company_id)
    if status:
        q = q.filter(CompanyAgent.status.in_(status.split(",")))
    return [ser_agent(a) for a in q.order_by(CompanyAgent.created_at).all()]


def validate_org(agents: List[CompanyAgent], agent_id: Optional[str], role: str,
                 reports_to: Optional[str], status: str) -> None:
    """The org is a single tree: one CEO at the root, everyone else reports to
    someone in the same company, and no chain of managers loops back."""
    if status == "terminated":
        return
    live = {a.id: a for a in agents if a.status != "terminated" and a.id != agent_id}
    if role == "ceo":
        if reports_to:
            raise CompanyError(422, "The CEO is the top of the org and reports to no one")
        if any(a.role == "ceo" for a in live.values()):
            raise CompanyError(409, "This company already has a CEO")
        return
    if not reports_to:
        if not any(a.role == "ceo" for a in live.values()):
            raise CompanyError(422, "Hire a CEO first: every other agent reports to someone")
        raise CompanyError(422, "Choose who this agent reports to")
    if reports_to == agent_id:
        raise CompanyError(422, "An agent can't report to itself")
    if reports_to not in live:
        raise CompanyError(422, "reports_to must be an active agent in this company")
    # Walk up from the new manager; reaching this agent means a cycle.
    seen = set()
    cur = live.get(reports_to)
    while cur is not None:
        if cur.id in seen:
            break
        seen.add(cur.id)
        if cur.reports_to == agent_id and agent_id is not None:
            raise CompanyError(422, "That would make a reporting loop")
        cur = live.get(cur.reports_to) if cur.reports_to else None


def _validate_session(db, owner: str, session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    from core.database import Session as ChatSession
    q = owner_filter(db.query(ChatSession), ChatSession, owner).filter(ChatSession.id == session_id)
    if not db.query(q.exists()).scalar():
        raise CompanyError(422, "session_id must be one of your chats")
    return session_id


def request_wakeup(db, owner: str, agent: CompanyAgent, source: str, *, issue_id: Optional[str] = None,
                   comment_id: Optional[str] = None, approval_id: Optional[str] = None,
                   force: bool = False) -> Optional[CompanyWakeup]:
    """Queue a wake-up for PR C's executor, if the agent's policy wants one.

    An agent the executor paused because its model is offline (auto_paused)
    still collects assignment, mention and approval wake-ups: they run when
    the model is back. Timer heartbeats don't pile up meanwhile."""
    if agent.status in ("terminated", "pending_approval"):
        return None
    if agent.status == "paused" and (not agent.auto_paused or source == "timer"):
        return None
    pol = heartbeat_policy(agent.heartbeat or None)
    key = {"assignment": "wake_on_assignment", "mention": "wake_on_mention",
           "on_demand": "wake_on_demand"}.get(source)
    if not force and key and not pol.get(key, True):
        return None
    w = CompanyWakeup(id=_id(), owner=_owner_val(owner), company_id=agent.company_id, agent_id=agent.id,
                      source=source, issue_id=issue_id, comment_id=comment_id, approval_id=approval_id)
    db.add(w)
    return w


def ser_wakeup(w: CompanyWakeup) -> Dict:
    return {"id": w.id, "company_id": w.company_id, "agent_id": w.agent_id, "source": w.source,
            "issue_id": w.issue_id, "comment_id": w.comment_id, "approval_id": w.approval_id,
            "status": w.status, "run_id": w.run_id, "created_at": _iso(w.created_at),
            "consumed_at": _iso(w.consumed_at)}


def list_wakeups(db, owner: str, company_id: str, status: Optional[str] = "pending") -> List[Dict]:
    get_company(db, owner, company_id)
    q = _scoped(db, CompanyWakeup, owner).filter(CompanyWakeup.company_id == company_id)
    if status:
        q = q.filter(CompanyWakeup.status == status)
    return [ser_wakeup(w) for w in q.order_by(CompanyWakeup.created_at).limit(200).all()]


def create_agent(db, owner: str, actor: Actor, company_id: str, data: Dict) -> Dict:
    c = get_company(db, owner, company_id)
    agents = _company_agents(db, owner, c.id)
    role = _choice(data.get("role", "general"), "role", AGENT_ROLES)
    reports_to = data.get("reports_to") or None
    name = _text(data.get("name"), "name", required=True, max_len=80)
    if any(a.name.lower() == name.lower() and a.status != "terminated" for a in agents):
        raise CompanyError(409, f"There is already an agent named {name}")
    # An agent asking to hire waits for approval when the company wants that.
    needs_ok = bool(c.require_approval_for_new_agents) and actor.type == "agent"
    status = "pending_approval" if needs_ok else _choice(data.get("status", "idle"), "status", ("active", "idle", "paused"))
    validate_org(agents, None, role, reports_to, status)
    a = CompanyAgent(
        id=_id(), owner=_owner_val(owner), company_id=c.id, name=name, role=role,
        title=_text(data.get("title"), "title", max_len=120),
        capabilities=_text(data.get("capabilities"), "capabilities", max_len=4000),
        reports_to=reports_to, status=status,
        model=validate_model_spec(data.get("model"), owner),
        instructions=_text(data.get("instructions"), "instructions", max_len=50000),
        enabled_tools=_tool_list(data.get("enabled_tools"), "enabled_tools"),
        disabled_tools=_tool_list(data.get("disabled_tools"), "disabled_tools"),
        heartbeat=heartbeat_policy(data.get("heartbeat")),
        monthly_budget_cents=_cents(data.get("monthly_budget_cents", 0), "monthly_budget_cents"),
        spent_cents=0, budget_month=month_key(),
        session_id=_validate_session(db, owner, data.get("session_id")),
        coding_agent=_coding_agent(data.get("coding_agent")), workdir=_workdir(data.get("workdir")),
    )
    db.add(a)
    db.flush()
    log_activity(db, owner, c.id, actor, "agent.created", "agent", a.id,
                 {"name": a.name, "role": a.role, "model": a.model, "reports_to": a.reports_to}, agent_id=a.id)
    if needs_ok:
        ap = CompanyApproval(
            id=_id(), owner=_owner_val(owner), company_id=c.id, type="hire_agent", title=f"Hire {a.name}",
            requested_by_type=actor.type, requested_by_id=actor.id, status="pending",
            payload={"agent_id": a.id, "name": a.name, "role": a.role, "model": a.model,
                     "reports_to": a.reports_to})
        db.add(ap)
        log_activity(db, owner, c.id, actor, "approval.created", "approval", ap.id, {"type": "hire_agent"})
    db.commit()
    return ser_agent(a)


def update_agent(db, owner: str, actor: Actor, agent_id: str, data: Dict) -> Dict:
    a = get_agent(db, owner, agent_id)
    agents = _company_agents(db, owner, a.company_id)
    changes: Dict[str, Any] = {}

    def setf(field, value):
        old = getattr(a, field)
        if old != value:
            changes[field] = {"from": old, "to": value}
            setattr(a, field, value)

    role = _choice(data["role"], "role", AGENT_ROLES) if "role" in data else a.role
    reports_to = (data.get("reports_to") or None) if "reports_to" in data else a.reports_to
    status = a.status
    if "status" in data and data["status"] != a.status:
        if a.status == "terminated":
            raise CompanyError(422, "A terminated agent can't come back; hire a new one")
        if a.status == "pending_approval":
            raise CompanyError(422, "This agent is waiting for its hire to be approved")
        status = _choice(data["status"], "status", USER_AGENT_STATUSES)
    if "name" in data:
        name = _text(data["name"], "name", required=True, max_len=80)
        if any(o.id != a.id and o.name.lower() == name.lower() and o.status != "terminated" for o in agents):
            raise CompanyError(409, f"There is already an agent named {name}")
        setf("name", name)
    if status == "terminated" and a.status != "terminated":
        reports = [o for o in agents if o.reports_to == a.id and o.status != "terminated"]
        if reports and not a.reports_to:
            raise CompanyError(409, "Make someone else CEO before terminating the CEO")
        for o in reports:   # their manager's manager takes them on
            o.reports_to = a.reports_to
        if reports:
            changes["reports_moved_to"] = a.reports_to
        db.query(CompanyIssue).filter(CompanyIssue.assignee_agent_id == a.id,
                                      CompanyIssue.status.notin_(("done", "cancelled"))).update(
            {CompanyIssue.assignee_agent_id: None, CompanyIssue.checkout_run_id: None,
             CompanyIssue.execution_locked_at: None}, synchronize_session=False)
    elif role != a.role or reports_to != a.reports_to:
        if a.role == "ceo" and role != "ceo" and any(o.reports_to == a.id and o.status != "terminated" for o in agents):
            # Demoting the CEO would leave the tree without a root.
            raise CompanyError(409, "Make someone else CEO first (Make CEO on the new one)")
        validate_org(agents, a.id, role, reports_to, status)
    setf("role", role)
    setf("reports_to", reports_to)
    setf("status", status)
    if a.status != "paused" and a.pause_reason:
        a.pause_reason = None     # resumed: the reason it was paused no longer holds
    if "status" in data and a.auto_paused:
        a.auto_paused = False     # the person decided: resumed, or paused by hand (never auto-resumed)
    if "title" in data:
        setf("title", _text(data["title"], "title", max_len=120))
    if "capabilities" in data:
        setf("capabilities", _text(data["capabilities"], "capabilities", max_len=4000))
    if "model" in data:
        new = (data["model"] or "").strip() or None
        if new != a.model:
            setf("model", validate_model_spec(new, owner))
    if "instructions" in data:
        new = _text(data["instructions"], "instructions", max_len=50000)
        if new != a.instructions:
            changes["instructions"] = {"updated": True}
            a.instructions = new
    if "enabled_tools" in data:
        setf("enabled_tools", _tool_list(data["enabled_tools"], "enabled_tools"))
    if "disabled_tools" in data:
        setf("disabled_tools", _tool_list(data["disabled_tools"], "disabled_tools"))
    if "heartbeat" in data:
        merged = {**heartbeat_policy(a.heartbeat or None), **(data["heartbeat"] or {})} \
            if isinstance(data["heartbeat"], dict) else data["heartbeat"]
        setf("heartbeat", heartbeat_policy(merged))
    if "monthly_budget_cents" in data:
        setf("monthly_budget_cents", _cents(data["monthly_budget_cents"], "monthly_budget_cents"))
    if "session_id" in data:
        setf("session_id", _validate_session(db, owner, data["session_id"] or None))
    if "coding_agent" in data:
        setf("coding_agent", _coding_agent(data["coding_agent"]))
    if "workdir" in data:
        setf("workdir", _workdir(data["workdir"]))
    if changes:
        action = "agent.terminated" if changes.get("status", {}).get("to") == "terminated" else "agent.updated"
        log_activity(db, owner, a.company_id, actor, action, "agent", a.id, {"changes": _jsonable(changes)},
                     agent_id=a.id)
    db.commit()
    return ser_agent(a)


def make_ceo(db, owner: str, actor: Actor, agent_id: str, old_ceo_role: str = "general") -> Dict:
    """Put `agent_id` at the top of the org. The old CEO keeps its own reports
    and now reports to the new CEO with `old_ceo_role`, so the tree stays a
    tree with no other moves."""
    a = get_agent(db, owner, agent_id)
    if a.status in ("terminated", "pending_approval"):
        raise CompanyError(422, f"{a.name} can't lead the company ({a.status.replace('_', ' ')})")
    _choice(old_ceo_role, "old_ceo_role", [r for r in AGENT_ROLES if r != "ceo"])
    if a.role == "ceo" and not a.reports_to:
        return ser_agent(a)
    old = next((o for o in _company_agents(db, owner, a.company_id)
                if o.role == "ceo" and o.status != "terminated" and o.id != a.id), None)
    if old is not None:
        old.role = old_ceo_role
        old.reports_to = a.id
    a.role = "ceo"
    a.reports_to = None
    log_activity(db, owner, a.company_id, actor, "agent.made_ceo", "agent", a.id,
                 {"previous_ceo": old.id if old else None}, agent_id=a.id)
    db.commit()
    return ser_agent(a)


def delete_agent(db, owner: str, actor: Actor, agent_id: str) -> None:
    a = get_agent(db, owner, agent_id)
    if _scoped(db, CompanyAgent, owner).filter(CompanyAgent.reports_to == a.id).count():
        raise CompanyError(409, "This agent has reports; move them or terminate the agent instead")
    for col in (CompanyIssue.assignee_agent_id,):
        db.query(CompanyIssue).filter(col == a.id).update(
            {col: None, CompanyIssue.checkout_run_id: None, CompanyIssue.execution_locked_at: None},
            synchronize_session=False)
    db.query(CompanyGoal).filter(CompanyGoal.owner_agent_id == a.id).update(
        {CompanyGoal.owner_agent_id: None}, synchronize_session=False)
    db.query(CompanyProject).filter(CompanyProject.lead_agent_id == a.id).update(
        {CompanyProject.lead_agent_id: None}, synchronize_session=False)
    db.query(CompanyWakeup).filter(CompanyWakeup.agent_id == a.id).delete(synchronize_session=False)
    db.query(CompanyRun).filter(CompanyRun.agent_id == a.id).delete(synchronize_session=False)
    log_activity(db, owner, a.company_id, actor, "agent.deleted", "agent", a.id, {"name": a.name})
    db.delete(a)
    db.commit()


def org_tree(db, owner: str, company_id: str) -> Dict:
    """The org chart: the CEO's tree, plus anyone not in it (terminated, or
    left without a manager by an older edit) listed apart."""
    get_company(db, owner, company_id)
    agents = sorted(_company_agents(db, owner, company_id), key=lambda a: (a.created_at or utcnow_naive(), a.name))
    kids: Dict[Optional[str], List[CompanyAgent]] = {}
    for a in agents:
        if a.status != "terminated":
            kids.setdefault(a.reports_to, []).append(a)
    placed = set()

    def node(a: CompanyAgent, depth: int = 0) -> Dict:
        placed.add(a.id)
        children = [node(k, depth + 1) for k in kids.get(a.id, []) if k.id not in placed and depth < 50]
        return {**ser_agent(a), "reports": children}

    root = next((a for a in kids.get(None, []) if a.role == "ceo"), None)
    tree = node(root) if root else None
    others = [ser_agent(a) for a in agents if a.id not in placed]
    return {"root": tree, "unplaced": others, "count": len(agents)}


# ── Goals, projects, labels ──────────────────────────────────────────────

def ser_goal(g: CompanyGoal) -> Dict:
    return {"id": g.id, "company_id": g.company_id, "parent_id": g.parent_id, "title": g.title,
            "description": g.description or "", "level": g.level, "status": g.status,
            "owner_agent_id": g.owner_agent_id, "created_at": _iso(g.created_at), "updated_at": _iso(g.updated_at)}


def _same_company(db, owner: str, model, ident: Optional[str], company_id: str, what: str) -> Optional[str]:
    if not ident:
        return None
    row = _scoped(db, model, owner).filter(model.id == ident).first()
    if row is None or row.company_id != company_id:
        raise CompanyError(422, f"{what} must belong to this company")
    return ident


def _check_goal_parent(db, owner: str, goal_id: Optional[str], parent_id: Optional[str], company_id: str):
    _same_company(db, owner, CompanyGoal, parent_id, company_id, "parent goal")
    cur, seen = parent_id, set()
    while cur:
        if cur == goal_id:
            raise CompanyError(422, "A goal can't sit under itself")
        if cur in seen:
            break
        seen.add(cur)
        cur = db.query(CompanyGoal.parent_id).filter(CompanyGoal.id == cur).scalar()


def list_goals(db, owner: str, company_id: str) -> List[Dict]:
    get_company(db, owner, company_id)
    rows = _scoped(db, CompanyGoal, owner).filter(CompanyGoal.company_id == company_id) \
        .order_by(CompanyGoal.created_at).all()
    return [ser_goal(g) for g in rows]


def create_goal(db, owner: str, actor: Actor, company_id: str, data: Dict) -> Dict:
    c = get_company(db, owner, company_id)
    _check_goal_parent(db, owner, None, data.get("parent_id"), c.id)
    g = CompanyGoal(
        id=_id(), owner=_owner_val(owner), company_id=c.id, parent_id=data.get("parent_id") or None,
        title=_text(data.get("title"), "title", required=True), description=_text(data.get("description"), "description", max_len=8000),
        level=_choice(data.get("level", "company"), "level", GOAL_LEVELS),
        status=_choice(data.get("status", "planned"), "status", GOAL_STATUSES),
        owner_agent_id=_same_company(db, owner, CompanyAgent, data.get("owner_agent_id"), c.id, "owner agent"),
    )
    db.add(g)
    db.flush()
    log_activity(db, owner, c.id, actor, "goal.created", "goal", g.id, {"title": g.title})
    db.commit()
    return ser_goal(g)


def update_goal(db, owner: str, actor: Actor, goal_id: str, data: Dict) -> Dict:
    g = _get(db, CompanyGoal, goal_id, owner, "Goal")
    changes = _apply(g, data, {
        "title": lambda v: _text(v, "title", required=True),
        "description": lambda v: _text(v, "description", max_len=8000),
        "level": lambda v: _choice(v, "level", GOAL_LEVELS),
        "status": lambda v: _choice(v, "status", GOAL_STATUSES),
        "owner_agent_id": lambda v: _same_company(db, owner, CompanyAgent, v, g.company_id, "owner agent"),
        "parent_id": lambda v: (_check_goal_parent(db, owner, g.id, v or None, g.company_id), v or None)[1],
    })
    if changes:
        log_activity(db, owner, g.company_id, actor, "goal.updated", "goal", g.id, {"changes": changes})
    db.commit()
    return ser_goal(g)


def delete_goal(db, owner: str, actor: Actor, goal_id: str) -> None:
    g = _get(db, CompanyGoal, goal_id, owner, "Goal")
    db.query(CompanyGoal).filter(CompanyGoal.parent_id == g.id).update(
        {CompanyGoal.parent_id: g.parent_id}, synchronize_session=False)
    db.query(CompanyProject).filter(CompanyProject.goal_id == g.id).update(
        {CompanyProject.goal_id: None}, synchronize_session=False)
    db.query(CompanyIssue).filter(CompanyIssue.goal_id == g.id).update(
        {CompanyIssue.goal_id: None}, synchronize_session=False)
    log_activity(db, owner, g.company_id, actor, "goal.deleted", "goal", g.id, {"title": g.title})
    db.delete(g)
    db.commit()


def _apply(row, data: Dict, fields: Dict) -> Dict:
    changes = {}
    for k, fn in fields.items():
        if k in data:
            v = fn(data[k])
            old = getattr(row, k)
            if old != v:
                changes[k] = {"from": old, "to": v}
                setattr(row, k, v)
    return changes


def ser_project(p: CompanyProject) -> Dict:
    return {"id": p.id, "company_id": p.company_id, "goal_id": p.goal_id, "name": p.name,
            "description": p.description or "", "status": p.status, "lead_agent_id": p.lead_agent_id,
            "target_date": p.target_date, "color": p.color, "created_at": _iso(p.created_at),
            "updated_at": _iso(p.updated_at)}


def _date(v):
    if not v:
        return None
    if not isinstance(v, str) or not _DATE_RE.match(v):
        raise CompanyError(422, "target_date must be YYYY-MM-DD")
    return v


def _color(v):
    if not v:
        return None
    if not isinstance(v, str) or not _COLOR_RE.match(v):
        raise CompanyError(422, "color must be like #3b82f6")
    return v.lower()


def list_projects(db, owner: str, company_id: str) -> List[Dict]:
    get_company(db, owner, company_id)
    rows = _scoped(db, CompanyProject, owner).filter(CompanyProject.company_id == company_id) \
        .order_by(CompanyProject.created_at).all()
    return [ser_project(p) for p in rows]


def _project_fields(db, owner, company_id):
    return {
        "name": lambda v: _text(v, "name", required=True, max_len=120),
        "description": lambda v: _text(v, "description", max_len=8000),
        "status": lambda v: _choice(v, "status", PROJECT_STATUSES),
        "goal_id": lambda v: _same_company(db, owner, CompanyGoal, v, company_id, "goal"),
        "lead_agent_id": lambda v: _same_company(db, owner, CompanyAgent, v, company_id, "lead agent"),
        "target_date": _date,
        "color": _color,
    }


def create_project(db, owner: str, actor: Actor, company_id: str, data: Dict) -> Dict:
    c = get_company(db, owner, company_id)
    p = CompanyProject(id=_id(), owner=_owner_val(owner), company_id=c.id, name="", status="backlog")
    _apply(p, {"name": data.get("name"), **{k: v for k, v in data.items() if k != "name"}},
           _project_fields(db, owner, c.id))
    db.add(p)
    db.flush()
    log_activity(db, owner, c.id, actor, "project.created", "project", p.id, {"name": p.name})
    db.commit()
    return ser_project(p)


def update_project(db, owner: str, actor: Actor, project_id: str, data: Dict) -> Dict:
    p = _get(db, CompanyProject, project_id, owner, "Project")
    changes = _apply(p, data, _project_fields(db, owner, p.company_id))
    if changes:
        log_activity(db, owner, p.company_id, actor, "project.updated", "project", p.id, {"changes": changes})
    db.commit()
    return ser_project(p)


def delete_project(db, owner: str, actor: Actor, project_id: str) -> None:
    p = _get(db, CompanyProject, project_id, owner, "Project")
    db.query(CompanyIssue).filter(CompanyIssue.project_id == p.id).update(
        {CompanyIssue.project_id: None}, synchronize_session=False)
    log_activity(db, owner, p.company_id, actor, "project.deleted", "project", p.id, {"name": p.name})
    db.delete(p)
    db.commit()


def ser_label(lb: CompanyLabel) -> Dict:
    return {"id": lb.id, "company_id": lb.company_id, "name": lb.name, "color": lb.color}


def list_labels(db, owner: str, company_id: str) -> List[Dict]:
    get_company(db, owner, company_id)
    rows = _scoped(db, CompanyLabel, owner).filter(CompanyLabel.company_id == company_id) \
        .order_by(CompanyLabel.name).all()
    return [ser_label(lb) for lb in rows]


def create_label(db, owner: str, actor: Actor, company_id: str, data: Dict) -> Dict:
    c = get_company(db, owner, company_id)
    name = _text(data.get("name"), "name", required=True, max_len=40)
    if _scoped(db, CompanyLabel, owner).filter(CompanyLabel.company_id == c.id,
                                               CompanyLabel.name == name).first():
        raise CompanyError(409, f"There is already a label named {name}")
    lb = CompanyLabel(id=_id(), owner=_owner_val(owner), company_id=c.id, name=name, color=_color(data.get("color")))
    db.add(lb)
    db.flush()
    log_activity(db, owner, c.id, actor, "label.created", "label", lb.id, {"name": name})
    db.commit()
    return ser_label(lb)


def delete_label(db, owner: str, actor: Actor, label_id: str) -> None:
    lb = _get(db, CompanyLabel, label_id, owner, "Label")
    db.query(CompanyIssueLabel).filter(CompanyIssueLabel.label_id == lb.id).delete(synchronize_session=False)
    log_activity(db, owner, lb.company_id, actor, "label.deleted", "label", lb.id, {"name": lb.name})
    db.delete(lb)
    db.commit()


# ── Issues ───────────────────────────────────────────────────────────────

def check_transition(old: str, new: str) -> None:
    _choice(new, "status", ISSUE_STATUSES)
    if old != new and new not in ISSUE_TRANSITIONS.get(old, ()):
        allowed = ", ".join(_say(s) for s in ISSUE_TRANSITIONS.get(old, ())) or "nothing"
        raise CompanyError(422, f"An issue can't go from {_say(old)} to {_say(new)} (from {_say(old)}: {allowed})")


def _stamp_status(i: CompanyIssue, new: str) -> None:
    now = utcnow_naive()
    if new == "in_progress" and not i.started_at:
        i.started_at = now
    if new == "done":
        i.completed_at = now
    elif i.status == "done":
        i.completed_at = None
    if new == "cancelled":
        i.cancelled_at = now
    elif i.status == "cancelled":
        i.cancelled_at = None
    if i.status == "in_progress" and new != "in_progress":
        i.checkout_run_id = None
        i.execution_locked_at = None
    i.status = new


def _issue_labels(db, issue_ids: List[str]) -> Dict[str, List[Dict]]:
    if not issue_ids:
        return {}
    rows = db.query(CompanyIssueLabel.issue_id, CompanyLabel).join(
        CompanyLabel, CompanyLabel.id == CompanyIssueLabel.label_id).filter(
        CompanyIssueLabel.issue_id.in_(issue_ids)).all()
    out: Dict[str, List[Dict]] = {}
    for iid, lb in rows:
        out.setdefault(iid, []).append(ser_label(lb))
    return out


def ser_issue(i: CompanyIssue, labels: Optional[List[Dict]] = None) -> Dict:
    return {"id": i.id, "company_id": i.company_id, "identifier": i.identifier, "number": i.number,
            "title": i.title, "description": i.description or "", "status": i.status, "priority": i.priority,
            "parent_id": i.parent_id, "project_id": i.project_id, "goal_id": i.goal_id,
            "assignee_agent_id": i.assignee_agent_id, "assignee_user": i.assignee_user,
            "created_by_type": i.created_by_type, "created_by_id": i.created_by_id,
            "checkout_run_id": i.checkout_run_id, "execution_locked_at": _iso(i.execution_locked_at),
            "started_at": _iso(i.started_at), "completed_at": _iso(i.completed_at),
            "cancelled_at": _iso(i.cancelled_at), "labels": labels or [],
            "created_at": _iso(i.created_at), "updated_at": _iso(i.updated_at)}


def get_issue_row(db, owner: str, issue_id: str) -> CompanyIssue:
    return _get(db, CompanyIssue, issue_id, owner, "Issue")


def _check_issue_parent(db, owner: str, issue_id: Optional[str], parent_id: Optional[str], company_id: str):
    _same_company(db, owner, CompanyIssue, parent_id, company_id, "parent issue")
    cur, seen = parent_id, set()
    while cur:
        if cur == issue_id:
            raise CompanyError(422, "An issue can't be its own sub-issue")
        if cur in seen:
            break
        seen.add(cur)
        cur = db.query(CompanyIssue.parent_id).filter(CompanyIssue.id == cur).scalar()


def _assignee_agent(db, owner: str, agent_id: Optional[str], company_id: str) -> Optional[CompanyAgent]:
    if not agent_id:
        return None
    a = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.id == agent_id).first()
    if a is None or a.company_id != company_id:
        raise CompanyError(422, "assignee_agent_id must be an agent in this company")
    if a.status in ("terminated", "pending_approval"):
        raise CompanyError(422, f"{a.name} can't take work right now ({a.status.replace('_', ' ')})")
    return a


def _set_labels(db, owner: str, issue: CompanyIssue, label_ids: Any) -> Optional[Dict]:
    if label_ids is None:
        label_ids = []
    if not isinstance(label_ids, list):
        raise CompanyError(422, "label_ids must be a list")
    wanted = list(dict.fromkeys(label_ids))
    if wanted:
        found = {lb.id for lb in _scoped(db, CompanyLabel, owner).filter(
            CompanyLabel.id.in_(wanted), CompanyLabel.company_id == issue.company_id)}
        if len(found) != len(wanted):
            raise CompanyError(422, "label_ids must be labels of this company")
    have = [r for (r,) in db.query(CompanyIssueLabel.label_id).filter(CompanyIssueLabel.issue_id == issue.id)]
    if set(have) == set(wanted):
        return None
    db.query(CompanyIssueLabel).filter(CompanyIssueLabel.issue_id == issue.id).delete(synchronize_session=False)
    for lid in wanted:
        db.add(CompanyIssueLabel(issue_id=issue.id, label_id=lid, owner=_owner_val(owner)))
    return {"from": sorted(have), "to": sorted(wanted)}


def _next_number(db, company: CompanyCompany) -> int:
    # Increment in the database, not in Python, so two creates in parallel
    # never get the same number.
    db.execute(update(CompanyCompany).where(CompanyCompany.id == company.id)
               .values(issue_counter=CompanyCompany.issue_counter + 1))
    n = db.execute(select(CompanyCompany.issue_counter).where(CompanyCompany.id == company.id)).scalar_one()
    company.issue_counter = n
    return n


def create_issue(db, owner: str, actor: Actor, company_id: str, data: Dict) -> Dict:
    c = get_company(db, owner, company_id)
    title = _text(data.get("title"), "title", required=True, max_len=300)
    status = _choice(data.get("status", "backlog"), "status", ISSUE_STATUSES)
    if status in ("done", "cancelled"):
        raise CompanyError(422, "A new issue can't start as done or cancelled")
    if data.get("assignee_agent_id") and data.get("assignee_user"):
        raise CompanyError(422, "Assign an issue to an agent or a person, not both")
    agent = _assignee_agent(db, owner, data.get("assignee_agent_id"), c.id)
    _check_issue_parent(db, owner, None, data.get("parent_id"), c.id)
    n = _next_number(db, c)
    i = CompanyIssue(
        id=_id(), owner=_owner_val(owner), company_id=c.id, number=n, identifier=f"{c.issue_prefix}-{n}",
        title=title, description=_text(data.get("description"), "description", max_len=50000),
        status=status, priority=_choice(data.get("priority", "medium"), "priority", PRIORITIES),
        parent_id=data.get("parent_id") or None,
        project_id=_same_company(db, owner, CompanyProject, data.get("project_id"), c.id, "project"),
        goal_id=_same_company(db, owner, CompanyGoal, data.get("goal_id"), c.id, "goal"),
        assignee_agent_id=agent.id if agent else None,
        assignee_user=_text(data.get("assignee_user"), "assignee_user", max_len=80),
        created_by_type=actor.type, created_by_id=actor.id,
        started_at=utcnow_naive() if status == "in_progress" else None,
    )
    db.add(i)
    db.flush()
    if data.get("label_ids"):
        _set_labels(db, owner, i, data["label_ids"])
    log_activity(db, owner, c.id, actor, "issue.created", "issue", i.id,
                 {"identifier": i.identifier, "title": title, "status": status})
    if agent and agent.id != actor.agent_id:   # an agent taking work on itself is already awake
        request_wakeup(db, owner, agent, "assignment", issue_id=i.id)
    db.commit()
    return get_issue(db, owner, i.id)


def update_issue(db, owner: str, actor: Actor, issue_id: str, data: Dict) -> Dict:
    i = get_issue_row(db, owner, issue_id)
    changes: Dict[str, Any] = {}

    def setf(field, value):
        old = getattr(i, field)
        if old != value:
            changes[field] = {"from": old, "to": value}
            setattr(i, field, value)

    if "title" in data:
        setf("title", _text(data["title"], "title", required=True, max_len=300))
    if "description" in data:
        new = _text(data["description"], "description", max_len=50000)
        if new != i.description:
            changes["description"] = {"updated": True}
            i.description = new
    if "priority" in data:
        setf("priority", _choice(data["priority"], "priority", PRIORITIES))
    if "project_id" in data:
        setf("project_id", _same_company(db, owner, CompanyProject, data["project_id"], i.company_id, "project"))
    if "goal_id" in data:
        setf("goal_id", _same_company(db, owner, CompanyGoal, data["goal_id"], i.company_id, "goal"))
    if "parent_id" in data:
        _check_issue_parent(db, owner, i.id, data["parent_id"] or None, i.company_id)
        setf("parent_id", data["parent_id"] or None)
    woke = None
    if "assignee_agent_id" in data or "assignee_user" in data:
        new_agent = data.get("assignee_agent_id", i.assignee_agent_id) or None
        new_user = data.get("assignee_user", i.assignee_user) or None
        if "assignee_agent_id" in data and data["assignee_agent_id"] and "assignee_user" not in data:
            new_user = None
        if "assignee_user" in data and data["assignee_user"] and "assignee_agent_id" not in data:
            new_agent = None
        if new_agent and new_user:
            raise CompanyError(422, "Assign an issue to an agent or a person, not both")
        agent = _assignee_agent(db, owner, new_agent, i.company_id) if new_agent != i.assignee_agent_id else None
        if new_agent != i.assignee_agent_id:
            # A new assignee: whoever held the checkout no longer does.
            i.checkout_run_id = None
            i.execution_locked_at = None
            woke = agent
        setf("assignee_agent_id", new_agent)
        setf("assignee_user", _text(new_user, "assignee_user", max_len=80))
    if "status" in data and data["status"] != i.status:
        old = i.status
        check_transition(old, data["status"])
        _stamp_status(i, data["status"])
        changes["status"] = {"from": old, "to": data["status"]}
    if "label_ids" in data:
        lc = _set_labels(db, owner, i, data["label_ids"])
        if lc:
            changes["labels"] = lc
    if changes:
        if set(changes) == {"status"}:
            action = "issue.status_changed"
        elif "assignee_agent_id" in changes or "assignee_user" in changes:
            action = "issue.assigned"
        else:
            action = "issue.updated"
        log_activity(db, owner, i.company_id, actor, action, "issue", i.id,
                     {"identifier": i.identifier, "changes": _jsonable(changes)})
        i.updated_at = utcnow_naive()
    if woke and woke.id != actor.agent_id:
        request_wakeup(db, owner, woke, "assignment", issue_id=i.id)
    db.commit()
    return get_issue(db, owner, i.id)


def delete_issue(db, owner: str, actor: Actor, issue_id: str) -> None:
    i = get_issue_row(db, owner, issue_id)
    db.query(CompanyIssue).filter(CompanyIssue.parent_id == i.id).update(
        {CompanyIssue.parent_id: i.parent_id}, synchronize_session=False)
    for model, col in ((CompanyIssueLabel, CompanyIssueLabel.issue_id), (CompanyIssueComment, CompanyIssueComment.issue_id)):
        db.query(model).filter(col == i.id).delete(synchronize_session=False)
    db.query(CompanyIssueRelation).filter(or_(CompanyIssueRelation.issue_id == i.id,
                                              CompanyIssueRelation.related_issue_id == i.id)).delete(synchronize_session=False)
    db.query(CompanyRun).filter(CompanyRun.issue_id == i.id).update({CompanyRun.issue_id: None}, synchronize_session=False)
    log_activity(db, owner, i.company_id, actor, "issue.deleted", "issue", i.id,
                 {"identifier": i.identifier, "title": i.title})
    db.delete(i)
    db.commit()


def list_issues(db, owner: str, company_id: str, *, status: Optional[str] = None,
                assignee_agent_id: Optional[str] = None, assignee_user: Optional[str] = None,
                project_id: Optional[str] = None, goal_id: Optional[str] = None,
                priority: Optional[str] = None, parent_id: Optional[str] = None,
                label_id: Optional[str] = None, q: Optional[str] = None, limit: int = 500) -> List[Dict]:
    get_company(db, owner, company_id)
    qry = _scoped(db, CompanyIssue, owner).filter(CompanyIssue.company_id == company_id)
    if status:
        qry = qry.filter(CompanyIssue.status.in_([s for s in status.split(",") if s]))
    if priority:
        qry = qry.filter(CompanyIssue.priority.in_([p for p in priority.split(",") if p]))
    if assignee_agent_id:
        qry = qry.filter(CompanyIssue.assignee_agent_id.is_(None) if assignee_agent_id == "none"
                         else CompanyIssue.assignee_agent_id == assignee_agent_id)
    if assignee_user:
        qry = qry.filter(CompanyIssue.assignee_user == assignee_user)
    if project_id:
        qry = qry.filter(CompanyIssue.project_id == project_id)
    if goal_id:
        qry = qry.filter(CompanyIssue.goal_id == goal_id)
    if parent_id:
        qry = qry.filter(CompanyIssue.parent_id == parent_id)
    if label_id:
        qry = qry.filter(CompanyIssue.id.in_(select(CompanyIssueLabel.issue_id).where(CompanyIssueLabel.label_id == label_id)))
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(CompanyIssue.title.ilike(like), CompanyIssue.identifier.ilike(like),
                             CompanyIssue.description.ilike(like)))
    rank = case({p: r for p, r in PRIORITY_RANK.items()}, value=CompanyIssue.priority, else_=len(PRIORITIES))
    rows = qry.order_by(rank, CompanyIssue.updated_at.desc()).limit(max(1, min(limit, 2000))).all()
    labels = _issue_labels(db, [r.id for r in rows])
    return [ser_issue(r, labels.get(r.id)) for r in rows]


def get_issue(db, owner: str, issue_id: str) -> Dict:
    i = get_issue_row(db, owner, issue_id)
    out = ser_issue(i, _issue_labels(db, [i.id]).get(i.id))
    subs = _scoped(db, CompanyIssue, owner).filter(CompanyIssue.parent_id == i.id).order_by(CompanyIssue.number).all()
    out["sub_issues"] = [{"id": s.id, "identifier": s.identifier, "title": s.title, "status": s.status} for s in subs]
    parent = _scoped(db, CompanyIssue, owner).filter(CompanyIssue.id == i.parent_id).first() if i.parent_id else None
    out["parent"] = {"id": parent.id, "identifier": parent.identifier, "title": parent.title} if parent else None
    rels = _scoped(db, CompanyIssueRelation, owner).filter(or_(CompanyIssueRelation.issue_id == i.id,
                                                               CompanyIssueRelation.related_issue_id == i.id)).all()
    other_ids = {r.related_issue_id if r.issue_id == i.id else r.issue_id for r in rels}
    others = {o.id: o for o in db.query(CompanyIssue).filter(CompanyIssue.id.in_(other_ids))} if other_ids else {}

    def brief(iid):
        o = others.get(iid)
        return {"id": iid, "identifier": o.identifier if o else "", "title": o.title if o else "", "status": o.status if o else ""}

    out["blocks"] = [{"relation_id": r.id, **brief(r.related_issue_id)} for r in rels if r.issue_id == i.id]
    out["blocked_by"] = [{"relation_id": r.id, **brief(r.issue_id)} for r in rels if r.related_issue_id == i.id]
    out["comment_count"] = _scoped(db, CompanyIssueComment, owner).filter(CompanyIssueComment.issue_id == i.id).count()
    return out


def _lock_is_free_clause(run_id: str, cutoff: datetime):
    """SQL for "this run may take the lock": nobody holds it, this run already
    does, the holder's lock is stale, or the holder's run has finished."""
    finished = select(CompanyRun.id).where(CompanyRun.status.in_(RUN_TERMINAL))
    return or_(CompanyIssue.checkout_run_id.is_(None),
               CompanyIssue.checkout_run_id == run_id,
               CompanyIssue.execution_locked_at.is_(None),
               CompanyIssue.execution_locked_at < cutoff,
               CompanyIssue.checkout_run_id.in_(finished))


def checkout_issue(db, owner: str, actor: Actor, issue_id: str, agent_id: str, run_id: str,
                   expected_statuses: Optional[List[str]] = None) -> Dict:
    """Claim an issue for one run of one agent and move it to in_progress.

    A single conditional UPDATE does the claim, so of two runs racing for the
    same issue exactly one changes the row; the other gets 409. The run that
    already holds the issue can check out again. A lock is taken over when it
    is older than STALE_LOCK_SECONDS or its run has finished."""
    i = get_issue_row(db, owner, issue_id)
    run_id = _text(run_id, "run_id", required=True, max_len=80)
    agent = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.id == agent_id).first() if agent_id else None
    if agent is None or agent.company_id != i.company_id:
        raise CompanyError(422, "agent_id must be an agent in this company")
    if agent.status in ("terminated", "paused", "pending_approval"):
        raise CompanyError(409, f"{agent.name} is {agent.status.replace('_', ' ')} and can't take work")
    # Only a live run of this agent may hold an issue: the lock is how the
    # executor knows which run is working on what, and finish_run frees it.
    run = _scoped(db, CompanyRun, owner).filter(CompanyRun.id == run_id).first()
    if run is None:
        raise CompanyError(422, "run_id must be a live run of this agent")
    if run.agent_id != agent.id or run.status not in RUN_LIVE:
        raise CompanyError(409, "That run belongs to another agent or has finished")
    statuses = list(expected_statuses or CHECKOUT_STATUSES)
    bad = [s for s in statuses if s not in CHECKOUT_STATUSES]
    if bad:
        raise CompanyError(422, f"An issue can't be checked out from {', '.join(bad)}")
    now = utcnow_naive()
    cutoff = now - timedelta(seconds=STALE_LOCK_SECONDS)
    prev = {"status": i.status, "run": i.checkout_run_id}
    stmt = (update(CompanyIssue)
            .where(CompanyIssue.id == i.id,
                   CompanyIssue.status.in_(statuses),
                   or_(CompanyIssue.assignee_agent_id.is_(None), CompanyIssue.assignee_agent_id == agent.id),
                   CompanyIssue.assignee_user.is_(None),
                   _lock_is_free_clause(run_id, cutoff))
            .values(checkout_run_id=run_id, execution_locked_at=now, assignee_agent_id=agent.id,
                    status="in_progress", updated_at=now,
                    started_at=case((CompanyIssue.started_at.is_(None), now), else_=CompanyIssue.started_at))
            .execution_options(synchronize_session=False))
    res = db.execute(stmt)
    if res.rowcount != 1:
        db.rollback()
        db.refresh(i)
        if i.status not in statuses:
            raise CompanyError(409, f"{i.identifier} is {_say(i.status)}, not one of {', '.join(map(_say, statuses))}")
        if i.assignee_user or (i.assignee_agent_id and i.assignee_agent_id != agent.id):
            raise CompanyError(409, f"{i.identifier} is assigned to someone else")
        raise CompanyError(409, f"{i.identifier} is checked out by another run")
    took_over = bool(prev["run"]) and prev["run"] != run_id
    if prev["run"] != run_id or prev["status"] != "in_progress":
        log_activity(db, owner, i.company_id, Actor(actor.type, actor.id, run_id),
                     "issue.checked_out", "issue", i.id,
                     {"identifier": i.identifier, "run_id": run_id, "from_status": prev["status"],
                      **({"took_over_from": prev["run"]} if took_over else {})}, agent_id=agent.id)
    db.commit()
    db.refresh(i)
    return get_issue(db, owner, i.id)


def release_issue(db, owner: str, actor: Actor, issue_id: str, run_id: Optional[str] = None,
                  force: bool = False) -> Dict:
    """Give up a checkout. The holding run releases its own; the person can
    force any release. An in_progress issue goes back to todo."""
    i = get_issue_row(db, owner, issue_id)
    if not i.checkout_run_id:
        return get_issue(db, owner, i.id)
    if not force and run_id != i.checkout_run_id:
        raise CompanyError(409, f"{i.identifier} is held by another run")
    held = i.checkout_run_id
    i.checkout_run_id = None
    i.execution_locked_at = None
    if i.status == "in_progress":
        i.status = "todo"
    i.updated_at = utcnow_naive()
    log_activity(db, owner, i.company_id, actor, "issue.released", "issue", i.id,
                 {"identifier": i.identifier, "run_id": held, "forced": bool(force and run_id != held)})
    db.commit()
    return get_issue(db, owner, i.id)


def add_relation(db, owner: str, actor: Actor, issue_id: str, related_issue_id: str, rtype: str = "blocks") -> Dict:
    i = get_issue_row(db, owner, issue_id)
    _choice(rtype, "type", RELATION_TYPES)
    other = _same_company(db, owner, CompanyIssue, related_issue_id, i.company_id, "related issue")
    if other == i.id:
        raise CompanyError(422, "An issue can't block itself")
    exists = _scoped(db, CompanyIssueRelation, owner).filter(
        or_((CompanyIssueRelation.issue_id == i.id) & (CompanyIssueRelation.related_issue_id == other),
            (CompanyIssueRelation.issue_id == other) & (CompanyIssueRelation.related_issue_id == i.id))).first()
    if exists:
        raise CompanyError(409, "Those issues are already linked")
    r = CompanyIssueRelation(id=_id(), owner=_owner_val(owner), company_id=i.company_id, issue_id=i.id,
                             related_issue_id=other, type=rtype)
    db.add(r)
    log_activity(db, owner, i.company_id, actor, "issue.relation_added", "issue", i.id,
                 {"identifier": i.identifier, "type": rtype, "related_issue_id": other})
    db.commit()
    return get_issue(db, owner, i.id)


def remove_relation(db, owner: str, actor: Actor, relation_id: str) -> None:
    r = _get(db, CompanyIssueRelation, relation_id, owner, "Relation")
    log_activity(db, owner, r.company_id, actor, "issue.relation_removed", "issue", r.issue_id,
                 {"type": r.type, "related_issue_id": r.related_issue_id})
    db.delete(r)
    db.commit()


# ── Comments and @mentions ───────────────────────────────────────────────

def _slug(name: str) -> str:
    return re.sub(r"\s+", "-", name.strip().lower())


def parse_mentions(body: str, agents: List[Any]) -> List[str]:
    """Agent ids @mentioned in `body`, in order of first mention.

    An agent is mentioned as @Name (spaces allowed, any case) or @name-with-
    hyphens. The longest name wins, so "@Ada Lovelace" finds Ada Lovelace
    and not an agent called Ada. An @ inside a word (an email address) and a
    name running on into more letters ("@Adams" for Ada) don't count."""
    if not body or "@" not in body:
        return []
    keys = []
    for a in agents:
        for k in {a.name.strip().lower(), _slug(a.name)}:
            if k:
                keys.append((k, a.id))
    keys.sort(key=lambda kv: -len(kv[0]))
    low = body.lower()
    found: List[str] = []
    for m in re.finditer(r"@", body):
        at = m.start()
        if at > 0 and (body[at - 1].isalnum() or body[at - 1] in "_."):
            continue
        rest = low[at + 1:]
        for k, aid in keys:
            if rest.startswith(k):
                after = rest[len(k):len(k) + 1]
                if after and (after.isalnum() or after in "_-"):
                    continue
                if aid not in found:
                    found.append(aid)
                break
    return found


def ser_comment(cm: CompanyIssueComment) -> Dict:
    return {"id": cm.id, "issue_id": cm.issue_id, "body": cm.body, "author_type": cm.author_type,
            "author_id": cm.author_id, "run_id": cm.run_id, "mentions": cm.mentions or [],
            "created_at": _iso(cm.created_at)}


def list_comments(db, owner: str, issue_id: str) -> List[Dict]:
    i = get_issue_row(db, owner, issue_id)
    rows = _scoped(db, CompanyIssueComment, owner).filter(CompanyIssueComment.issue_id == i.id) \
        .order_by(CompanyIssueComment.created_at).all()
    return [ser_comment(c) for c in rows]


def add_comment(db, owner: str, actor: Actor, issue_id: str, body: Any) -> Dict:
    i = get_issue_row(db, owner, issue_id)
    body = _text(body, "body", required=True, max_len=50000)
    agents = [a for a in _company_agents(db, owner, i.company_id) if a.status != "terminated"]
    mentions = parse_mentions(body, agents)
    cm = CompanyIssueComment(id=_id(), owner=_owner_val(owner), company_id=i.company_id, issue_id=i.id, body=body,
                             author_type=actor.type, author_id=actor.id, run_id=actor.run_id,
                             mentions=mentions or None)
    db.add(cm)
    db.flush()
    by_id = {a.id: a for a in agents}
    for aid in mentions:
        if aid != actor.agent_id:
            request_wakeup(db, owner, by_id[aid], "mention", issue_id=i.id, comment_id=cm.id)
    log_activity(db, owner, i.company_id, actor, "comment.created", "issue", i.id,
                 {"identifier": i.identifier, "comment_id": cm.id, "mentions": mentions})
    i.updated_at = utcnow_naive()
    db.commit()
    return ser_comment(cm)


def delete_comment(db, owner: str, actor: Actor, comment_id: str) -> None:
    cm = _get(db, CompanyIssueComment, comment_id, owner, "Comment")
    log_activity(db, owner, cm.company_id, actor, "comment.deleted", "issue", cm.issue_id, {"comment_id": cm.id})
    db.delete(cm)
    db.commit()


# ── Runs (written by PR C's executor; read here) ─────────────────────────

def ser_run(r: CompanyRun) -> Dict:
    return {"id": r.id, "company_id": r.company_id, "agent_id": r.agent_id, "issue_id": r.issue_id,
            "invocation_source": r.invocation_source, "status": r.status, "model": r.model,
            "session_id": r.session_id, "error": r.error, "result_summary": r.result_summary,
            "usage": r.usage or {}, "created_at": _iso(r.created_at), "started_at": _iso(r.started_at),
            "finished_at": _iso(r.finished_at)}


def list_runs(db, owner: str, company_id: str, *, agent_id: Optional[str] = None,
              issue_id: Optional[str] = None, status: Optional[str] = None, limit: int = 50) -> List[Dict]:
    get_company(db, owner, company_id)
    q = _scoped(db, CompanyRun, owner).filter(CompanyRun.company_id == company_id)
    if agent_id:
        q = q.filter(CompanyRun.agent_id == agent_id)
    if issue_id:
        q = q.filter(CompanyRun.issue_id == issue_id)
    if status:
        q = q.filter(CompanyRun.status.in_(status.split(",")))
    return [ser_run(r) for r in q.order_by(CompanyRun.created_at.desc()).limit(max(1, min(limit, 500))).all()]


def get_run(db, owner: str, run_id: str) -> Dict:
    return ser_run(_get(db, CompanyRun, run_id, owner, "Run"))


def create_run(db, owner: str, agent_id: str, source: str, *, issue_id: Optional[str] = None,
               model: Optional[str] = None) -> CompanyRun:
    """For the executor (PR C): record a queued run of `agent_id`."""
    a = get_agent(db, owner, agent_id)
    r = CompanyRun(id=_id(), owner=_owner_val(owner), company_id=a.company_id, agent_id=a.id,
                   issue_id=issue_id, invocation_source=_choice(source, "invocation_source", RUN_SOURCES),
                   status="queued", model=model or a.model)
    db.add(r)
    db.commit()
    return r


def finish_run(db, owner: str, run_id: str, status: str, *, error: Optional[str] = None,
               result_summary: Optional[str] = None, usage: Optional[Dict] = None) -> CompanyRun:
    """For the executor (PR C): close a run and release any issue it held."""
    r = _get(db, CompanyRun, run_id, owner, "Run")
    if status not in RUN_TERMINAL:
        raise CompanyError(422, f"status must be one of: {', '.join(RUN_TERMINAL)}")
    r.status = status
    r.finished_at = utcnow_naive()
    r.error, r.result_summary = error, result_summary
    if usage is not None:
        r.usage = usage
    db.query(CompanyIssue).filter(CompanyIssue.checkout_run_id == r.id).update(
        {CompanyIssue.checkout_run_id: None, CompanyIssue.execution_locked_at: None}, synchronize_session=False)
    db.commit()
    return r


# ── Approvals ────────────────────────────────────────────────────────────

def ser_approval(ap: CompanyApproval) -> Dict:
    return {"id": ap.id, "company_id": ap.company_id, "type": ap.type, "title": ap.title or "",
            "requested_by_type": ap.requested_by_type, "requested_by_id": ap.requested_by_id,
            "status": ap.status, "payload": ap.payload or {}, "decision_note": ap.decision_note or "",
            "decided_by": ap.decided_by, "decided_at": _iso(ap.decided_at),
            "created_at": _iso(ap.created_at), "updated_at": _iso(ap.updated_at)}


def list_approvals(db, owner: str, company_id: str, status: Optional[str] = None) -> List[Dict]:
    get_company(db, owner, company_id)
    q = _scoped(db, CompanyApproval, owner).filter(CompanyApproval.company_id == company_id)
    if status:
        q = q.filter(CompanyApproval.status.in_(status.split(",")))
    return [ser_approval(a) for a in q.order_by(CompanyApproval.created_at.desc()).limit(500).all()]


def create_approval(db, owner: str, actor: Actor, company_id: str, data: Dict) -> Dict:
    c = get_company(db, owner, company_id)
    atype = _choice(data.get("type"), "type", APPROVAL_TYPES)
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise CompanyError(422, "payload must be an object")
    if atype == "hire_agent" and payload.get("model"):
        payload = {**payload, "model": validate_model_spec(payload["model"], owner)}
    ap = CompanyApproval(id=_id(), owner=_owner_val(owner), company_id=c.id, type=atype,
                         title=_text(data.get("title"), "title", max_len=200),
                         requested_by_type=actor.type, requested_by_id=actor.id, status="pending",
                         payload=payload)
    db.add(ap)
    db.flush()
    log_activity(db, owner, c.id, actor, "approval.created", "approval", ap.id, {"type": atype, "title": ap.title})
    db.commit()
    return ser_approval(ap)


def get_approval(db, owner: str, approval_id: str) -> Dict:
    return ser_approval(_get(db, CompanyApproval, approval_id, owner, "Approval"))


MAX_RUN_STEPS = 200


def _plan_options(options: Dict, owner: str) -> Dict:
    """The run choices made when approving a plan or strategy: the model to
    carry it out on (any model the owner can use), and run limits."""
    out: Dict[str, Any] = {}
    model = options.get("chosen_model")
    if model is not None and str(model).strip():
        out["chosen_model"] = validate_model_spec(model, owner)
    steps = options.get("max_steps")
    if steps not in (None, ""):
        if isinstance(steps, bool) or not isinstance(steps, (int, float)) or not 1 <= int(steps) <= MAX_RUN_STEPS:
            raise CompanyError(422, f"max_steps must be between 1 and {MAX_RUN_STEPS}")
        out["max_steps"] = int(steps)
    cost = options.get("max_cost_usd")
    if cost not in (None, ""):
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 < float(cost) <= 10000:
            raise CompanyError(422, "max_cost_usd must be a positive amount")
        out["max_cost_usd"] = round(float(cost), 4)
    return out


def pause_agent(db, owner: str, agent: CompanyAgent, reason: str, *, actor: Optional[Actor] = None,
                action: str = "agent.paused") -> None:
    """Pause an agent with a reason (the executor's budget hard stop). Pending
    wake-ups are dropped: a paused agent wakes for nothing until resumed."""
    agent.status = "paused"
    agent.pause_reason = reason[:500]
    agent.auto_paused = False
    db.query(CompanyWakeup).filter(CompanyWakeup.agent_id == agent.id, CompanyWakeup.status == "pending") \
        .update({CompanyWakeup.status: "dropped"}, synchronize_session=False)
    log_activity(db, owner, agent.company_id, actor or Actor("system"), action, "agent", agent.id,
                 {"name": agent.name, "reason": agent.pause_reason}, agent_id=agent.id)


def decide_approval(db, owner: str, actor: Actor, approval_id: str, decision: str,
                    note: Optional[str] = None, options: Optional[Dict] = None) -> Dict:
    """Record the person's decision and apply what it settles here:

    - hire_agent: the pending agent becomes idle (approved) or terminated.
    - plan / strategy approved: `options` may carry chosen_model, max_steps
      and max_cost_usd, stored in the payload; the executor runs the
      requester on that model with the approved plan pinned.
    - budget_override approved: `options.budget_cents` (0 = no limit) becomes
      the new monthly limit of the scope that ran out, and the paused agent
      is resumed and woken.
    Any decision on an agent's request wakes that agent (source approval)."""
    ap = _get(db, CompanyApproval, approval_id, owner, "Approval")
    _choice(decision, "decision", APPROVAL_DECISIONS)
    if ap.status not in ("pending", "revision_requested"):
        raise CompanyError(409, f"This approval is already {_say(ap.status)}")
    options = options or {}
    if not isinstance(options, dict):
        raise CompanyError(422, "options must be an object")
    payload = dict(ap.payload or {})
    if decision == "approved" and ap.type in ("plan", "strategy"):
        payload.update(_plan_options(options, owner))
    ap.status = decision
    ap.decision_note = _text(note, "note", max_len=4000)
    ap.decided_by = actor.id
    ap.decided_at = utcnow_naive()
    agent_id = payload.get("agent_id")
    if ap.type == "hire_agent" and agent_id and decision != "revision_requested":
        a = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.id == agent_id).first()
        if a is not None and a.status == "pending_approval":
            a.status = "idle" if decision == "approved" else "terminated"
    resumed = None
    if ap.type == "budget_override" and decision == "approved":
        if "budget_cents" in options and options["budget_cents"] is not None:
            limit = _cents(options["budget_cents"], "budget_cents")
            payload["new_limit_cents"] = limit
            if payload.get("scope") == "company":
                c = _scoped(db, CompanyCompany, owner).filter(CompanyCompany.id == ap.company_id).first()
                if c is not None:
                    c.monthly_budget_cents = limit
            elif agent_id:
                a = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.id == agent_id).first()
                if a is not None:
                    a.monthly_budget_cents = limit
        a = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.id == agent_id).first() if agent_id else None
        if a is not None and a.status == "paused":
            a.status = "idle"
            a.pause_reason = None
            a.auto_paused = False
            resumed = a
    ap.payload = payload
    requester = None
    if ap.requested_by_type == "agent" and ap.requested_by_id:
        requester = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.id == ap.requested_by_id).first()
    if requester is None and resumed is not None:
        requester = resumed
    if requester is None and ap.type in ("plan", "strategy") and agent_id:
        # A plan the person wrote for an agent: that agent carries it out.
        requester = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.id == agent_id).first()
    if requester is not None:
        request_wakeup(db, owner, requester, "approval", approval_id=ap.id, issue_id=payload.get("issue_id"))
    log_activity(db, owner, ap.company_id, actor, f"approval.{decision}", "approval", ap.id,
                 {"type": ap.type, "title": ap.title, "note": ap.decision_note,
                  **({"chosen_model": payload["chosen_model"]} if decision == "approved" and payload.get("chosen_model") else {})})
    db.commit()
    if ap.type == "google_write":
        # The held Google change (src/action_approvals.py): approved, it runs
        # once when the agent wakes (company_executor._prepare); anything
        # else, it never runs.
        try:
            from src import action_approvals
            action_approvals.company_decided(ap.id, decision, ap.decision_note or "", by=actor.id or "")
        except Exception:
            pass
    if ap.type == "plan" and decision in ("approved", "rejected"):
        # Its copy in Plans (src/native_plans.py) shows the same answer.
        try:
            from src import native_plans
            native_plans.company_decided(ap.id, decision, ap.decision_note or "")
        except Exception:
            pass
    return ser_approval(ap)


def approval_defaults(db, owner: str, approval_id: str) -> Dict:
    """What the Approve dialog starts with: the model last chosen for this
    agent's approved plans (so the choice is remembered per agent), else the
    agent's own model, and the limits used last time."""
    ap = _get(db, CompanyApproval, approval_id, owner, "Approval")
    agent_id = (ap.payload or {}).get("agent_id") or (ap.requested_by_id if ap.requested_by_type == "agent" else None)
    agent = _scoped(db, CompanyAgent, owner).filter(CompanyAgent.id == agent_id).first() if agent_id else None
    out: Dict[str, Any] = {"model": agent.model if agent else None, "max_steps": None, "max_cost_usd": None,
                           "agent_model": agent.model if agent else None, "remembered": False}
    if agent is not None:
        prev = _scoped(db, CompanyApproval, owner).filter(
            CompanyApproval.company_id == ap.company_id, CompanyApproval.type.in_(("plan", "strategy")),
            CompanyApproval.status == "approved", CompanyApproval.id != ap.id,
            CompanyApproval.requested_by_id == agent.id).order_by(CompanyApproval.decided_at.desc()).first()
        p = (prev.payload or {}) if prev is not None else {}
        if p.get("chosen_model"):
            out.update(model=p["chosen_model"], remembered=True)
        out["max_steps"] = p.get("max_steps")
        out["max_cost_usd"] = p.get("max_cost_usd")
    if ap.type == "budget_override":
        out["budget_cents"] = (ap.payload or {}).get("limit_cents")
    return out


def cancel_approval(db, owner: str, actor: Actor, approval_id: str) -> Dict:
    ap = _get(db, CompanyApproval, approval_id, owner, "Approval")
    if ap.status not in ("pending", "revision_requested"):
        raise CompanyError(409, f"This approval is already {_say(ap.status)}")
    ap.status = "cancelled"
    log_activity(db, owner, ap.company_id, actor, "approval.cancelled", "approval", ap.id, {"type": ap.type})
    db.commit()
    return ser_approval(ap)


# ── Dashboard ────────────────────────────────────────────────────────────

def dashboard(db, owner: str, company_id: str) -> Dict:
    c = get_company(db, owner, company_id)
    agents = _company_agents(db, owner, c.id)
    agent_counts = {s: 0 for s in AGENT_STATUSES}
    for a in agents:
        agent_counts[a.status] = agent_counts.get(a.status, 0) + 1
    issue_counts = {s: 0 for s in ISSUE_STATUSES}
    from sqlalchemy import func
    for st, n in _scoped(db, CompanyIssue, owner).filter(CompanyIssue.company_id == c.id) \
            .with_entities(CompanyIssue.status, func.count()).group_by(CompanyIssue.status):
        issue_counts[st] = n
    pending = _scoped(db, CompanyApproval, owner).filter(CompanyApproval.company_id == c.id,
                                                         CompanyApproval.status == "pending").count()
    return {"company": ser_company(c), "agents": agent_counts, "agent_total": len(agents),
            "issues": issue_counts, "issue_total": sum(issue_counts.values()),
            "pending_approvals": pending, "recent_activity": list_activity(db, owner, c.id, limit=15)}


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return _iso(v)
    return v
