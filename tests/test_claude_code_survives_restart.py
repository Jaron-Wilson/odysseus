"""Claude Code runs outlive a server restart, are never started twice, and
Approve starts the run at once with an id the chat can show.

Seen live on 2026-09-27: after a restart the Approve link answered "(502)"
(the proxy, while the server came back), and a restart during an approved run
killed the CLI, so approving again started it over. The user: "if a server
restarts and claude code is already running it should be able to continue
without spinning up 10 different claude codes doing the same thing".
"""
import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_CLI = r'''#!{python}
import json, sys, time
sys.stdin.read()
def out(o):
    print(json.dumps(o), flush=True)
out({{"type": "system", "subtype": "init", "session_id": "abc12345"}})
out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Working on it."}}]}}}})
time.sleep({delay})
out({{"type": "result", "result": "All done.", "is_error": False}})
'''


class _SM:
    def __init__(self):
        self.msgs = []

    def add_message(self, sid, msg):
        self.msgs.append((sid, msg))

    def save_sessions(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def make(delay=0.0):
        exe = bindir / "claude"
        exe.write_text(FAKE_CLI.format(python=sys.executable, delay=delay))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "appr.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    from src import claude_code_agents as agents_reg
    monkeypatch.setattr(agents_reg, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agents_reg, "AGENTS_FILE", str(tmp_path / "agents.json"))
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "claude")

    async def no_pdf(*a, **k):
        return None, "skipped in tests"
    import src.doc_pdf as doc_pdf
    monkeypatch.setattr(doc_pdf, "render_markdown_pdf", no_pdf)
    sm = _SM()
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    jobs._JOBS.clear()
    yield make, sm
    for j in list(jobs._JOBS.values()):
        if j.status == "running":
            jobs.kill_run(j)
    jobs._JOBS.clear()


def test_a_run_outlives_the_server_and_is_reattached(env, tmp_path):
    make, sm = env
    make(delay=3)
    # The "server": a separate process running the tool, killed mid-run the
    # way a restart kills it.
    server = tmp_path / "server.py"
    server.write_text(textwrap.dedent(f"""
        import asyncio, json, sys
        sys.path.insert(0, {HERE!r})
        from src import claude_code_approvals as approvals, claude_code_jobs as jobs
        from src.agent_tools import claude_code_tool as cct
        approvals.DATA_DIR = {str(tmp_path)!r}
        approvals.APPROVALS_FILE = {str(tmp_path / "appr.json")!r}
        jobs.RUNS_DIR = {str(tmp_path / "runs")!r}
        jobs.JOBS_FILE = {str(tmp_path / "jobs.json")!r}
        cct.DEFAULT_ENGINE = "claude"
        asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(
            {{"action": "ask", "prompt": "long job", "cwd": {str(tmp_path)!r}}}),
            {{"session_id": "chat-1", "owner": ""}}))
    """))
    proc = subprocess.Popen([sys.executable, str(server)], cwd=HERE,
                            env={**os.environ, "DATABASE_URL": "sqlite:///:memory:"})
    try:
        for _ in range(150):
            time.sleep(0.1)
            recs = jobs.load_records()
            if recs and recs[0].get("pid"):
                break
        assert recs, "the run was never recorded"
        rec = recs[0]
        time.sleep(0.5)
    finally:
        proc.send_signal(signal.SIGKILL)                  # the server dies
        proc.wait()
    assert jobs.pid_alive(rec["pid"], os.path.join(jobs.RUNS_DIR, rec["id"])), \
        "the CLI died with the server"

    async def restart():
        assert cct.reattach_runs() == 1
        job = jobs.get(rec["id"])
        assert job.reattached and job.status == "running"
        # A second attempt at the same agent is refused, not started.
        again = await cct.ClaudeCodeTool().execute(json.dumps(
            {"action": "ask", "prompt": "again", "cwd": str(tmp_path),
             "session_id": job.cli_session_id}), {"session_id": "chat-1"})
        assert "busy with job" in again["error"]
        await asyncio.wait_for(job.task, timeout=20)
        return job

    job = asyncio.run(restart())
    assert job.status == "done"
    sid, msg = sm.msgs[-1]
    assert sid == "chat-1"
    assert "All done." in msg.content and "kept running through a server restart" in msg.content
    assert "Working on it." in msg.metadata["tool_events"][0]["output"]
    assert jobs.load_records() == []                      # nothing left to reattach


