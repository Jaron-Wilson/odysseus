"""Restarts that overlap: nothing lost, nothing run twice.

Seen live on 2026-09-28: a restart started the new server while the old one
was still finishing a reply. The old one started a planning run after the new
one had done its startup reattach, then exited mid-turn: the reply was never
saved and the run's result was never posted ("it glitched out and now I
don't see anything"). Also: a branch rename (six git commands) went through
an OpenCode plan.
"""
import asyncio
import json
import os
import time

import pytest

from src import agent_runs
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    jobs._JOBS.clear()
    yield tmp_path
    jobs._JOBS.clear()


def _orphan(store, job_id, server_pid, exited=True):
    d = store / "runs" / job_id
    d.mkdir(parents=True)
    (d / "out.jsonl").write_text(json.dumps({"type": "result", "result": "Rename plan.", "is_error": False}) + "\n")
    if exited:
        (d / "exit").write_text("0\n")
    return {"id": job_id, "chat_session_id": "chat-1", "owner": "", "action": "ask", "cwd": "/w",
            "model": "m", "engine": "claude", "prompt": "p", "cli_session_id": "", "plan_id": "",
            "pid": 999999, "started": time.time(), "detached": True, "banner": "b", "notify": None,
            "spec": {"action": "ask", "timeout": 60, "started": time.time()}, "server_pid": server_pid}


def test_records_of_another_live_server_survive_a_save(store):
    other = _orphan(store, "a1b2c3d4", server_pid=os.getppid())       # a live process, not us
    with open(jobs.JOBS_FILE, "w") as f:
        json.dump([other], f)
    jobs.save()                                                        # we have no running jobs
    assert [r["id"] for r in jobs.load_records()] == ["a1b2c3d4"]


def test_the_sweep_adopts_only_runs_no_live_server_follows(store, monkeypatch):
    posted = []

    async def fake_post(job, result):
        posted.append((job.id, result.get("output")))
    monkeypatch.setattr(cct, "_post_background_result", fake_post)
    followed = _orphan(store, "f0110wed", server_pid=os.getppid())    # its server is alive
    orphan = _orphan(store, "0rphan01", server_pid=4_000_000)         # its server is gone
    legacy = _orphan(store, "1egacy01", server_pid=None)              # recorded before this change
    with open(jobs.JOBS_FILE, "w") as f:
        json.dump([followed, orphan, legacy], f)

    async def run():
        n = cct.reattach_runs()
        await asyncio.gather(*[j.task for j in jobs._JOBS.values() if j.task])
        return n
    assert asyncio.run(run()) == 2
    assert sorted(p[0] for p in posted) == ["0rphan01", "1egacy01"]
    assert all(out == "Rename plan." for _, out in posted)


def test_running_replies_are_stopped_and_saved_before_a_deploy():
    saved = []

    async def turn():
        try:
            yield "data: {\"delta\": \"half a reply\"}\n\n"
            await asyncio.sleep(30)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            saved.append("partial saved")
            raise
        finally:
            saved.append("closed")

    async def run():
        agent_runs.start("chat-x", turn())
        await asyncio.sleep(0.1)
        n = await agent_runs.stop_all(timeout=5)
        return n
    assert asyncio.run(run()) == 1
    assert "closed" in saved and agent_runs.get_status("chat-x") == "stopped"


def test_a_list_of_commands_is_refused_as_a_chore():
    rename = ("Quick branch-rename task on the laptop:\n"
              "   - git branch -m pastor-notes jaron-pastor-notes\n"
              "   - git push origin jaron-pastor-notes\n"
              "   - git push origin --delete pastor-notes\n")
    out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps({"prompt": rename, "cwd": "/tmp"}), {}))
    assert out["chore"] and "Run them yourself with bash" in out["error"]
    assert not cct._is_command_list("Implement the giving page, then:\n- git add .\n- git commit\n- git push")
