"""Coding agents post a status line back to Odysseus while they work.

Asked for on 2026-09-28: "what if we allow the coding agents to ping back to
the website so that we can get a little status update like how claude code
does it".
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

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE = r'''#!{py}
import json, os, sys, time
prompt = sys.stdin.read()
open({pf!r}, "w").write(prompt)
f = os.environ["ODYSSEUS_STATUS_FILE"]
def st(state, detail):
    with open(f, "a") as h: h.write(json.dumps({{"state": state, "detail": detail}}) + "\n")
st("working", "Renaming the branches")
time.sleep(0.6)
st("needs_input", "Which remote should I push to?")
st("needs_input", "Which remote should I push to?")
time.sleep(0.6)
st("done", "Both branches renamed")
print(json.dumps({{"type": "result", "result": "Renamed.", "is_error": False}}), flush=True)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text(FAKE.format(py=sys.executable, pf=str(tmp_path / "prompt.txt")))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "claude")
    jobs._JOBS.clear()
    sent = []

    async def fake_send(sid, notify, heading, body, **kw):
        sent.append((sid, heading, body, kw.get("kind")))
    from src import chat_queue
    monkeypatch.setattr(chat_queue, "send_notification", fake_send)
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "Branch rename")
    work = tmp_path / "work"
    work.mkdir()
    approvals.record_plan("11111111-2222-3333-4444-555555555555", cwd=str(work), plan="p",
                          engine="claude", chat_session_id="chat-1")
    approvals.set_status("11111111-2222-3333-4444-555555555555", "approved")
    return tmp_path, work, sent


def test_status_lines_show_up_live_and_needs_input_notifies_once(env):
    tmp, work, sent = env
    seen = []

    async def progress(p):
        if p.get("agent_status"):
            seen.append((p["agent_status"]["state"], p["agent_status"]["detail"]))

    async def run():
        return await cct.ClaudeCodeTool().execute(json.dumps({
            "action": "execute", "session_id": "11111111-2222-3333-4444-555555555555",
            "prompt": "Carry out the approved plan.", "cwd": str(work)}),
            {"session_id": "chat-1", "progress_cb": progress})
    monkeypatch_interval = cct.PROGRESS_INTERVAL_S
    cct.PROGRESS_INTERVAL_S = 0.2
    try:
        out = asyncio.run(run())
    finally:
        cct.PROGRESS_INTERVAL_S = monkeypatch_interval
    assert out["exit_code"] == 0
    job = jobs.list_jobs()[0]
    assert job.agent_status["state"] == "done" and job.agent_status["detail"] == "Both branches renamed"
    assert [t["state"] for t in job.timeline] == ["working", "needs_input", "needs_input", "done"]
    assert ("working", "Renaming the branches") in seen
    assert sent == [("chat-1", "Claude Code needs you: Branch rename", "Which remote should I push to?", "question")]
    prompt = (tmp / "prompt.txt").read_text()
    assert "$ODYSSEUS_STATUS_FILE" in prompt and "needs_input" in prompt
    assert job.public()["agent_status"]["detail"] == "Both branches renamed"


def test_only_build_runs_are_asked_to_report():
    src = open(os.path.join(HERE, "src", "agent_tools", "claude_code_tool.py"), encoding="utf-8").read()
    assert 'if action == "execute":\n            prompt = prompt.rstrip() + STATUS_INSTRUCTIONS' in src
    js = open(os.path.join(HERE, "static", "js", "chat.js"), encoding="utf-8").read()
    assert "json.agent_status && json.agent_status.detail" in js
    assert "_statusHtml(j.agent_status)" in open(os.path.join(HERE, "static", "js", "bgTasks.js")).read()


def test_garbage_lines_do_not_break_it(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    j = jobs.Job(chat_session_id="", owner="", action="execute", cwd="/w", model="m", engine="opencode", prompt="p")
    for raw in ("not json at all", '{"state": "weird", "detail": "x"}', "[]", ""):
        cct._take_status(j, raw)
    assert [t["state"] for t in j.timeline] == ["working", "working"]
