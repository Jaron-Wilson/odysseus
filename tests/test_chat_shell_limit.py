"""Each chat sets how many shell (bash) calls in a row the agent may make.

Asked for on 2026-09-29: "in the chat let me be able to change tool bash
calls, current limit is 12 but i want to in the chat bypass that limit".
"""
import asyncio
import json
import os

import pytest

import src.agent_loop as al
from src import chat_prefs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_prefs, "PREFS_FILE", str(tmp_path / "chat_prefs.json"))


def _run(monkeypatch, session_id, n_calls=15):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    ran, seen, n = [], [], {"i": 0}

    async def fake_exec(block, *a, **k):
        ran.append(block.tool_type)
        return (block.tool_type, {"output": "ok", "exit_code": 0})

    async def fake_stream(_candidates, messages, **kwargs):
        seen.append(json.dumps(messages[-1])[-500:])
        i = n["i"]
        n["i"] += 1
        text = f"Step {i}.\n```bash\ncat /tmp/f{i}.txt\n```" if i < n_calls else "Done."
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "execute_tool_block", fake_exec, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    async def go():
        return [c async for c in al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "check the server"}],
            max_rounds=30, relevant_tools={"bash"}, session_id=session_id)]
    asyncio.run(go())
    return ran, seen


def test_the_default_is_still_twelve():
    assert chat_prefs.get("chat-a")["bash_limit"] == 12 == al.BASH_STREAK_STOP
    assert chat_prefs.bash_limit("chat-a") == 12 and chat_prefs.bash_limit("") == 12


def test_no_limit_lets_every_call_run(monkeypatch):
    chat_prefs.set_pref("chat-free", "bash_limit", 0)
    ran, seen = _run(monkeypatch, "chat-free")
    assert ran.count("bash") == 15
    assert not any("bash calls in a row, this chat's limit" in s for s in seen)


def test_a_lower_limit_stops_sooner_and_says_how_to_raise_it(monkeypatch):
    chat_prefs.set_pref("chat-low", "bash_limit", 3)
    ran, seen = _run(monkeypatch, "chat-low")
    assert ran.count("bash") == 3
    assert any("more than 3 bash calls in a row, this chat's limit" in s and "Shell button" in s for s in seen)


def test_only_sensible_values():
    for bad in (-1, 1001, "lots", None):
        with pytest.raises(ValueError):
            chat_prefs.set_pref("chat-b", "bash_limit", bad)
    assert chat_prefs.set_pref("chat-b", "bash_limit", "50")["bash_limit"] == 50
    assert chat_prefs.set_pref("chat-b", "bash_limit", 12) == chat_prefs.DEFAULTS    # back to default


def test_the_button_is_there():
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert 'id="shell-limit-btn"' in html
    js = open(os.path.join(ROOT, "static", "js", "chatShellLimit.js"), encoding="utf-8").read()
    assert "const STEPS = [12, 25, 50, 100, 0];" in js and "bash_limit: value" in js
    assert "import './chatShellLimit.js';" in open(os.path.join(ROOT, "static", "js", "chat.js")).read()
