"""A step that only uses tools keeps its chat bubble, and a reply always ends
with the agent saying something.

Asked for on 2026-09-29: "when its using tools i want it to now just do the
basic chatbox, and stream, its getting anoying not having a chat when its
just a tool call", and "there should not ever ... JUST BE A PLAIN OLD TOOL
CALL AT THE BOTTOM OF THE PAGE". Also the Devices panel said "Install"
beside "an update is available" ("Install not an update?").
"""
import asyncio
import os

import src.llm_core as llm_core
from src import agent_loop

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _closing(monkeypatch, reply=None, error=None, events=None):
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        if error:
            raise error
        return reply

    monkeypatch.setattr(llm_core, "llm_call_async", fake)
    text = asyncio.run(agent_loop._closing_words(
        [{"role": "user", "content": "what is in my repo?"}],
        events or [{"tool": "bash", "exit_code": 0}],
        endpoint_url="http://x/v1", model="m", headers={}, max_tokens=4096))
    return text, seen


def test_the_agent_says_what_it_did(monkeypatch):
    text, seen = _closing(monkeypatch, reply="<think>hm</think>The repo has app.py and src.")
    assert text == "The repo has app.py and src."
    assert "Do NOT call any tools" in seen["messages"][-1]["content"]
    assert seen["max_tokens"] == 1024


def test_a_failed_call_still_ends_with_words(monkeypatch):
    events = [{"tool": "bash", "exit_code": 0}, {"tool": "read_file", "exit_code": 1}]
    text, _ = _closing(monkeypatch, error=RuntimeError("down"), events=events)
    assert text.startswith("I ran bash, read_file; the last step failed")
    text, _ = _closing(monkeypatch, reply="  ", events=events[:1])
    assert "the last step finished" in text


def test_the_loop_asks_for_them_when_the_last_round_is_silent():
    src = _read("src", "agent_loop.py")
    i = src.index("_closing = await _closing_words(")
    before = src[i - 400:i]
    assert "tool_events and not _force_answer" in before
    assert 'yield f\'data: {json.dumps({"delta": _closing})}' in src[i:i + 400]


def test_a_tool_only_step_keeps_its_bubble():
    chat = _read("static", "js", "chat.js")
    assert "roundHolder.classList.add('msg-tool-only')" in chat
    assert "chatRenderer.addToolStatus(_sb," in chat
    assert chat.count("chatRenderer.settleToolStatus()") == 3      # every way a stream ends
    rend = _read("static", "js", "chatRenderer.js")
    assert "msg msg-ai msg-tool-only" in rend                       # saved chats too
    tail = rend[rend.rindex("const chatRenderer = {"):]
    assert "addToolStatus" in tail and "settleToolStatus" in tail   # chat.js uses the default export
    assert ".msg-tool-status" in _read("static", "style.css")


def test_the_devices_button_says_install_and_update():
    js = _read("static", "js", "devicesSettings.js")
    assert "return 'Install and update'" in js and "${toolsButton(t)}" in js
