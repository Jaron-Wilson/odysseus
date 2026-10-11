"""Company tools: what an agent uses during its run to work its issues.

A Company agent (src/company_executor.py) runs the ordinary agent loop on
its own model. These tools are added to its turn and call the same service
functions as the HTTP API (src/company.py), as Actor("agent", agent_id,
run_id), so every change is validated the same way and logged as that agent
and run.

They only work inside a Company run: the executor sets the run's context
(owner, company, agent, run) with :func:`agent_context`, which the drain task
of the run and the threads its tools run in inherit. Outside one, the agent
loop hides the tools and a stray call is refused. Every lookup is limited to
the agent's own company; an issue of another company is "not found".

Kept free of heavy imports at module level: src/agent_tools imports
TOOL_NAMES from here to build its tool-tag list.
"""
from __future__ import annotations

import contextvars
import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

LIST_ISSUES = "company_list_issues"
GET_ISSUE = "company_get_issue"
CHECKOUT = "company_checkout_issue"
UPDATE_ISSUE = "company_update_issue"
COMMENT = "company_comment"
CREATE_ISSUE = "company_create_issue"
REQUEST_APPROVAL = "company_request_approval"
RELEASE = "company_release_issue"

TOOL_NAMES = frozenset({LIST_ISSUES, GET_ISSUE, CHECKOUT, UPDATE_ISSUE, COMMENT, CREATE_ISSUE,
                        REQUEST_APPROVAL, RELEASE})
READ_ONLY = frozenset({LIST_ISSUES, GET_ISSUE})
MUTATING = TOOL_NAMES - READ_ONLY

_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "odysseus_company_agent", default=None)


@contextmanager
def agent_context(*, owner: str, company_id: str, agent_id: str, run_id: str) -> Iterator[Dict[str, Any]]:
    """Mark the work started inside this block as a run of one agent."""
    val = {"owner": owner or "", "company_id": company_id, "agent_id": agent_id, "run_id": run_id,
           "comments": []}
    token = _ctx.set(val)
    try:
        yield val
    finally:
        try:
            _ctx.reset(token)
        except ValueError:
            _ctx.set(None)


def current() -> Optional[Dict[str, Any]]:
    return _ctx.get()


def active() -> bool:
    return _ctx.get() is not None


# ── Schemas (native function calling) and prompt lines (fenced blocks) ──

_ISSUE_ARG = {"type": "string", "description": "The issue's identifier, like ENG-12 (or its id)."}

