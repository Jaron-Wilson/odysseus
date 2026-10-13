"""`/claude direct`: a chat whose replies come straight from its attached
Claude Code session (src/claude_direct.py).

Asked for on 2026-10-13: "any way we could put this in a seperate chat on my
odysseus server so i can chat with it and it uses this claude code session
only to respond?" What matters:

  * direct mode needs an attached session, is kept per chat, and attaching
    again or detaching ends it; the chat's model is no longer told about the
    session, since it no longer answers;
  * a direct turn resumes THAT session in its own folder, sends the message
    as the prompt, streams Claude's words unaltered and saves both sides;
  * one turn at a time per session - a second is refused, not run beside it;
  * a new session id from the CLI is followed; a missing CLI says so;
  * only an admin, and the chat route checks direct mode before any model.

The claude CLI here is a fake: nothing runs against a real session.
"""
import asyncio
import json
import os
import stat
import sys

import pytest

from src import claude_attach
from src import claude_direct
from src import claude_agent_view as view
from tests.test_claude_attach import SID, PROJ, _client, _write_session, home  # noqa: F401

NEW_SID = "7238cfa3-e12f-4867-bccd-ba64f3e00799"

FAKE = r'''#!{py}
import json, os, sys, time
argv = sys.argv[1:]
state = {state!r}
agents = json.load(open(state)) if os.path.exists(state) else []
def save(): json.dump(agents, open(state, "w"))
def log(prompt=""):
    open({log!r}, "a").write(json.dumps({{"argv": argv, "cwd": os.getcwd(), "prompt": prompt}}) + "\n")
if argv[:1] == ["agents"]:
    print(json.dumps(agents)); sys.exit(0)
if argv[:1] == ["stop"]:
    log()
    for a in agents:
        if a["id"] == argv[1]: a["pid"] = None; a["status"] = None
    save(); print("stopped " + argv[1]); sys.exit(0)
if "--bg" in argv:
    log()
    sid = argv[argv.index("--resume") + 1]
    for a in agents:
        if a["sessionId"] == sid: a["pid"] = 4242; a["status"] = "idle"
    save(); print("backgrounded · " + sid[:8]); sys.exit(0)
prompt = sys.stdin.read()
log(prompt)
if any(a["sessionId"] == argv[argv.index("--resume") + 1] and a.get("pid") for a in agents):
    print("Error: That session is running in the background.", file=sys.stderr); sys.exit(1)
sid = os.environ.get("FAKE_NEW_SID") or argv[argv.index("--resume") + 1]
def say(obj): print(json.dumps(obj), flush=True)
say({{"type": "system", "subtype": "init", "session_id": sid}})
say({{"type": "assistant", "session_id": sid, "message": {{"content": [
    {{"type": "tool_use", "name": "Bash", "input": {{"command": "git status"}}}}]}}}})
if os.environ.get("FAKE_FIRST"):
    say({{"type": "assistant", "session_id": sid, "message": {{"content": [
        {{"type": "text", "text": os.environ["FAKE_FIRST"]}}]}}}})
time.sleep(float(os.environ.get("FAKE_SLEEP") or 0))
say({{"type": "assistant", "session_id": sid, "message": {{"content": [
    {{"type": "text", "text": "All 7 emails went out. *Nothing* reworded."}}]}}}})
say({{"type": "result", "session_id": sid, "result": "All 7 emails went out.", "is_error": False,
      "total_cost_usd": 0.42}})
'''


@pytest.fixture
def cli(home, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    state = tmp_path / "agents.json"
    exe = bindir / "claude"
    exe.write_text(FAKE.format(py=sys.executable, log=str(log), state=str(state)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    claude_direct._running.clear()
    monkeypatch.setattr(view, "ENABLED", True)
    monkeypatch.setattr(view, "_LIST_TTL_S", 0.0)
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"projects": {}}))
    monkeypatch.setattr(view, "CLAUDE_JSON", str(cj))
    return log


