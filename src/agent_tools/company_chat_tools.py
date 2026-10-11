"""Company tools for regular chats: the chat model acting on the user's
Company as the user (docs/company.md, "Company in chat").

Every call goes through src/company.py with the user as the actor, so the
rules, validation and activity log are the same as clicking in the Company
page. Only the chat owner's companies are reachable.

These are the person's tools. The agents' own tools (checkout, release,
working an issue inside a run) belong to the executor and are not here; the
names below are deliberately "company_<thing>_<verb>" so they don't collide.

Registration (small, delimited hooks): TOOL_TAGS in src/agent_tools/__init__.py,
schemas in src/tool_schemas.py, dispatch in src/tool_execution.py, retrieval
text in src/tool_index.py, plan-mode lists in src/tool_security.py, and the
availability rules in src/agent_loop.py (hidden for an owner with no company,
pulled in when a message mentions the Company).

This module must stay cheap to import: src/agent_tools/__init__.py reads the
name sets at import time, before the database or the tool schemas load.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

READ_TOOLS = frozenset({
    "company_overview",
    "company_issue_list",
    "company_issue_get",
    "company_agent_status",
})
WRITE_TOOLS = frozenset({
    "company_issue_create",
    "company_issue_update",
    "company_issue_comment",
    "company_agent_wake",
    "company_approval_decide",
})
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS

_ISSUE_REF = {"type": "string", "description": "The issue: its identifier like 'JAR-1' (or its id)."}
_COMPANY_REF = {"type": "string", "description": "Which company: name, issue prefix or id. "
                                                 "Leave out when the user has one company."}
_AGENT_REF = {"type": "string", "description": "The agent: its name, its role ('CEO'), 'PREFIX/role' "
                                               "when several companies have one, or its id."}

SCHEMAS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "company_overview",
        "description": ("The user's Company (their organization of AI agents): with no company, lists "
                        "their companies; with one, its goals, agents and their status, open issues by "
                        "status and approvals waiting. Read-only."),
        "parameters": {"type": "object", "properties": {"company": _COMPANY_REF}, "required": []}}},
    {"type": "function", "function": {
        "name": "company_issue_list",
        "description": ("List issues of a company, most important first. Filter by status, assignee "
                        "or words. Read-only."),
        "parameters": {"type": "object", "properties": {
            "company": _COMPANY_REF,
            "status": {"type": "string", "description": "Comma-separated: backlog, todo, in_progress, in_review, "
                                                        "blocked, done, cancelled, or 'open' (default) or 'all'."},
            "assignee": {"type": "string", "description": "An agent (name or role), 'me' or 'none'."},
            "query": {"type": "string", "description": "Words to find in titles and descriptions."},
            "limit": {"type": "integer", "description": "How many (default 20, at most 50)."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "company_issue_get",
        "description": "One issue in full: status, priority, assignee, description, sub-issues and comments. Read-only.",
        "parameters": {"type": "object", "properties": {"issue": _ISSUE_REF}, "required": ["issue"]}}},
    {"type": "function", "function": {
        "name": "company_agent_status",
        "description": ("What an agent is doing: role, model, status, its open issues, and its recent "
                        "runs and activity. Use for 'what is the CEO working on'. Read-only."),
        "parameters": {"type": "object", "properties": {"agent": _AGENT_REF, "company": _COMPANY_REF},
                       "required": ["agent"]}}},
    {"type": "function", "function": {
        "name": "company_issue_create",
        "description": ("Create an issue in the user's company, as the user. Assigning it to an agent "
                        "queues a wake-up for that agent."),
        "parameters": {"type": "object", "properties": {
            "company": _COMPANY_REF,
            "title": {"type": "string", "description": "Short title."},
            "description": {"type": "string", "description": "What needs doing, with any context from the chat."},
            "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "status": {"type": "string", "enum": ["backlog", "todo", "in_progress", "blocked"],
                       "description": "Default backlog (todo when assigned)."},
            "assignee": {"type": "string", "description": "An agent (name or role) or 'me'."},
            "parent": {"type": "string", "description": "Parent issue identifier, for a sub-issue."},
        }, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "company_issue_update",
        "description": ("Change an issue as the user: status (only allowed moves), priority, title, "
                        "description or assignee. Use to assign or reassign: assignee 'CEO', 'me' or 'none'."),
        "parameters": {"type": "object", "properties": {
            "issue": _ISSUE_REF,
            "status": {"type": "string", "enum": ["backlog", "todo", "in_progress", "in_review", "done",
                                                  "blocked", "cancelled"]},
            "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "assignee": {"type": "string", "description": "An agent (name or role), 'me', or 'none' to unassign."},
        }, "required": ["issue"]}}},
    {"type": "function", "function": {
        "name": "company_issue_comment",
        "description": ("Comment on an issue as the user. @Name in the text mentions an agent, which "
                        "wakes it to read the comment."),
        "parameters": {"type": "object", "properties": {
            "issue": _ISSUE_REF,
            "body": {"type": "string", "description": "The comment."},
        }, "required": ["issue", "body"]}}},
    {"type": "function", "function": {
        "name": "company_agent_wake",
        "description": "Ask an agent to run now (a wake-up the agent picks up), optionally about one issue.",
        "parameters": {"type": "object", "properties": {
            "agent": _AGENT_REF, "company": _COMPANY_REF,
            "issue": {"type": "string", "description": "Optional issue identifier the wake-up is about."},
        }, "required": ["agent"]}}},
    {"type": "function", "function": {
        "name": "company_approval_decide",
        "description": ("Decide an approval waiting for the user (a hire, a strategy, a plan): approve, "
                        "reject or ask for changes. Only when the user has said which."),
        "parameters": {"type": "object", "properties": {
            "approval": {"type": "string", "description": "The approval id, or its title when it is unique."},
            "company": _COMPANY_REF,
            "decision": {"type": "string", "enum": ["approved", "rejected", "revision_requested"]},
            "note": {"type": "string", "description": "Why, shown to whoever asked."},
        }, "required": ["approval", "decision"]}}},
]

# Retrieval text (src/tool_index.py): richer than the schema descriptions.
TOOL_DESCRIPTIONS: Dict[str, str] = {
    "company_overview": "The user's Company, their organization of AI agents (CEO, CTO, engineers): companies, goals, agents and what state they are in, open issues, approvals waiting. Use for 'how is my company doing', 'show my org', 'what's going on at the company'.",
    "company_issue_list": "List the Company's issues (tickets like JAR-1): open work, filter by status, by agent (what is assigned to the CEO), or by words.",
    "company_issue_get": "Read one Company issue such as JAR-1 in full: status, priority, assignee, description, sub-issues and its comments.",
    "company_agent_status": "What a Company agent (the CEO, an engineer) is working on: its open issues, last runs and recent activity. 'What is the CEO doing', 'status of Ada'.",
    "company_issue_create": "Create an issue / ticket / task in the user's Company and optionally assign it to an agent: 'create an issue for this', 'file a ticket', 'make a task for the CTO'.",
    "company_issue_update": "Change a Company issue: assign or reassign it to an agent ('assign JAR-2 to the CEO'), move its status (todo, in progress, done), change priority, title or description.",
    "company_issue_comment": "Comment on a Company issue as the user: 'comment on JAR-1 that ...', 'tell the CEO on JAR-3 to ...', reply on an issue thread.",
    "company_agent_wake": "Wake a Company agent so it runs now: 'wake the CEO', 'ping the engineer to look at JAR-4', 'get the CTO going'.",
    "company_approval_decide": "Approve, reject or request changes on a Company approval (a hire, a strategy, a plan) when the user decides.",
}

# Fenced-block prompt lines for models without native tool calling
# (src/agent_loop.py TOOL_SECTIONS).
TOOL_SECTIONS: Dict[str, str] = {
    "company_overview": "- ```company_overview```: The user's Company (org of AI agents): {\"company\": \"Acme\"} or {} to list companies. Read-only.",
    "company_issue_list": "- ```company_issue_list```: List Company issues: {\"status\": \"open\", \"assignee\": \"CEO\", \"query\": \"login\"}. Read-only.",
    "company_issue_get": "- ```company_issue_get```: One issue with comments: {\"issue\": \"JAR-1\"}. Read-only.",
    "company_agent_status": "- ```company_agent_status```: What an agent is working on: {\"agent\": \"CEO\"}. Read-only.",
    "company_issue_create": "- ```company_issue_create```: Create an issue as the user: {\"title\": \"...\", \"description\": \"...\", \"priority\": \"high\", \"assignee\": \"CEO\"}.",
    "company_issue_update": "- ```company_issue_update```: Change an issue: {\"issue\": \"JAR-2\", \"assignee\": \"CEO\"} or {\"issue\": \"JAR-2\", \"status\": \"done\"}.",
    "company_issue_comment": "- ```company_issue_comment```: Comment as the user: {\"issue\": \"JAR-1\", \"body\": \"...\"}.",
    "company_agent_wake": "- ```company_agent_wake```: Wake an agent now: {\"agent\": \"CEO\", \"issue\": \"JAR-1\"}.",
    "company_approval_decide": "- ```company_approval_decide```: Decide an approval: {\"approval\": \"<id>\", \"decision\": \"approved\", \"note\": \"...\"}.",
}


# ── Availability (src/agent_loop.py) ─────────────────────────────────────

_TOPIC_RE = re.compile(
    r"\b(company|companies|org chart|my org|my agents|issues?|tickets?|approvals?|wake|ceo|cto|cmo|cfo)\b"
    r"|<company_context>", re.I)


def hidden_tools(owner: Optional[str]) -> set:
    """All Company chat tools when the owner has no company, else nothing."""
    from src import company_chat
    return set() if company_chat.owner_has_company(owner) else set(ALL_TOOLS)


def relevant_tools(owner: Optional[str], text: str) -> set:
    """The Company tools to offer for this message: all of them when it is
    about the Company (a mention, an issue identifier, the words), else none."""
    if not text:
        return set()
    from src import company_chat
    topical = bool(_TOPIC_RE.search(text))
    if not (topical or company_chat.owner_text_may_mention(text)):
        return set()
    if not company_chat.owner_has_company(owner):
        return set()
    if topical or company_chat.resolve(owner, text):
        return set(ALL_TOOLS)
    return set()


# ── Lookups ──────────────────────────────────────────────────────────────

class _Miss(Exception):
    pass


def _scoped(db, model, owner):
    from src.auth_helpers import owner_filter
    return owner_filter(db.query(model), model, owner, include_shared=False)


def _slug(s: str) -> str:
    return re.sub(r"\s+", "-", (s or "").strip().lower())


def _find_company(db, owner: str, ref: Any):
    from core.database import CompanyCompany
    rows = _scoped(db, CompanyCompany, owner).order_by(CompanyCompany.created_at).all()
    if not rows:
        raise _Miss("You have no company yet. Create one on the Company page (Build > Company).")
    ref = str(ref or "").strip()
    if not ref:
        if len(rows) == 1:
            return rows[0]
        raise _Miss("Which company? You have: " + ", ".join(f"{c.name} ({c.issue_prefix})" for c in rows))
    low = ref.lower().lstrip("@")
    for c in rows:
        if c.id == ref or c.name.strip().lower() == low or _slug(c.name) == low or (c.issue_prefix or "").lower() == low:
            return c
    hits = [c for c in rows if low in c.name.lower()]
    if len(hits) == 1:
        return hits[0]
    raise _Miss(f"No company matches '{ref}'. You have: " + ", ".join(f"{c.name} ({c.issue_prefix})" for c in rows))


def _find_issue(db, owner: str, ref: Any):
    from core.database import CompanyCompany, CompanyIssue
    ref = str(ref or "").strip().lstrip("@")
    if not ref:
        raise _Miss("Say which issue, like JAR-1.")
    m = re.fullmatch(r"([A-Za-z][A-Za-z0-9]{1,9})-(\d{1,7})", ref)
    if m:
        c = _scoped(db, CompanyCompany, owner).filter(CompanyCompany.issue_prefix == m.group(1).upper()).first()
        i = c and _scoped(db, CompanyIssue, owner).filter(CompanyIssue.company_id == c.id,
                                                          CompanyIssue.number == int(m.group(2))).first()
    else:
        i = _scoped(db, CompanyIssue, owner).filter(CompanyIssue.id == ref).first()
    if not i:
        raise _Miss(f"No issue {ref} in your companies.")
    return i


def _find_agent(db, owner: str, ref: Any, company=None, *, live_only: bool = True):
    from core.database import CompanyAgent, CompanyCompany
    ref = str(ref or "").strip().lstrip("@")
    if not ref:
        raise _Miss("Say which agent, by name or role.")
    q = _scoped(db, CompanyAgent, owner)
    if "/" in ref:
        pre, ref = ref.split("/", 1)
        company = _scoped(db, CompanyCompany, owner).filter(CompanyCompany.issue_prefix == pre.upper()).first() or company
    if company is not None:
        q = q.filter(CompanyAgent.company_id == company.id)
    rows = q.order_by(CompanyAgent.created_at).all()
    if live_only:
        rows = [a for a in rows if a.status != "terminated"]
    exact = [a for a in rows if a.id == ref]
    if exact:
        return exact[0]
    low = ref.lower()
    low = re.sub(r"^the\s+", "", low)
    for test in (lambda a: a.name.strip().lower() == low or _slug(a.name) == low,
                 lambda a: a.role == low,
                 lambda a: (a.title or "").strip().lower() == low,
                 lambda a: low in a.name.lower()):
        hits = [a for a in rows if test(a)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = {c.id: c for c in _scoped(db, CompanyCompany, owner).all()}
            raise _Miss(f"'{ref}' could be: " + "; ".join(
                f"{a.name} ({a.role}) in {names[a.company_id].name} - say {names[a.company_id].issue_prefix}/{a.name}"
                for a in hits if a.company_id in names))
    raise _Miss(f"No agent '{ref}'" + (f" in {company.name}" if company is not None else " in your companies") + ".")


def _assignee(db, owner: str, value: Any, company) -> Dict[str, Any]:
    v = str(value or "").strip()
    if v.lower() in ("none", "nobody", "unassigned", "no one", "noone", ""):
        return {"assignee_agent_id": None, "assignee_user": None}
    if v.lower() in ("me", "myself", "user", "the user", "i"):
        return {"assignee_user": "me", "assignee_agent_id": None}
    a = _find_agent(db, owner, v, company)
    return {"assignee_agent_id": a.id, "assignee_user": None}


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {"ref": raw} if raw else {}


def _words(s: Any) -> str:
    return str(s or "").replace("_", " ").replace("cancelled", "canceled")


def _issue_line(i: Dict[str, Any], agents: Dict[str, Any]) -> str:
    who = agents.get(i.get("assignee_agent_id"))
    assignee = f"{who.name}" if who else ("me" if i.get("assignee_user") else "unassigned")
    return f"{i['identifier']} [{_words(i['status'])}, {i['priority']}, {assignee}] {i['title']}"


def _agents_by_id(db, owner, company_id) -> Dict[str, Any]:
    from core.database import CompanyAgent
    return {a.id: a for a in _scoped(db, CompanyAgent, owner).filter(CompanyAgent.company_id == company_id)}


# ── The tools ────────────────────────────────────────────────────────────

def _overview(db, owner, args):
    from src import company as co
    from src import company_chat
    if not args.get("company") and not args.get("ref"):
        rows = co.list_companies(db, owner)
        if len(rows) != 1:
            if not rows:
                raise _Miss("You have no company yet. Create one on the Company page (Build > Company).")
            return "Your companies:\n" + "\n".join(f"- {c['name']} (prefix {c['issue_prefix']}, {c['status']})" for c in rows)
    c = _find_company(db, owner, args.get("company") or args.get("ref"))
    out = [company_chat.company_snapshot(db, owner, c.id)]
    agents = _agents_by_id(db, owner, c.id)
    live = [a for a in agents.values() if a.status != "terminated"]
    if live:
        out.append("Agents:\n" + "\n".join(
            f"- {a.name} ({company_chat.ROLE_LABEL.get(a.role, a.role)}), {_words(a.status)}, model {a.model or 'none'}"
            for a in live))
    issues = co.list_issues(db, owner, c.id, status=",".join(company_chat.OPEN_ISSUE_STATUSES), limit=15)
    if issues:
        out.append("Open issues (most important first):\n" + "\n".join("- " + _issue_line(i, agents) for i in issues))
    pending = co.list_approvals(db, owner, c.id, status="pending")
    if pending:
        out.append("Approvals waiting:\n" + "\n".join(f"- {p['id']} {p['type'].replace('_', ' ')}: {p['title'] or '(untitled)'}"
                                                      for p in pending[:10]))
    return "\n\n".join(out)


def _issue_list(db, owner, args):
    from src import company as co
    from src import company_chat
    c = _find_company(db, owner, args.get("company"))
    status = str(args.get("status") or "open").strip().lower()
    if status == "open":
        status = ",".join(company_chat.OPEN_ISSUE_STATUSES)
    elif status == "all":
        status = None
    kw: Dict[str, Any] = {"status": status, "q": args.get("query") or None}
    if args.get("assignee"):
        a = _assignee(db, owner, args["assignee"], c)
        if a.get("assignee_agent_id"):
            kw["assignee_agent_id"] = a["assignee_agent_id"]
        elif a.get("assignee_user"):
            kw["assignee_user"] = "me"
        else:
            kw["assignee_agent_id"] = "none"
    limit = max(1, min(int(args.get("limit") or 20), 50))
    rows = co.list_issues(db, owner, c.id, limit=limit, **kw)
    agents = _agents_by_id(db, owner, c.id)
    if not rows:
        return f"No matching issues in {c.name}."
    return f"Issues in {c.name} ({len(rows)}):\n" + "\n".join("- " + _issue_line(i, agents) for i in rows)


def _issue_get(db, owner, args):
    from src import company as co
    from src import company_chat
    i = _find_issue(db, owner, args.get("issue") or args.get("ref"))
    full = co.get_issue(db, owner, i.id)
    out = [company_chat.issue_snapshot(db, owner, i.id).split("\n  latest comments")[0].split("\n  no comments")[0]]
    if full.get("parent"):
        out.append(f"Sub-issue of {full['parent']['identifier']} {full['parent']['title']}")
    if full.get("sub_issues"):
        out.append("Sub-issues: " + "; ".join(f"{s['identifier']} {s['title']} ({_words(s['status'])})" for s in full["sub_issues"]))
    if full.get("blocked_by"):
        out.append("Blocked by: " + ", ".join(b["identifier"] for b in full["blocked_by"]))
    if i.description:
        out.append("Description:\n" + i.description[:3000])
    comments = co.list_comments(db, owner, i.id)[-10:]
    if comments:
        lines = []
        for cm in comments:
            who = company_chat._author(db, owner, cm["author_type"], cm["author_id"])
            lines.append(f"- {who} ({cm['created_at'][:16].replace('T', ' ')}): {cm['body'][:1200]}")
        out.append("Comments (latest last):\n" + "\n".join(lines))
    return "\n\n".join(out)


def _agent_status(db, owner, args):
    from src import company as co
    from src import company_chat
    company = _find_company(db, owner, args["company"]) if args.get("company") else None
    a = _find_agent(db, owner, args.get("agent") or args.get("ref"), company, live_only=False)
    out = [company_chat.agent_snapshot(db, owner, a.id)]
    runs = co.list_runs(db, owner, a.company_id, agent_id=a.id, limit=5)
    if runs:
        out.append("Recent runs:\n" + "\n".join(
            f"- {r['status']} {(r['finished_at'] or r['created_at'] or '')[:16].replace('T', ' ')} "
            f"({r['invocation_source']}): {(r['result_summary'] or r['error'] or '')[:300]}" for r in runs))
    acts = co.list_activity(db, owner, a.company_id, agent_id=a.id, limit=8)
    if acts:
        out.append("Recent activity:\n" + "\n".join(
            f"- {x['created_at'][:16].replace('T', ' ')} {x['action']} {(x['details'] or {}).get('identifier', '')}".rstrip()
            for x in acts))
    from core.database import CompanyWakeup
    pending = _scoped(db, CompanyWakeup, owner).filter(CompanyWakeup.agent_id == a.id,
                                                       CompanyWakeup.status == "pending").count()
    if pending:
        out.append(f"Wake-ups waiting: {pending}")
    return "\n\n".join(out)


def _issue_create(db, owner, actor, args):
    from src import company as co
    c = _find_company(db, owner, args.get("company"))
    data: Dict[str, Any] = {"title": args.get("title"), "description": args.get("description") or None,
                            "priority": args.get("priority") or "medium"}
    if args.get("assignee"):
        data.update(_assignee(db, owner, args["assignee"], c))
    status = args.get("status") or ("todo" if data.get("assignee_agent_id") else "backlog")
    data["status"] = status
    if args.get("parent"):
        data["parent_id"] = _find_issue(db, owner, args["parent"]).id
    i = co.create_issue(db, owner, actor, c.id, data)
    who = _agents_by_id(db, owner, c.id).get(i.get("assignee_agent_id"))
    return (f"Created {i['identifier']}: {i['title']} in {c.name} ({_words(i['status'])}, {i['priority']}"
            + (f", assigned to {who.name}, who will be woken" if who else "") + ").")


def _issue_update(db, owner, actor, args):
    from src import company as co
    i = _find_issue(db, owner, args.get("issue") or args.get("ref"))
    data: Dict[str, Any] = {}
    for k in ("status", "priority", "title", "description"):
        if args.get(k) not in (None, ""):
            data[k] = args[k]
    if "assignee" in args:
        c = _find_company(db, owner, i.company_id)
        data.update(_assignee(db, owner, args.get("assignee"), c))
    if not data:
        raise _Miss("Nothing to change: pass status, priority, title, description or assignee.")
    before = {"status": i.status, "assignee": i.assignee_agent_id or i.assignee_user}
    out = co.update_issue(db, owner, actor, i.id, data)
    agents = _agents_by_id(db, owner, out["company_id"])
    bits = []
    if out["status"] != before["status"]:
        bits.append(f"status {_words(before['status'])} -> {_words(out['status'])}")
    if "assignee_agent_id" in data or "assignee_user" in data:
        who = agents.get(out.get("assignee_agent_id"))
        bits.append("assigned to " + (f"{who.name} (woken)" if who else "you" if out.get("assignee_user") else "nobody"))
    for k in ("priority", "title"):
        if k in data:
            bits.append(f"{k} {out[k]}")
    if "description" in data:
        bits.append("description updated")
    return f"Updated {out['identifier']}: " + ", ".join(bits or ["no change"]) + "."


def _issue_comment(db, owner, actor, args):
    from src import company as co
    i = _find_issue(db, owner, args.get("issue") or args.get("ref"))
    cm = co.add_comment(db, owner, actor, i.id, args.get("body"))
    agents = _agents_by_id(db, owner, i.company_id)
    named = [agents[m].name for m in cm.get("mentions") or [] if m in agents]
    return f"Commented on {i.identifier}." + (f" Mentioned (and woke) {', '.join(named)}." if named else "")


def _agent_wake(db, owner, actor, args):
    from src import company as co
    company = _find_company(db, owner, args["company"]) if args.get("company") else None
    a = _find_agent(db, owner, args.get("agent") or args.get("ref"), company)
    issue_id = None
    if args.get("issue"):
        iss = _find_issue(db, owner, args["issue"])
        if iss.company_id != a.company_id:
            raise _Miss(f"{iss.identifier} is not in {a.name}'s company.")
        issue_id = iss.id
    w = co.request_wakeup(db, owner, a, "on_demand", issue_id=issue_id)
    if w is None:
        db.rollback()
        raise _Miss(f"{a.name} can't be woken ({_words(a.status)} or wake on demand is off).")
    co.log_activity(db, owner, a.company_id, actor, "agent.wake_requested", "agent", a.id,
                    {"source": "chat", **({"issue_id": issue_id} if issue_id else {})}, agent_id=a.id)
    db.commit()
    return f"Woke {a.name}: a wake-up is queued and the agent will run when the executor picks it up."


def _approval_decide(db, owner, actor, args):
    from src import company as co
    from core.database import CompanyApproval
    ref = str(args.get("approval") or args.get("ref") or "").strip()
    q = _scoped(db, CompanyApproval, owner).filter(CompanyApproval.status.in_(("pending", "revision_requested")))
    if args.get("company"):
        q = q.filter(CompanyApproval.company_id == _find_company(db, owner, args["company"]).id)
    rows = q.all()
    hit = [r for r in rows if r.id == ref] or [r for r in rows if (r.title or "").strip().lower() == ref.lower()] \
        or [r for r in rows if ref and ref.lower() in (r.title or "").lower()]
    if len(hit) != 1:
        if not rows:
            raise _Miss("No approvals are waiting.")
        raise _Miss(("Which approval? " if len(hit) > 1 or not ref else f"No waiting approval matches '{ref}'. ")
                    + "Waiting: " + "; ".join(f"{r.id} {r.type.replace('_', ' ')}: {r.title or '(untitled)'}" for r in rows[:10]))
    ap = co.decide_approval(db, owner, actor, hit[0].id, str(args.get("decision") or ""), args.get("note"))
    return f"Approval '{ap['title'] or ap['type']}' is now {_words(ap['status'])}."


_READERS = {"company_overview": _overview, "company_issue_list": _issue_list,
            "company_issue_get": _issue_get, "company_agent_status": _agent_status}
_WRITERS = {"company_issue_create": _issue_create, "company_issue_update": _issue_update,
            "company_issue_comment": _issue_comment, "company_agent_wake": _agent_wake,
            "company_approval_decide": _approval_decide}


def run_tool(name: str, content: str, owner: Optional[str]) -> Dict[str, Any]:
    """Run one Company chat tool as the chat owner. Returns the usual
    {"output", "exit_code"} or {"error", "exit_code": 1}."""
    if name not in ALL_TOOLS:
        return {"error": f"Unknown Company tool {name}", "exit_code": 1}
    import core.database as cdb
    from src import company as co
    owner = owner or ""
    args = _args(content)
    db = cdb.SessionLocal()
    try:
        if name in _READERS:
            text = _READERS[name](db, owner, args)
        else:
            text = _WRITERS[name](db, owner, co.user_actor(owner), args)
        return {"output": text, "exit_code": 0}
    except _Miss as e:
        db.rollback()
        return {"error": str(e), "exit_code": 1}
    except co.CompanyError as e:
        db.rollback()
        return {"error": e.detail, "exit_code": 1}
    except Exception as e:
        db.rollback()
        logger.warning("%s failed: %s", name, e, exc_info=True)
        return {"error": f"{name}: {type(e).__name__}: {e}"[:400], "exit_code": 1}
    finally:
        db.close()
