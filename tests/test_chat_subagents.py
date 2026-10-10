"""Threads of a chat: branches and subagents (src/chat_subagents.py).

Asked for on 2026-10-10: "I want to be able to tell a chat to branch this
out, and then be able to have basically a subagent for the chat, like Claude
where I can say subagent this out, it can branch the chat, and have 'Threads'
so that I can visit different threads in each chat."

The agent loop is a fake here: each test hands spawn() a generator that
yields the SSE chunks a real run would.
"""
import asyncio
import json
import os
import uuid

import pytest

from core.database import init_db
from core.models import ChatMessage, set_session_manager_instance, get_session_manager_instance
from core.session_manager import SessionManager
import routes.thread_routes as thread_routes
from src import agent_runs, chat_subagents

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _delta(text):
    return f"data: {json.dumps({'delta': text})}\n\n"


def fake_loop(reply="Found it: three flights.", *, wait=None, fail=False):
    """A stand-in for stream_agent_loop, recording what it was given."""
    seen = {}

    async def loop(endpoint_url, model, context, **kw):
        seen.update(context=context, model=model, session_id=kw.get("session_id"))
        yield _delta("Working... ")
        if wait is not None:
            await wait.wait()
        if fail:
            raise RuntimeError("model went away")
        yield _delta(reply)
        yield "data: [DONE]\n\n"

    loop.seen = seen
    return loop


@pytest.fixture
def env(monkeypatch):
    init_db()
    sm = SessionManager()
    prev = get_session_manager_instance()
    set_session_manager_instance(sm)
    sid = "p-" + uuid.uuid4().hex[:8]
    s = sm.create_session(sid, "Costa Rica trip", "http://x/v1", "qwen3.8-27b")
    for role, text in [("user", "we fly in March"), ("assistant", "Noted: March."),
                       ("user", "what about hotels?"), ("assistant", "Arenal has a few good ones."),
                       ("user", "and flights?")]:
        s.add_message(ChatMessage(role, text))
    monkeypatch.setattr(thread_routes, "_verify_session_owner", lambda *a, **k: None)
    monkeypatch.setattr(thread_routes, "get_current_user", lambda request: None)
    # Each test starts with the default limits.
    monkeypatch.setattr(chat_subagents, "MAX_CONCURRENT", 3)
    monkeypatch.setattr(chat_subagents, "MAX_SUBAGENT_DEPTH", 2)
    monkeypatch.setattr(chat_subagents, "MAX_THREAD_DEPTH", 4)
    router = thread_routes.setup_thread_routes(sm)
    yield sm, s, router
    set_session_manager_instance(prev)


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {"content-type": "application/json"}

    async def json(self):
        return self._body


async def call(router, method, url, body=None):
    """Call a route handler in this event loop (the test database is an
    in-memory SQLite that a TestClient's worker thread cannot see)."""
    import re
    from fastapi import HTTPException
    for r in router.routes:
        if method not in getattr(r, "methods", set()):
            continue
        m = re.match("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", r.path) + "$", url)
        if m:
            try:
                return 200, await r.endpoint(_Req(body or {}), **m.groupdict())
            except HTTPException as e:
                return e.status_code, {"detail": e.detail}
    raise AssertionError(f"no route for {method} {url}")


async def _finish(thread_id):
    run = agent_runs._RUNS.get(thread_id)
    if run and run.task:
        await asyncio.wait_for(run.task, 5)


def _ids(s):
    return [m.metadata["_db_id"] for m in s.history]


