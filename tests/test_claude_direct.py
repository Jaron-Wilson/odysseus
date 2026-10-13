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
    block = src[i:i + 400]
    assert "require_admin(request)" in block and "claude_direct.stream(session, sess" in block