SCHEMAS: List[Dict] = [
    {"type": "function", "function": {
        "name": LIST_ISSUES,
        "description": ("List Company issues. scope 'mine' (default): open issues assigned to you; 'team': "
                        "open issues of the agents who report to you; 'unassigned': open issues nobody has; "
                        "'all': every open issue in the company. Pass status to filter."),
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["mine", "team", "unassigned", "all"]},
            "status": {"type": "string", "description": "Only this status: backlog, todo, in_progress, in_review, blocked, done, cancelled."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": GET_ISSUE,
        "description": "Read one issue: description, status, assignee, parent and sub-issues, and its recent comments.",
        "parameters": {"type": "object", "properties": {"issue": _ISSUE_ARG}, "required": ["issue"]}}},
    {"type": "function", "function": {
        "name": CHECKOUT,
        "description": ("Check out an issue before working on it: claims it for this run and moves it to "
                        "in_progress. Fails if another run holds it or it is assigned to someone else."),
        "parameters": {"type": "object", "properties": {"issue": _ISSUE_ARG}, "required": ["issue"]}}},
    {"type": "function", "function": {
        "name": UPDATE_ISSUE,
        "description": ("Move an issue you work on (or one of your reports' issues) to a new status, "
                        "and/or assign it, optionally with a comment. Managers triage with this: set assignee "
                        "on an unassigned issue to give it to a report (or yourself); the assignee is woken. "
                        "Allowed moves: backlog->todo/in_progress/blocked; "
                        "todo->in_progress/blocked; in_progress->in_review/done/blocked/todo; "
                        "in_review->done/in_progress/todo; blocked->todo/in_progress. Use in_review when "
                        "the work is ready for someone to check, done when it is finished."),
        "parameters": {"type": "object", "properties": {
            "issue": _ISSUE_ARG,
            "status": {"type": "string", "enum": ["backlog", "todo", "in_progress", "in_review", "done", "blocked", "cancelled"]},
            "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "assignee": {"type": "string", "description": ("Name of the agent to assign: yourself or (managers) "
                                                           "an agent who reports to you. \"nobody\" unassigns.")},
            "comment": {"type": "string", "description": "Optional comment saying what changed and why."},
        }, "required": ["issue"]}}},
    {"type": "function", "function": {
        "name": COMMENT,
        "description": ("Comment on an issue: progress, findings, questions. @Name mentions another agent "
                        "and wakes it to read the comment."),
        "parameters": {"type": "object", "properties": {
            "issue": _ISSUE_ARG,
            "body": {"type": "string", "description": "The comment, in Markdown."},
        }, "required": ["issue", "body"]}}},
    {"type": "function", "function": {
        "name": CREATE_ISSUE,
        "description": ("Managers only: open a new issue (a sub-issue when parent is given) and assign it to "
                        "yourself or to an agent who reports to you. The assignee is woken to work on it."),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "description": {"type": "string", "description": "What done looks like, in full."},
            "assignee": {"type": "string", "description": "Name of the agent to assign (yourself or a report)."},
            "parent": {"type": "string", "description": "Identifier of the parent issue, for a sub-issue."},
            "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        }, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": REQUEST_APPROVAL,
        "description": ("Ask the person who runs the company to approve something before you go ahead: "
                        "type 'plan' (a step-by-step plan for an issue; when approved you are woken to "
                        "carry it out, possibly on a different model), 'strategy', or 'hire_agent' (managers "
                        "only: a new agent reporting to you, with name, role and model). After asking, end "
                        "your turn: you are woken when they decide."),
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["plan", "strategy", "hire_agent"]},
            "title": {"type": "string"},
            "plan": {"type": "string", "description": "The plan or strategy, as a numbered list of steps."},
            "issue": _ISSUE_ARG,
            "hire": {"type": "object", "description": "For hire_agent: {name, role, title, model, instructions, capabilities}.",
                     "properties": {"name": {"type": "string"}, "role": {"type": "string"},
                                    "title": {"type": "string"}, "model": {"type": "string"},
                                    "instructions": {"type": "string"}, "capabilities": {"type": "string"}}},
        }, "required": ["type", "title"]}}},
    {"type": "function", "function": {
        "name": RELEASE,
        "description": "Give up your checkout of an issue without finishing it (it goes back to todo if in progress).",
        "parameters": {"type": "object", "properties": {"issue": _ISSUE_ARG}, "required": ["issue"]}}},
]

PROMPT_SECTIONS: Dict[str, str] = {
    LIST_ISSUES: "- ```company_list_issues```: Your Company issues. Args (JSON): {\"scope\": \"mine\"} (or \"team\", \"unassigned\", \"all\"), optional \"status\".",
    GET_ISSUE: "- ```company_get_issue```: Read one issue and its comments. Args (JSON): {\"issue\": \"ENG-12\"}.",
    CHECKOUT: "- ```company_checkout_issue```: Claim an issue for this run before working on it (moves it to in_progress). Args (JSON): {\"issue\": \"ENG-12\"}.",
    UPDATE_ISSUE: "- ```company_update_issue```: Move an issue along and/or assign it (managers: give unassigned issues to a report, who is woken), with an optional comment. Args (JSON): {\"issue\": \"ENG-12\", \"status\": \"in_review\", \"comment\": \"Ready: ...\"} or {\"issue\": \"ENG-12\", \"assignee\": \"Name\", \"comment\": \"Why them\"}.",
    COMMENT: "- ```company_comment```: Comment on an issue; @Name wakes that agent. Args (JSON): {\"issue\": \"ENG-12\", \"body\": \"...\"}.",
    CREATE_ISSUE: "- ```company_create_issue```: Managers: open a (sub-)issue for yourself or a report. Args (JSON): {\"title\": \"...\", \"description\": \"...\", \"assignee\": \"Name\", \"parent\": \"ENG-12\"}.",
    REQUEST_APPROVAL: "- ```company_request_approval```: Ask the person to approve a plan, strategy or hire, then end your turn. Args (JSON): {\"type\": \"plan\", \"title\": \"...\", \"plan\": \"1. ...\", \"issue\": \"ENG-12\"}.",
    RELEASE: "- ```company_release_issue```: Give up your checkout of an issue. Args (JSON): {\"issue\": \"ENG-12\"}.",
}


# ── Running a tool ──────────────────────────────────────────────────────

class _Refused(Exception):
    pass


def _args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = (content or "").strip()
    if not text:
        return {}
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        # A fenced block with just an identifier: "ENG-12".
        return {"issue": text.splitlines()[0].strip()}
    return v if isinstance(v, dict) else {}


def _say(s: str) -> str:
    return str(s).replace("_", " ").replace("cancelled", "canceled")