def test_a_finished_while_down_run_is_still_posted(env, tmp_path):
    """The CLI finished while the server was down: its result still lands."""
    make, sm = env
    job = jobs.register(chat_session_id="chat-2", owner="", action="ask", cwd=str(tmp_path),
                        model="sonnet", engine="claude", prompt="p")
    os.makedirs(job.run_dir)
    with open(os.path.join(job.run_dir, "out.jsonl"), "w") as f:
        f.write(json.dumps({"type": "result", "result": "Finished offline.", "is_error": False}) + "\n")
    with open(os.path.join(job.run_dir, "exit"), "w") as f:
        f.write("0\n")
    job.pid = 999999
    job.spec = {"action": "ask", "cwd": str(tmp_path), "timeout": 60, "started": time.time()}
    jobs.save()
    jobs._JOBS.clear()

    async def restart():
        assert cct.reattach_runs() == 1
        j = jobs.get(job.id)
        await asyncio.wait_for(j.task, timeout=10)
        return j

    j = asyncio.run(restart())
    assert j.status == "done" and "Finished offline." in sm.msgs[-1][1].content


def test_an_approved_plan_never_runs_twice(env, tmp_path):
    make, sm = env
    make(delay=0)
    approvals.record_plan("plan-1", cwd=str(tmp_path), plan="do it", chat_session_id="chat-3")
    approvals.set_status("plan-1", "approved")
    run_id = approvals.assign_run_id("plan-1")
    assert run_id and approvals.assign_run_id("plan-1") == run_id      # fixed once
    live = jobs.register(job_id=run_id, chat_session_id="chat-3", owner="", action="execute",
                         cwd=str(tmp_path), model="sonnet", engine="claude", prompt="p")
    live.plan_id = "plan-1"
    out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(
        {"action": "execute", "session_id": "plan-1", "prompt": "go", "cwd": str(tmp_path)}),
        {"session_id": "chat-3"}))
    assert out["already_running"] and out["job_id"] == run_id
    assert "Do NOT start it again" in out["error"]
    assert approvals.get("plan-1")["status"] == "approved"             # not spent
    # Once that run is over, the approval runs under the id the chat was shown.
    jobs.finish(live, "failed")
    out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(
        {"action": "execute", "session_id": "plan-1", "prompt": "go", "cwd": str(tmp_path)}),
        {"session_id": "chat-3"}))
    assert out["exit_code"] == 0 and out["job_id"] == run_id


def test_an_untracked_cli_on_the_same_session_is_found(env, tmp_path):
    make, sm = env
    exe = tmp_path / "bin" / "claude"
    exe.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(20)\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    sid = "0f0f0f0f-1111-2222-3333-444444444444"
    p = subprocess.Popen([str(exe), "-p", "--resume", sid])
    try:
        time.sleep(0.3)
        assert cct._os_process_for(sid) == p.pid
        out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(
            {"action": "ask", "prompt": "x", "cwd": str(tmp_path), "session_id": sid}), {}))
        assert out["already_running"] and out["pid"] == p.pid
    finally:
        p.kill()
        p.wait()
    assert cct._os_process_for(sid) is None


def test_approve_starts_the_run_and_a_retry_gets_the_same_id(env, tmp_path, monkeypatch):
    started = []
    import src.screen_control_resume as scr
    monkeypatch.setattr(scr, "start_turn", lambda sid, prompt, **kw: started.append((sid, prompt)) or True)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from routes.claude_code_routes import setup_claude_code_routes
    import routes.claude_code_routes as ccr
    monkeypatch.setattr(ccr, "_auth_disabled", lambda: True)
    monkeypatch.setattr(ccr, "get_current_user", lambda r: "")
    app = FastAPI()
    app.include_router(setup_claude_code_routes())
    c = TestClient(app)
    approvals.record_plan("plan-2", cwd=str(tmp_path), plan="p", chat_session_id="chat-4")
    r = c.post("/api/claude_code/approve/plan-2").json()
    assert r["status"] == "approved" and r["resuming"] and r["chat_session_id"] == "chat-4"
    assert len(r["run_id"]) == 8
    sid, prompt = started[0]
    assert sid == "chat-4" and f"run {r['run_id']}" in prompt and '"session_id": "plan-2"' in prompt
    again = c.post("/api/claude_code/approve/plan-2").json()
    assert again["already"] and again["run_id"] == r["run_id"] and len(started) == 1


def test_page_retries_through_a_restart_and_attaches():
    js = open(os.path.join(HERE, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert "[502, 503, 504].includes(res.status)" in js and "Server restarting, retrying" in js
    assert "Plan approved · run ${d.run_id}" in js and "cm.resumeStream(d.chat_session_id)" in js
    app = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    assert "reattach_runs()" in app
