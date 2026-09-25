"""Approving screen control carries the task on, visibly, as one agent.

Seen live on 2026-09-25: the approval resumed the agent headless, so the chat
showed nothing; the user typed "approved"; that started a second agent in the
same chat while the first was still going, and both drove the PC at once.

Now the resume is a detached agent_runs run: registered before the approve
click returns (so the open chat can attach and stream it), and cancelled
cleanly -- partial reply saved -- if the user sends a message meanwhile.
"""
import asyncio
import json

from src import agent_runs, screen_control_resume as scr


class _Sess:
    def __init__(self, sid):
        self.id = sid
        self.model = "qwen3.8-27b"
        self.endpoint_url = "http://llm"
        self.messages = []

    def get_context_messages(self):
        return [{"role": m.role, "content": m.content} for m in self.messages]


class _SM:
    def __init__(self, sess):
        self.sess = sess
        self.saves = 0

    def get_session(self, sid):
        if sid != self.sess.id:
            raise KeyError(sid)
        return self.sess

    def add_message(self, sid, msg):
        self.sess.messages.append(msg)

    def save_sessions(self):
        self.saves += 1


def _events(*items):
    return [f"data: {json.dumps(i)}\n\n" for i in items]


def _install(monkeypatch, sid="chat-1"):
    sess = _Sess(sid)
    sm = _SM(sess)
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    agent_runs._RUNS.pop(sid, None)
    return sess, sm


def test_resume_is_a_live_run_and_saves_its_reply(monkeypatch):
    sess, sm = _install(monkeypatch)
    seen_context = {}

    async def fake_loop(url, model, context, **kw):
        seen_context["last"] = context[-1]["content"]
        for ev in _events({"delta": "Taking a fresh screenshot. "},
                          {"type": "tool_output", "tool": "screenshot", "command": "", "output": "ok",
                           "exit_code": 0},
                          {"delta": "Done."}):
            yield ev
        yield "data: [DONE]\n\n"

    async def run():
        assert scr.start_resume("chat-1", "windows-desktop", agent_loop=fake_loop) is True
        # Registered synchronously: the approving browser can attach now.
        assert agent_runs.is_active("chat-1")
        replay = [ev async for ev in agent_runs.subscribe("chat-1")]
        return replay

    replay = asyncio.run(run())
    assert any("Taking a fresh screenshot" in ev for ev in replay)
    assert seen_context["last"].startswith("[Screen control approved for windows-desktop]")
    roles = [m.role for m in sess.messages]
    assert roles == ["user", "assistant"]
    assert sess.messages[0].metadata["source"] == "screen_control_approved"
    reply = sess.messages[1]
    assert reply.content == "Taking a fresh screenshot. Done."
    assert reply.metadata["tool_events"][0]["tool"] == "screenshot"
    assert reply.metadata["source"] == "screen_control_resumed"


def test_no_resume_into_a_chat_with_a_live_turn(monkeypatch):
    _install(monkeypatch)

    async def slow():
        await asyncio.sleep(0.2)
        yield "data: [DONE]\n\n"

    async def run():
        agent_runs.start("chat-1", slow())
        return scr.start_resume("chat-1", "windows-desktop")

    assert asyncio.run(run()) is False


def test_a_new_message_cancels_the_resume_and_keeps_its_partial_reply(monkeypatch):
    sess, sm = _install(monkeypatch)
    gate = {}

    async def long_loop(url, model, context, **kw):
        yield _events({"delta": "Opening the terminal"})[0]
        gate["reached"] = True
        await asyncio.sleep(10)                 # would keep driving the PC
        yield _events({"delta": " and typing."})[0]

    async def users_message():
        yield _events({"delta": "(the user's own turn)"})[0]
        yield "data: [DONE]\n\n"

    async def run():
        assert scr.start_resume("chat-1", "windows-desktop", agent_loop=long_loop)
        for _ in range(50):
            if gate.get("reached"):
                break
            await asyncio.sleep(0.01)
        # What chat_routes does when the user sends a message: start a run for
        # the same session, which cancels the one in flight.
        agent_runs.start("chat-1", users_message())
        await asyncio.sleep(0.2)

    asyncio.run(run())
    replies = [m for m in sess.messages if m.role == "assistant"]
    assert len(replies) == 1
    assert replies[0].content == "Opening the terminal"     # the partial, saved once


def test_missing_or_blank_chat_is_not_an_error(monkeypatch):
    _install(monkeypatch)
    assert scr.start_resume("", "pc") is False
    assert scr.start_resume("no-such-chat", "pc") is False


def test_approved_counts_as_continuing_the_task():
    from src.agent_loop import _is_explicit_continuation
    for reply in ("approved", "Approved", "i approved it", "ok approved", "granted"):
        assert _is_explicit_continuation(reply), reply