class _Run:
    """One tool call's view of the run: the db session and who is acting."""

    def __init__(self, db, ctx: Dict[str, Any]):
        from src import company as co
        self.co = co
        self.db = db
        self.ctx = ctx
        self.owner = ctx["owner"]
        self.company_id = ctx["company_id"]
        self.actor = co.Actor("agent", ctx["agent_id"], ctx["run_id"])
        self.agent = co._scoped(db, co.CompanyAgent, self.owner).filter(
            co.CompanyAgent.id == ctx["agent_id"]).first()
        if self.agent is None or self.agent.company_id != self.company_id:
            raise _Refused("This run's agent no longer exists")
        self._agents = None

    @property
    def agents(self):
        if self._agents is None:
            self._agents = [a for a in self.co._company_agents(self.db, self.owner, self.company_id)]
        return self._agents

    def name(self, agent_id: Optional[str]) -> str:
        if not agent_id:
            return ""
        return next((a.name for a in self.agents if a.id == agent_id), "an agent")

    def reports(self, agent_id: Optional[str] = None) -> List[str]:
        """Everyone below `agent_id` (this agent by default), any depth."""
        root = agent_id or self.agent.id
        live = [a for a in self.agents if a.status != "terminated"]
        out, frontier = [], [root]
        while frontier:
            cur = frontier.pop()
            for a in live:
                if a.reports_to == cur and a.id not in out:
                    out.append(a.id)
                    frontier.append(a.id)
        return out

    def is_manager(self) -> bool:
        return self.agent.role == "ceo" or bool(self.reports())

    def issue(self, ref: Any):
        co = self.co
        ref = str(ref or "").strip()
        if not ref:
            raise _Refused("Say which issue (its identifier, like ENG-12)")
        q = co._scoped(self.db, co.CompanyIssue, self.owner).filter(co.CompanyIssue.company_id == self.company_id)
        row = q.filter(co.CompanyIssue.identifier == ref.upper()).first() or q.filter(co.CompanyIssue.id == ref).first()
        if row is None:
            raise _Refused(f"No issue {ref} in your company")
        return row

    def agent_named(self, ref: Any):
        ref = str(ref or "").strip().lstrip("@")
        if not ref:
            return None
        low = ref.lower()
        for a in self.agents:
            if a.status != "terminated" and (a.id == ref or a.name.lower() == low
                                             or a.name.lower().replace(" ", "-") == low):
                return a
        raise _Refused(f"No agent called {ref} in your company")

    def may_change(self, issue) -> bool:
        me = self.agent.id
        return (issue.assignee_agent_id in (None, me) or issue.assignee_agent_id in self.reports()
                or (issue.created_by_type == "agent" and issue.created_by_id == me))

    def brief(self, i) -> str:
        who = self.name(i.assignee_agent_id) or (i.assignee_user and "a person") or "unassigned"
        lock = ""
        if i.checkout_run_id:
            lock = " (checked out by this run)" if i.checkout_run_id == self.ctx["run_id"] else " (checked out by another run)"
        return f"{i.identifier} [{_say(i.status)}, {i.priority}] {i.title} - {who}{lock}"


def _list_issues(r: _Run, a: Dict) -> str:
    co = r.co
    scope = (a.get("scope") or "mine").lower()
    q = co._scoped(r.db, co.CompanyIssue, r.owner).filter(co.CompanyIssue.company_id == r.company_id)
    if a.get("status"):
        q = q.filter(co.CompanyIssue.status == co._choice(a["status"], "status", co.ISSUE_STATUSES))
    else:
        q = q.filter(co.CompanyIssue.status.notin_(("done", "cancelled")))
    if scope == "mine":
        q = q.filter(co.CompanyIssue.assignee_agent_id == r.agent.id)
    elif scope == "team":
        ids = r.reports()
        if not ids:
            return "Nobody reports to you."
        q = q.filter(co.CompanyIssue.assignee_agent_id.in_(ids))
    elif scope == "unassigned":
        q = q.filter(co.CompanyIssue.assignee_agent_id.is_(None), co.CompanyIssue.assignee_user.is_(None))
    elif scope != "all":
        raise _Refused("scope must be mine, team, unassigned or all")
    rows = q.all()
    rows.sort(key=lambda i: (co.PRIORITY_RANK.get(i.priority, 9), i.number))
    if not rows:
        return "No issues." if scope != "mine" else "No open issues are assigned to you."
    return "\n".join(r.brief(i) for i in rows[:50])