def _agent(cli_log, status):
    """This session as a background agent with its own id, as `claude agents` lists it."""
    state = cli_log.parent / "agents.json"
    state.write_text(json.dumps([{"id": "c06db0e3", "sessionId": SID, "kind": "background",
                                  "pid": 1143561, "status": status, "name": "pearson-figure-descriptions"}]))
    return state


class _Sess:
    def __init__(self):
        self.history = []

    def add_message(self, m):
        self.history.append(m)


class _Mgr:
    saves = 0

    def save_sessions(self):
        _Mgr.saves += 1


async def _collect(chat_id, sess, message):
    return [c async for c in claude_direct.stream(chat_id, sess, message, _Mgr())]


def _deltas(chunks, thinking=False):
    out = []
    for c in chunks:
        if c.startswith("data: {"):
            d = json.loads(c[6:])
            if "delta" in d and bool(d.get("thinking")) == thinking:
                out.append(d["delta"])
    return "".join(out)


# ── The switch ──────────────────────────────────────────────────────────

def test_direct_needs_an_attached_session_and_attaching_or_detaching_ends_it(home, monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/claude_attach/chat-1/direct", json={"on": True})
    assert r.status_code == 409 and "attach" in r.json()["detail"]["error"].lower()
    c.post("/api/claude_attach/chat-1", json={"id": "7238cfa3"})
    assert claude_attach.context_note("chat-1")             # the model is told of it
    r = c.post("/api/claude_attach/chat-1/direct", json={"on": True})
    assert r.status_code == 200 and r.json()["attached"]["direct"] is True
    assert claude_direct.is_on("chat-1")
    assert claude_attach.context_note("chat-1") == ""       # ...not once it no longer answers
    assert c.post("/api/claude_attach/chat-1/direct", json={"on": False}).json()["attached"]["direct"] is False
    assert not claude_direct.is_on("chat-1")
    c.post("/api/claude_attach/chat-1/direct", json={"on": True})
    c.post("/api/claude_attach/chat-1", json={"id": "7238cfa3"})
    assert not claude_direct.is_on("chat-1"), "attaching again starts with direct off"
    c.post("/api/claude_attach/chat-1/direct", json={"on": True})
    c.delete("/api/claude_attach/chat-1")
    assert not claude_direct.is_on("chat-1")


def test_only_an_admin_on_their_own_chat(home, monkeypatch):
    claude_attach.attach("chat-1", "7238cfa3")
    assert _client(monkeypatch, user="guest").post("/api/claude_attach/chat-1/direct", json={"on": True}).status_code == 403
    assert _client(monkeypatch).post("/api/claude_attach/theirs/direct", json={"on": True}).status_code == 404
    assert not claude_direct.is_on("chat-1")


# ── A turn ──────────────────────────────────────────────────────────────

def test_a_turn_resumes_the_session_and_its_words_are_the_reply(home, cli):
    _root, proj_a, _ = home
    claude_attach.attach("chat-1", "7238cfa3")
    claude_attach.set_direct("chat-1", True)
    sess = _Sess()
    chunks = asyncio.run(_collect("chat-1", sess, "did they send?"))
    assert _deltas(chunks) == "All 7 emails went out. *Nothing* reworded."
    assert "git status" in _deltas(chunks, thinking=True), "tool calls show as progress, not as the reply"
    assert chunks[-1] == "data: [DONE]\n\n"
    call = json.loads(cli.read_text().splitlines()[0])
    assert call["argv"][call["argv"].index("--resume") + 1] == SID
    assert call["argv"][call["argv"].index("--permission-mode") + 1] == "auto"
    assert call["cwd"] == str(proj_a) and call["prompt"] == "did they send?"
    assert [(m.role, m.content) for m in sess.history] == [
        ("user", "did they send?"), ("assistant", "All 7 emails went out. *Nothing* reworded.")]
    assert sess.history[1].metadata["model"] == claude_direct.MODEL_LABEL
    assert sess.history[1].metadata["claude_direct"]["cost_usd"] == 0.42


def test_a_second_message_while_one_runs_is_refused(home, cli, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "1.0")
    claude_attach.attach("chat-1", "7238cfa3")
    claude_attach.set_direct("chat-1", True)

    async def both():
        first = asyncio.create_task(_collect("chat-1", _Sess(), "one"))
        await asyncio.sleep(0.3)
        second_sess = _Sess()
        second = await _collect("chat-1", second_sess, "two")
        return await first, second, second_sess

    first, second, second_sess = asyncio.run(both())
    assert "still answering" in _deltas(second) and second_sess.history == []
    # The page is told, so it shows the running reply instead of an error.
    busy = [json.loads(c[6:]) for c in second if '"claude_direct_busy"' in c]
    assert busy and busy[0]["live"]["running"] is True
    assert "All 7 emails" in _deltas(first)
    assert len(cli.read_text().splitlines()) == 1, "never two writers to one session"


def test_a_new_session_id_is_followed(home, cli, monkeypatch):
    root, proj_a, _ = home
    _write_session(root, PROJ, NEW_SID, str(proj_a))
    monkeypatch.setenv("FAKE_NEW_SID", NEW_SID)
    claude_attach.attach("chat-1", SID)
    claude_attach.set_direct("chat-1", True)
    asyncio.run(_collect("chat-1", _Sess(), "carry on"))
    a = claude_attach.get("chat-1")
    assert a["session_id"] == NEW_SID and a["direct"] is True


def test_no_cli_says_so_and_still_saves(home, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    claude_attach.attach("chat-1", "7238cfa3")
    claude_attach.set_direct("chat-1", True)
    sess = _Sess()
    chunks = asyncio.run(_collect("chat-1", sess, "hello"))
    assert "not installed" in _deltas(chunks) and "not installed" in sess.history[-1].content


def test_detached_meanwhile_explains(home):
    sess = _Sess()
    chunks = asyncio.run(_collect("chat-1", sess, "hello"))
    assert "/claude attach" in _deltas(chunks) and sess.history == []


# ── Background agents ───────────────────────────────────────────────────
# "well i have all these chats as claude agents though": the CLI will not
# resume a session its running agent holds.

def test_an_idle_agent_is_taken_over_for_the_turn_and_parked_again(home, cli):
    state = _agent(cli, "idle")
    claude_attach.attach("chat-1", "7238cfa3")
    claude_attach.set_direct("chat-1", True)
    sess = _Sess()
    chunks = asyncio.run(_collect("chat-1", sess, "status of feature"))
    assert _deltas(chunks) == "All 7 emails went out. *Nothing* reworded."
    calls = [json.loads(l)["argv"] for l in cli.read_text().splitlines()]
    assert calls[0] == ["stop", "c06db0e3"]
    assert "-p" in calls[1] and "--fork-session" not in calls[1], "the real session, not a copy"
    assert "--bg" in calls[2] and calls[2][calls[2].index("--resume") + 1] == SID
    assert json.loads(state.read_text())[0]["pid"], "it is a background agent again afterwards"


def test_a_busy_agent_is_never_interrupted(home, cli):
    _agent(cli, "busy")
    claude_attach.attach("chat-1", "7238cfa3")
    claude_attach.set_direct("chat-1", True)
    sess = _Sess()
    chunks = asyncio.run(_collect("chat-1", sess, "status of feature"))
    assert "busy working" in _deltas(chunks) and "c06db0e3" in _deltas(chunks)
    assert not cli.exists() or cli.read_text().strip() == "", "no stop, no turn"
    assert sess.history[-1].role == "assistant"


def test_attach_takes_an_agent_id_too(home, cli):
    _agent(cli, "idle")
    assert claude_attach.attach("chat-1", "c06db0e3")["id"] == SID


# ── The chat route ──────────────────────────────────────────────────────

def test_the_chat_route_checks_direct_mode_before_any_model():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "routes", "chat_routes.py"), encoding="utf-8").read()
    src = src[src.index("async def chat_stream("):]
    i = src.index("claude_direct.is_on(session)")
    assert i < src.index("_clear_orphaned_session_endpoint(sess, owner=owner)")
    assert i < src.index("No model selected for this chat")
    assert i < src.index("ctx = await build_chat_context(")
    # Refused while a turn runs, never queued behind it like a model reply.
    assert i < src.index("_q = chat_queue.add(session")
    block = src[i:i + 1500]
    assert "require_admin(request)" in block
    # Detached like any reply, so a reload reattaches with /api/chat/resume;
    # the busy check comes first, since start() would cancel the running one.
    assert block.index("claude_direct.busy_reply(session)") < block.index(
        "agent_runs.start(session, claude_direct.stream(session, sess")
    assert "agent_runs.subscribe(session)" in block