# ── branches ────────────────────────────────────────────────────────────
def test_branch_copies_the_conversation_and_stays_out_of_the_parent(env):
    sm, s, router = env

    async def go():
        ids = _ids(s)
        code, t = await call(router, "POST", f"/api/session/{s.id}/branch",
                             {"anchor_msg_id": ids[3], "title": "Hotels only"})
        assert code == 200 and t["kind"] == "branch" and t["name"] == "Hotels only"
        thread = sm.get_session(t["id"])
        assert thread.parent_session_id == s.id and thread.thread_anchor_id == ids[3]
        assert [m.content for m in thread.history] == [
            "we fly in March", "Noted: March.", "what about hotels?", "Arenal has a few good ones."]
        assert all(m.metadata.get("thread_seed") for m in thread.history)
        assert len(s.history) == 5                                   # the parent is untouched
        # Without an anchor: the whole chat, named after its last question.
        code, t2 = await call(router, "POST", f"/api/session/{s.id}/branch", {})
        assert len(sm.get_session(t2["id"]).history) == 5 and t2["name"] == "Branch: and flights?"
        code, d = await call(router, "GET", f"/api/session/{s.id}/threads")
        rows = {x["id"]: x for x in d["threads"]}
        assert rows[t["id"]]["kind"] == "branch" and rows[t["id"]]["status"] == "idle"
        assert rows[t["id"]]["message_count"] == 0 and rows[t["id"]]["last_activity"]
        # A branch of a branch, and the breadcrumb back up.
        code, t3 = await call(router, "POST", f"/api/session/{t['id']}/branch", {"title": "Deeper"})
        assert code == 200
        code, where = await call(router, "GET", f"/api/session/{t3['id']}/thread-info")
        assert [p["id"] for p in where["path"]] == [s.id, t["id"], t3["id"]]
        assert where["path"][0]["kind"] == "chat" and where["kind"] == "branch"
        code, d = await call(router, "GET", f"/api/session/{s.id}/threads")
        assert {x["id"]: x for x in d["threads"]}[t["id"]]["thread_count"] == 1
        assert (await call(router, "POST", f"/api/session/{s.id}/branch", {"anchor_msg_id": "nope"}))[0] == 404

    asyncio.run(go())


def test_threads_nest_only_so_deep(env, monkeypatch):
    sm, s, router = env
    monkeypatch.setattr(chat_subagents, "MAX_THREAD_DEPTH", 2)

    async def go():
        _, a = await call(router, "POST", f"/api/session/{s.id}/branch", {})
        _, b = await call(router, "POST", f"/api/session/{a['id']}/branch", {})
        code, d = await call(router, "POST", f"/api/session/{b['id']}/branch", {})
        assert code == 400 and "at most 2" in d["detail"]

    asyncio.run(go())


def test_the_branch_tool_leaves_the_request_behind(env):
    sm, s, router = env
    s.add_message(ChatMessage("assistant", "Flights from DEN start at $420."))
    s.add_message(ChatMessage("user", "branch this out, I want to try a different route"))
    out = chat_subagents.run_branch_tool('{"title": "Different route"}', session_id=s.id)
    assert out["exit_code"] == 0 and f"(#session-{out['thread']['id']})" in out["output"]
    thread = sm.get_session(out["thread"]["id"])
    assert thread.history[-1].content == "Flights from DEN start at $420."
    assert "only works inside a chat" in chat_subagents.run_branch_tool("{}", session_id=None)["error"]
    # Another user's chat is not there.
    mine = sm.create_session("j-" + uuid.uuid4().hex[:6], "Jaron's", "http://x/v1", "m", owner="jaron")
    assert "error" in chat_subagents.run_branch_tool("{}", session_id=mine.id, owner="someone-else")
    assert "error" in chat_subagents.run_subagent_tool("x", session_id=mine.id, owner="someone-else")
    assert chat_subagents.run_branch_tool("{}", session_id=mine.id, owner="jaron")["exit_code"] == 0


