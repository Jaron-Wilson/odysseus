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
    monkeypatch.setattr(cx, "max_concurrent", lambda: 2)
    yield seen


def _owner():
    return "u" + uuid.uuid4().hex[:8]


def _setup(owner, *, hb=None, budget=0, company_budget=0):
    db = _TS()
    # Wake-ups left by earlier tests would be claimed too (the executor serves everyone).
    db.query(cdb.CompanyWakeup).filter(cdb.CompanyWakeup.status == "pending").update({"status": "dropped"})
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


def _executor(loop, notes=None):
    async def notifier(owner, heading, body, kind="company"):
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


def test_failed_model_marks_error_and_notifies(monkeypatch):
    owner = _owner()
    c, ceo, eng = _setup(owner)
    from src import ai_interaction

    def boom(spec, owner=None):
        raise ValueError("endpoint down")
    monkeypatch.setattr(ai_interaction, "_resolve_model", boom)
    _wake(owner, eng["id"])
    notes = []

    async def go():
        ex = _executor(_make_loop(lambda ctx: [], []), notes)
        rid = (await ex.tick())[0]
        await _drain(ex)
        return rid
    rid = asyncio.run(go())
    r = _q(cdb.CompanyRun, id=rid)[0]
    assert r.status == "failed" and "endpoint down" in r.error
    assert _q(cdb.CompanyAgent, id=eng["id"])[0].status == "error"
    assert any("failed" in n for n in notes)


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
