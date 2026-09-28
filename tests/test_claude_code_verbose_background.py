"""Claude Code from a chat: verbose output, an explicit model, and runs that
can be sent to the background.

Seen live on 2026-09-27:
- The console showed a 16-line tail, lines cut at 200 characters and every
  tool result hidden, so a long run read as a spinner.
- A run could only be backgrounded when it was started; one already going
  held the chat until it ended.
- Simple chores (open a PR) went to Claude Code with no model named, so the
  CLI used the user's own default, the most expensive model, and nothing
  said which model ran.
"""
import asyncio
import json
import os
import stat
import sys

import pytest

from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

FAKE_CLI = r'''#!{python}
import json, sys, time
open({argv_file!r}, "a").write(json.dumps(sys.argv[1:]) + "\n")
sys.stdin.read()
def out(o):
    print(json.dumps(o), flush=True)
out({{"type": "system", "subtype": "init", "session_id": "abc12345", "cwd": "/p", "model": "claude-sonnet-5"}})
out({{"type": "assistant", "message": {{"content": [
    {{"type": "thinking", "thinking": "Look at the README first."}},
    {{"type": "text", "text": "Reading the project."}},
    {{"type": "tool_use", "name": "Bash", "input": {{"command": "ls -la"}}}}]}}}})
out({{"type": "user", "message": {{"content": [
    {{"type": "tool_result", "content": "a\nb\nc\nd\ne\nf"}}]}}}})
time.sleep({delay})
out({{"type": "result", "result": "All done.", "is_error": False, "duration_ms": 1200, "num_turns": 2, "total_cost_usd": 0.03}})
'''


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def make(delay=0.0):
        argv_file = tmp_path / "argv.jsonl"
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "claude"
        exe.write_text(FAKE_CLI.format(python=sys.executable, argv_file=str(argv_file), delay=delay))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        return argv_file
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "appr.json"))
    from src import claude_code_agents as agents_reg
    monkeypatch.setattr(agents_reg, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agents_reg, "AGENTS_FILE", str(tmp_path / "agents.json"))
    monkeypatch.setattr(agents_reg, "_chat_name",
                        lambda cid: {"chat-A": "Website rebrand", "chat-B": "Quick fixes"}.get(cid, ""))

    async def no_pdf(*a, **k):
        return None, "skipped in tests"
    import src.doc_pdf as doc_pdf
    monkeypatch.setattr(doc_pdf, "render_markdown_pdf", no_pdf)
    jobs._JOBS.clear()
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    # These drive a fake `claude` CLI, so pin the engine (the default is OpenCode).
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "claude")
    return make


def _argv(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_the_console_is_verbose():
    lines = []
    for ev in [
        {"type": "system", "subtype": "init", "session_id": "abc12345", "cwd": "/p", "model": "claude-sonnet-5"},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "Check the tests first."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/p/app.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "1\n2\n3\n4\n5\n6"}]}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "No such file"}]}},
        {"type": "result", "duration_ms": 61000, "num_turns": 7, "total_cost_usd": 0.412},
    ]:
        lines += (cct._summarize(ev) or "").splitlines()
    assert lines[0] == "· session abc12345 in /p · model claude-sonnet-5"
    assert "✻ Check the tests first." in lines
    assert "● Edit(/p/app.py)" in lines
    assert "  ⎿ 1" in lines and "    4" in lines and "    … 2 more lines" in lines
    assert "  ⎿ error: No such file" in lines
    assert lines[-1] == "· finished in 61s · 7 turns · $0.41"


def test_an_unnamed_model_is_sonnet_and_shown(cli, tmp_path):
    argv_file = cli()
    seen = []

    async def cb(p):
        seen.append(p)

    out = asyncio.run(cct.ClaudeCodeTool().execute(
        json.dumps({"prompt": "fix it", "cwd": str(tmp_path)}), {"progress_cb": cb}))
    argv = _argv(argv_file)[0]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert out["model"] == "sonnet"
    assert "model sonnet" in out["console"]
    assert "[Approve plan · runs on Claude Code · sonnet]" in out["approval"]["approve"]
    assert approvals.get(out["session_id"])["model"] == "sonnet"
    # Verbose, and the card knows it can be backgrounded.
    assert "  ⎿ a" in out["console"] and "✻ Look at the README first." in out["console"]
    assert seen[0]["job_id"] and seen[0]["can_background"] is True


