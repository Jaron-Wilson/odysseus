"""Company executor: wakes Company agents and runs them on their models.

docs/company.md has the design. In short, a loop (started with the app,
like the task scheduler) does three things every few seconds:

1. Timer heartbeats. Each agent whose heartbeat policy is enabled gets a
   ``timer`` wake-up once ``interval_sec`` has passed since its last
   heartbeat (or since it was hired), unless one is already waiting.
2. Claiming. Pending ``company_wakeups`` are grouped per agent; all of an
   agent's pending wake-ups become ONE run (the most important reason leads:
   approval, assignment, mention, on demand, timer). At most
   ``company_max_concurrent_runs`` runs (default 2) go at once, and one per
   agent.
3. Notifications for new approvals waiting on the person.

A run: budgets are checked (hard stop pauses the agent and opens a
budget_override approval instead of running), the agent's model spec is
resolved, the run gets a fresh thread under the agent's workspace chat (the
"Company" folder), and the ordinary agent loop runs there with the agent's
instructions, its org context, the reason it woke and its issues, its tool
restrictions, and the Company tools (src/company_tools.py) acting as the
agent. Model calls are attributed to the company, agent and run
(src/usage.py). Between agent rounds the run re-checks budgets and its own
cost limit, and pauses while the user is using Odysseus when the model is
local (see "Foreground gate"). At the end the run is closed, held issues are
released, the summary is posted on the issue, spend is copied onto the
agent and company rows, and the agent goes back to idle (or error).

Foreground gate: like scheduled tasks (src/interactive_gate.py), a run on a
LOCAL model waits for Odysseus to be quiet before it starts, and between
rounds waits again if the user came back, so agents never compete with the
user's own chat for a GPU. Cloud models are not gated: they don't share a
GPU with anyone. COMPANY_FOREGROUND_GATE=always|never overrides that.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TICK_S = 3.0
DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MAX_ROUNDS = 20
RESULT_CHARS = 4000
TRIAGE_LIMIT = 20               # unassigned issues listed in a manager's wake message
SUSPECT_CTX_CAPS = (2048, 4096, 8192)   # a call with exactly this many input tokens was likely cut off
SOURCE_RANK = {"approval": 0, "assignment": 1, "mention": 2, "on_demand": 3, "timer": 4}
FOLDER = "Company"
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_NOTE_RE = re.compile(r"(?m)^\s*_Note:.*?_\s*$\n?")

_EXECUTOR: Optional["CompanyExecutor"] = None


def get_executor() -> Optional["CompanyExecutor"]:
    return _EXECUTOR


def max_concurrent() -> int:
    try:
        from src.settings import get_setting
        n = int(get_setting("company_max_concurrent_runs", DEFAULT_MAX_CONCURRENT) or DEFAULT_MAX_CONCURRENT)
    except Exception:
        n = DEFAULT_MAX_CONCURRENT
    return max(1, min(n, 8))


def default_max_rounds() -> int:
    try:
        from src.settings import get_setting
        n = int(get_setting("company_agent_max_rounds", DEFAULT_MAX_ROUNDS) or DEFAULT_MAX_ROUNDS)
    except Exception:
        n = DEFAULT_MAX_ROUNDS
    return max(1, min(n, 200))


def is_local_endpoint(url: Optional[str]) -> bool:
    """True for a model server on this machine or the local network/tailnet
    (loopback, private, CGNAT 100.64/10, .local/.lan/.ts.net names)."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost", "host.docker.internal") or host.endswith((".local", ".lan", ".internal", ".ts.net", ".home")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host          # a bare hostname: a box on the LAN
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def should_gate(url: Optional[str]) -> bool:
    mode = os.getenv("COMPANY_FOREGROUND_GATE", "local").strip().lower()
    if mode in ("never", "off", "0", "false", "no"):
        return False
    if mode in ("always", "on", "1", "true", "yes"):
        return True
    return is_local_endpoint(url)


def gate_max_wait() -> float:
    """The longest a run waits for Odysseus to be quiet (seconds), so an open,
    busy page can't hold an agent back forever."""
    try:
        return max(0.0, float(os.getenv("COMPANY_GATE_MAX_WAIT_S", "60")))
    except ValueError:
        return 60.0


async def wait_quiet(label: str) -> None:
    from src.interactive_gate import wait_for_interactive_quiet
    try:
        await asyncio.wait_for(wait_for_interactive_quiet(label), timeout=gate_max_wait() or None)
    except asyncio.TimeoutError:
        logger.info("[company] %s: Odysseus stayed busy; going ahead", label)


def _money(cents: float) -> str:
    return f"${float(cents or 0) / 100:.2f}"


def _clean(text: str) -> str:
    return _NOTE_RE.sub("", _THINK_RE.sub("", text or "")).strip()


def _collect(chunk: str, st: Dict[str, Any]) -> None:
    """Gather the reply text, tool events and errors of one SSE chunk."""
    if not isinstance(chunk, str):
        return
    is_error = chunk.startswith("event: error")
    for line in chunk.splitlines():
        if not line.startswith("data: "):
            continue
        body = line[6:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if is_error or (d.get("error") and not d.get("type")):
            st["error"] = str(d.get("error") or d.get("message") or "Model error")[:1000]
        elif isinstance(d.get("delta"), str):
            st["text"] += d["delta"]
        elif d.get("type") == "agent_step":
            st["round"] = d.get("round", st["round"])
            st["text"] += "\n\n"
        elif d.get("type") == "tool_output":
            st["tools"].append({"round": st["round"], "tool": d.get("tool"), "command": d.get("command"),
                                "output": d.get("output"), "exit_code": d.get("exit_code")})


def _new_state() -> Dict[str, Any]:
    return {"text": "", "tools": [], "round": 1, "error": None, "stop_reason": None, "notices": []}


# ── Prompt ──────────────────────────────────────────────────────────────

PROTOCOL = """## How to work
- You were woken for the reasons in the message below. Do the work with your tools and the Company tools. Nobody is watching this conversation live, so don't ask questions here: ask in an issue comment instead (@Name wakes another agent).
- Check an issue out with company_checkout_issue before working on it, and work one issue at a time.
- Record progress and findings with company_comment. Move the issue with company_update_issue: in_review when it is ready for someone to check, done when finished, blocked (with a comment saying why) when you can't go on.
- Managers (and whoever is at the top of the org): triage first. When the message lists unassigned issues, give each one to the report whose role fits it best (or to yourself) with company_update_issue {"issue", "assignee", "comment": one line on why}; the assignee is woken to start. Only then do hands-on work of your own.
- Managers: break large work into sub-issues with company_create_issue and assign them to your reports instead of doing their work yourself.
- Before a large, risky or costly change, ask first with company_request_approval (type plan, numbered steps) and end your turn; you are woken when it is decided.
- Keep the run focused. End with a short summary of what you did and what is next: it is posted on the issue."""


def _role_label(role: str) -> str:
    return {"ceo": "CEO", "cto": "CTO", "cmo": "CMO", "cfo": "CFO", "pm": "PM", "qa": "QA",
            "devops": "DevOps"}.get(role, (role or "general").capitalize())


def build_prompt(db, owner: str, agent, company, run, wakes: List[Any],
                 notices: Optional[List[str]] = None) -> Tuple[str, str, Optional[str], Dict[str, Any]]:
    """(system prompt, wake message, approved plan or None, limits)."""
    from src import company as co
    agents = co._company_agents(db, owner, company.id)
    by_id = {a.id: a for a in agents}
    name = lambda aid: by_id[aid].name if aid in by_id else "an agent"  # noqa: E731

    sys_parts = [agent.instructions.strip() if agent.instructions and agent.instructions.strip()
                 else f"You are {agent.name}, the {_role_label(agent.role)} of {company.name}."]
    org = [f"## Your place in {company.name}",
           f"You are {agent.name}, {_role_label(agent.role)}{f' ({agent.title})' if agent.title else ''}."]
    if company.description:
        org.append(f"The company: {company.description.strip()[:1500]}")
    goals = co._scoped(db, co.CompanyGoal, owner).filter(
        co.CompanyGoal.company_id == company.id, co.CompanyGoal.status.in_(("active", "planned"))).all()
    top = [g for g in goals if not g.parent_id][:6]
    if top:
        org.append("Company goals: " + "; ".join(g.title for g in top) + ".")
    mine = [g for g in goals if g.owner_agent_id == agent.id][:6]
    if mine:
        org.append("Goals you own: " + "; ".join(g.title for g in mine) + ".")
    if agent.reports_to and agent.reports_to in by_id:
        m = by_id[agent.reports_to]
        org.append(f"You report to {m.name} ({_role_label(m.role)}).")
    else:
        org.append("You are at the top of the org and report to the person who runs the company.")
    reports = [a for a in agents if a.reports_to == agent.id and a.status != "terminated"]
    if reports:
        org.append("Your reports:\n" + "\n".join(
            f"- {a.name} ({_role_label(a.role)}){': ' + a.capabilities.strip()[:300] if a.capabilities else ''}"
            for a in reports))
    if agent.capabilities:
        org.append(f"Your capabilities: {agent.capabilities.strip()[:1500]}")
    sys_parts += ["\n".join(org), PROTOCOL]

    # Managers and the top of the org also see unassigned work and their reports' load.
    manages = bool(reports) or not (agent.reports_to and agent.reports_to in by_id)

    # Why it woke.
    reasons, approved_plan, limits = [], None, {}
    focus_ids: List[str] = []
    for w in sorted(wakes, key=lambda w: (SOURCE_RANK.get(w.source, 9), w.created_at or datetime.min)):
        issue = co._scoped(db, co.CompanyIssue, owner).filter(co.CompanyIssue.id == w.issue_id).first() if w.issue_id else None
        ident = f"{issue.identifier} \"{issue.title}\"" if issue else ""
        if issue and issue.id not in focus_ids:
            focus_ids.append(issue.id)
        if w.source == "assignment":
            reasons.append(f"- You were assigned {ident or 'an issue'}.")
        elif w.source == "mention":
            cm = co._scoped(db, co.CompanyIssueComment, owner).filter(
                co.CompanyIssueComment.id == w.comment_id).first() if w.comment_id else None
            who = (name(cm.author_id) if cm and cm.author_type == "agent" else "The person") if cm else "Someone"
            body = f": \"{cm.body.strip()[:1500]}\"" if cm else ""
            reasons.append(f"- {who} mentioned you on {ident or 'an issue'}{body}")
        elif w.source == "on_demand":
            reasons.append("- The person who runs the company asked you to wake up and check your work"
                           + (", including triaging unassigned issues." if manages else "."))
        elif w.source == "timer":
            reasons.append("- Your regular heartbeat: check your issues and move your work along"
                           + (", and triage any unassigned issues." if manages else "."))
        elif w.source == "approval":
            ap = co._scoped(db, co.CompanyApproval, owner).filter(co.CompanyApproval.id == w.approval_id).first() \
                if w.approval_id else None
            if ap is None:
                continue
            p = ap.payload or {}
            note = f" Their note: \"{ap.decision_note}\"" if ap.decision_note else ""
            title = ap.title or ap.type
            if ap.type in ("plan", "strategy") and ap.status == "approved":
                on = f" It runs on {run.model}." if p.get("chosen_model") else ""
                reasons.append(f"- Your {ap.type} \"{title}\" was APPROVED.{on}{note} Carry it out now, step by step, "
                               "ticking steps off as you go.")
                approved_plan = approved_plan or p.get("plan")
                for k in ("max_steps", "max_cost_usd"):
                    if p.get(k) and k not in limits:
                        limits[k] = p[k]
            elif ap.status == "rejected":
                reasons.append(f"- Your {ap.type.replace('_', ' ')} \"{title}\" was REJECTED.{note} Don't do it; "
                               "say on the issue what you will do instead.")
            elif ap.status == "revision_requested":
                reasons.append(f"- Changes were requested to your {ap.type.replace('_', ' ')} \"{title}\".{note} "
                               "Revise it and ask again with company_request_approval.")
            elif ap.type == "hire_agent" and ap.status == "approved":
                reasons.append(f"- Your hire \"{title}\" was approved; the new agent reports to you.{note}")
            elif ap.type == "budget_override" and ap.status == "approved":
                reasons.append(f"- Your budget was raised and you were resumed.{note} Carry on with your work.")
            if p.get("issue_id") and p["issue_id"] not in focus_ids:
                focus_ids.append(p["issue_id"])
    if run.issue_id and run.issue_id not in focus_ids:
        focus_ids.insert(0, run.issue_id)

    msg = [f"[Company run: {company.name}]", "", "Why you are awake:"] + (reasons or ["- A heartbeat."])
    for n in notices or []:
        msg += ["", f"Notice: {n}"]
    for iid in focus_ids[:3]:
        i = co._scoped(db, co.CompanyIssue, owner).filter(co.CompanyIssue.id == iid).first()
        if i is None or i.company_id != company.id:
            continue
        msg += ["", f"## {i.identifier}: {i.title}",
                f"Status {i.status.replace('_', ' ')}, priority {i.priority}, assigned to "
                f"{name(i.assignee_agent_id) if i.assignee_agent_id else ('a person' if i.assignee_user else 'nobody')}."]
        if i.description:
            msg.append(i.description.strip()[:4000])
        cms = co._scoped(db, co.CompanyIssueComment, owner).filter(co.CompanyIssueComment.issue_id == i.id) \
            .order_by(co.CompanyIssueComment.created_at.desc()).limit(8).all()
        if cms:
            msg.append("Recent comments:")
            for c in reversed(cms):
                who = name(c.author_id) if c.author_type == "agent" else ("System" if c.author_type == "system" else "The person")
                msg.append(f"- {who}: {c.body.strip()[:800]}")
    open_rows = co._scoped(db, co.CompanyIssue, owner).filter(
        co.CompanyIssue.company_id == company.id, co.CompanyIssue.assignee_agent_id == agent.id,
        co.CompanyIssue.status.notin_(("done", "cancelled"))).all()
    open_rows.sort(key=lambda i: (co.PRIORITY_RANK.get(i.priority, 9), i.number))
    msg += ["", "Your open issues:"]
    msg += [f"- {i.identifier} [{i.status.replace('_', ' ')}, {i.priority}] {i.title}" for i in open_rows[:20]] or ["- none"]
    if manages:
        msg += _triage_section(db, owner, company, agent, reports, co)
    return "\n\n".join(sys_parts), "\n".join(msg), approved_plan, limits


def _triage_section(db, owner: str, company, agent, reports: List[Any], co) -> List[str]:
    """For a manager: unassigned open issues (backlog/todo, nobody on them),
    one line of workload per report, and what to do about it."""
    unassigned = co._scoped(db, co.CompanyIssue, owner).filter(
        co.CompanyIssue.company_id == company.id, co.CompanyIssue.status.in_(("backlog", "todo")),
        co.CompanyIssue.assignee_agent_id.is_(None), co.CompanyIssue.assignee_user.is_(None)).all()
    unassigned.sort(key=lambda i: (co.PRIORITY_RANK.get(i.priority, 9), i.number))
    out: List[str] = []
    if unassigned:
        more = len(unassigned) - TRIAGE_LIMIT
        out += ["", f"Unassigned issues ({len(unassigned)}):"]
        out += [f"- {i.identifier} [{i.status.replace('_', ' ')}, {i.priority}] {i.title}"
                for i in unassigned[:TRIAGE_LIMIT]]
        if more > 0:
            out.append(f"- ...and {more} more (company_list_issues scope unassigned)")
    if reports:
        ids = [a.id for a in reports]
        rows = co._scoped(db, co.CompanyIssue, owner).filter(
            co.CompanyIssue.company_id == company.id, co.CompanyIssue.assignee_agent_id.in_(ids),
            co.CompanyIssue.status.notin_(("done", "cancelled"))).all()
        out += ["", "Your reports' workload:"]
        for a in reports:
            theirs = [i for i in rows if i.assignee_agent_id == a.id]
            busy = sorted((i for i in theirs if i.status == "in_progress"), key=lambda i: i.number)
            line = f"- {a.name} ({_role_label(a.role)}, {a.status}): {len(theirs)} open"
            if busy:
                line += ", in progress: " + ", ".join(i.identifier for i in busy[:5])
            out.append(line)
    if unassigned:
        who = "a report whose role fits it, or yourself" if reports else "yourself, or hire help with company_request_approval"
        out += ["", f"Triage first: assign each unassigned issue to {who}, using company_update_issue with "
                "\"assignee\" and a one-line \"comment\" saying why. Assignees are woken to start. Then work "
                "your own issues. One call per issue, like:",
                "```company_update_issue",
                f"{{\"issue\": \"{unassigned[0].identifier}\", \"assignee\": \"<name>\", \"comment\": \"<why them>\"}}",
                "```"]
    return out


def truncated_at(run_id: str) -> Optional[int]:
    """The suspicious round number (2048, 4096, 8192) a model call of this
    run used exactly as its input tokens, or None. Local servers that cut a
    prompt to fit their window report the window size as the prompt size."""
    try:
        from core.database import UsageEvent
        from src import usage
        db = usage._session_factory()
        try:
            rows = db.query(UsageEvent.input_tokens, UsageEvent.cached_input_tokens).filter(
                UsageEvent.company_run_id == run_id).all()
        finally:
            db.close()
    except Exception as e:
        logger.debug("truncation check failed: %s", e)
        return None
    for inp, cached in rows:
        total = int(inp or 0) + int(cached or 0)
        if total in SUSPECT_CTX_CAPS:
            return total
    return None


# ── Notifications (the owner only) ──────────────────────────────────────

_NOTICES: Dict[str, Deque[Dict[str, Any]]] = {}


def recent_notices(owner: str, since: float = 0.0) -> List[Dict[str, Any]]:
    return [n for n in _NOTICES.get(owner or "", ()) if n["ts"] > since]


async def notify(owner: str, heading: str, body: str, *, kind: str = "company") -> None:
    """Tell the company's owner: a toast on an open Company page, and a push
    to that owner's browsers (all of them in single-user mode)."""
    q = _NOTICES.setdefault(owner or "", deque(maxlen=50))
    q.append({"ts": time.time(), "kind": kind, "heading": heading, "body": body})
    try:
        if owner:
            from src import webpush
            for s in webpush.load_subscriptions():
                if s.get("owner") == owner:
                    await webpush.send(heading, body, endpoint=s["endpoint"], url="/#company",
                                       tag=f"odysseus-{kind}")
        else:
            from src.chat_queue import send_notification
            await send_notification("", {}, heading, body, kind=kind, anchor="company")
    except Exception as e:
        logger.debug("[company] notification failed: %s", e)


# ── The executor ────────────────────────────────────────────────────────

class CompanyExecutor:
    def __init__(self, session_manager=None, *, agent_loop: Optional[Callable] = None,
                 tick_s: float = TICK_S, notifier: Optional[Callable] = None):
        self._sm = session_manager
        self._agent_loop = agent_loop
        self._tick_s = tick_s
        self._notify = notifier or notify
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._running = False
        self._stopping = False
        self.runs: Dict[str, Dict[str, Any]] = {}     # run_id -> {task, agent_id, owner, thread_id, stop}
        self._notified: set = set()

    # lifecycle
    async def start(self) -> None:
        global _EXECUTOR
        _EXECUTOR = self
        self.mark_interrupted()
        self._seed_notified()
        self._running = True
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        logger.info("[company] executor started (up to %d runs at once)", max_concurrent())

    async def stop(self, timeout: float = 8.0) -> None:
        self._stopping = True
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        tasks = [r["task"] for r in self.runs.values() if r.get("task") and not r["task"].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        logger.info("[company] executor stopped")

    def kick(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        await asyncio.sleep(2)
        while self._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("[company] executor tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._tick_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def tick(self) -> List[str]:
        self.add_timer_wakeups()
        started = self.dispatch()
        await self.notify_new_approvals()
        return started

    def _db(self):
        from core import database as cdb
        return cdb.SessionLocal()

    def _session_manager(self):
        if self._sm is not None:
            return self._sm
        from src.ai_interaction import get_session_manager
        return get_session_manager()

    # startup
    def mark_interrupted(self) -> int:
        """Runs left queued or running by a previous process are over."""
        from core.database import CompanyAgent, CompanyIssue, CompanyRun, utcnow_naive
        from src import company as co
        db = self._db()
        try:
            stale = db.query(CompanyRun).filter(CompanyRun.status.in_(co.RUN_LIVE)).all()
            now = utcnow_naive()
            ids = [r.id for r in stale if r.id not in self.runs]
            for r in stale:
                if r.id in self.runs:
                    continue
                r.status = "interrupted"
                r.finished_at = now
                r.error = r.error or "Odysseus restarted while this run was going"
                co.log_activity(db, r.owner or "", r.company_id, co.Actor("system"), "run.interrupted", "run", r.id,
                                {"run_id": r.id}, agent_id=r.agent_id, run_id=r.id)
            if ids:
                db.query(CompanyIssue).filter(CompanyIssue.checkout_run_id.in_(ids)).update(
                    {CompanyIssue.checkout_run_id: None, CompanyIssue.execution_locked_at: None},
                    synchronize_session=False)
            busy = {r["agent_id"] for r in self.runs.values()}
            for a in db.query(CompanyAgent).filter(CompanyAgent.status == "running").all():
                if a.id not in busy:
                    a.status = "idle"
            db.commit()
            if ids:
                logger.info("[company] marked %d run(s) interrupted by a restart", len(ids))
            return len(ids)
        finally:
            db.close()

    def _seed_notified(self) -> None:
        from core.database import CompanyApproval
        db = self._db()
        try:
            self._notified |= {a.id for a in db.query(CompanyApproval.id).filter(CompanyApproval.status == "pending")}
        finally:
            db.close()

    # 1. timer heartbeats
    def add_timer_wakeups(self, now: Optional[datetime] = None) -> int:
        from core.database import CompanyAgent, CompanyCompany, CompanyWakeup, utcnow_naive
        from src import company as co
        now = now or utcnow_naive()
        busy = {r["agent_id"] for r in self.runs.values()}
        db = self._db()
        added = 0
        try:
            companies = {c.id: c for c in db.query(CompanyCompany).filter(CompanyCompany.status == "active")}
            agents = db.query(CompanyAgent).filter(CompanyAgent.status.in_(("idle", "active", "error"))).all()
            waiting = {w.agent_id for w in db.query(CompanyWakeup.agent_id).filter(CompanyWakeup.status == "pending")}
            for a in agents:
                if a.company_id not in companies or a.id in busy or a.id in waiting:
                    continue
                try:
                    pol = co.heartbeat_policy(a.heartbeat or None)
                except co.CompanyError:
                    continue
                if not pol.get("enabled"):
                    continue
                last = a.last_heartbeat_at or a.created_at or now
                if (now - last).total_seconds() < int(pol.get("interval_sec") or 3600):
                    continue
                if co.request_wakeup(db, a.owner or "", a, "timer") is not None:
                    added += 1
            if added:
                db.commit()
            return added
        finally:
            db.close()

    # 2. claiming
    def _busy_agents(self) -> set:
        return {r["agent_id"] for r in self.runs.values()}

    def dispatch(self) -> List[str]:
        """Turn pending wake-ups into runs, within the concurrency limits."""
        from core.database import CompanyAgent, CompanyCompany, CompanyRun, CompanyWakeup, utcnow_naive
        from src import company as co
        slots = max_concurrent() - len(self.runs)
        if slots <= 0:
            return []
        db = self._db()
        started: List[str] = []
        try:
            pending = db.query(CompanyWakeup).filter(CompanyWakeup.status == "pending") \
                .order_by(CompanyWakeup.created_at).all()
            groups: Dict[str, List[Any]] = {}
            for w in pending:
                groups.setdefault(w.agent_id, []).append(w)
            for agent_id, wakes in groups.items():
                if slots <= 0:
                    break
                if agent_id in self._busy_agents():
                    continue
                agent = db.query(CompanyAgent).filter(CompanyAgent.id == agent_id).first()
                owner = (agent.owner or "") if agent else ""
                if agent is None or agent.status in ("paused", "terminated", "pending_approval"):
                    for w in wakes:
                        w.status = "dropped"
                        w.consumed_at = utcnow_naive()
                    db.commit()
                    continue
                company = db.query(CompanyCompany).filter(CompanyCompany.id == agent.company_id).first()
                if company is None or company.status != "active":
                    continue            # a paused company keeps its wake-ups for later
                live = db.query(CompanyRun).filter(CompanyRun.agent_id == agent.id,
                                                   CompanyRun.status.in_(co.RUN_LIVE)).first()
                if live is not None:
                    continue
                verdict = self.check_budget(db, owner, agent, company)
                db.commit()
                if verdict["state"] == "hard_stop":
                    continue
                lead = sorted(wakes, key=lambda w: (SOURCE_RANK.get(w.source, 9), w.created_at))[0]
                issue_id = next((w.issue_id for w in sorted(wakes, key=lambda w: SOURCE_RANK.get(w.source, 9))
                                 if w.issue_id), None)
                model = agent.model
                for w in wakes:
                    if w.source == "approval" and w.approval_id:
                        ap = db.query(co.CompanyApproval).filter(co.CompanyApproval.id == w.approval_id).first()
                        p = (ap.payload or {}) if ap is not None else {}
                        if ap is not None and ap.status == "approved" and p.get("chosen_model"):
                            model = p["chosen_model"]
                        issue_id = issue_id or p.get("issue_id")
                run = CompanyRun(id=uuid.uuid4().hex, owner=agent.owner, company_id=agent.company_id,
                                 agent_id=agent.id, issue_id=issue_id, invocation_source=lead.source,
                                 status="queued", model=model)
                db.add(run)
                db.flush()
                now = utcnow_naive()
                n = db.query(CompanyWakeup).filter(CompanyWakeup.id.in_([w.id for w in wakes]),
                                                   CompanyWakeup.status == "pending").update(
                    {CompanyWakeup.status: "consumed", CompanyWakeup.run_id: run.id,
                     CompanyWakeup.consumed_at: now}, synchronize_session=False)
                if not n:
                    db.rollback()
                    continue
                co.log_activity(db, owner, agent.company_id, co.Actor("system"), "run.queued", "run", run.id,
                                {"source": lead.source, "wakeups": n, "model": model}, agent_id=agent.id,
                                run_id=run.id)
                db.commit()
                info = {"agent_id": agent.id, "owner": owner, "company_id": agent.company_id,
                        "thread_id": None, "stop": None, "notices": verdict.get("notices") or []}
                self.runs[run.id] = info
                info["task"] = asyncio.create_task(self._execute(run.id))
                started.append(run.id)
                slots -= 1
            return started
        finally:
            db.close()

    # budgets
    def check_budget(self, db, owner: str, agent, company, *, notify_warn: bool = True) -> Dict[str, Any]:
        """Check the agent's and the company's monthly budgets over recorded
        usage, copy the spend onto their rows, and act on the result: a hard
        stop pauses the agent and opens a budget_override approval; a warning
        is noted (and notified once a month)."""
        from src import budgets
        from src import company as co
        o = owner or None
        out: Dict[str, Any] = {"state": "ok", "notices": []}
        try:
            ca = budgets.check("company_agent", agent.id, o, agent.monthly_budget_cents or 0)
            cc = budgets.check("company", company.id, o, company.monthly_budget_cents or 0)
        except Exception as e:
            logger.warning("[company] budget check failed: %s", e)
            return out
        month = co.month_key()
        agent.spent_cents, agent.budget_month = int(round(ca["spent_cents"])), month
        company.spent_cents, company.budget_month = int(round(cc["spent_cents"])), month
        out.update(agent=ca, company=cc)
        for scope, b, who in (("company_agent", ca, f"{agent.name}'s"), ("company", cc, f"{company.name}'s")):
            if b["state"] == "hard_stop":
                reason = (f"Monthly budget reached: {_money(b['spent_cents'])} of {_money(b['limit_cents'])} "
                          f"({'this agent' if scope == 'company_agent' else 'the company'})")
                co.pause_agent(db, owner, agent, reason, action="agent.budget_paused")
                exists = db.query(co.CompanyApproval).filter(
                    co.CompanyApproval.company_id == company.id, co.CompanyApproval.type == "budget_override",
                    co.CompanyApproval.status == "pending").all()
                if not any((x.payload or {}).get("agent_id") == agent.id for x in exists):
                    ap = co.CompanyApproval(
                        id=uuid.uuid4().hex, owner=agent.owner, company_id=company.id, type="budget_override",
                        title=f"Budget for {agent.name}", requested_by_type="system", requested_by_id=None,
                        status="pending",
                        payload={"agent_id": agent.id, "scope": scope, "limit_cents": b["limit_cents"],
                                 "spent_cents": b["spent_cents"], "month": month, "reason": reason})
                    db.add(ap)
                    db.flush()
                    self._notified.add(ap.id)
                    co.log_activity(db, owner, company.id, co.Actor("system"), "approval.created", "approval", ap.id,
                                    {"type": "budget_override", "title": ap.title})
                self._spawn(self._notify(owner, f"{agent.name} paused: budget reached",
                                         f"{reason}. Raise it in Company > Approvals.", kind="company-budget"))
                out["state"] = "hard_stop"
                out["reason"] = reason
                return out
        for scope, b, entity, label in (("company_agent", ca, agent, agent.name), ("company", cc, company, company.name)):
            if b["state"] != "warn":
                continue
            out["state"] = "warn"
            text = (f"{label} has used {b['pct']:.0f}% of its monthly budget "
                    f"({_money(b['spent_cents'])} of {_money(b['limit_cents'])}). Be economical.")
            out["notices"].append(text)
            if notify_warn and not self._warned_this_month(db, owner, company.id, entity.id, month):
                co.log_activity(db, owner, company.id, co.Actor("system"), "budget.warning",
                                "agent" if scope == "company_agent" else "company", entity.id,
                                {"month": month, "pct": b["pct"], "spent_cents": b["spent_cents"],
                                 "limit_cents": b["limit_cents"], "name": label}, agent_id=agent.id)
                self._spawn(self._notify(owner, f"Budget warning: {label}", text, kind="company-budget"))
        return out

    def _warned_this_month(self, db, owner, company_id, entity_id, month) -> bool:
        from core.database import CompanyActivity
        start = datetime.strptime(month + "-01", "%Y-%m-%d")
        return db.query(CompanyActivity).filter(
            CompanyActivity.company_id == company_id, CompanyActivity.action == "budget.warning",
            CompanyActivity.entity_id == entity_id, CompanyActivity.created_at >= start).first() is not None

    def _spawn(self, coro) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    # 3. approvals waiting on the person
    async def notify_new_approvals(self) -> None:
        from core.database import CompanyAgent, CompanyApproval
        db = self._db()
        try:
            rows = db.query(CompanyApproval).filter(CompanyApproval.status == "pending").all()
            fresh = [a for a in rows if a.id not in self._notified]
            for ap in fresh:
                self._notified.add(ap.id)
                who = None
                if ap.requested_by_type == "agent" and ap.requested_by_id:
                    who = db.query(CompanyAgent.name).filter(CompanyAgent.id == ap.requested_by_id).scalar()
                if not who:
                    continue        # the person's own requests need no push
                kind = {"plan": "A plan", "strategy": "A strategy", "hire_agent": "A hire"}.get(ap.type, "A request")
                await self._notify(ap.owner or "", f"{kind} is waiting for your approval",
                                   f"{who}: {ap.title or ap.type}", kind="company-approval")
        finally:
            db.close()

    # stopping a run
    def stop_run(self, owner: str, run_id: str) -> bool:
        """Stop a queued or running run. Returns False if it isn't live here."""
        info = self.runs.get(run_id)
        if info is None or (info.get("owner") or "") != (owner or ""):
            return False
        info["stop"] = "user"
        tid = info.get("thread_id")
        from src import agent_runs
        if tid and agent_runs.stop(tid):
            return True
        t = info.get("task")
        if t and not t.done():
            t.cancel()
        return True

    # a run
    async def _execute(self, run_id: str) -> None:
        from src import agent_runs
        info = self.runs[run_id]
        owner = info["owner"]
        st = _new_state()
        final = {"status": "failed", "error": None}
        thread_id = None
        try:
            prep = await self._prepare(run_id, info)
            if prep is None:
                final = {"status": "cancelled", "error": info.get("skip") or "Nothing to run"}
                return
            # The person asking (Wake now, an approval) is not kept waiting.
            if prep.get("gate") and prep["source"] not in ("on_demand", "approval"):
                await wait_quiet(f"company run {run_id[:8]}")
            if info.get("stop"):
                raise asyncio.CancelledError()
            thread_id = self._begin(run_id, info, prep)
            info["thread_id"] = thread_id
            from src import company_tools
            from src.usage import usage_scope
            with usage_scope(source="company", owner=owner or None, session_id=thread_id,
                             company_id=info["company_id"], company_agent_id=info["agent_id"],
                             company_run_id=run_id), \
                    company_tools.agent_context(owner=owner, company_id=info["company_id"],
                                                agent_id=info["agent_id"], run_id=run_id) as ctx:
                info["ctx"] = ctx
                run = agent_runs.start(thread_id, self._stream(run_id, info, prep, st), background=True)
            await asyncio.wait({run.task})
            if st["stop_reason"]:
                final = {"status": "cancelled", "error": st["stop_reason"]}
            elif info.get("stop") == "user" or run.status == "stopped":
                final = {"status": "interrupted" if (self._stopping or agent_runs.restarting) else "cancelled",
                         "error": "Odysseus restarted" if (self._stopping or agent_runs.restarting) else "Stopped by you"}
            elif run.status == "error":
                final = {"status": "failed", "error": st["error"] or "The agent run failed"}
            elif st["error"] and not st["text"].strip() and not st["tools"]:
                final = {"status": "failed", "error": st["error"]}
            else:
                final = {"status": "succeeded", "error": None}
        except asyncio.CancelledError:
            if thread_id:
                agent_runs.stop(thread_id)
                r = agent_runs._RUNS.get(thread_id)
                if r and r.task:
                    await asyncio.wait({r.task}, timeout=5)
            stopping = self._stopping or agent_runs.restarting
            final = {"status": "interrupted" if stopping else "cancelled",
                     "error": "Odysseus restarted" if stopping else "Stopped by you"}
        except Exception as e:
            logger.warning("[company] run %s failed: %s", run_id[:8], e, exc_info=True)
            final = {"status": "failed", "error": str(e)[:1000]}
        finally:
            try:
                await asyncio.shield(self._finish(run_id, info, st, final))
            except Exception:
                logger.exception("[company] could not finish run %s", run_id)
            self.runs.pop(run_id, None)
            self.kick()

    async def _prepare(self, run_id: str, info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Resolve the model and build the prompt; None to skip the run."""
        from core.database import CompanyAgent, CompanyCompany, CompanyRun, CompanyWakeup
        from src import company as co
        owner = info["owner"]
        db = self._db()
        try:
            run = db.query(CompanyRun).filter(CompanyRun.id == run_id).first()
            agent = db.query(CompanyAgent).filter(CompanyAgent.id == info["agent_id"]).first()
            company = db.query(CompanyCompany).filter(CompanyCompany.id == info["company_id"]).first()
            if run is None or agent is None or company is None:
                info["skip"] = "The agent or company is gone"
                return None
            spec = run.model or agent.model
            if not spec:
                raise RuntimeError(f"{agent.name} has no model: pick one in the org chart")
            wakes = db.query(CompanyWakeup).filter(CompanyWakeup.run_id == run_id).all()
            system, user, plan, limits = build_prompt(db, owner, agent, company, run, wakes, info.get("notices"))
            disabled = set(agent.disabled_tools or []) | {"ask_user"}
            if not agent.coding_agent:
                disabled.add("claude_code")      # coding agents are off unless picked for this agent
            elif agent.workdir:
                system += (f"\n\n## Coding\nFor coding work use the claude_code tool (engine "
                           f"{agent.coding_agent}) with cwd {agent.workdir}. Its plans wait for the person's "
                           "approval in your run's chat.")
            relevant = None
            if agent.enabled_tools:
                from src import company_tools
                relevant = set(agent.enabled_tools) | set(company_tools.TOOL_NAMES)
            prep = {"spec": spec, "system": system, "user": user, "approved_plan": plan,
                    "max_rounds": int(limits.get("max_steps") or default_max_rounds()),
                    "max_cost_usd": limits.get("max_cost_usd"), "disabled": disabled, "relevant": relevant,
                    "agent_name": agent.name, "company_name": company.name, "source": run.invocation_source,
                    "issue_id": run.issue_id, "workspace": agent.session_id,
                    "coding_agent": agent.coding_agent}
        finally:
            db.close()
        from src import ai_interaction
        try:
            url, model_id, headers = await asyncio.to_thread(ai_interaction._resolve_model, prep["spec"], owner or None)
        except Exception as e:
            raise RuntimeError(f"Model {prep['spec']} is not available: {e}")
        # The agent loop sizes its input budget from the model's window; without
        # it the loop falls back to a 6000-token budget and trims the run's own
        # instructions away on local models with long tool prompts.
        try:
            from src.model_context import get_context_length
            ctx_len = await asyncio.to_thread(get_context_length, url, model_id)
        except Exception as e:
            logger.debug("context length lookup failed: %s", e)
            ctx_len = 0
        prep.update(url=url, model_id=model_id, headers=headers or {}, gate=should_gate(url),
                    context_length=int(ctx_len or 0))
        return prep

    def _workspace(self, sm, owner: str, prep: Dict[str, Any]) -> str:
        """The agent's workspace chat: reused across runs, made on first use,
        filed in the Company folder. Each run is a thread under it."""
        from core.database import Session as DbSession
        from core.models import ChatMessage
        sid = prep.get("workspace")
        db = self._db()
        try:
            if sid and db.query(DbSession.id).filter(DbSession.id == sid).first():
                return sid
            sid = str(uuid.uuid4())
            sm.create_session(sid, f"{prep['agent_name']} · {prep['company_name']}", prep["url"],
                              prep["model_id"], owner=owner or None)
            db.query(DbSession).filter(DbSession.id == sid).update({DbSession.folder: FOLDER})
            db.commit()
        finally:
            db.close()
        try:
            sm.add_message(sid, ChatMessage(
                "assistant", f"Workspace of {prep['agent_name']} in {prep['company_name']}. Each run of the agent "
                "is a thread of this chat.", metadata={"source": "company_workspace"}))
        except Exception:
            logger.debug("workspace intro not saved", exc_info=True)
        return sid

    def _begin(self, run_id: str, info: Dict[str, Any], prep: Dict[str, Any]) -> str:
        """Make the run's thread, then mark the run and the agent running."""
        from core.database import CompanyAgent, CompanyRun, utcnow_naive
        from core.models import ChatMessage
        from src import company as co
        owner = info["owner"]
        sm = self._session_manager()
        if sm is None:
            raise RuntimeError("Odysseus sessions are not ready yet")
        ws = self._workspace(sm, owner, prep)
        tid = str(uuid.uuid4())
        when = datetime.now().strftime("%b %d %H:%M")
        thread = sm.create_session(tid, f"Run {when} · {prep['source'].replace('_', ' ')}", prep["url"],
                                   prep["model_id"], owner=owner or None, parent_session_id=ws)
        if prep["headers"]:
            thread.headers = dict(prep["headers"])
        try:
            from core.database import set_session_mode
            set_session_mode(tid, "agent")
        except Exception:
            pass
        # The run's chat allows only the agent's chosen coding agent (src/chat_prefs.py).
        try:
            from src import chat_prefs
            for eng in chat_prefs.ENGINE_KEYS:
                chat_prefs.set_pref(tid, eng, eng == prep.get("coding_agent"))
        except Exception:
            logger.debug("coding agent prefs not set", exc_info=True)
        sm.add_message(tid, ChatMessage("user", prep["user"], metadata={"source": "company_run", "run_id": run_id}))
        try:
            sm.save_sessions()
        except Exception:
            pass
        db = self._db()
        try:
            now = utcnow_naive()
            run = db.query(CompanyRun).filter(CompanyRun.id == run_id).first()
            run.status, run.started_at, run.session_id, run.model = "running", now, tid, prep["spec"]
            agent = db.query(CompanyAgent).filter(CompanyAgent.id == info["agent_id"]).first()
            info["prev_status"] = agent.status
            agent.status, agent.last_heartbeat_at, agent.session_id = "running", now, ws
            co.log_activity(db, owner, run.company_id, co.Actor("system"), "run.started", "run", run.id,
                            {"source": run.invocation_source, "model": prep["spec"]}, agent_id=agent.id, run_id=run.id)
            db.commit()
        finally:
            db.close()
        return tid

    async def _stream(self, run_id: str, info: Dict[str, Any], prep: Dict[str, Any], st: Dict[str, Any]):
        """The agent loop's events for this run, with the checks between
        rounds, saving the reply in the run's thread when it ends."""
        loop_fn = self._agent_loop
        if loop_fn is None:
            from src.agent_loop import stream_agent_loop as loop_fn
        messages = [{"role": "system", "content": prep["system"]}, {"role": "user", "content": prep["user"]}]
        gen = loop_fn(prep["url"], prep["model_id"], messages, headers=prep["headers"],
                      max_rounds=prep["max_rounds"], session_id=info["thread_id"],
                      disabled_tools=set(prep["disabled"]), owner=info["owner"] or None,
                      relevant_tools=set(prep["relevant"]) if prep["relevant"] else None,
                      approved_plan=prep["approved_plan"], context_length=prep.get("context_length") or 0,
                      auto_input_budget=True)
        try:
            for n in info.get("notices") or []:
                yield f'data: {json.dumps({"type": "company_notice", "text": n})}\n\n'
            async for chunk in gen:
                _collect(chunk, st)
                yield chunk
                if '"agent_step"' in chunk:
                    why = await self._between_rounds(run_id, info, prep)
                    if why:
                        st["stop_reason"] = why
                        note = f"\n\n_Stopped: {why}._"
                        st["text"] += note
                        yield f'data: {json.dumps({"delta": note})}\n\n'
                        yield "data: [DONE]\n\n"
                        break
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass
            if st["text"].strip() or st["tools"]:
                try:
                    from core.models import ChatMessage
                    sm = self._session_manager()
                    sm.add_message(info["thread_id"], ChatMessage(
                        "assistant", st["text"].strip(),
                        metadata={"tool_events": st["tools"], "model": prep["model_id"], "source": "company_run",
                                  "run_id": run_id}))
                    sm.save_sessions()
                except Exception as e:
                    logger.warning("[company] could not save run %s reply: %s", run_id[:8], e)

    async def _between_rounds(self, run_id: str, info: Dict[str, Any], prep: Dict[str, Any]) -> Optional[str]:
        """Between agent rounds: the run's own cost limit, the agent's and the
        company's budgets, and the foreground gate. A reason to stop, or None."""
        from core.database import CompanyAgent, CompanyCompany
        if prep.get("max_cost_usd"):
            try:
                from src import budgets
                from src.usage import utcnow
                s = budgets.spent("company_run", run_id, info["owner"] or None,
                                  start=utcnow() - timedelta(days=2), end=utcnow() + timedelta(minutes=5))
                if s["cost_usd"] >= float(prep["max_cost_usd"]):
                    return f"the run's cost limit of ${float(prep['max_cost_usd']):.2f} was reached"
            except Exception as e:
                logger.debug("run cost check failed: %s", e)
        db = self._db()
        try:
            agent = db.query(CompanyAgent).filter(CompanyAgent.id == info["agent_id"]).first()
            company = db.query(CompanyCompany).filter(CompanyCompany.id == info["company_id"]).first()
            if agent is None or company is None:
                return "the agent was removed"
            if agent.status in ("paused", "terminated"):
                return f"{agent.name} was {agent.status}"
            verdict = self.check_budget(db, info["owner"], agent, company, notify_warn=True)
            db.commit()
            if verdict["state"] == "hard_stop":
                return verdict.get("reason") or "the monthly budget was reached"
        finally:
            db.close()
        if prep.get("gate"):
            from src.interactive_gate import has_foreground_activity
            if has_foreground_activity():
                await wait_quiet(f"company run {run_id[:8]}")
        return None

    async def _finish(self, run_id: str, info: Dict[str, Any], st: Dict[str, Any], final: Dict[str, Any]) -> None:
        from core.database import CompanyAgent, CompanyCompany, CompanyIssue, CompanyRun
        from src import budgets
        from src import company as co
        from src.usage import utcnow
        owner = info["owner"]
        summary = _clean(st["text"])[:RESULT_CHARS] or None
        usage: Dict[str, Any] = {"rounds": st["round"], "tool_calls": len(st["tools"])}
        try:
            s = budgets.spent("company_run", run_id, owner or None, start=utcnow() - timedelta(days=2),
                              end=utcnow() + timedelta(minutes=5))
            usage.update(cost_usd=round(s["cost_usd"], 6), input_tokens=s["input_tokens"],
                         output_tokens=s["output_tokens"], cached_input_tokens=s["cached_input_tokens"],
                         unpriced_calls=s["unpriced_calls"])
        except Exception as e:
            logger.debug("run usage summary failed: %s", e)
        if st.get("stop_reason"):
            usage["stop_reason"] = st["stop_reason"]
        cap = truncated_at(run_id)
        if cap:
            usage["context_warning"] = (f"A model call used exactly {cap} input tokens, which looks like the model's "
                                        "context limit: the prompt and tool instructions may have been cut off. "
                                        "Give the model a larger context window.")
            logger.warning("[company] run %s: a model call used exactly %d input tokens; context may be truncated",
                           run_id[:8], cap)
        db = self._db()
        try:
            run = db.query(CompanyRun).filter(CompanyRun.id == run_id).first()
            if run is None:
                return
            held = [i for i in db.query(CompanyIssue).filter(CompanyIssue.checkout_run_id == run_id)]
            if not run.issue_id and held:
                run.issue_id = held[0].id
            target = run.issue_id
            db.commit()
            co.finish_run(db, owner, run_id, final["status"], error=final.get("error"),
                          result_summary=summary, usage=usage)
            # The result, on the issue it was about, unless the agent already said its piece there.
            commented = set((info.get("ctx") or {}).get("comments") or [])
            if final["status"] == "succeeded" and summary and target and target not in commented:
                try:
                    co.add_comment(db, owner, co.Actor("agent", info["agent_id"], run_id), target, summary)
                except co.CompanyError as e:
                    logger.debug("result comment skipped: %s", e.detail)
            agent = db.query(CompanyAgent).filter(CompanyAgent.id == info["agent_id"]).first()
            company = db.query(CompanyCompany).filter(CompanyCompany.id == info["company_id"]).first()
            if agent is not None:
                if final["status"] == "failed" and agent.status in ("running", "idle", "active"):
                    agent.status = "error"
                elif agent.status == "running":
                    prev = info.get("prev_status")
                    agent.status = prev if prev in ("active", "idle") else "idle"
                if company is not None and agent.status != "paused":
                    # Copies this month's spend onto the agent and company rows.
                    self.check_budget(db, owner, agent, company, notify_warn=False)
            co.log_activity(db, owner, run.company_id, co.Actor("system"), f"run.{final['status']}", "run", run_id,
                            {"error": final.get("error"), "cost_usd": usage.get("cost_usd"),
                             "summary": (summary or "")[:200], "context_warning": usage.get("context_warning")},
                            agent_id=info["agent_id"], run_id=run_id)
            db.commit()
            name = agent.name if agent is not None else "An agent"
        finally:
            db.close()
        if final["status"] == "failed":
            await self._notify(owner, f"{name}'s run failed", (final.get("error") or "")[:200], kind="company-run")


async def start_executor(session_manager=None) -> Optional[CompanyExecutor]:
    """Start the executor unless turned off (ODYSSEUS_COMPANY_EXECUTOR=0, or
    ODYSSEUS_INPROCESS_TASKS=0 for a deployment that runs work elsewhere)."""
    off = ("0", "false", "no", "off")
    if os.getenv("ODYSSEUS_COMPANY_EXECUTOR", "1").strip().lower() in off \
            or os.getenv("ODYSSEUS_INPROCESS_TASKS", "1").strip().lower() in off:
        logger.info("[company] executor disabled by environment")
        return None
    ex = CompanyExecutor(session_manager)
    await ex.start()
    return ex
