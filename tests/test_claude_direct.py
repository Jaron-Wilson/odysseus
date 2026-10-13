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
from tests.test_claude_attach import SID, PROJ, _client, _write_session, home  # noqa: F401

NEW_SID = "7238cfa3-e12f-4867-bccd-ba64f3e00799"

FAKE = r'''#!{py}
import json, os, sys, time
argv = sys.argv[1:]
prompt = sys.stdin.read()
open({log!r}, "a").write(json.dumps({{"argv": argv, "cwd": os.getcwd(), "prompt": prompt}}) + "\n")
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
    exe = bindir / "claude"
    exe.write_text(FAKE.format(py=sys.executable, log=str(log)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    claude_direct._running.clear()
    return log


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
