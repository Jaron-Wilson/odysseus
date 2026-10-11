"""Company in regular chats (src/company_chat.py, src/agent_tools/company_chat_tools.py).

What matters:

  * @mentions of a company, an agent (by name, role or PREFIX/role) and issue
    identifiers resolve against the owner's own companies only; a name that
    fits several things comes back ambiguous unless the message settles it;
  * the per-turn snapshot carries the facts asked for and nothing of another
    owner's, and is added to the turn's user message only;
  * each chat tool acts as the user through src/company.py and lands in the
    activity log; write tools are side-effect tools (blocked in plan mode);
  * the tools are hidden for an owner with no company and pulled in when a
    message mentions the Company.
"""
import json
import sys
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
from src import company as co  # noqa: E402
from src import company_chat as cc  # noqa: E402
from src.agent_tools import company_chat_tools as tools  # noqa: E402

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)
MODEL = "qwen3:8b@Local Ollama"


@pytest.fixture(autouse=True)
def _bind(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)

    def fake_resolve(spec, owner=None):
        if spec != MODEL:
            raise ValueError("no such model")
        return "http://x/v1/chat/completions", spec.split("@")[0], {}

    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    yield


def _owner():
    return "u" + uuid.uuid4().hex[:8]


def _org(owner, name="Acme Labs", prefix=None, ceo="Ada"):
    """A company with a CEO and an engineer, and two issues."""
    db = _TS()
    try:
        me = co.user_actor(owner)
        c = co.create_company(db, owner, me, {"name": name, **({"issue_prefix": prefix} if prefix else {})})
        boss = co.create_agent(db, owner, me, c["id"], {"name": ceo, "role": "ceo", "model": MODEL})
        eng = co.create_agent(db, owner, me, c["id"], {"name": "Grace Hopper", "role": "engineer",
                                                       "model": MODEL, "reports_to": boss["id"]})
        i1 = co.create_issue(db, owner, me, c["id"], {"title": "Fix the login page", "priority": "high",
                                                      "assignee_agent_id": boss["id"], "status": "todo"})
        i2 = co.create_issue(db, owner, me, c["id"], {"title": "Write docs"})
        return {"company": c, "ceo": boss, "eng": eng, "i1": i1, "i2": i2}
    finally:
        db.close()


def _parse(owner, text):
    db = _TS()
    try:
        return cc.parse_mentions(db, owner, text)
    finally:
        db.close()


def _activity(owner, cid):
    db = _TS()
    try:
        return co.list_activity(db, owner, cid, limit=50)
    finally:
        db.close()


# ── Mentions ─────────────────────────────────────────────────────────────

def test_mentions_company_agent_role_and_issue():
    o = _owner()
    org = _org(o, prefix="JAR")
    ms = _parse(o, "@Acme Labs: can @CEO and @grace-hopper look at JAR-1 and @JAR-2?")
    got = [(m.kind, m.id, m.label) for m in ms]
    assert got == [
        ("company", org["company"]["id"], "@Acme Labs"),
        ("agent", org["ceo"]["id"], "@CEO"),
        ("agent", org["eng"]["id"], "@grace-hopper"),
        ("issue", org["i1"]["id"], "JAR-1"),
        ("issue", org["i2"]["id"], "@JAR-2"),
    ]


def test_mentions_ignore_emails_unknown_prefixes_and_missing_issues():
    o = _owner()
    _org(o, prefix="JAR")
    assert _parse(o, "mail ada@acme.com about COVID-19 and JAR-99, or @Adams") == []


def test_longest_name_wins():
    o = _owner()
    org = _org(o, prefix="JAR")
    db = _TS()
    try:
        co.create_agent(db, o, co.user_actor(o), org["company"]["id"],
                        {"name": "Grace", "role": "qa", "model": MODEL, "reports_to": org["ceo"]["id"]})
    finally:
        db.close()
    (m,) = _parse(o, "ask @Grace Hopper")
    assert m.id == org["eng"]["id"]