# ── A reply that survives a refresh ─────────────────────────────────────
# "i dont have the claude code report back when i refresh the page or switch
# devices": the reply is saved when the turn starts and filled in as it goes,
# on the real session store, so another page reads it from the chat.

FIRST = "Reading the repo now."
FINAL = FIRST + "\n\nAll 7 emails went out. *Nothing* reworded."


@pytest.fixture
def store(home, monkeypatch):
    import uuid
    from core.database import init_db
    from core.models import set_session_manager_instance, get_session_manager_instance
    from core.session_manager import SessionManager
    init_db()
    sm = SessionManager()
    prev = get_session_manager_instance()
    set_session_manager_instance(sm)
    chat = "cd-" + uuid.uuid4().hex[:8]
    sm.create_session(chat, "Login work", "", "", owner="jaron")
    claude_attach.attach(chat, "7238cfa3")
    claude_attach.set_direct(chat, True)
    yield sm, chat
    set_session_manager_instance(prev)


def _rows(chat):
    from core.database import SessionLocal, ChatMessage as DbMsg
    db = SessionLocal()
    try:
        return [(r.id, r.role, r.content, json.loads(r.meta_data or "{}"))
                for r in db.query(DbMsg).filter(DbMsg.session_id == chat).order_by(DbMsg.timestamp).all()]
    finally:
        db.close()


