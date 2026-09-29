"""A run brought back into the chat stops with the chat's Stop, and the chat
can find it by its job id.

Seen 2026-09-29: "had to go to background tasks and press stop there the
inchat does not do it". A brought-back run carried on when the chat's Stop
was pressed. And the chat's model could not follow it: 'attach' was not in
the tool's actions, and status answered "no background session matching
fd81af50", as it only read the Claude CLI's own sessions.
"""
import asyncio
import json

import pytest

from src import agent_runs
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    if hasattr(jobs, "HISTORY_FILE"):
        monkeypatch.setattr(jobs, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(cct, "PROGRESS_INTERVAL_S", 0.02)
    jobs._JOBS.clear()
    killed = []
    monkeypatch.setattr(jobs, "kill_run", lambda j: killed.append(j.id) or True)
    monkeypatch.setattr(agent_runs, "restarting", False)
    j = jobs.register(chat_session_id="chat-1", owner="jaron", action="execute", cwd=str(tmp_path),
                      model="ody-totoro-114335/qwen3.8:27b", engine="opencode", prompt="music bar")
    j.detached = True
    j.lines.append("Now the now_playing loop")
    return j, killed


def _stop_while_attached(j):
    async def go():
        t = asyncio.ensure_future(cct.ClaudeCodeTool()._attach(j.id, {}))
        await asyncio.sleep(0.1)
        assert j.attached
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())


def test_the_chats_stop_stops_a_brought_back_run(job):
    j, killed = job
    _stop_while_attached(j)
    assert killed == [j.id] and j.status == "stopped"


def test_a_restart_leaves_it_running(job, monkeypatch):
    j, killed = job
    monkeypatch.setattr(agent_runs, "restarting", True)
    _stop_while_attached(j)
    assert killed == [] and j.status == "running" and not j.attached


def test_stop_all_says_it_is_a_restart(monkeypatch):
    monkeypatch.setattr(agent_runs, "restarting", False)
    asyncio.run(agent_runs.stop_all(timeout=0.1))
    assert agent_runs.restarting is True


def test_status_finds_odysseus_runs_by_job_id(job):
    j, _ = job
    out = asyncio.run(cct.ClaudeCodeTool()._job_status(j.id, "chat-1"))
    assert out["exit_code"] == 0
    assert j.id in out["output"] and "running" in out["output"] and "attach" in out["output"]
    assert "now_playing" in out["output"]
    # And without an id: the runs of this chat.
    out = asyncio.run(cct.ClaudeCodeTool()._job_status("", "chat-1"))
    assert j.id in out["output"]


def test_attach_is_one_of_the_tools_actions():
    from src import tool_schemas
    text = open(tool_schemas.__file__, encoding="utf-8").read()
    i = text.index('"name": "claude_code"')
    schema = text[i:i + 6000]
    assert '"attach"]' in schema and "'Bring back'" in schema