def test_execute_keeps_the_plan_model(cli, tmp_path):
    argv_file = cli()
    plan = asyncio.run(cct.ClaudeCodeTool().execute(
        json.dumps({"prompt": "fix it", "cwd": str(tmp_path), "model": "haiku"}), {}))
    approvals.set_status(plan["session_id"], "approved")
    asyncio.run(cct.ClaudeCodeTool().execute(json.dumps({
        "action": "execute", "session_id": plan["session_id"], "cwd": str(tmp_path),
        "prompt": "Approved."}), {}))
    runs = _argv(argv_file)
    assert runs[1][runs[1].index("--model") + 1] == "haiku"


class _SM:
    def __init__(self):
        self.msgs = []

    def add_message(self, sid, msg):
        self.msgs.append((sid, msg))

    def save_sessions(self):
        pass


def test_a_running_run_can_be_sent_to_the_background(cli, tmp_path, monkeypatch):
    cli(delay=1.5)
    sm = _SM()
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)

    async def run():
        task = asyncio.create_task(cct.ClaudeCodeTool().execute(
            json.dumps({"prompt": "big refactor", "cwd": str(tmp_path)}),
            {"session_id": "chat-1", "owner": ""}))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if jobs.list_jobs():
                break
        job = jobs.list_jobs()[0]
        await asyncio.sleep(0.3)
        assert jobs.detach(job.id)
        out = await asyncio.wait_for(task, timeout=2)      # the chat is free at once
        assert out["background"] is True and out["job_id"] == job.id
        assert job.status == "running"
        await asyncio.wait_for(job.task, timeout=10)       # the CLI carried on
        return job, out

    job, out = asyncio.run(run())
    assert "Moved to the background as job" in out["output"]
    assert job.status == "done"
    sid, msg = sm.msgs[-1]
    assert sid == "chat-1" and msg.role == "assistant"
    assert f"Background Claude Code job `{job.id}` finished" in msg.content
    assert "All done." in msg.content and "[Approve plan · runs on Claude Code · sonnet]" in msg.content
    assert msg.metadata["tool_events"][0]["tool"] == "claude_code"


def test_stop_kills_a_background_run(cli, tmp_path):
    cli(delay=30)

    async def run():
        task = asyncio.create_task(cct.ClaudeCodeTool().execute(
            json.dumps({"prompt": "x", "cwd": str(tmp_path)}), {}))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if jobs.list_jobs():
                break
        job = jobs.list_jobs()[0]
        await asyncio.sleep(0.3)
        jobs.detach(job.id)
        await task
        assert jobs.stop(job.id)
        await asyncio.wait_for(job.task, timeout=10)
        return job

    job = asyncio.run(run())
    assert job.status == "stopped" and job.finished


def test_routes_panel_and_rules_are_wired():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    routes = open(os.path.join(here, "routes", "claude_code_routes.py")).read()
    for path in ('"/api/claude_code/jobs"', '"/api/claude_code/jobs/{job_id}/background"',
                 '"/api/claude_code/jobs/{job_id}/stop"'):
        assert path in routes
    js = open(os.path.join(here, "static", "js", "chat.js")).read()
    assert "_bgBtn.className = 'cc-bg-btn';" in js and "import './bgTasks.js';" in js
    assert 'id="tool-bg-btn"' in open(os.path.join(here, "static", "index.html")).read()
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    desc = next(t["function"]["description"] for t in FUNCTION_TOOL_SCHEMAS
                if t.get("function", {}).get("name") == "claude_code")
    assert "gh pr create" in desc and "which engine and model" in desc
    rules = open(os.path.join(here, "src", "agent_loop.py")).read()
    assert "NOT for chores: committing, pushing, opening a PR" in rules


def _resume_of(argv):
    return argv[argv.index("--resume") + 1] if "--resume" in argv else None


