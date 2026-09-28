"""Say OpenCode when it is OpenCode.

Asked for on 2026-09-28: "can we have the text say differently, I don't like
seeing it say claude when it's opencode". The tool is named claude_code, but
most runs are OpenCode; every label the user sees names the engine that ran.
"""
import asyncio
import json
import os
import shutil
import subprocess
import time

import pytest

from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_engine_label():
    assert cct.engine_label("opencode") == "OpenCode"
    assert cct.engine_label("claude") == "Claude Code"
    assert cct.engine_label("") == "OpenCode"


def test_posted_results_name_the_engine(monkeypatch, tmp_path):
    msgs = []

    class _SM:
        def add_message(self, sid, m):
            msgs.append(m)

        def save_sessions(self):
            pass
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: _SM())
    job = jobs.Job(chat_session_id="c1", owner="", action="execute", cwd="/p", model="local",
                   engine="opencode", prompt="x")
    job.finished = time.time()
    asyncio.run(cct._post_background_result(job, {"exit_code": 0, "output": "done", "console": "$ opencode run"}))
    assert msgs[-1].content.startswith("**Background OpenCode job") and "Claude" not in msgs[-1].content
    assert msgs[-1].metadata["tool_events"][0]["label"] == "OpenCode"
    job.engine, job.reattached = "claude", True
    asyncio.run(cct._post_background_result(job, {"exit_code": 0, "output": "done"}))
    assert msgs[-1].content.startswith("**Claude Code job")


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_cards_name_the_engine_even_for_old_runs():
    src = open(os.path.join(HERE, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    i = src.index("export function toolDisplayName(ev) {")
    j = src.index("\n}\n", i) + 3
    fn = src[i:j].replace("export function", "function")
    cases = [
        ({"tool": "claude_code", "label": "OpenCode"}, "OpenCode"),
        ({"tool": "claude_code", "output": "$ opencode --agent plan\n  pid 1"}, "OpenCode"),
        ({"tool": "claude_code", "output": "$ claude --permission-mode plan"}, "Claude Code"),
        ({"tool": "claude_code", "command": '{"engine": "claude", "prompt": "x"}'}, "Claude Code"),
        ({"tool": "claude_code", "command": '{"prompt": "x"}'}, "Coding agent"),
        ({"tool": "bash", "command": "ls"}, "bash"),
    ]
    script = fn + "\nconst cases = " + json.dumps([c for c, _ in cases]) + ";\n" \
        "console.log(JSON.stringify(cases.map(toolDisplayName)));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert json.loads(out.stdout) == [want for _, want in cases], out.stderr


def test_everything_else_is_wired():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    loop = read("src", "agent_loop.py")
    assert 'tool_event["label"] = result["engine_label"]' in loop
    assert 'tool_output_data["label"] = result["engine_label"]' in loop
    assert 'never "Claude Code" for an OpenCode run' in loop
    tool = read("src", "agent_tools", "claude_code_tool.py")
    assert '"engine_label": engine_label(engine),' in tool
    chat = read("static", "js", "chat.js")
    assert "chatRenderer.toolDisplayName(json)" in chat and "json.engine_label" in chat
    assert "[Plan approved · run {run_id} · {engine}]" in read("routes", "claude_code_routes.py")
    assert "Coding agents are OFF in this chat" in read("static", "js", "chatClaudeToggle.js")