# ── subagents ───────────────────────────────────────────────────────────
def test_subagent_runs_in_the_background_and_posts_its_report_back(env):
    sm, s, router = env
    loop = fake_loop("Cheapest: $420 on United, 6:05am.")

    async def go():
        t = chat_subagents.spawn(sm, s.id, "find the cheapest flights DEN to SJO in March",
                                 agent_loop=loop)
        assert t["status"] == "running" and agent_runs.is_active(t["id"])
        code, d = await call(router, "GET", f"/api/session/{s.id}/threads")
        row = d["threads"][0]
        assert (row["kind"], row["status"], row["task"]) == (
            "subagent", "running", "find the cheapest flights DEN to SJO in March")
        await _finish(t["id"])
        return t

    t = asyncio.run(go())
    thread = sm.get_session(t["id"])
    # It started from the end of the chat and the task.
    ctx = loop.seen["context"]
    assert ctx[-1]["role"] == "user" and "find the cheapest flights" in ctx[-1]["content"]
    assert [m["content"] for m in ctx[:-1]][-1] == "and flights?"
    assert loop.seen["session_id"] == t["id"]
    assert thread.history[-1].role == "assistant" and "$420 on United" in thread.history[-1].content
    # The report is in the parent, once.
    back = s.history[-1]
    assert back.metadata == {**back.metadata, "source": "subagent_result", "thread_id": t["id"], "status": "done"}
    assert back.content.startswith("[Subagent finished · ") and "$420 on United" in back.content
    assert "Working..." in back.content                     # the reply as written
    info = chat_subagents.info(t["id"])
    assert info["status"] == "done" and info["posted_back"] and info["finished_at"]
    chat_subagents.on_run_finished(t["id"], "done")        # a later run in the thread
    assert sum(1 for m in s.history if m.metadata.get("source") == "subagent_result") == 1
    assert chat_subagents.status_of(t["id"], chat_subagents.info(t["id"])) == "done"


def test_stopping_a_subagent(env):
    sm, s, router = env

    async def go():
        gate = asyncio.Event()
        t = chat_subagents.spawn(sm, s.id, "a long job", agent_loop=fake_loop(wait=gate))
        await asyncio.sleep(0.05)
        assert agent_runs.stop(t["id"]) is True           # what POST /api/chat/stop/{id} does
        await _finish(t["id"])
        return t

    t = asyncio.run(go())
    assert chat_subagents.info(t["id"])["status"] == "stopped"
    back = s.history[-1]
    assert back.metadata["source"] == "subagent_result" and back.metadata["status"] == "stopped"
    assert back.content.startswith("[Subagent stopped · ")


def test_a_failed_subagent_says_so(env):
    sm, s, router = env

    async def go():
        t = chat_subagents.spawn(sm, s.id, "doomed", agent_loop=fake_loop(fail=True))
        await _finish(t["id"])
        return t

    t = asyncio.run(go())
    assert chat_subagents.info(t["id"])["status"] == "failed"
    assert s.history[-1].content.startswith("[Subagent failed · ")


def test_at_most_three_subagents_run_per_chat(env):
    sm, s, router = env

    async def go():
        gate = asyncio.Event()
        ts = [chat_subagents.spawn(sm, s.id, f"job {i}", agent_loop=fake_loop(wait=gate)) for i in range(3)]
        code, d = await call(router, "POST", f"/api/session/{s.id}/subagents", {"task": "job 4"})
        assert code == 429 and "3 subagents are already running" in d["detail"]
        out = chat_subagents.run_subagent_tool('{"task": "job 4"}', session_id=s.id)
        assert out["exit_code"] == 1 and "already running" in out["error"]
        # Another chat has its own three.
        other = sm.create_session("o-" + uuid.uuid4().hex[:6], "Other", "http://x/v1", "m")
        chat_subagents.spawn(sm, other.id, "fine", agent_loop=fake_loop(wait=gate))
        gate.set()
        for t in ts:
            await _finish(t["id"])
        # Room again once they are done.
        t4 = chat_subagents.spawn(sm, s.id, "job 4", agent_loop=fake_loop())
        await _finish(t4["id"])
        for tid in list(agent_runs._RUNS):
            await _finish(tid)

    asyncio.run(go())