async def _drain_all(agen):
    return [c async for c in agen]


def test_the_reply_is_saved_at_the_start_and_filled_in_as_it_streams(home, cli, store, monkeypatch):
    sm, chat = store
    monkeypatch.setenv("FAKE_FIRST", FIRST)
    monkeypatch.setenv("FAKE_SLEEP", "1.0")
    monkeypatch.setattr(claude_direct, "LIVE_SAVE_EVERY_S", 0.0)
    sess = sm.get_session(chat)

    async def go():
        agen = claude_direct.stream(chat, sess, "status?", sm)
        await agen.__anext__()                       # model_info: the turn has started
        at_start = _rows(chat)
        rest = asyncio.create_task(_drain_all(agen))
        await asyncio.sleep(0.7)
        midway = _rows(chat)
        live_mid = claude_direct.live_state(chat, sess)
        await rest
        return at_start, midway, live_mid

    at_start, midway, live_mid = asyncio.run(go())
    assert [(r[1], r[2]) for r in at_start] == [("user", "status?"), ("assistant", "")]
    assert at_start[1][3]["claude_direct"]["running"] is True
    assert at_start[1][3]["claude_direct"]["started_at"]
    assert midway[1][0] == at_start[1][0], "the same message, updated in place"
    assert midway[1][2] == FIRST and midway[1][3]["claude_direct"]["running"] is True
    assert "git status" in midway[1][3]["claude_direct"]["activity"]
    assert live_mid["running"] is True and live_mid["text"] == FIRST
    final = _rows(chat)
    assert len(final) == 2 and final[1][0] == at_start[1][0], "the final save replaces the placeholder"
    assert final[1][2] == FINAL
    meta = final[1][3]
    assert meta["claude_direct"]["running"] is False and meta["claude_direct"]["cost_usd"] == 0.42
    assert meta["model"] == claude_direct.MODEL_LABEL and meta["response_time"] >= 1.0
    assert sess.history[-1].content == FINAL and chat not in claude_direct._live
    after = claude_direct.live_state(chat, sess)
    assert after["running"] is False and after["text"] == FINAL


