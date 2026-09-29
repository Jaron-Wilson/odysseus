"""A coding-agent run that goes silent is stopped, and the chat is told why.

Seen 2026-09-29: two OpenCode runs sat without a line of output for 20 and 40
minutes (their model server had stopped answering), and the chat showed a
bare tool call the whole time: "there should not ever ... just be a plain
old tool call at the bottom of the page it should always end with what an
agent says".
"""
import asyncio
import json
import os
import stat
import sys
import time

from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

SILENT = r'''#!{py}
import time
time.sleep(60)
'''


def test_a_silent_run_is_stopped_with_a_reason(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "opencode"
    exe.write_text(SILENT.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    if hasattr(jobs, "HISTORY_FILE"):
        monkeypatch.setattr(jobs, "HISTORY_FILE", str(tmp_path / "h.jsonl"))
    from src import opencode_providers
    monkeypatch.setattr(opencode_providers, "_endpoints", lambda: [])
    monkeypatch.setattr(cct, "STALL_S", 1)
    jobs._JOBS.clear()
    work = tmp_path / "work"
    work.mkdir()
    args = {"action": "ask", "engine": "opencode", "cwd": str(work), "prompt": "What does main.py do?"}
    t0 = time.time()
    out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(args), {"session_id": ""}))
    assert time.time() - t0 < 30                              # not the full minute
    assert out.get("stalled") and out["exit_code"] == 1
    assert "wrote nothing" in out["error"] and "Tell the user" in out["error"]
    assert "looks stuck" in out.get("output", "")
