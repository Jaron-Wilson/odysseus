"""Company: an organization of AI agents (src/company.py, static/js/company.js, docs/company.md).

Everything is under /api/company and scoped to the signed-in owner; another
account's ids answer 404. The rules (org tree, issue transitions, checkout)
live in src/company.py so the agent executor can share them.

  Companies   GET/POST /companies, GET/PATCH/DELETE /companies/{cid}
              GET /companies/{cid}/dashboard
  Agents      GET/POST /companies/{cid}/agents, GET /companies/{cid}/org
              GET/PATCH/DELETE /agents/{aid}, POST /agents/{aid}/make-ceo, POST /agents/{aid}/wake
  Goals       GET/POST /companies/{cid}/goals, PATCH/DELETE /goals/{gid}
  Projects    GET/POST /companies/{cid}/projects, PATCH/DELETE /projects/{pid}
  Labels      GET/POST /companies/{cid}/labels, DELETE /labels/{lid}
  Issues      GET/POST /companies/{cid}/issues (filters: status, assignee_agent_id, assignee_user,
              project_id, goal_id, priority, parent_id, label_id, q), GET/PATCH/DELETE /issues/{iid}
              POST /issues/{iid}/checkout, POST /issues/{iid}/release
              POST /issues/{iid}/relations, DELETE /relations/{rid}
  Comments    GET/POST /issues/{iid}/comments, DELETE /comments/{cmid}
  Activity    GET /companies/{cid}/activity
  Runs        GET /companies/{cid}/runs, GET /runs/{rid}, GET /runs/{rid}/log, POST /runs/{rid}/stop
              GET /companies/{cid}/live (agent and run status for the page, polled)
  Wake-ups    GET /companies/{cid}/wakeups
  Approvals   GET/POST /companies/{cid}/approvals, GET /approvals/{apid}, GET /approvals/{apid}/defaults
              POST /approvals/{apid}/decide, POST /approvals/{apid}/cancel
  Settings    GET/PUT /settings (runs at once, across companies)

The executor that runs agents is src/company_executor.py; these routes nudge
it after a change that may have queued a wake-up, so it doesn't wait a tick.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request

import core.database as cdb
from src import company as co
from src.auth_helpers import require_user

PREFIX = "/api/company"


def _kick() -> None:
    try:
        from src.company_executor import get_executor
        ex = get_executor()
        if ex is not None:
            ex.kick()
    except Exception:
        pass


def _call(fn, *args, **kwargs):
    db = cdb.SessionLocal()
    try:
        return fn(db, *args, **kwargs)
    except co.CompanyError as e:
        db.rollback()
        raise HTTPException(e.status, e.detail)
    finally:
        db.close()


def _body(body: Any) -> Dict:
    if not isinstance(body, dict):
        raise HTTPException(422, "Expected a JSON object")
    return body


def _run_log(db, owner: str, rid: str) -> Dict:
    from src import agent_runs
    from src.company_executor import _collect, _new_state
    run = co.get_run(db, owner, rid)
    out = {"run": run, "messages": [], "live": None}
    sid = run.get("session_id")
    if not sid:
        return out
    from core.database import ChatMessage as DbMsg
    rows = db.query(DbMsg).filter(DbMsg.session_id == sid).order_by(DbMsg.timestamp).all()
    import json as _json
    for m in rows:
        try:
            meta = _json.loads(m.meta_data) if m.meta_data else {}
        except (ValueError, TypeError):
            meta = {}
        out["messages"].append({"role": m.role, "content": m.content or "",
                                "tools": (meta or {}).get("tool_events") or []})
    r = agent_runs._RUNS.get(sid)
    if r is not None and r.status == "running":
        st = _new_state()
        for ev in list(r.buffer):
            _collect(ev, st)
        out["live"] = {"text": st["text"], "tools": st["tools"], "round": st["round"]}
    return out


def _live(db, owner: str, cid: str, since: float) -> Dict:
    from src.company_executor import get_executor, recent_notices
    c = co.get_company(db, owner, cid)
    agents = co._company_agents(db, owner, c.id)
    runs = co._scoped(db, cdb.CompanyRun, owner).filter(cdb.CompanyRun.company_id == c.id,
                                                       cdb.CompanyRun.status.in_(co.RUN_LIVE)).all()
    by_agent = {r.agent_id: r.id for r in runs}
    pending = co._scoped(db, cdb.CompanyApproval, owner).filter(cdb.CompanyApproval.company_id == c.id,
                                                               cdb.CompanyApproval.status == "pending").count()
    waiting = co._scoped(db, cdb.CompanyWakeup, owner).filter(cdb.CompanyWakeup.company_id == c.id,
                                                             cdb.CompanyWakeup.status == "pending").all()
    import time as _time
    return {"now": _time.time(), "executor": get_executor() is not None,
            "agents": {a.id: {"status": a.status, "run_id": by_agent.get(a.id), "pause_reason": a.pause_reason or "",
                              "spent_cents": a.spent_cents if a.budget_month == co.month_key() else 0,
                              "monthly_budget_cents": a.monthly_budget_cents or 0} for a in agents},
            "runs": [co.ser_run(r) for r in runs], "pending_approvals": pending,
            "waiting_wakeups": {w.agent_id: 1 for w in waiting},
            "company": co.ser_company(c),
            "notices": recent_notices(owner or "", since)}


def setup_company_routes() -> APIRouter:
    r = APIRouter(prefix=PREFIX, tags=["company"])

    def who(request: Request):
        user = require_user(request)
        return user, co.user_actor(user)

    # ── Companies ────────────────────────────────────────────────────────
    @r.get("/companies")
    def list_companies(request: Request):
        user, _ = who(request)
        return {"companies": _call(co.list_companies, user)}

    @r.post("/companies")
    def create_company(request: Request, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.create_company, user, actor, _body(body))

    @r.get("/companies/{cid}")
    def get_company(request: Request, cid: str):
        user, _ = who(request)
        return _call(lambda db: co.ser_company(co.get_company(db, user, cid)))

    @r.patch("/companies/{cid}")
    def update_company(request: Request, cid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.update_company, user, actor, cid, _body(body))

    @r.delete("/companies/{cid}")
    def delete_company(request: Request, cid: str):
        user, actor = who(request)
        _call(co.delete_company, user, actor, cid)
        return {"ok": True}

    @r.get("/companies/{cid}/dashboard")
    def dashboard(request: Request, cid: str):
        user, _ = who(request)
        return _call(co.dashboard, user, cid)

    # ── Agents ───────────────────────────────────────────────────────────
    @r.get("/companies/{cid}/agents")
    def list_agents(request: Request, cid: str, status: Optional[str] = None):
        user, _ = who(request)
        return {"agents": _call(co.list_agents, user, cid, status)}

    @r.post("/companies/{cid}/agents")
    def create_agent(request: Request, cid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.create_agent, user, actor, cid, _body(body))

    @r.get("/companies/{cid}/org")
    def org(request: Request, cid: str):
        user, _ = who(request)
        return _call(co.org_tree, user, cid)

    @r.get("/agents/{aid}")
    def get_agent(request: Request, aid: str):
        user, _ = who(request)
        return _call(lambda db: co.ser_agent(co.get_agent(db, user, aid)))

    @r.patch("/agents/{aid}")
    def update_agent(request: Request, aid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.update_agent, user, actor, aid, _body(body))

    @r.delete("/agents/{aid}")
    def delete_agent(request: Request, aid: str):
        user, actor = who(request)
        _call(co.delete_agent, user, actor, aid)
        return {"ok": True}

    @r.post("/agents/{aid}/make-ceo")
    def make_ceo(request: Request, aid: str, body: Dict[str, Any] = Body(default_factory=dict)):
        user, actor = who(request)
        return _call(co.make_ceo, user, actor, aid, _body(body).get("old_ceo_role") or "general")

    @r.post("/agents/{aid}/wake")
    def wake(request: Request, aid: str):
        """Ask for a heartbeat now. Queued; PR C's executor picks it up."""
        user, actor = who(request)

        def go(db):
            a = co.get_agent(db, user, aid)
            w = co.request_wakeup(db, user, a, "on_demand")
            if w is None:
                raise co.CompanyError(409, f"{a.name} can't be woken ({a.status.replace('_', ' ')}"
                                           " or wake on demand is off)")
            co.log_activity(db, user, a.company_id, actor, "agent.wake_requested", "agent", a.id, agent_id=a.id)
            db.commit()
            return co.ser_wakeup(w)
        out = _call(go)
        _kick()
        return out

    @r.get("/companies/{cid}/wakeups")
    def wakeups(request: Request, cid: str, status: Optional[str] = "pending"):
        user, _ = who(request)
        return {"wakeups": _call(co.list_wakeups, user, cid, status or None)}

    # ── Goals, projects, labels ──────────────────────────────────────────
    @r.get("/companies/{cid}/goals")
    def list_goals(request: Request, cid: str):
        user, _ = who(request)
        return {"goals": _call(co.list_goals, user, cid)}

    @r.post("/companies/{cid}/goals")
    def create_goal(request: Request, cid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.create_goal, user, actor, cid, _body(body))

    @r.patch("/goals/{gid}")
    def update_goal(request: Request, gid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.update_goal, user, actor, gid, _body(body))

    @r.delete("/goals/{gid}")
    def delete_goal(request: Request, gid: str):
        user, actor = who(request)
        _call(co.delete_goal, user, actor, gid)
        return {"ok": True}

    @r.get("/companies/{cid}/projects")
    def list_projects(request: Request, cid: str):
        user, _ = who(request)
        return {"projects": _call(co.list_projects, user, cid)}

    @r.post("/companies/{cid}/projects")
    def create_project(request: Request, cid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.create_project, user, actor, cid, _body(body))

    @r.patch("/projects/{pid}")
    def update_project(request: Request, pid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.update_project, user, actor, pid, _body(body))

    @r.delete("/projects/{pid}")
    def delete_project(request: Request, pid: str):
        user, actor = who(request)
        _call(co.delete_project, user, actor, pid)
        return {"ok": True}

    @r.get("/companies/{cid}/labels")
    def list_labels(request: Request, cid: str):
        user, _ = who(request)
        return {"labels": _call(co.list_labels, user, cid)}

    @r.post("/companies/{cid}/labels")
    def create_label(request: Request, cid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.create_label, user, actor, cid, _body(body))

    @r.delete("/labels/{lid}")
    def delete_label(request: Request, lid: str):
        user, actor = who(request)
        _call(co.delete_label, user, actor, lid)
        return {"ok": True}

    # ── Issues ───────────────────────────────────────────────────────────
    @r.get("/companies/{cid}/issues")
    def list_issues(request: Request, cid: str, status: Optional[str] = None,
                    assignee_agent_id: Optional[str] = None, assignee_user: Optional[str] = None,
                    project_id: Optional[str] = None, goal_id: Optional[str] = None,
                    priority: Optional[str] = None, parent_id: Optional[str] = None,
                    label_id: Optional[str] = None, q: Optional[str] = None, limit: int = 500):
        user, _ = who(request)
        return {"issues": _call(co.list_issues, user, cid, status=status, assignee_agent_id=assignee_agent_id,
                                assignee_user=assignee_user, project_id=project_id, goal_id=goal_id,
                                priority=priority, parent_id=parent_id, label_id=label_id,
                                q=(q or "")[:200] or None, limit=limit)}

    @r.post("/companies/{cid}/issues")
    def create_issue(request: Request, cid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        out = _call(co.create_issue, user, actor, cid, _body(body))
        _kick()
        return out

    @r.get("/issues/{iid}")
    def get_issue(request: Request, iid: str):
        user, _ = who(request)
        return _call(co.get_issue, user, iid)

    @r.patch("/issues/{iid}")
    def update_issue(request: Request, iid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        out = _call(co.update_issue, user, actor, iid, _body(body))
        _kick()
        return out

    @r.delete("/issues/{iid}")
    def delete_issue(request: Request, iid: str):
        user, actor = who(request)
        _call(co.delete_issue, user, actor, iid)
        return {"ok": True}

    @r.post("/issues/{iid}/checkout")
    def checkout(request: Request, iid: str, body: Dict[str, Any] = Body(...)):
        """{agent_id, run_id, expected_statuses?}: claim the issue for one run."""
        user, actor = who(request)
        b = _body(body)
        return _call(co.checkout_issue, user, actor, iid, b.get("agent_id"), b.get("run_id"),
                     b.get("expected_statuses"))

    @r.post("/issues/{iid}/release")
    def release(request: Request, iid: str, body: Dict[str, Any] = Body(default_factory=dict)):
        """{run_id?, force?}: the holding run releases; force releases any lock."""
        user, actor = who(request)
        b = _body(body)
        return _call(co.release_issue, user, actor, iid, b.get("run_id"), bool(b.get("force")))

    @r.post("/issues/{iid}/relations")
    def add_relation(request: Request, iid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        b = _body(body)
        return _call(co.add_relation, user, actor, iid, b.get("related_issue_id"), b.get("type") or "blocks")

    @r.delete("/relations/{rid}")
    def remove_relation(request: Request, rid: str):
        user, actor = who(request)
        _call(co.remove_relation, user, actor, rid)
        return {"ok": True}

    # ── Comments ─────────────────────────────────────────────────────────
    @r.get("/issues/{iid}/comments")
    def list_comments(request: Request, iid: str):
        user, _ = who(request)
        return {"comments": _call(co.list_comments, user, iid)}

    @r.post("/issues/{iid}/comments")
    def add_comment(request: Request, iid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        out = _call(co.add_comment, user, actor, iid, _body(body).get("body"))
        _kick()
        return out

    @r.delete("/comments/{cmid}")
    def delete_comment(request: Request, cmid: str):
        user, actor = who(request)
        _call(co.delete_comment, user, actor, cmid)
        return {"ok": True}

    # ── Activity, runs ───────────────────────────────────────────────────
    @r.get("/companies/{cid}/activity")
    def activity(request: Request, cid: str, limit: int = 50, before: Optional[str] = None,
                 entity_type: Optional[str] = None, entity_id: Optional[str] = None,
                 agent_id: Optional[str] = None):
        user, _ = who(request)
        return {"activity": _call(co.list_activity, user, cid, limit=limit, before=before,
                                  entity_type=entity_type, entity_id=entity_id, agent_id=agent_id)}

    @r.get("/companies/{cid}/runs")
    def runs(request: Request, cid: str, agent_id: Optional[str] = None, issue_id: Optional[str] = None,
             status: Optional[str] = None, limit: int = 50):
        user, _ = who(request)
        return {"runs": _call(co.list_runs, user, cid, agent_id=agent_id, issue_id=issue_id,
                              status=status, limit=limit)}

    @r.get("/runs/{rid}")
    def get_run(request: Request, rid: str):
        user, _ = who(request)
        return _call(co.get_run, user, rid)

    @r.get("/runs/{rid}/log")
    def run_log(request: Request, rid: str):
        """The run's transcript: the wake message, the reply so far (live while
        it runs) and its tool calls."""
        user, _ = who(request)
        return _call(_run_log, user, rid)

    @r.post("/runs/{rid}/stop")
    def stop_run(request: Request, rid: str):
        user, actor = who(request)
        run = _call(co.get_run, user, rid)
        if run["status"] not in co.RUN_LIVE:
            raise HTTPException(409, "This run has already finished")
        from src.company_executor import get_executor
        ex = get_executor()
        if ex is not None and ex.stop_run(user or "", rid):
            return {"ok": True, "stopping": True}

        def orphan(db):
            # Not running in this process (left over): close it here.
            co.finish_run(db, user, rid, "cancelled", error="Stopped by you")
            return {"ok": True, "stopping": False}
        return _call(orphan)

    @r.get("/companies/{cid}/live")
    def live(request: Request, cid: str, since: float = 0.0):
        """Polled by the page: each agent's status and live run, runs going
        now, waiting approvals and wake-ups, and notices since `since`."""
        user, _ = who(request)
        return _call(_live, user, cid, since)

    @r.get("/settings")
    def get_settings(request: Request):
        who(request)
        from src.company_executor import default_max_rounds, max_concurrent
        return {"max_concurrent_runs": max_concurrent(), "max_rounds": default_max_rounds()}

    @r.put("/settings")
    def put_settings(request: Request, body: Dict[str, Any] = Body(...)):
        user, _ = who(request)
        from src.tool_security import owner_is_admin_or_single_user
        if not owner_is_admin_or_single_user(user):
            raise HTTPException(403, "Only an admin can change this")
        b = _body(body)
        from src.settings import load_settings, save_settings
        s = load_settings()
        for key, field, lo, hi in (("company_max_concurrent_runs", "max_concurrent_runs", 1, 8),
                                   ("company_agent_max_rounds", "max_rounds", 1, 200)):
            if field in b:
                v = b[field]
                if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                    raise HTTPException(422, f"{field} must be between {lo} and {hi}")
                s[key] = v
        save_settings(s)
        _kick()
        return get_settings(request)

    # ── Approvals ────────────────────────────────────────────────────────
    @r.get("/companies/{cid}/approvals")
    def list_approvals(request: Request, cid: str, status: Optional[str] = None):
        user, _ = who(request)
        return {"approvals": _call(co.list_approvals, user, cid, status)}

    @r.post("/companies/{cid}/approvals")
    def create_approval(request: Request, cid: str, body: Dict[str, Any] = Body(...)):
        user, actor = who(request)
        return _call(co.create_approval, user, actor, cid, _body(body))

    @r.get("/approvals/{apid}")
    def get_approval(request: Request, apid: str):
        user, _ = who(request)
        return _call(co.get_approval, user, apid)

    @r.post("/approvals/{apid}/decide")
    def decide(request: Request, apid: str, body: Dict[str, Any] = Body(...)):
        """{decision: approved | rejected | revision_requested, note?, options?}.

        options (or the same keys at the top level): for a plan or strategy,
        chosen_model (any model spec from GET /api/models), max_steps and
        max_cost_usd for the run that carries it out; for a budget_override,
        budget_cents, the new monthly limit (0 = no limit)."""
        user, actor = who(request)
        b = _body(body)
        options = b.get("options")
        if options is None:
            options = {k: b[k] for k in ("chosen_model", "max_steps", "max_cost_usd", "budget_cents") if k in b}
        out = _call(co.decide_approval, user, actor, apid, b.get("decision"), b.get("note"), options)
        _kick()
        return out

    @r.get("/approvals/{apid}/defaults")
    def approval_defaults(request: Request, apid: str):
        """What the Approve dialog starts with (the model last chosen for this agent)."""
        user, _ = who(request)
        return _call(co.approval_defaults, user, apid)

    @r.post("/approvals/{apid}/cancel")
    def cancel(request: Request, apid: str):
        user, actor = who(request)
        return _call(co.cancel_approval, user, actor, apid)

    return r
