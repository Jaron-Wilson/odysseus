"""Company executor (src/company_executor.py) and agent tools (src/company_tools.py).

The model is faked: a stand-in for stream_agent_loop that calls Company
tools the way the real loop would (inside the run's context), so the run
lifecycle, tool attribution and limits are tested without a model server.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import timedelta

import pytest

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
from src import company as co  # noqa: E402
from src import company_executor as cx  # noqa: E402
from src import company_tools as ct  # noqa: E402

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


class FakeSM:
    """Just enough of the session manager: sessions as DB rows, messages in memory."""

    def __init__(self):
        self.messages = {}

    def create_session(self, sid, name, url, model, rag=False, owner=None, parent_session_id=None,
                       thread_anchor_id=None):
        db = _TS()
        db.add(cdb.Session(id=sid, name=name, endpoint_url=url, model=model, owner=owner,
                           parent_session_id=parent_session_id))
        db.commit()
        db.close()
        return type("S", (), {"id": sid, "headers": {}})()

    def add_message(self, sid, msg):
        self.messages.setdefault(sid, []).append(msg)

    def save_sessions(self):
        pass


@pytest.fixture(autouse=True)
def _bind(monkeypatch, tmp_path):
    from src import chat_prefs
    monkeypatch.setattr(chat_prefs, "PREFS_FILE", str(tmp_path / "chat_prefs.json"))
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    from src import usage
    monkeypatch.setattr(usage, "_session_factory", lambda: _TS())
    monkeypatch.setenv("COMPANY_FOREGROUND_GATE", "never")
    seen = {}

    def fake_resolve(spec, owner=None):
        seen["spec"] = spec
        return "http://fake/v1/chat/completions", spec.split("@")[0], {}

    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    from src import model_context
    monkeypatch.setattr(model_context, "get_context_length", lambda url, model: 32768)
    monkeypatch.setattr(cx, "max_concurrent", lambda: 2)
    yield seen


def _owner():
    return "u" + uuid.uuid4().hex[:8]


def _setup(owner, *, hb=None, budget=0, company_budget=0):
    db = _TS()
    # Wake-ups left by earlier tests would be claimed too (the executor serves everyone).
    db.query(cdb.CompanyWakeup).filter(cdb.CompanyWakeup.status == "pending").update({"status": "dropped"})
    db.query(cdb.CompanyAgent).filter(cdb.CompanyAgent.auto_paused.is_(True)).update({"auto_paused": False})
    db.commit()
    u = co.user_actor(owner)
    c = co.create_company(db, owner, u, {"name": "Acme " + owner, "monthly_budget_cents": company_budget})
    ceo = co.create_agent(db, owner, u, c["id"], {"name": "Ada", "role": "ceo", "model": "qwen3:8b@Local"})
    eng = co.create_agent(db, owner, u, c["id"], {"name": "Bo", "role": "engineer", "reports_to": ceo["id"],
                                                  "model": "qwen3:8b@Local", "heartbeat": hb,
                                                  "monthly_budget_cents": budget})
    db.close()
    return c, ceo, eng


async def _drain(ex):
    for _ in range(200):
        if not ex.runs:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("runs did not finish")


def _make_loop(script, calls):
    """A fake stream_agent_loop: `script(ctx)` runs Company tools, then it
    replies with text."""
    async def loop(url, model, messages, **kw):
        calls.append({"model": model, "messages": messages, **kw})
        ctx = ct.current()
        assert ctx is not None, "Company context must reach the agent loop"
        for tool, args in script(ctx):
            res = await asyncio.to_thread(ct.run_tool, tool, json.dumps(args))
            calls[-1].setdefault("results", []).append(res)
            yield f'data: {json.dumps({"type": "tool_output", "tool": tool, "output": res.get("output") or res.get("error")})}\n\n'
            yield f'data: {json.dumps({"type": "agent_step", "round": 2})}\n\n'
        yield f'data: {json.dumps({"delta": "Did the work. Ready for review."})}\n\n'
        yield "data: [DONE]\n\n"
    return loop


def _executor(loop, notes=None, owner=None):
    """`owner`: keep only that owner's pushes (the executor serves every
    owner, and earlier tests leave approvals and paused agents behind)."""
    async def notifier(o, heading, body, kind="company"):
        if owner is None or o == owner:
            (notes if notes is not None else []).append(heading)
    return cx.CompanyExecutor(FakeSM(), agent_loop=loop, notifier=notifier)


def _wake(owner, agent_id, source="on_demand", **kw):
    db = _TS()
    a = co.get_agent(db, owner, agent_id)
    w = co.request_wakeup(db, owner, a, source, **kw)
    db.commit()
    db.close()
    return w


def _q(model, **flt):
    db = _TS()
    try:
        return db.query(model).filter_by(**flt).all()
    finally:
        db.close()


def test_run_works_an_issue_as_the_agent_and_finishes():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    db = _TS()
    i = co.create_issue(db, owner, co.user_actor(owner), c["id"],
                        {"title": "Write docs", "status": "todo", "assignee_agent_id": eng["id"]})
    db.close()
    calls = []
    ident = i["identifier"]

    def script(ctx):
        return [(ct.CHECKOUT, {"issue": ident}),
                (ct.COMMENT, {"issue": ident, "body": "Started; @Ada please look later."}),
                (ct.UPDATE_ISSUE, {"issue": ident, "status": "in_review"})]

    async def go():
        ex = _executor(_make_loop(script, calls))
        started = await ex.tick()
        assert len(started) == 1
        await _drain(ex)
        return started[0]
    run_id = asyncio.run(go())
    assert all(r["exit_code"] == 0 for r in calls[0]["results"]), calls[0]["results"]
    run = _q(cdb.CompanyRun, id=run_id)[0]
    assert run.status == "succeeded" and run.invocation_source == "assignment" and run.issue_id == i["id"]
    assert run.session_id and "Ready for review" in run.result_summary
    iss = _q(cdb.CompanyIssue, id=i["id"])[0]
    assert iss.status == "in_review" and iss.checkout_run_id is None
    comments = _q(cdb.CompanyIssueComment, issue_id=i["id"])
    assert comments[0].author_type == "agent" and comments[0].author_id == eng["id"] and comments[0].run_id == run_id
    # The agent commented itself, so the summary is not posted again.
    assert len(comments) == 1
    acts = {a.action: a for a in _q(cdb.CompanyActivity, company_id=c["id"])}
    assert acts["issue.checked_out"].run_id == run_id and acts["issue.checked_out"].actor_type == "agent"
    # The mention woke the CEO; the agent is idle again and its run thread sits under its workspace.
    assert [w.source for w in _q(cdb.CompanyWakeup, agent_id=ceo["id"], status="pending")] == ["mention"]
    agent = _q(cdb.CompanyAgent, id=eng["id"])[0]
    assert agent.status == "idle" and agent.last_heartbeat_at and agent.session_id
    thread = _q(cdb.Session, id=run.session_id)[0]
    assert thread.parent_session_id == agent.session_id
    assert _q(cdb.Session, id=agent.session_id)[0].folder == "Company"
    # The prompt carried the instructions, org and the issue.
    sys_msg, user_msg = calls[0]["messages"][0]["content"], calls[0]["messages"][1]["content"]
    assert "You report to Ada" in sys_msg and ident in user_msg and "assigned" in user_msg
    assert "ask_user" in calls[0]["disabled_tools"]
    # The loop is told the model's window, so it doesn't trim to its 6000-token fallback.
    assert calls[0]["context_length"] == 32768 and calls[0]["auto_input_budget"] is True


def test_wakeups_for_one_agent_coalesce_into_one_run():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _wake(owner, eng["id"], "on_demand")
    _wake(owner, eng["id"], "timer")
    _wake(owner, eng["id"], "on_demand")
    calls = []

    async def go():
        ex = _executor(_make_loop(lambda ctx: [], calls))
        started = await ex.tick()
        assert await ex.tick() == []          # the agent is busy: no second run
        await _drain(ex)
        return started
    started = asyncio.run(go())
    assert len(started) == 1 and len(calls) == 1
    ws = _q(cdb.CompanyWakeup, agent_id=eng["id"])
    assert {w.status for w in ws} == {"consumed"} and {w.run_id for w in ws} == {started[0]}
    assert _q(cdb.CompanyRun, id=started[0])[0].invocation_source == "on_demand"


def test_concurrency_limit_and_one_run_per_agent(monkeypatch):
    monkeypatch.setattr(cx, "max_concurrent", lambda: 1)
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _wake(owner, eng["id"])
    _wake(owner, ceo["id"])
    gate = asyncio.Event

    async def go():
        release = gate()

        async def slow(url, model, messages, **kw):
            await release.wait()
            yield 'data: {"delta": "ok"}\n\n'
        ex = _executor(slow)
        first = await ex.tick()
        second = await ex.tick()
        assert len(first) == 1 and second == []
        release.set()
        await _drain(ex)
        third = await ex.tick()
        assert len(third) == 1
        await _drain(ex)
    asyncio.run(go())
    assert len([r for r in _q(cdb.CompanyRun, company_id=c["id"]) if r.status == "succeeded"]) == 2


def test_timer_heartbeats_respect_the_interval():
    owner = _owner()
    c, ceo, eng = _setup(owner, hb={"enabled": True, "interval_sec": 600})
    ex = _executor(None)
    now = cdb.utcnow_naive()
    assert ex.add_timer_wakeups(now=now) == 0                       # just hired
    assert ex.add_timer_wakeups(now=now + timedelta(seconds=601)) == 1
    assert ex.add_timer_wakeups(now=now + timedelta(seconds=602)) == 0   # one is already waiting
    ws = _q(cdb.CompanyWakeup, agent_id=eng["id"])
    assert [w.source for w in ws] == ["timer"]
    assert _q(cdb.CompanyWakeup, agent_id=ceo["id"]) == []         # heartbeat off by default
    # The run for it failed before it started (no last_heartbeat_at from a
    # run): the heartbeat still counts from when it fired.
    db = _TS()
    db.query(cdb.CompanyWakeup).filter_by(agent_id=eng["id"]).update({"status": "consumed"})
    db.commit()
    db.close()
    assert ex.add_timer_wakeups(now=now + timedelta(seconds=610)) == 0
    assert ex.add_timer_wakeups(now=now + timedelta(seconds=1200)) == 0
    assert ex.add_timer_wakeups(now=now + timedelta(seconds=1202)) == 1


def test_company_tools_refuse_other_companies_and_outside_a_run():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    c2, ceo2, eng2 = _setup(owner)
    db = _TS()
    other = co.create_issue(db, owner, co.user_actor(owner), c2["id"], {"title": "Theirs", "status": "todo"})
    db.close()
    assert ct.run_tool(ct.LIST_ISSUES, "{}")["exit_code"] == 1        # no run context
    ctx = {"owner": owner, "company_id": c["id"], "agent_id": eng["id"], "run_id": "x", "comments": []}
    r = ct.run_tool(ct.COMMENT, json.dumps({"issue": other["identifier"], "body": "hi"}), ctx=ctx)
    assert r["exit_code"] == 1 and "No issue" in r["error"]
    r = ct.run_tool(ct.GET_ISSUE, json.dumps({"issue": other["id"]}), ctx=ctx)
    assert r["exit_code"] == 1
    # An engineer with no reports can't open issues for others.
    r = ct.run_tool(ct.CREATE_ISSUE, json.dumps({"title": "x", "assignee": "Ada"}), ctx=ctx)
    assert r["exit_code"] == 1 and "managers" in r["error"].lower()
    # Checkout with a run id that is not a live run is refused.
    db = _TS()
    mine = co.create_issue(db, owner, co.user_actor(owner), c["id"], {"title": "Mine", "status": "todo"})
    db.close()
    r = ct.run_tool(ct.CHECKOUT, json.dumps({"issue": mine["identifier"]}), ctx=ctx)
    assert r["exit_code"] == 1 and "live run" in r["error"]
    # The CEO may delegate to a report.
    ctx_ceo = dict(ctx, agent_id=ceo["id"])
    r = ct.run_tool(ct.CREATE_ISSUE, json.dumps({"title": "Sub", "assignee": "Bo", "parent": mine["identifier"]}),
                    ctx=ctx_ceo)
    assert r["exit_code"] == 0, r
    sub = [i for i in _q(cdb.CompanyIssue, company_id=c["id"]) if i.title == "Sub"][0]
    assert sub.parent_id == mine["id"] and sub.assignee_agent_id == eng["id"] and sub.created_by_id == ceo["id"]


def test_company_tools_hidden_outside_company_runs():
    from src import agent_loop
    assert not ct.active()
    with ct.agent_context(owner="", company_id="c", agent_id="a", run_id="r"):
        assert ct.active()
    assert not ct.active()
    assert set(ct.TOOL_NAMES) <= set(agent_loop.TOOL_SECTIONS)


def test_plan_approval_with_chosen_model_runs_on_that_model(_bind):
    owner = _owner()
    c, ceo, eng = _setup(owner)
    db = _TS()
    i = co.create_issue(db, owner, co.user_actor(owner), c["id"],
                        {"title": "Big change", "status": "todo", "assignee_agent_id": eng["id"]})
    db.close()
    calls = []

    def ask(ctx):
        return [(ct.REQUEST_APPROVAL, {"type": "plan", "title": "Plan", "plan": "1. a\n2. b", "issue": i["identifier"]})]

    async def first():
        ex = _executor(_make_loop(ask, calls))
        await ex.tick()
        await _drain(ex)
    asyncio.run(first())
    ap = _q(cdb.CompanyApproval, company_id=c["id"], type="plan")[0]
    assert ap.status == "pending" and ap.requested_by_id == eng["id"] and ap.payload["plan"].startswith("1.")
    db = _TS()
    assert co.approval_defaults(db, owner, ap.id)["model"] == "qwen3:8b@Local"
    co.decide_approval(db, owner, co.user_actor(owner), ap.id, "approved", "go",
                       {"chosen_model": "gpt-5@OpenRouter", "max_steps": 7})
    db.close()

    async def second():
        ex = _executor(_make_loop(lambda ctx: [], calls))
        started = await ex.tick()
        await _drain(ex)
        return started[0]
    rid = asyncio.run(second())
    run = _q(cdb.CompanyRun, id=rid)[0]
    assert run.invocation_source == "approval" and run.model == "gpt-5@OpenRouter"
    assert calls[-1]["model"] == "gpt-5" and calls[-1]["approved_plan"].startswith("1.")
    assert calls[-1]["max_rounds"] == 7
    assert "APPROVED" in calls[-1]["messages"][1]["content"]
    db = _TS()
    # The next approval for this agent remembers the model.
    ap2 = co.create_approval(db, owner, co.Actor("agent", eng["id"], rid), c["id"],
                             {"type": "plan", "title": "Again", "payload": {"plan": "1."}})
    assert co.approval_defaults(db, owner, ap2["id"])["model"] == "gpt-5@OpenRouter"
    db.close()


def _spend(owner, agent_id, company_id, usd):
    from src.usage import record_usage
    record_usage(owner=owner, model="gpt-5", input_tokens=10, output_tokens=10, billing_type="metered",
                 cost_usd=usd, company_id=company_id, company_agent_id=agent_id, source="company")


def test_budget_hard_stop_pauses_agent_and_blocks_the_run():
    owner = _owner()
    c, ceo, eng = _setup(owner, budget=100)
    _spend(owner, eng["id"], c["id"], 1.50)
    _wake(owner, eng["id"])
    calls, notes = [], []

    async def go():
        ex = _executor(_make_loop(lambda ctx: [], calls), notes)
        started = await ex.tick()
        await asyncio.sleep(0.05)
        return started
    assert asyncio.run(go()) == []
    assert calls == []
    a = _q(cdb.CompanyAgent, id=eng["id"])[0]
    assert a.status == "paused" and "budget" in a.pause_reason.lower() and a.spent_cents == 150
    ap = _q(cdb.CompanyApproval, company_id=c["id"], type="budget_override")[0]
    assert ap.status == "pending" and ap.payload["agent_id"] == eng["id"]
    assert any("budget" in n for n in notes)
    # Approving with a higher limit resumes and wakes the agent.
    db = _TS()
    co.decide_approval(db, owner, co.user_actor(owner), ap.id, "approved", None, {"budget_cents": 500})
    db.close()
    a = _q(cdb.CompanyAgent, id=eng["id"])[0]
    assert a.status == "idle" and a.monthly_budget_cents == 500 and not a.pause_reason
    assert [w.source for w in _q(cdb.CompanyWakeup, agent_id=eng["id"], status="pending")] == ["approval"]


def test_free_models_are_never_blocked():
    owner = _owner()
    c, ceo, eng = _setup(owner, budget=1)
    from src.usage import record_usage
    record_usage(owner=owner, model="qwen3", input_tokens=5000, output_tokens=5000, billing_type="local",
                 cost_usd=0.0, company_id=c["id"], company_agent_id=eng["id"])
    _wake(owner, eng["id"])

    async def go():
        ex = _executor(_make_loop(lambda ctx: [], []))
        s = await ex.tick()
        await _drain(ex)
        return s
    assert len(asyncio.run(go())) == 1


def test_restart_marks_live_runs_interrupted_and_releases_locks():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    db = _TS()
    run = co.create_run(db, owner, eng["id"], "on_demand")
    run.status = "running"
    a = db.get(cdb.CompanyAgent, eng["id"])
    a.status = "running"
    db.commit()
    rid = run.id
    i = co.create_issue(db, owner, co.user_actor(owner), c["id"], {"title": "x", "status": "todo"})
    co.checkout_issue(db, owner, co.Actor("agent", eng["id"], rid), i["id"], eng["id"], rid)
    db.close()
    assert _executor(None).mark_interrupted() >= 1
    r = _q(cdb.CompanyRun, id=rid)[0]
    assert r.status == "interrupted" and r.finished_at and "restart" in r.error
    assert _q(cdb.CompanyIssue, id=i["id"])[0].checkout_run_id is None
    assert _q(cdb.CompanyAgent, id=eng["id"])[0].status == "idle"


def test_stop_cancels_a_running_run():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _wake(owner, eng["id"])

    async def go():
        hold = asyncio.Event()

        async def forever(url, model, messages, **kw):
            yield 'data: {"delta": "working"}\n\n'
            await hold.wait()
            yield 'data: {"delta": "never"}\n\n'
        ex = _executor(forever)
        rid = (await ex.tick())[0]
        for _ in range(100):
            if ex.runs.get(rid, {}).get("thread_id"):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert ex.stop_run(owner, rid)
        assert not ex.stop_run("someone-else", rid) or True
        await _drain(ex)
        return rid
    rid = asyncio.run(go())
    r = _q(cdb.CompanyRun, id=rid)[0]
    assert r.status == "cancelled" and r.error == "Stopped by you"
    assert _q(cdb.CompanyAgent, id=eng["id"])[0].status == "idle"


def test_failed_model_marks_error_and_notifies():
    owner = _owner()
    c, ceo, eng = _setup(owner)

    async def loop(url, model, messages, **kw):
        yield 'event: error\ndata: {"error": "Invalid API key", "status": 401}\n\n'
    _wake(owner, eng["id"])
    notes = []

    async def go():
        ex = _executor(loop, notes, owner)
        rid = (await ex.tick())[0]
        await _drain(ex)
        return rid
    rid = asyncio.run(go())
    r = _q(cdb.CompanyRun, id=rid)[0]
    assert r.status == "failed" and "Invalid API key" in r.error
    assert _q(cdb.CompanyAgent, id=eng["id"])[0].status == "error"
    assert any("failed" in n for n in notes)
    # Not an offline model: the agent is not paused and its wake-up is used up.
    assert {w.status for w in _q(cdb.CompanyWakeup, agent_id=eng["id"])} == {"consumed"}


def test_paused_agents_wakeups_are_dropped():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _wake(owner, eng["id"])
    db = _TS()
    a = db.get(cdb.CompanyAgent, eng["id"])
    a.status = "paused"
    db.commit()
    db.close()

    async def go():
        return await _executor(_make_loop(lambda ctx: [], [])).tick()
    assert asyncio.run(go()) == []
    assert {w.status for w in _q(cdb.CompanyWakeup, agent_id=eng["id"])} == {"dropped"}


def test_local_endpoint_detection():
    assert cx.is_local_endpoint("http://127.0.0.1:11434/v1")
    assert cx.is_local_endpoint("http://100.104.99.21:8000/v1")
    assert cx.is_local_endpoint("http://gpubox:8000/v1")
    assert not cx.is_local_endpoint("https://openrouter.ai/api/v1")


def test_coding_agent_setting_applies_to_the_run_chat(tmp_path):
    from src import chat_prefs
    owner = _owner()
    c, ceo, eng = _setup(owner)
    db = _TS()
    with pytest.raises(co.CompanyError):
        co.update_agent(db, owner, co.user_actor(owner), eng["id"], {"workdir": os.path.dirname(co.__file__)})
    with pytest.raises(co.CompanyError):
        co.update_agent(db, owner, co.user_actor(owner), eng["id"], {"coding_agent": "cursor"})
    co.update_agent(db, owner, co.user_actor(owner), eng["id"], {"coding_agent": "opencode", "workdir": str(tmp_path)})
    db.close()
    _wake(owner, eng["id"])
    _wake(owner, ceo["id"])
    calls = []

    async def go():
        ex = _executor(_make_loop(lambda ctx: [], calls))
        await ex.tick()
        await _drain(ex)
    asyncio.run(go())
    by_agent = {}
    for call in calls:
        by_agent["eng" if "You report to Ada" in call["messages"][0]["content"] else "ceo"] = call
    assert "claude_code" not in by_agent["eng"]["disabled_tools"] and str(tmp_path) in by_agent["eng"]["messages"][0]["content"]
    assert "claude_code" in by_agent["ceo"]["disabled_tools"]
    tid = by_agent["eng"]["session_id"]
    assert chat_prefs.engine_allowed(tid, "opencode") and not chat_prefs.engine_allowed(tid, "claude")


# ── Triage: a woken manager sees unassigned work and its reports' load ──

def _triage_setup(owner):
    c, ceo, eng = _setup(owner)
    db = _TS()
    u = co.user_actor(owner)
    qa = co.create_agent(db, owner, u, c["id"], {"name": "Cy", "role": "qa", "reports_to": ceo["id"],
                                                 "model": "qwen3:8b@Local"})
    mine = co.create_issue(db, owner, u, c["id"], {"title": "Set strategy", "status": "todo",
                                                   "assignee_agent_id": ceo["id"]})
    busy = co.create_issue(db, owner, u, c["id"], {"title": "Build login", "status": "todo",
                                                   "assignee_agent_id": eng["id"]})
    free = [co.create_issue(db, owner, u, c["id"], {"title": f"Loose end {n}", "status": "todo",
                                                    "priority": "high" if n == 3 else "medium"})
            for n in range(1, 4)]
    backlog = co.create_issue(db, owner, u, c["id"], {"title": "Someday idea", "status": "backlog"})
    person = co.create_issue(db, owner, u, c["id"], {"title": "Person's task", "status": "todo",
                                                     "assignee_user": "me"})
    done = co.create_issue(db, owner, u, c["id"], {"title": "Old done", "status": "todo"})
    co.update_issue(db, owner, u, done["id"], {"status": "in_progress"})
    co.update_issue(db, owner, u, done["id"], {"status": "done"})
    db.query(cdb.CompanyWakeup).filter(cdb.CompanyWakeup.status == "pending").update({"status": "dropped"})
    db.commit()
    db.close()
    return c, ceo, eng, qa, {"mine": mine, "busy": busy, "free": free, "backlog": backlog,
                             "person": person, "done": done}


def _prompt_for(owner, c, agent_id, source="on_demand"):
    db = _TS()
    try:
        agent = db.query(cdb.CompanyAgent).filter_by(id=agent_id).first()
        company = db.query(cdb.CompanyCompany).filter_by(id=c["id"]).first()
        wake = co.request_wakeup(db, owner, agent, source)
        db.flush()
        run = type("R", (), {"issue_id": None, "model": agent.model})()
        system, user, _, _ = cx.build_prompt(db, owner, agent, company, run, [wake])
        db.rollback()
        return system, user
    finally:
        db.close()


def test_manager_prompt_lists_unassigned_issues_and_report_workload():
    owner = _owner()
    c, ceo, eng, qa, iss = _triage_setup(owner)
    db = _TS()
    co.update_issue(db, owner, co.user_actor(owner), iss["busy"]["id"], {"status": "in_progress"})
    db.close()
    system, user = _prompt_for(owner, c, ceo["id"])
    assert "Unassigned issues (4):" in user
    for i in iss["free"] + [iss["backlog"]]:
        assert i["identifier"] in user
    # Assigned, person-held and finished issues are not up for triage.
    section = user.split("Unassigned issues")[1].split("Your reports' workload")[0]
    for k in ("mine", "busy", "person", "done"):
        assert iss[k]["identifier"] + " " not in section
    # Priority order: the high one leads.
    assert section.index(iss["free"][2]["identifier"]) < section.index(iss["free"][0]["identifier"])
    assert f"- Bo (Engineer, idle): 1 open, in progress: {iss['busy']['identifier']}" in user
    assert "- Cy (QA, idle): 0 open" in user
    assert "Triage first" in user and "company_update_issue" in user and '"assignee"' in user
    assert "triage first" in system.lower()
    assert "triaging unassigned issues" in user


def test_unassigned_list_is_capped(monkeypatch):
    monkeypatch.setattr(cx, "TRIAGE_LIMIT", 2)
    owner = _owner()
    c, ceo, eng, qa, iss = _triage_setup(owner)
    _, user = _prompt_for(owner, c, ceo["id"], source="timer")
    assert "Unassigned issues (4):" in user and "...and 2 more" in user
    assert "triage any unassigned issues" in user


def test_non_manager_prompt_has_no_triage_section():
    owner = _owner()
    c, ceo, eng, qa, iss = _triage_setup(owner)
    _, user = _prompt_for(owner, c, eng["id"])
    assert "Unassigned issues" not in user and "workload" not in user and "Triage first" not in user
    assert iss["busy"]["identifier"] in user           # its own issues are still there


def test_top_of_org_without_reports_still_triages():
    owner = _owner()
    db = _TS()
    u = co.user_actor(owner)
    c = co.create_company(db, owner, u, {"name": "Solo " + owner})
    solo = co.create_agent(db, owner, u, c["id"], {"name": "Sol", "role": "ceo", "model": "qwen3:8b@Local"})
    loose = co.create_issue(db, owner, u, c["id"], {"title": "Loose", "status": "todo"})
    db.close()
    _, user = _prompt_for(owner, c, solo["id"])
    assert loose["identifier"] in user and "Triage first" in user and "workload" not in user


def test_manager_assigns_an_unassigned_issue_to_a_report_and_wakes_them():
    owner = _owner()
    c, ceo, eng, qa, iss = _triage_setup(owner)
    target = iss["free"][0]
    calls = []

    def script(ctx):
        return [(ct.UPDATE_ISSUE, {"issue": target["identifier"], "assignee": "Bo",
                                   "comment": "Bo owns the backend."})]

    _wake(owner, ceo["id"], "on_demand")

    async def go():
        ex = _executor(_make_loop(script, calls))
        started = await ex.tick()
        await _drain(ex)
        return started
    started = asyncio.run(go())
    assert len(started) == 1
    res = calls[0]["results"][0]
    assert res["exit_code"] == 0 and "assigned to Bo" in res["output"], res
    row = _q(cdb.CompanyIssue, id=target["id"])[0]
    assert row.assignee_agent_id == eng["id"]
    wakes = _q(cdb.CompanyWakeup, agent_id=eng["id"], status="pending")
    assert [(w.source, w.issue_id) for w in wakes] == [("assignment", target["id"])]
    comments = _q(cdb.CompanyIssueComment, issue_id=target["id"])
    assert any(cm.author_id == ceo["id"] and "backend" in cm.body for cm in comments)
    acts = [a for a in _q(cdb.CompanyActivity, company_id=c["id"]) if a.action == "issue.assigned"]
    assert any(a.actor_type == "agent" for a in acts)


def test_assignee_rules_in_update_issue():
    owner = _owner()
    c, ceo, eng, qa, iss = _triage_setup(owner)
    loose = iss["free"][1]["identifier"]
    ctx_eng = {"owner": owner, "company_id": c["id"], "agent_id": eng["id"], "run_id": "x", "comments": []}
    # A non-manager can't hand work to others...
    r = ct.run_tool(ct.UPDATE_ISSUE, json.dumps({"issue": loose, "assignee": "Cy"}), ctx=ctx_eng)
    assert r["exit_code"] == 1 and "managers" in r["error"].lower()
    # ...but may pick an unassigned issue up itself.
    r = ct.run_tool(ct.UPDATE_ISSUE, json.dumps({"issue": loose, "assignee": "me"}), ctx=ctx_eng)
    assert r["exit_code"] == 0 and "assigned to you" in r["output"], r
    # A manager can't assign to someone outside its reports.
    db = _TS()
    other = co.create_agent(db, owner, co.user_actor(owner), c["id"],
                            {"name": "Dee", "role": "engineer", "reports_to": eng["id"], "model": "qwen3:8b@Local"})
    db.close()
    third = iss["free"][2]
    r = ct.run_tool(ct.UPDATE_ISSUE, json.dumps({"issue": third["identifier"], "assignee": "Dee"}),
                    ctx=dict(ctx_eng, agent_id=qa["id"]))
    assert r["exit_code"] == 1
    # The CEO reaches the whole tree below it.
    r = ct.run_tool(ct.UPDATE_ISSUE, json.dumps({"issue": third["identifier"], "assignee": "Dee"}),
                    ctx=dict(ctx_eng, agent_id=ceo["id"]))
    assert r["exit_code"] == 0, r
    assert _q(cdb.CompanyIssue, id=third["id"])[0].assignee_agent_id == other["id"]


def test_run_notes_a_suspicious_input_token_cap():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _wake(owner, eng["id"])

    async def loop(url, model, messages, **kw):
        ctx = ct.current()
        db = _TS()
        db.add(cdb.UsageEvent(id=str(uuid.uuid4()), owner=owner, source="company", company_id=ctx["company_id"],
                              company_agent_id=ctx["agent_id"], company_run_id=ctx["run_id"], model=model,
                              input_tokens=4096, output_tokens=10, idempotency_key=str(uuid.uuid4())))
        db.commit()
        db.close()
        yield 'data: {"delta": "ok"}\n\n'

    async def go():
        ex = _executor(loop)
        started = await ex.tick()
        await _drain(ex)
        return started[0]
    run_id = asyncio.run(go())
    run = _q(cdb.CompanyRun, id=run_id)[0]
    assert "4096" in (run.usage or {}).get("context_warning", "")
    assert cx.truncated_at(run_id) == 4096


# ── Focused tools and honest outcomes ──

def _assigned(owner, c, eng, title="Fix the build"):
    db = _TS()
    i = co.create_issue(db, owner, co.user_actor(owner), c["id"],
                        {"title": title, "status": "todo", "assignee_agent_id": eng["id"]})
    db.close()
    return i


def _run_with(loop, notes=None, owner=None):
    async def go():
        ex = _executor(loop, notes, owner)
        started = await ex.tick()
        await _drain(ex)
        return started[0]
    return asyncio.run(go())


def test_run_gets_a_focused_tool_set_with_the_coding_agent(tmp_path):
    owner = _owner()
    c, ceo, eng = _setup(owner)
    db = _TS()
    co.update_agent(db, owner, co.user_actor(owner), eng["id"], {"coding_agent": "claude", "workdir": str(tmp_path)})
    db.close()
    _wake(owner, eng["id"])
    _wake(owner, ceo["id"])
    calls = []

    async def go():
        ex = _executor(_make_loop(lambda ctx: [], calls))
        await ex.tick()
        await _drain(ex)
    asyncio.run(go())
    by_agent = {("eng" if "You report to Ada" in k["messages"][0]["content"] else "ceo"): k for k in calls}
    eng_tools = by_agent["eng"]["relevant_tools"]
    assert set(ct.TOOL_NAMES) <= eng_tools and "claude_code" in eng_tools
    assert {"bash", "read_file", "grep"} <= eng_tools
    assert not any(t.startswith("mcp__") for t in eng_tools) and "ask_user" not in eng_tools
    ceo_tools = by_agent["ceo"]["relevant_tools"]
    assert set(ct.TOOL_NAMES) <= ceo_tools and "claude_code" not in ceo_tools


def test_run_that_only_calls_unrelated_tools_is_no_progress_and_says_blocked():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    i = _assigned(owner, c, eng)
    notes = []

    async def loop(url, model, messages, **kw):
        for tool in ("mcp__6bbb560b__binary_session", "mcp__19d772b0__vscode_status"):
            yield f'data: {json.dumps({"type": "tool_output", "tool": tool, "output": "{}", "exit_code": 0})}\n\n'
        msg = (f"I'm blocked from starting {i['identifier']} because the Company issue tools and the "
               "required claude_code tool are not available in this run.")
        yield f'data: {json.dumps({"delta": msg})}\n\n'
        yield "data: [DONE]\n\n"
    rid = _run_with(loop, notes, owner)
    run = _q(cdb.CompanyRun, id=rid)[0]
    assert run.status == "no_progress", run.error
    assert "blocked" in run.error and "mcp__6bbb560b__binary_session" in run.error
    assert run.usage["no_progress"] is True and run.usage["blocked"] is True
    # The agent's own words land on the issue so the blocker is visible there.
    comments = _q(cdb.CompanyIssueComment, issue_id=i["id"])
    assert len(comments) == 1 and "blocked" in comments[0].body
    acts = [a for a in _q(cdb.CompanyActivity, company_id=c["id"]) if a.action == "run.no_progress"]
    assert acts and acts[0].details["blocked"] is True
    assert any("no progress" in n for n in notes)
    assert _q(cdb.CompanyAgent, id=eng["id"])[0].status == "idle"


def test_run_with_no_tool_calls_on_assigned_issue_is_flagged():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _assigned(owner, c, eng)

    async def loop(url, model, messages, **kw):
        yield 'data: {"delta": "I will look at this later."}\n\n'
    rid = _run_with(loop)
    run = _q(cdb.CompanyRun, id=rid)[0]
    assert run.status == "no_progress"
    assert run.usage["warning"] == "The agent made no tool calls"
    assert "no tool calls" in run.error


def test_on_demand_run_without_issues_still_succeeds():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _wake(owner, eng["id"])
    rid = _run_with(_make_loop(lambda ctx: [], []))
    run = _q(cdb.CompanyRun, id=rid)[0]
    assert run.status == "succeeded" and "warning" not in (run.usage or {})


def test_stalled_model_with_zero_tokens_fails():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _wake(owner, ceo["id"])
    notes = []

    async def loop(url, model, messages, **kw):
        note = "\n\n_No response from qwen3.8:27b: it accepted the request but sent nothing for 180s._"
        yield f'data: {json.dumps({"delta": note, "stall": True})}\n\n'
    rid = _run_with(loop, notes, owner)
    run = _q(cdb.CompanyRun, id=rid)[0]
    assert run.status == "failed" and "No response from qwen3.8:27b" in run.error
    assert run.usage["input_tokens"] == 0 and run.usage["output_tokens"] == 0
    acts = [a for a in _q(cdb.CompanyActivity, company_id=c["id"]) if a.action == "run.failed"]
    assert acts and "No response" in acts[0].details["error"]
    # A model that sent nothing counts as offline: one retry, no push yet,
    # the wake-up goes back to pending.
    assert notes == []
    assert {w.status for w in _q(cdb.CompanyWakeup, agent_id=ceo["id"])} == {"pending"}
    assert _q(cdb.CompanyAgent, id=ceo["id"])[0].status == "idle"


def test_empty_model_reply_with_zero_tokens_fails():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    _wake(owner, eng["id"])

    async def loop(url, model, messages, **kw):
        yield "data: [DONE]\n\n"
    rid = _run_with(loop)
    run = _q(cdb.CompanyRun, id=rid)[0]
    assert run.status == "failed" and "0 input and 0 output tokens" in run.error


def test_endpoint_with_an_impossible_port_fails_the_run(monkeypatch):
    owner = _owner()
    c, ceo, eng = _setup(owner)
    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "_resolve_model",
                        lambda spec, owner=None: ("http://totoro:114335/v1/chat/completions", "qwen3.8:27b", {}))
    _wake(owner, eng["id"])
    rid = _run_with(_make_loop(lambda ctx: [], []))
    run = _q(cdb.CompanyRun, id=rid)[0]
    assert run.status == "failed" and "65535" in run.error


def test_validate_model_spec_rejects_an_impossible_port(monkeypatch):
    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "_resolve_model",
                        lambda spec, owner=None: ("http://totoro:114335/v1/chat/completions", "qwen3.8:27b", {}))
    with pytest.raises(co.CompanyError) as e:
        co.validate_model_spec("qwen3.8:27b@totoro", _owner())
    assert e.value.status == 422 and "65535" in e.value.detail


def test_invalid_port_error():
    from src.endpoint_resolver import invalid_port_error
    assert invalid_port_error("http://totoro:114335/v1")
    assert invalid_port_error("http://totoro:0/v1")
    assert invalid_port_error("http://totoro:11434/v1") is None
    assert invalid_port_error("https://api.openai.com/v1") is None


def test_says_blocked():
    assert cx.says_blocked("I'm blocked from starting JAR-7")
    assert cx.says_blocked("this run does not expose company_checkout_issue")
    assert not cx.says_blocked("Fixed the build and moved JAR-7 to review.")


# ── Offline models: retry once, then pause the endpoint's agents ─────────

def _offline_resolver(monkeypatch, state):
    from src import ai_interaction

    def resolve(spec, owner=None):
        state["calls"] += 1
        state.setdefault("log", []).append(owner)
        if not state["up"]:
            raise ValueError(f"Model '{spec}' not found on any configured endpoint")
        return "http://fake/v1/chat/completions", spec.split("@")[0], {}
    monkeypatch.setattr(ai_interaction, "_resolve_model", resolve)


def _clocked(ex, monkeypatch):
    """Fake time for the executor and its timer heartbeats."""
    t = {"now": 1_000_000.0}
    t0, base = t["now"], cdb.utcnow_naive()
    ex._clock = lambda: t["now"]
    monkeypatch.setattr(cdb, "utcnow_naive", lambda: base + timedelta(seconds=t["now"] - t0))
    return t


async def _step(ex, t, seconds):
    t["now"] += seconds
    started = await ex.tick()
    await _drain(ex)
    return started


def test_offline_model_heartbeat_runs_twice_then_pauses_the_endpoint(monkeypatch):
    owner = _owner()
    c, ceo, eng = _setup(owner, hb={"enabled": True, "interval_sec": 1800})
    state = {"up": False, "calls": 0}
    _offline_resolver(monkeypatch, state)
    notes, calls = [], []

    async def go():
        ex = _executor(_make_loop(lambda ctx: [], calls), notes, owner)
        t = _clocked(ex, monkeypatch)
        for _ in range(600):                # an hour of ticks, one every 6 s
            await _step(ex, t, 6)
    asyncio.run(go())
    runs = _q(cdb.CompanyRun, agent_id=eng["id"])
    assert len(runs) == 2 and {r.status for r in runs} == {"failed"}     # the run and its one retry
    assert calls == []
    for a in (eng, ceo):                    # Ada never ran: paused with Bo, same endpoint
        row = _q(cdb.CompanyAgent, id=a["id"])[0]
        assert row.status == "paused" and row.auto_paused
        assert row.pause_reason == "Model offline: qwen3:8b@Local did not respond twice"
    assert notes == ["Local is offline. Paused 2 agents: Ada, Bo."]
    acts = [a.action for a in _q(cdb.CompanyActivity, company_id=c["id"])]
    assert acts.count("agent.auto_paused") == 2 and acts.count("run.failed") == 2
    # Two runs, then a probe at most every OFFLINE_PROBE_S.
    # (Agents of earlier tests with heartbeats run here too: count this owner's calls.)
    assert 2 < state["log"].count(owner) <= 2 + 3600 // cx.OFFLINE_PROBE_S + 1


def test_offline_recovery_resumes_only_auto_paused_agents_and_runs_waiting_work(monkeypatch):
    owner = _owner()
    c, ceo, eng = _setup(owner)
    db = _TS()
    u = co.user_actor(owner)
    cy = co.create_agent(db, owner, u, c["id"], {"name": "Cy", "role": "engineer", "reports_to": ceo["id"],
                                                 "model": "qwen3:8b@Local"})
    co.update_agent(db, owner, u, cy["id"], {"status": "paused"})         # paused by hand
    di = co.create_agent(db, owner, u, c["id"], {"name": "Di", "role": "engineer", "reports_to": ceo["id"],
                                                 "model": "gpt-4o@Cloud"})
    db.close()
    state = {"up": False, "calls": 0}
    _offline_resolver(monkeypatch, state)
    _wake(owner, eng["id"])
    notes, calls = [], []

    def status(a):
        row = _q(cdb.CompanyAgent, id=a["id"])[0]
        return row.status, bool(row.auto_paused)

    async def go():
        ex = _executor(_make_loop(lambda ctx: [], calls), notes, owner)
        t = _clocked(ex, monkeypatch)
        assert len(await _step(ex, t, 0)) == 1            # fails: one retry later
        assert {w.status for w in _q(cdb.CompanyWakeup, agent_id=eng["id"])} == {"pending"}
        assert status(eng) == ("idle", False) and notes == []
        assert await _step(ex, t, 10) == []               # waiting for the retry
        assert len(await _step(ex, t, cx.OFFLINE_RETRY_S)) == 1   # the retry fails too
        assert status(eng) == ("paused", True) and status(ceo) == ("paused", True)
        assert status(cy) == ("paused", False) and status(di) == ("idle", False)
        assert notes == ["Local is offline. Paused 2 agents: Ada, Bo."]
        # Work assigned meanwhile waits for the model.
        _assigned(owner, c, eng)
        for _ in range(3):
            assert await _step(ex, t, 30) == []
        ws = _q(cdb.CompanyWakeup, agent_id=eng["id"])
        assert sorted(w.source for w in ws) == ["assignment", "on_demand"]
        assert {w.status for w in ws} == {"pending"}
        state["up"] = True
        started = await _step(ex, t, cx.OFFLINE_PROBE_S)
        assert notes[:2] == ["Local is offline. Paused 2 agents: Ada, Bo.", "Local is back. Resumed 2 agents."]
        assert status(eng) == ("idle", False) and status(ceo) == ("idle", False)
        assert status(cy) == ("paused", False)            # a person's pause is theirs to lift
        assert len(started) == 1
        run = _q(cdb.CompanyRun, id=started[0])[0]
        assert run.agent_id == eng["id"] and run.invocation_source == "assignment"
        assert {w.status for w in _q(cdb.CompanyWakeup, agent_id=eng["id"])} == {"consumed"}
    asyncio.run(go())
    acts = [a.action for a in _q(cdb.CompanyActivity, company_id=c["id"])]
    assert acts.count("agent.auto_resumed") == 2


def test_resuming_by_hand_clears_auto_pause():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    db = _TS()
    a = db.get(cdb.CompanyAgent, eng["id"])
    a.status, a.pause_reason, a.auto_paused = "paused", "Model offline: x did not respond twice", True
    db.commit()
    co.update_agent(db, owner, co.user_actor(owner), eng["id"], {"status": "idle"})
    a = db.get(cdb.CompanyAgent, eng["id"])
    assert (a.status, a.pause_reason, a.auto_paused) == ("idle", None, False)
    db.close()


def test_auto_paused_agents_keep_work_but_not_heartbeats():
    owner = _owner()
    c, ceo, eng = _setup(owner, hb={"enabled": True, "interval_sec": 600})
    db = _TS()
    a = db.get(cdb.CompanyAgent, eng["id"])
    a.status, a.auto_paused = "paused", True
    assert co.request_wakeup(db, owner, a, "mention") is not None
    assert co.request_wakeup(db, owner, a, "timer") is None
    a.auto_paused = False                   # paused by a person: nothing queues
    assert co.request_wakeup(db, owner, a, "mention") is None
    db.rollback()
    db.close()


def test_pushes_are_deduplicated_and_rate_limited():
    notes = []
    ex = _executor(None, notes)
    t = {"now": 1000.0}
    ex._clock = lambda: t["now"]

    async def go():
        for _ in range(10):                 # the same failure ten times: one push
            await ex._alert("o", "Bo's run failed", "x", kind="company-run", dedupe=("failed", "bo", "x"))
        assert notes == ["Bo's run failed"]
        for i in range(8):                  # then the rate limit: 5 per window
            await ex._alert("o", f"notice {i}", "b")
        assert notes[1:] == ["notice 0", "notice 1", "notice 2", "notice 3"]
        await ex.flush_held()
        assert len(notes) == 5              # the window is still full
        t["now"] += cx.PUSH_WINDOW_S + 1
        await ex.flush_held()
        assert notes[-1] == "4 more Company notices"
        await ex.flush_held()
        assert len(notes) == 6
        t["now"] += cx.PUSH_DEDUPE_S
        await ex._alert("o", "Bo's run failed", "x", kind="company-run", dedupe=("failed", "bo", "x"))
        assert notes[-1] == "Bo's run failed"
    asyncio.run(go())


def test_repeated_failures_push_once():
    owner = _owner()
    c, ceo, eng = _setup(owner)

    async def loop(url, model, messages, **kw):
        yield 'event: error\ndata: {"error": "Invalid API key", "status": 401}\n\n'
    notes = []

    async def go():
        ex = _executor(loop, notes, owner)
        for _ in range(4):
            _wake(owner, eng["id"])
            assert len(await ex.tick()) == 1
            await _drain(ex)
    asyncio.run(go())
    assert notes == ["Bo's run failed"]
    assert len(_q(cdb.CompanyRun, agent_id=eng["id"])) == 4


def test_failure_comments_do_not_wake_their_author():
    owner = _owner()
    c, ceo, eng = _setup(owner)
    i = _assigned(owner, c, eng)
    db = _TS()
    db.query(cdb.CompanyWakeup).filter_by(agent_id=eng["id"]).update({"status": "consumed"})
    db.commit()
    co.add_comment(db, owner, co.Actor("agent", eng["id"], "r1"), i["id"],
                   "@Bo could not finish: the model is offline. @Ada FYI")
    db.close()
    assert [w.status for w in _q(cdb.CompanyWakeup, agent_id=eng["id"])] == ["consumed"]
    assert [w.source for w in _q(cdb.CompanyWakeup, agent_id=ceo["id"])] == ["mention"]


def test_endpoint_unavailable():
    for err in ("Model qwen3:8b@100.102.86.125:11434 is not available: Model 'qwen3:8b@100.102.86.125:11434' "
                "not found on any configured endpoint",
                "Cannot reach 100.102.86.125:11434", "Upstream x unreachable (cooldown active)",
                "No response from qwen3.8:27b: it accepted the request but sent nothing for 180s.",
                "[Errno 111] Connection refused"):
        assert cx.endpoint_unavailable(err), err
    for err in ("Invalid API key", "The agent run failed", None, ""):
        assert not cx.endpoint_unavailable(err)
    assert cx.endpoint_key("qwen3:8b@100.102.86.125:11434") == cx.endpoint_key("qwen3.8:27b@100.102.86.125:11434")
    assert cx.endpoint_key("qwen3:8b@100.102.86.125:11434") != cx.endpoint_key("qwen3:8b@100.102.86.125:11435")
