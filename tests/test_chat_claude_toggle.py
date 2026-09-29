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
    chat_prefs.set_pref("chat-a", "claude", False)
    assert not chat_prefs.engine_allowed("chat-a", "claude")
    assert chat_prefs.engine_allowed("chat-a", "opencode") and chat_prefs.claude_code_allowed("chat-a")
    chat_prefs.set_pref("chat-a", "opencode", False)
    assert not chat_prefs.claude_code_allowed("chat-a")    # both off: no coding agent at all
    assert chat_prefs.claude_code_allowed("chat-b")
    chat_prefs.set_pref("chat-a", "claude", True)
    chat_prefs.set_pref("chat-a", "opencode", True)
    assert chat_prefs._load() == {}                        # back to default: nothing stored
    with pytest.raises(ValueError):
        chat_prefs.set_pref("chat-a", "nope", 1)


def test_the_old_single_switch_still_means_both_off():
    chat_prefs._save({"chat-old": {"claude_code": False}})
    assert chat_prefs.get("chat-old") == {"claude": False, "opencode": False, "tidy": False}
    chat_prefs.set_pref("chat-old", "opencode", True)
    assert chat_prefs.get("chat-old") == {"claude": False, "opencode": True, "tidy": False}


def _off(chat, *engines):
    for e in engines:
        chat_prefs.set_pref(chat, e, False)


def test_the_tool_refuses_in_a_chat_with_both_off():
    from src.agent_tools import claude_code_tool as cct
    _off("chat-off", "claude", "opencode")
    out = asyncio.run(cct.ClaudeCodeTool().execute(
        json.dumps({"prompt": "x", "cwd": "/tmp"}), {"session_id": "chat-off"}))
    assert out["disabled"] and "both switched off" in out["error"]


def test_one_engine_off_moves_a_plan_to_the_other(monkeypatch, tmp_path):
    import stat, sys
    from src.agent_tools import claude_code_tool as cct
    from src import claude_code_approvals as approvals, claude_code_jobs as jobs
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    async def no_pdf(*a, **k):
        return None, "skipped"
    import src.doc_pdf as doc_pdf
    monkeypatch.setattr(doc_pdf, "render_markdown_pdf", no_pdf)
    monkeypatch.setattr(cct, "_notify_plan", lambda *a, **k: asyncio.sleep(0))
    bindir = tmp_path / "bin"; bindir.mkdir()
    ran = tmp_path / "ran.txt"
    for name, line in (("opencode", "print(json.dumps({'type':'text','sessionID':'ses_1','part':{'text':'Local plan.'}}))"),
                       ("claude", "sys.stdin.read(); print(json.dumps({'type':'result','result':'Claude plan.','is_error':False}))")):
        exe = bindir / name
        exe.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(ran)!r}, 'w').write({name!r})\n{line}\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "opencode")
    _off("chat-x", "opencode")                            # Claude only in this chat
    out = asyncio.run(cct.ClaudeCodeTool().execute(
        json.dumps({"prompt": "plan it", "cwd": str(tmp_path)}), {"session_id": "chat-x"}))
    assert ran.read_text() == "claude" and "OpenCode is switched off" in out["engine_note"]
    # An approved plan for the switched-off engine is refused, not moved.
    approvals.record_plan("ses_9", cwd=str(tmp_path), plan="p", engine="opencode")
    approvals.set_status("ses_9", "approved")
    out = asyncio.run(cct.ClaudeCodeTool().execute(
        json.dumps({"action": "execute", "session_id": "ses_9", "prompt": "go", "cwd": str(tmp_path)}),
        {"session_id": "chat-x"}))
    assert out["disabled"] and "cannot run on Claude Code" in out["error"]


def test_bash_cannot_run_the_cli_instead():
    from src import tool_execution as te
    from src.agent_tools import ToolBlock
    _off("chat-off", "claude", "opencode")
    for cmd in ("claude -p 'do it'", "cd /x && opencode run hi", "ssh lap 'claude --resume x'"):
        desc, res = asyncio.run(te.execute_tool_block(ToolBlock("bash", cmd), session_id="chat-off"))
        assert desc == "bash: refused" and "switched off" in res["error"], cmd
    _off("chat-half", "claude")                           # only Claude is off here
    desc, _ = asyncio.run(te.execute_tool_block(ToolBlock("bash", "echo hi && claude -p x"), session_id="chat-half"))
    assert desc == "bash: refused"
    desc, res = asyncio.run(te.execute_tool_block(ToolBlock("bash", "opencode run hi"), session_id="chat-half"))
    assert desc != "bash: refused"
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
    _off("chat-off", "claude", "opencode")

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
    assert c.get("/api/chat-prefs/chat-1").json() == {"claude": True, "opencode": True, "tidy": False}
    assert c.put("/api/chat-prefs/chat-1", json={"claude": False}).json() == {"claude": False, "opencode": True, "tidy": False}
    assert c.put("/api/chat-prefs/chat-1", json={"bogus": 1}).status_code == 400
    html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    assert 'id="claude-toggle-btn"' in html
    assert "import './chatClaudeToggle.js';" in open(os.path.join(HERE, "static", "js", "chat.js")).read()
    js = open(os.path.join(HERE, "static", "js", "chatClaudeToggle.js")).read()
    assert "tag: 'no Claude'" in js and "tag: 'no OpenCode'" in js and "tag: 'off'" in js