def _get_issue(r: _Run, a: Dict) -> str:
    co = r.co
    i = r.issue(a.get("issue"))
    lines = [r.brief(i)]
    if i.description:
        lines += ["", i.description.strip()[:6000]]
    if i.parent_id:
        p = co._scoped(r.db, co.CompanyIssue, r.owner).filter(co.CompanyIssue.id == i.parent_id).first()
        if p is not None:
            lines += ["", f"Parent: {r.brief(p)}"]
    subs = co._scoped(r.db, co.CompanyIssue, r.owner).filter(co.CompanyIssue.parent_id == i.id).all()
    if subs:
        lines += ["", "Sub-issues:"] + [f"- {r.brief(s)}" for s in subs]
    comments = co._scoped(r.db, co.CompanyIssueComment, r.owner).filter(
        co.CompanyIssueComment.issue_id == i.id).order_by(co.CompanyIssueComment.created_at.desc()).limit(20).all()
    if comments:
        lines += ["", "Recent comments (oldest first):"]
        for c in reversed(comments):
            who = r.name(c.author_id) if c.author_type == "agent" else ("System" if c.author_type == "system" else "The person")
            lines.append(f"- {who}: {c.body.strip()[:1500]}")
    return "\n".join(lines)


def _checkout(r: _Run, a: Dict) -> str:
    i = r.issue(a.get("issue"))
    out = r.co.checkout_issue(r.db, r.owner, r.actor, i.id, r.agent.id, r.ctx["run_id"])
    return f"Checked out {out['identifier']}; it is in progress and yours for this run."


def _update_issue(r: _Run, a: Dict) -> str:
    co = r.co
    i = r.issue(a.get("issue"))
    if not r.may_change(i):
        raise _Refused(f"{i.identifier} is assigned to {r.name(i.assignee_agent_id) or 'someone else'}; comment instead")
    done = []
    if a.get("assignee"):
        ref = str(a["assignee"]).strip()
        if ref.lower() in ("nobody", "none", "unassigned"):
            if i.assignee_agent_id or i.assignee_user:
                co.update_issue(r.db, r.owner, r.actor, i.id, {"assignee_agent_id": None, "assignee_user": None})
                done.append("unassigned")
        else:
            who = r.agent if ref.lower() in ("me", "myself", "self") else r.agent_named(ref)
            if who.id != r.agent.id:
                if not r.is_manager():
                    raise _Refused("Only managers assign issues to others; you can assign it to yourself")
                if who.id not in r.reports():
                    raise _Refused(f"{who.name} does not report to you")
            if who.id != i.assignee_agent_id:
                co.update_issue(r.db, r.owner, r.actor, i.id, {"assignee_agent_id": who.id})
                done.append("assigned to you" if who.id == r.agent.id else f"assigned to {who.name}")
    status = a.get("status")
    if status and status != i.status:
        if status == "in_progress":
            co.checkout_issue(r.db, r.owner, r.actor, i.id, r.agent.id, r.ctx["run_id"])
        else:
            co.update_issue(r.db, r.owner, r.actor, i.id, {"status": status})
        done.append(f"moved to {_say(status)}")
    if a.get("priority") and a["priority"] != i.priority:
        co.update_issue(r.db, r.owner, r.actor, i.id, {"priority": a["priority"]})
        done.append(f"priority {a['priority']}")
    if a.get("comment"):
        _comment_on(r, i, a["comment"])
        done.append("commented")
    if not done:
        return f"{i.identifier} unchanged."
    return f"{i.identifier}: {', '.join(done)}."


def _comment_on(r: _Run, i, body: Any) -> Dict:
    cm = r.co.add_comment(r.db, r.owner, r.actor, i.id, body)
    r.ctx.setdefault("comments", []).append(i.id)
    return cm


def _comment(r: _Run, a: Dict) -> str:
    i = r.issue(a.get("issue"))
    cm = _comment_on(r, i, a.get("body"))
    woke = [r.name(m) for m in cm.get("mentions") or [] if m != r.agent.id]
    return f"Commented on {i.identifier}." + (f" Woke {', '.join(woke)}." if woke else "")


def _create_issue(r: _Run, a: Dict) -> str:
    if not r.is_manager():
        raise _Refused("Only managers open issues for others; ask your manager in a comment")
    assignee = r.agent_named(a.get("assignee")) if a.get("assignee") else None
    if assignee is not None and assignee.id != r.agent.id and assignee.id not in r.reports():
        raise _Refused(f"{assignee.name} does not report to you")
    parent = r.issue(a["parent"]) if a.get("parent") else None
    data = {"title": a.get("title"), "description": a.get("description"),
            "priority": a.get("priority") or (parent.priority if parent else "medium"),
            "status": "todo", "parent_id": parent.id if parent else None,
            "project_id": parent.project_id if parent else None, "goal_id": parent.goal_id if parent else None,
            "assignee_agent_id": assignee.id if assignee else None}
    out = r.co.create_issue(r.db, r.owner, r.actor, r.company_id, data)
    who = f", assigned to {assignee.name}" if assignee else ""
    return f"Opened {out['identifier']}: {out['title']}{who}."