def test_ambiguous_role_across_companies_and_how_it_settles():
    o = _owner()
    a = _org(o, "Acme Labs", "JAR", ceo="Ada")
    b = _org(o, "Blue Moon", "BLU", ceo="Bob")
    (m,) = _parse(o, "what is @CEO doing")
    assert m.kind == "ambiguous"
    assert {c["id"] for c in m.candidates} == {a["ceo"]["id"], b["ceo"]["id"]}
    # The rest of the message names one company: that settles it.
    ms = _parse(o, "@CEO, look at BLU-1")
    assert ms[0].kind == "agent" and ms[0].id == b["ceo"]["id"]
    # PREFIX/role picks explicitly.
    (m,) = _parse(o, "ping @JAR/CEO please")
    assert m.kind == "agent" and m.id == a["ceo"]["id"] and m.label == "@JAR/CEO"


def test_mentions_are_owner_scoped():
    mine, theirs = _owner(), _owner()
    _org(theirs, "Acme Labs", "JAR")
    assert _parse(mine, "@CEO JAR-1 @Acme Labs") == []


# ── Context snapshot ─────────────────────────────────────────────────────

def test_snapshot_contents():
    o = _owner()
    org = _org(o, prefix="JAR")
    db = _TS()
    try:
        co.add_comment(db, o, co.user_actor(o), org["i1"]["id"], "Please look at the redirect")
        co.add_comment(db, o, co.Actor("agent", org["ceo"]["id"]), org["i1"]["id"], "On it, checking the session cookie")
        co.create_goal(db, o, co.user_actor(o), org["company"]["id"], {"title": "Ship v1", "status": "active"})
        run = co.create_run(db, o, org["ceo"]["id"], "on_demand")
        co.finish_run(db, o, run.id, "succeeded", result_summary="Triaged the login bug")
        ms = cc.parse_mentions(db, o, "@Acme Labs @CEO JAR-1")
        text = cc.build_context(db, o, ms)
    finally:
        db.close()
    assert text.startswith("<company_context>") and text.endswith("</company_context>")
    assert "Issue JAR-1: Fix the login page" in text
    assert "status todo, priority high, assignee Ada (CEO)" in text
    assert "On it, checking the session cookie" in text and "Ada (CEO), agent" in text
    assert "Agent Ada: CEO" in text and f"model {MODEL}" in text
    assert "current issues: JAR-1 Fix the login page (todo)" in text
    assert "last run: succeeded" in text and "Triaged the login bug" in text
    assert "Company Acme Labs (issue prefix JAR" in text and "Ship v1 (active)" in text
    assert "open issues: backlog 1, todo 1" in text
    assert len(text) < cc.MAX_CONTEXT_CHARS + 500


def test_ambiguous_mention_is_explained_not_guessed():
    o = _owner()
    _org(o, "Acme Labs", "JAR")
    _org(o, "Blue Moon", "BLU", ceo="Bob")
    db = _TS()
    try:
        text = cc.build_context(db, o, cc.parse_mentions(db, o, "how is @CEO"))
    finally:
        db.close()
    assert "@CEO is ambiguous" in text and "Acme Labs" in text and "Blue Moon" in text
    assert "Agent Ada" not in text


def test_apply_mentions_adds_to_this_turn_only():
    o = _owner()
    _org(o, prefix="JAR")
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "ok"}, {"role": "user", "content": "status of JAR-1?"}]
    got = cc.apply_mentions(msgs, "status of JAR-1?", o)
    assert [m["kind"] for m in got] == ["issue"]
    assert msgs[3]["content"].startswith("status of JAR-1?\n\n<company_context>")
    assert msgs[1]["content"] == "earlier" and msgs[0]["content"] == "sys"
    # Another owner's message mentioning the same identifier gets nothing.
    other = [{"role": "user", "content": "status of JAR-1?"}]
    assert cc.apply_mentions(other, "status of JAR-1?", _owner()) == []
    assert other[0]["content"] == "status of JAR-1?"


def test_wants_company_action():
    o = _owner()
    _org(o, prefix="JAR")
    assert cc.wants_company_action("assign JAR-2 to @CEO", o)
    assert not cc.wants_company_action("what is JAR-2 about?", o)
    assert not cc.wants_company_action("assign JAR-2 to @CEO", _owner())


# ── Tools ────────────────────────────────────────────────────────────────

def _run(name, o, **args):
    return tools.run_tool(name, json.dumps(args), o)