def test_subagents_nest_one_level_at_most(env):
    sm, s, router = env

    async def go():
        a = chat_subagents.spawn(sm, s.id, "level one", agent_loop=fake_loop())
        await _finish(a["id"])
        b = chat_subagents.spawn(sm, a["id"], "level two", agent_loop=fake_loop())
        await _finish(b["id"])
        with pytest.raises(chat_subagents.ThreadError) as e:
            chat_subagents.spawn(sm, b["id"], "level three", agent_loop=fake_loop())
        assert "at most 2 deep" in str(e.value)
        out = chat_subagents.run_subagent_tool("level three", session_id=b["id"])
        assert out["exit_code"] == 1 and "do this one yourself" in out["error"]
        # A branch is not a subagent: one inside a branch still has room.
        br = chat_subagents.branch(sm, s.id)
        c = chat_subagents.spawn(sm, br["id"], "from a branch", agent_loop=fake_loop())
        await _finish(c["id"])
        # b's report went to a, not to the top-level chat.
        assert sm.get_session(a["id"]).history[-1].metadata.get("thread_id") == b["id"]

    asyncio.run(go())


def test_the_subagent_route_and_tool(env, monkeypatch):
    sm, s, router = env
    started = []
    real_spawn = chat_subagents.spawn

    def spawn(*a, **k):
        k["agent_loop"] = fake_loop()
        t = real_spawn(*a, **k)
        started.append(t["id"])
        return t

    monkeypatch.setattr(chat_subagents, "spawn", spawn)

    async def go():
        code, t = await call(router, "POST", f"/api/session/{s.id}/subagents",
                             {"task": "summarize hotels", "title": "Hotels"})
        assert code == 200 and t["name"] == "Hotels" and t["kind"] == "subagent"
        assert (await call(router, "POST", f"/api/session/{s.id}/subagents", {"task": "  "}))[0] == 400
        out = chat_subagents.run_subagent_tool('{"task": "compare car rentals", "title": "Cars"}',
                                               session_id=s.id)
        assert out["exit_code"] == 0 and "posted back to this chat" in out["output"]
        assert f"(#session-{out['thread']['id']})" in out["output"]
        for tid in started:
            await _finish(tid)
        code, d = await call(router, "GET", f"/api/session/{s.id}/threads")
        assert sorted((x["name"], x["status"]) for x in d["threads"]) == [("Cars", "done"), ("Hotels", "done")]
        assert all(x["result"] for x in d["threads"]) and d["limits"]["max_concurrent"] == 3

    asyncio.run(go())


def test_a_restart_reads_as_stopped(env):
    sm, s, router = env
    br = chat_subagents.branch(sm, s.id)
    chat_subagents.save_info(br["id"], kind="subagent", status="running")
    assert chat_subagents.status_of(br["id"], chat_subagents.info(br["id"])) == "stopped"


def test_wired_everywhere():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    from src.agent_tools import TOOL_TAGS
    from src.agent_loop import TOOL_SECTIONS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_policy import known_tool_names
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    for tool in ("branch_thread", "spawn_subagent"):
        assert tool in TOOL_TAGS and tool in TOOL_SECTIONS and tool in BUILTIN_TOOL_DESCRIPTIONS
        assert tool in names and tool in known_tool_names()
    assert "chat_subagents.on_run_finished(session_id, run.status)" in read("src", "agent_runs.py")
    assert 'elif tool in ("branch_thread", "spawn_subagent"):' in read("src", "tool_execution.py")
    slash = read("static", "js", "slashCommands.js")
    assert "branch: {" in slash and "subagent: {" in slash and "alias: ['sub']" in slash
    threads = read("static", "js", "chatThreads.js")
    assert "Branch from here" in threads and "/subagents" in threads and "/api/chat/stop/" in threads
    assert "Subagent (finished|failed|stopped)" in read("static", "js", "chatRenderer.js")


def test_asking_in_plain_words_offers_the_tools():
    from src.agent_loop import _classify_agent_request
    from src.tool_index import ToolIndex
    for text in ("Can you subagent this out?", "subagent out the research", "let's branch this out",
                 "branch off and try Postgres"):
        intent = _classify_agent_request([], text)
        assert "threads" in intent["domains"] and not intent["low_signal"], text
    hinted = set()
    for kws, tools in ToolIndex._KEYWORD_HINTS.items():
        if any(k in "please subagent this out" for k in kws):
            hinted |= tools
    assert {"branch_thread", "spawn_subagent"} <= hinted
    assert "threads" not in _classify_agent_request([], "what branch is the repo on?")["domains"]