def test_writes_are_spaced_out(home, cli, store, monkeypatch):
    """Updates go to the store every few seconds, not on every word."""
    sm, chat = store
    monkeypatch.setenv("FAKE_FIRST", FIRST)
    writes = []
    real = sm.update_message
    monkeypatch.setattr(sm, "update_message", lambda c, m: (writes.append(m.content), real(c, m))[1])
    asyncio.run(_drain_all(claude_direct.stream(chat, sm.get_session(chat), "status?", sm)))
    assert writes == [FINAL], "a quick turn is written once, at the end"


def test_another_device_reads_the_reply_so_far_then_the_final(home, cli, store, monkeypatch):
    import httpx
    from fastapi import FastAPI
    import src.ai_interaction as ai
    from routes import claude_sessions_routes
    from tests.test_claude_attach import _Auth
    sm, chat = store
    monkeypatch.setenv("FAKE_FIRST", FIRST)
    monkeypatch.setenv("FAKE_SLEEP", "1.0")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    app = FastAPI()
    app.state.auth_manager = _Auth({"jaron"})

    @app.middleware("http")
    async def _who(request, call_next):
        request.state.current_user = "jaron"
        return await call_next(request)

    app.include_router(claude_sessions_routes.setup_claude_sessions_routes())

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            idle = (await c.get(f"/api/claude_direct/{chat}/live")).json()
            turn = asyncio.create_task(_drain_all(claude_direct.stream(chat, sm.get_session(chat), "status?", sm)))
            await asyncio.sleep(0.7)
            mid = (await c.get(f"/api/claude_direct/{chat}/live")).json()
            await turn
            end = (await c.get(f"/api/claude_direct/{chat}/live")).json()
            return idle, mid, end

    idle, mid, end = asyncio.run(go())
    assert idle["running"] is False and idle["text"] == ""
    assert mid["running"] is True and mid["text"] == FIRST and "git status" in mid["activity"]
    assert end["running"] is False and end["text"] == FINAL and not end["interrupted"]


def test_a_restart_marks_a_reply_left_running_as_interrupted(store):
    from core.models import ChatMessage
    sm, chat = store
    sess = sm.get_session(chat)
    sess.add_message(ChatMessage("user", "deploy it"))
    sess.add_message(ChatMessage("assistant", "Half the build is done", metadata={
        "model": claude_direct.MODEL_LABEL, "claude_direct": {"session_id": SID, "running": True}}))
    sess.add_message(ChatMessage("user", "and the docs?"))
    sess.add_message(ChatMessage("assistant", "", metadata={
        "model": claude_direct.MODEL_LABEL, "claude_direct": {"session_id": SID, "running": True}}))
    sess.add_message(ChatMessage("assistant", "Finished.", metadata={
        "model": claude_direct.MODEL_LABEL, "claude_direct": {"session_id": SID, "running": False}}))
    assert claude_direct.mark_interrupted_on_startup() >= 2
    rows = _rows(chat)
    cut, empty, fine = rows[1], rows[3], rows[4]
    assert cut[2] == "Half the build is done", "the text so far is kept"
    assert cut[3]["claude_direct"]["running"] is False and cut[3]["claude_direct"]["interrupted"] == "restart"
    assert empty[2] and empty[3]["claude_direct"]["interrupted"] == "restart"
    assert fine[3]["claude_direct"] == {"session_id": SID, "running": False}
    assert sess.history[1].metadata["claude_direct"]["running"] is False, "a loaded chat sees it too"
    assert claude_direct.live_state(chat, sess)["running"] is False
    assert claude_direct.mark_interrupted_on_startup() == 0


