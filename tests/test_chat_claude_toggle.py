"""Claude Code on or off per chat.

Asked for on 2026-09-27: "let me disable and enable claude code per chat
please, this one keeps using it to do stuff when I said don't".
"""
import asyncio
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import chat_prefs

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_prefs, "PREFS_FILE", str(tmp_path / "chat_prefs.json"))


def test_the_switch_is_per_chat_and_defaults_on():
    assert chat_prefs.claude_code_allowed("chat-a")
    chat_prefs.set_pref("chat-a", "claude_code", False)
    assert not chat_prefs.claude_code_allowed("chat-a")
    assert chat_prefs.claude_code_allowed("chat-b")
    chat_prefs.set_pref("chat-a", "claude_code", True)
    assert chat_prefs._load() == {}                        # back to default: nothing stored
    with pytest.raises(ValueError):
        chat_prefs.set_pref("chat-a", "nope", 1)


def test_the_tool_refuses_in_a_chat_with_it_off():
    from src.agent_tools import claude_code_tool as cct
    chat_prefs.set_pref("chat-off", "claude_code", False)
    out = asyncio.run(cct.ClaudeCodeTool().execute(
        json.dumps({"prompt": "x", "cwd": "/tmp"}), {"session_id": "chat-off"}))
    assert out["disabled"] and "switched off for this chat" in out["error"]


def test_bash_cannot_run_the_cli_instead():
    from src import tool_execution as te
    from src.agent_tools import ToolBlock
    chat_prefs.set_pref("chat-off", "claude_code", False)
    for cmd in ("claude -p 'do it'", "cd /x && opencode run hi", "ssh lap 'claude --resume x'"):
        desc, res = asyncio.run(te.execute_tool_block(ToolBlock("bash", cmd), session_id="chat-off"))
        assert desc == "bash: refused" and "switched off" in res["error"], cmd
    assert not te._CODE_CLI_RE.search("cat CLAUDE.md && ls claude_code_runs")


def test_the_agent_is_not_offered_it(monkeypatch):
    import src.agent_loop as al
    seen = {}
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    real = al._build_agent_system_prompt if hasattr(al, "_build_agent_system_prompt") else None

    async def fake_stream(_c, messages, **kw):
        yield 'data: {"delta": "ok"}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)
    orig = al._assemble_prompt

    def spy(tool_names, disabled_tools=None, compact=False):
        seen["disabled"] = set(disabled_tools or ())
        return orig(tool_names, disabled_tools, compact)
    monkeypatch.setattr(al, "_assemble_prompt", spy)
    chat_prefs.set_pref("chat-off", "claude_code", False)

    async def run():
        return [c async for c in al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "refactor it"}],
            session_id="chat-off", max_rounds=1, relevant_tools={"claude_code", "bash"})]
    asyncio.run(run())
    assert "claude_code" in seen.get("disabled", set())


def test_routes_and_button(monkeypatch):
    import routes.chat_prefs_routes as r

    class _Sess:
        owner = None

    class _SM:
        def get_session(self, sid):
            return _Sess()
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: _SM())
    monkeypatch.setattr(r, "require_authenticated_request", lambda req: None)
    monkeypatch.setattr(r, "effective_user", lambda req: "")
    app = FastAPI()
    app.include_router(r.setup_chat_prefs_routes())
    c = TestClient(app)
    assert c.get("/api/chat-prefs/chat-1").json() == {"claude_code": True}
    assert c.put("/api/chat-prefs/chat-1", json={"claude_code": False}).json() == {"claude_code": False}
    assert c.put("/api/chat-prefs/chat-1", json={"bogus": 1}).status_code == 400
    html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    assert 'id="claude-toggle-btn"' in html
    assert "import './chatClaudeToggle.js';" in open(os.path.join(HERE, "static", "js", "chat.js")).read()