def test_read_tools():
    o = _owner()
    org = _org(o, prefix="JAR")
    r = _run("company_overview", o)
    assert r["exit_code"] == 0 and "Company Acme Labs" in r["output"] and "JAR-1 [todo, high, Ada]" in r["output"]
    r = _run("company_issue_list", o, assignee="CEO")
    assert "JAR-1" in r["output"] and "JAR-2" not in r["output"]
    r = _run("company_issue_get", o, issue="jar-1")
    assert "Fix the login page" in r["output"]
    r = _run("company_agent_status", o, agent="the CEO")
    assert "Agent Ada" in r["output"] and "JAR-1" in r["output"]
    assert _run("company_issue_get", _owner(), issue="JAR-1")["exit_code"] == 1   # another owner
    assert org


def test_write_tools_act_as_the_user_and_log_activity():
    o = _owner()
    org = _org(o, prefix="JAR")
    cid = org["company"]["id"]

    r = _run("company_issue_create", o, title="Add dark mode", description="From chat", assignee="Grace Hopper",
             priority="low")
    assert r["exit_code"] == 0 and "Created JAR-3" in r["output"] and "Grace Hopper" in r["output"]
    r = _run("company_issue_update", o, issue="JAR-2", assignee="CEO", status="todo")
    assert r["exit_code"] == 0 and "assigned to Ada" in r["output"]
    r = _run("company_issue_comment", o, issue="JAR-1", body="@Grace Hopper can you pair on this?")
    assert r["exit_code"] == 0 and "Grace Hopper" in r["output"]
    r = _run("company_agent_wake", o, agent="CEO", issue="JAR-1")
    assert r["exit_code"] == 0 and "Woke Ada" in r["output"]

    db = _TS()
    try:
        ap = co.create_approval(db, o, co.Actor("agent", org["ceo"]["id"]), cid,
                                {"type": "strategy", "title": "Focus on onboarding"})
        i2 = co.get_issue(db, o, org["i2"]["id"])
        wakes = co.list_wakeups(db, o, cid)
    finally:
        db.close()
    assert i2["assignee_agent_id"] == org["ceo"]["id"] and i2["status"] == "todo"
    assert {w["source"] for w in wakes} >= {"assignment", "mention", "on_demand"}
    r = _run("company_approval_decide", o, approval="onboarding", decision="approved", note="Go")
    assert r["exit_code"] == 0 and "approved" in r["output"]
    assert ap

    acts = _activity(o, cid)
    by_action = {}
    for a in acts:
        by_action.setdefault(a["action"], a)
    for action in ("issue.created", "issue.assigned", "comment.created", "agent.wake_requested", "approval.approved"):
        assert action in by_action, action
        assert by_action[action]["actor_type"] == "user" and by_action[action]["actor_id"] == o


def test_tool_errors_are_plain():
    o = _owner()
    _org(o, prefix="JAR")
    r = _run("company_issue_update", o, issue="JAR-1", status="done")      # todo -> done not allowed
    assert r["exit_code"] == 1 and "error" in r
    r = _run("company_issue_update", o, issue="JAR-404", status="todo")
    assert r["exit_code"] == 1 and "No issue" in r["error"]
    _org(o, "Blue Moon", "BLU", ceo="Bob")
    r = _run("company_agent_wake", o, agent="CEO")
    assert r["exit_code"] == 1 and "JAR/Ada" in r["error"] and "BLU/Bob" in r["error"]
    r = _run("company_issue_create", o, title="Which company?")
    assert r["exit_code"] == 1 and "Which company" in r["error"]


def test_write_tools_are_mutators_and_read_tools_are_safe():
    from src.tool_security import PLAN_MODE_READONLY_TOOLS, _PLAN_MODE_KNOWN_MUTATORS, plan_mode_disabled_tools
    assert tools.READ_TOOLS <= PLAN_MODE_READONLY_TOOLS
    assert tools.WRITE_TOOLS <= _PLAN_MODE_KNOWN_MUTATORS
    blocked = plan_mode_disabled_tools()
    assert tools.WRITE_TOOLS <= blocked and not (tools.READ_TOOLS & blocked)
    assert not (tools.ALL_TOOLS & PLAN_MODE_READONLY_TOOLS & _PLAN_MODE_KNOWN_MUTATORS)


def test_tools_are_registered():
    import src.agent_tools as at
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.agent_loop import TOOL_SECTIONS
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert tools.ALL_TOOLS <= names and tools.ALL_TOOLS <= at.TOOL_TAGS
    assert tools.ALL_TOOLS <= set(BUILTIN_TOOL_DESCRIPTIONS) and tools.ALL_TOOLS <= set(TOOL_SECTIONS)
    block = function_call_to_tool_block("company_issue_get", '{"issue": "JAR-1"}')
    assert block.tool_type == "company_issue_get" and json.loads(block.content) == {"issue": "JAR-1"}