def _request_approval(r: _Run, a: Dict) -> str:
    co = r.co
    atype = (a.get("type") or "").strip()
    title = a.get("title") or ""
    if atype == "hire_agent":
        if not r.is_manager():
            raise _Refused("Only managers can hire")
        h = a.get("hire") if isinstance(a.get("hire"), dict) else {}
        data = {"name": h.get("name") or a.get("name"), "role": h.get("role") or "general",
                "title": h.get("title"), "model": h.get("model") or r.agent.model,
                "instructions": h.get("instructions"), "capabilities": h.get("capabilities"),
                "reports_to": r.agent.id}
        ag = co.create_agent(r.db, r.owner, r.actor, r.company_id, data)
        if ag["status"] == "pending_approval":
            return (f"Asked to hire {ag['name']}; the hire waits for approval. End your turn now: "
                    "you are woken when it is decided.")
        return f"Hired {ag['name']} ({ag['role']}), reporting to you."
    if atype not in ("plan", "strategy"):
        raise _Refused("type must be plan, strategy or hire_agent")
    plan = (a.get("plan") or a.get("text") or "").strip()
    if not plan:
        raise _Refused("Write the plan (numbered steps) in 'plan'")
    issue = r.issue(a["issue"]) if a.get("issue") else None
    pending = co._scoped(r.db, co.CompanyApproval, r.owner).filter(
        co.CompanyApproval.company_id == r.company_id, co.CompanyApproval.type == atype,
        co.CompanyApproval.status == "pending", co.CompanyApproval.requested_by_id == r.agent.id).all()
    for p in pending:
        if (p.payload or {}).get("issue_id") == (issue.id if issue else None):
            return f"A {atype} for this is already waiting for approval. End your turn; you are woken when it is decided."
    ap = co.create_approval(r.db, r.owner, r.actor, r.company_id, {
        "type": atype, "title": title or (f"Plan for {issue.identifier}" if issue else f"{atype.title()} from {r.agent.name}"),
        "payload": {"plan": plan[:20000], "issue_id": issue.id if issue else None,
                    "issue": issue.identifier if issue else None, "agent_id": r.agent.id,
                    "run_id": r.ctx["run_id"], "model": r.agent.model}})
    return (f"Asked for approval of the {atype} \"{ap['title']}\". End your turn now and do not start the work: "
            "you are woken when it is decided.")


def _release(r: _Run, a: Dict) -> str:
    i = r.issue(a.get("issue"))
    out = r.co.release_issue(r.db, r.owner, r.actor, i.id, run_id=r.ctx["run_id"])
    return f"Released {out['identifier']} (now {_say(out['status'])})."


_HANDLERS = {LIST_ISSUES: _list_issues, GET_ISSUE: _get_issue, CHECKOUT: _checkout,
             UPDATE_ISSUE: _update_issue, COMMENT: _comment, CREATE_ISSUE: _create_issue,
             REQUEST_APPROVAL: _request_approval, RELEASE: _release}


def run_tool(tool: str, content: Any, *, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one Company tool for the current run. Returns the usual tool result
    dict ({output, exit_code} or {error, exit_code}). Synchronous: the caller
    runs it in a thread."""
    ctx = ctx or current()
    if not ctx:
        return {"error": f"{tool} only works inside a Company agent's run", "exit_code": 1}
    handler = _HANDLERS.get(tool)
    if handler is None:
        return {"error": f"Unknown Company tool {tool}", "exit_code": 1}
    from core import database as cdb
    from src import company as co
    db = cdb.SessionLocal()
    try:
        r = _Run(db, ctx)
        return {"output": handler(r, _args(content)), "exit_code": 0}
    except _Refused as e:
        db.rollback()
        return {"error": str(e), "exit_code": 1}
    except co.CompanyError as e:
        db.rollback()
        return {"error": e.detail, "exit_code": 1}
    except Exception as e:
        db.rollback()
        logger.warning("[company] %s failed: %s", tool, e, exc_info=True)
        return {"error": f"{tool}: {type(e).__name__}: {e}"[:400], "exit_code": 1}
    finally:
        db.close()