def test_stop_ends_the_turn_and_keeps_what_it_said(home, cli, store, monkeypatch):
    import time
    from src import agent_runs
    sm, chat = store
    monkeypatch.setenv("FAKE_FIRST", FIRST)
    monkeypatch.setenv("FAKE_SLEEP", "5.0")

    async def go():
        agent_runs.start(chat, claude_direct.stream(chat, sm.get_session(chat), "status?", sm))
        await asyncio.sleep(0.6)
        assert agent_runs.is_active(chat) and claude_direct.chat_busy(chat)
        agent_runs.stop(chat)
        await asyncio.wait({agent_runs._RUNS[chat].task}, timeout=10)

    t0 = time.time()
    asyncio.run(go())
    assert time.time() - t0 < 4, "the CLI was ended, not waited for"
    reply = _rows(chat)[-1]
    assert reply[2].startswith(FIRST) and "Stopped" in reply[2]
    assert reply[3]["claude_direct"]["running"] is False and reply[3]["claude_direct"]["interrupted"] == "stopped"
    assert not claude_direct.chat_busy(chat)


def test_a_restart_does_not_prompt_a_direct_chat_to_carry_on(store, monkeypatch):
    from src import agent_runs, restart_resume
    sm, chat = store
    saved = []
    monkeypatch.setattr(agent_runs, "active_sessions", lambda: [chat, "a-model-chat"])
    monkeypatch.setattr(restart_resume, "remember", lambda ids: saved.extend(ids) or len(ids))
    restart_resume.remember_active()
    assert saved == ["a-model-chat"]


# ── "Your report is ready" ──────────────────────────────────────────────

def _push_log(monkeypatch, bell=None):
    from src import chat_queue
    sent = []

    async def fake_send(session_id, notify, heading, body, **kw):
        sent.append((session_id, notify, heading, body))
        return {"sent": 1}

    monkeypatch.setattr(chat_queue, "send_notification", fake_send)
    monkeypatch.setattr(chat_queue, "get", lambda sid: {"items": [], "notify": bell})
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "Login work")
    return sent


async def _turn_then_settle(chat, sess):
    chunks = await _collect(chat, sess, "status?")
    await asyncio.sleep(0.1)          # the push goes out after the reply
    return chunks


def test_a_long_turn_sends_one_push_when_it_is_done(home, cli, monkeypatch):
    claude_attach.attach("chat-1", "7238cfa3")
    claude_attach.set_direct("chat-1", True)
    sent = _push_log(monkeypatch)
    monkeypatch.setattr(claude_direct, "LONG_TURN_S", 0)
    asyncio.run(_turn_then_settle("chat-1", _Sess()))
    assert len(sent) == 1
    chat, notify, heading, body = sent[0]
    assert chat == "chat-1" and notify == {} and heading == "Claude Code finished: Login work"
    assert body.startswith("All 7 emails went out.")


def test_a_short_turn_or_a_bell_already_set_sends_none_from_here(home, cli, monkeypatch):
    claude_attach.attach("chat-1", "7238cfa3")
    claude_attach.set_direct("chat-1", True)
    sent = _push_log(monkeypatch)
    asyncio.run(_turn_then_settle("chat-1", _Sess()))          # well under two minutes
    assert sent == []
    sent = _push_log(monkeypatch, bell={"device": "pixel-8a"})   # the bell sends its own
    monkeypatch.setattr(claude_direct, "LONG_TURN_S", 0)
    asyncio.run(_turn_then_settle("chat-1", _Sess()))
    assert sent == []