@pytest.mark.asyncio
async def test_dispatch_through_execute_tool_block_and_plan_mode_block():
    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block
    from src.tool_security import plan_mode_disabled_tools
    o = _owner()
    _org(o, prefix="JAR")
    desc, res = await execute_tool_block(ToolBlock("company_issue_comment", '{"issue": "JAR-1", "body": "hi"}'), owner=o)
    assert res["exit_code"] == 0 and "Commented on JAR-1" in res["output"]
    desc, res = await execute_tool_block(ToolBlock("company_issue_comment", '{"issue": "JAR-1", "body": "hi"}'),
                                         owner=o, disabled_tools=plan_mode_disabled_tools())
    assert res["exit_code"] == 1 and "disabled" in res["error"]
    desc, res = await execute_tool_block(ToolBlock("company_issue_get", '{"issue": "JAR-1"}'),
                                         owner=o, disabled_tools=plan_mode_disabled_tools())
    assert res["exit_code"] == 0


def test_tools_hidden_without_a_company_and_pulled_in_by_mentions():
    nobody, o = _owner(), _owner()
    _org(o, prefix="JAR")
    assert tools.hidden_tools(nobody) == set(tools.ALL_TOOLS)
    assert tools.hidden_tools(o) == set()
    assert tools.relevant_tools(nobody, "assign JAR-1 to @CEO") == set()
    assert tools.relevant_tools(o, "assign JAR-1 to @CEO") == set(tools.ALL_TOOLS)
    assert tools.relevant_tools(o, "ask @Ada about it") == set(tools.ALL_TOOLS)
    assert tools.relevant_tools(o, "what's the weather") == set()


def test_mention_directory_route():
    o = _owner()
    _org(o, prefix="JAR")
    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    from routes.company_chat_routes import setup_company_chat_routes
    app.include_router(setup_company_chat_routes())
    with TestClient(app) as c:
        d = c.get("/api/company/mentions", headers={"x-test-user": o}).json()
        assert [x["prefix"] for x in d["companies"]] == ["JAR"]
        assert {a["name"] for a in d["agents"]} == {"Ada", "Grace Hopper"}
        assert {i["identifier"] for i in d["issues"]} == {"JAR-1", "JAR-2"}
        empty = c.get("/api/company/mentions", headers={"x-test-user": _owner()}).json()
        assert empty == {"companies": [], "agents": [], "issues": []}


def _tools_sent(monkeypatch, owner, text, *, company_run=False):
    """Drive the real stream_agent_loop with a fake model; return the tool
    names it was offered on the first round."""
    import asyncio
    import src.agent_loop as al
    from src import company_tools
    sent = []
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)

    async def fake_stream(_candidates, messages, **kw):
        sent.append({(t.get("function") or {}).get("name") for t in kw.get("tools") or []})
        yield f'data: {json.dumps({"delta": "ok"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream)

    async def go():
        gen = al.stream_agent_loop("http://127.0.0.1:9/v1", "gpt-4o", [{"role": "user", "content": text}],
                                   owner=owner, max_rounds=1)
        return [c async for c in gen]

    if company_run:
        with company_tools.agent_context(owner=owner, company_id="c", agent_id="a", run_id="r"):
            asyncio.run(go())
    else:
        asyncio.run(go())
    assert sent, "the model was never called"
    return sent[0]


def test_agent_loop_offers_chat_tools_in_chats_only(monkeypatch):
    from src import company_tools
    o = _owner()
    _org(o, prefix="JAR")
    # A chat about the Company: the user's tools, never the agents' own.
    names = _tools_sent(monkeypatch, o, "please assign JAR-1 to @CEO")
    assert tools.ALL_TOOLS <= names
    assert not (set(company_tools.TOOL_NAMES) & names)
    # No company: none of them.
    names = _tools_sent(monkeypatch, _owner(), "please assign JAR-1 to @CEO")
    assert not (tools.ALL_TOOLS & names)
    # Inside a Company agent's run: the agent acts as itself, never as the user.
    names = _tools_sent(monkeypatch, o, "please assign JAR-1 to @CEO", company_run=True)
    assert not (tools.ALL_TOOLS & names)
    assert set(company_tools.TOOL_NAMES) <= names