def test_each_chat_carries_on_its_own_agent(cli, tmp_path):
    argv_file = cli()
    tool = cct.ClaudeCodeTool()
    first = asyncio.run(tool.execute(json.dumps(
        {"action": "ask", "prompt": "how is routing done?", "cwd": str(tmp_path)}),
        {"session_id": "chat-A"}))
    sid = first["session_id"]
    again = asyncio.run(tool.execute(json.dumps(
        {"action": "ask", "prompt": "and the auth?", "cwd": str(tmp_path)}),
        {"session_id": "chat-A"}))
    runs = _argv(argv_file)
    assert _resume_of(runs[0]) is None and _resume_of(runs[1]) == sid
    assert again["session_id"] == sid and "this chat's Claude Code agent" in again["agent"]
    # Another chat does not pick it up by itself...
    asyncio.run(tool.execute(json.dumps(
        {"action": "ask", "prompt": "hi", "cwd": str(tmp_path)}), {"session_id": "chat-B"}))
    assert _resume_of(_argv(argv_file)[2]) is None
    # ...and new_agent starts fresh even in the same chat.
    asyncio.run(tool.execute(json.dumps(
        {"action": "ask", "prompt": "fresh", "cwd": str(tmp_path), "new_agent": True}),
        {"session_id": "chat-A"}))
    assert _resume_of(_argv(argv_file)[3]) is None


def test_another_chat_can_carry_on_an_agent(cli, tmp_path):
    argv_file = cli()
    tool = cct.ClaudeCodeTool()
    first = asyncio.run(tool.execute(json.dumps(
        {"action": "plan", "prompt": "rebrand the site", "cwd": str(tmp_path)}),
        {"session_id": "chat-A"}))
    listing = tool._chat_agents("chat-B")
    assert 'chat "Website rebrand" `chat-A`' in listing["output"]
    assert "rebrand the site" in listing["output"]
    # By name, and with no cwd: it defaults to the agent's folder.
    out = asyncio.run(tool.execute(json.dumps(
        {"action": "ask", "prompt": "what did you change?", "from_chat": "rebrand"}),
        {"session_id": "chat-B"}))
    assert _resume_of(_argv(argv_file)[1]) == first["session_id"]
    assert out["cwd"] == str(tmp_path.resolve()) and "Website rebrand" in out["agent"]
    bad = asyncio.run(tool.execute(json.dumps(
        {"action": "ask", "prompt": "x", "from_chat": "nope"}), {"session_id": "chat-B"}))
    assert "no chat matching" in bad["error"]


def test_a_busy_agent_is_not_driven_twice(cli, tmp_path):
    cli(delay=3)
    tool = cct.ClaudeCodeTool()

    async def run():
        first = asyncio.create_task(tool.execute(json.dumps(
            {"action": "ask", "prompt": "long one", "cwd": str(tmp_path),
             "session_id": "11111111-2222-3333-4444-555555555555"}), {"session_id": "chat-A"}))
        await asyncio.sleep(0.5)
        second = await tool.execute(json.dumps(
            {"action": "ask", "prompt": "me too", "cwd": str(tmp_path),
             "session_id": "11111111-2222-3333-4444-555555555555"}), {"session_id": "chat-B"})
        await first
        return second

    second = asyncio.run(run())
    assert "busy with job" in second["error"]


def test_a_huge_event_line_does_not_stall_the_run(tmp_path, monkeypatch):
    """Seen live: stream-json events of 110 KB+ (a whole file read) passed
    asyncio's 64 KB line limit, the reader died, and the finished run was
    never noticed, so its result was never posted."""
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "appr.json"))
    import src.doc_pdf as doc_pdf

    async def no_pdf(*a, **k):
        return None, "skipped"
    monkeypatch.setattr(doc_pdf, "render_markdown_pdf", no_pdf)
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "claude")
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text(
        f"#!{sys.executable}\nimport json, sys\nsys.stdin.read()\n"
        "print(json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', "
        "'content': 'x' * 200000}]}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'result': 'Plan after a big read.', "
        "'is_error': False, 'duration_ms': 10}), flush=True)\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    out = asyncio.run(asyncio.wait_for(cct.ClaudeCodeTool().execute(
        json.dumps({"action": "ask", "prompt": "read it", "cwd": str(tmp_path)}), {}), timeout=20))
    assert out["exit_code"] == 0 and out["output"] == "Plan after a big read."
