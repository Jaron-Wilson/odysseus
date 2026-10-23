"""The coding agent's status/list report Odysseus's own runs of every engine
first, and the Claude Code CLI's sessions only in a separate labeled section.

Seen 2026-10-23: asked about OpenCode runs, `status` answered "no active job"
and offered an unrelated Claude CLI session, so the chat model claimed it had
no OpenCode status tool.
"""
import json
import time

import pytest

from src import agent_loop
from src import claude_code_jobs as jobs
from src import tool_index, tool_schemas
from src.agent_tools import claude_code_tool as cct

CLI_SESSION = {"id": "pearson1", "kind": "background", "state": "idle",
               "name": "pearson-figure-descriptions", "cwd": "/tmp/pearson"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(jobs, "_chat_name", lambda sid: "")


def _job(engine="opencode", action="execute", status="done", result=None,
         model="vllm3090/qwen3.8-27b", age_s=600, ran_s=300, job_id=""):
    j = jobs.register(job_id=job_id, chat_session_id="chat1", owner="", action=action,
                      cwd="/home/jaron/proj", model=model, engine=engine, prompt="do it")
    j.started = time.time() - age_s
    if status != "running":
        jobs.finish(j, status, result)
        j.finished = j.started + ran_s
    return j


def _cli(monkeypatch, sessions=(CLI_SESSION,), installed=True):
    monkeypatch.setattr(cct.shutil, "which", lambda n: "/usr/bin/claude" if installed else None)

    async def fake_list(self):
        return {"output": "x", "sessions": list(sessions), "count": len(sessions), "exit_code": 0}

    async def fake_text(self, sess):
        return cct.CLI_SECTION_TITLE + ":\n" + "\n".join(f"- `{s['id']}` {s['name']}" for s in sess)

    monkeypatch.setattr(cct.ClaudeCodeTool, "_list_agents", fake_list)
    monkeypatch.setattr(cct.ClaudeCodeTool, "_cli_sessions_text", fake_text)


async def _run(action, **kw):
    args = {"action": action, **kw}
    return await cct.ClaudeCodeTool().execute(json.dumps(args), {"session_id": "chat1"})


async def test_status_finished_opencode_job_shown(monkeypatch):
    _cli(monkeypatch)
    _job(result={"output": "Split-screen done. Pushed branch split-screen-view.", "exit_code": 0})
    out = (await _run("status"))["output"]
    assert "OpenCode execute" in out and "done" in out
    assert "Split-screen done" in out and "split-screen-view" in out
    assert "vllm3090/qwen3.8-27b" in out and "/home/jaron/proj" in out
    # the CLI section comes after the Odysseus runs
    assert out.index("OpenCode execute") < out.index(cct.CLI_SECTION_TITLE)
    assert "pearson-figure-descriptions" in out.split(cct.CLI_SECTION_TITLE)[1]


async def test_status_failed_and_stopped_show_reason(monkeypatch):
    _cli(monkeypatch, installed=False)
    _job(status="failed", result={"error": "opencode exited 1: model not found", "exit_code": 1})
    j = _job(status="stopped", engine="claude", action="plan")
    j.lines.extend(["thinking", "killed by user"])
    out = (await _run("status"))["output"]
    assert "failed" in out and "model not found" in out and "exit code 1" in out
    assert "stopped" in out and "Claude Code plan" in out and "killed by user" in out


async def test_job_id_lookup_of_finished_job(monkeypatch):
    _cli(monkeypatch)
    _job(result={"output": "All tests pass.", "exit_code": 0}, job_id="abcd1234")
    res = await _run("status", job_id="abcd1234")
    assert res["exit_code"] == 0 and "All tests pass." in res["output"]
    assert cct.CLI_SECTION_TITLE not in res["output"]
    res = await _run("status", job_id="nope9999")
    assert res["exit_code"] == 1 and "no coding run" in res["error"]


async def test_cli_sessions_only_in_labeled_section(monkeypatch):
    _cli(monkeypatch)
    out = (await _run("status"))["output"]
    assert "No coding runs" in out
    assert cct.CLI_SECTION_TITLE in out
    assert "No background Claude Code sessions" not in out
    # CLI missing: nothing about it at all
    _cli(monkeypatch, installed=False)
    out = (await _run("status"))["output"]
    assert "pearson" not in out and cct.CLI_SECTION_TITLE not in out
    assert "last 24 hours" in out and "restart" in out


async def test_list_shows_all_engines(monkeypatch):
    _cli(monkeypatch, sessions=())
    _job(engine="opencode")
    _job(engine="claude", result={"output": "ok", "exit_code": 0})
    _job(engine="antigravity", status="running")
    out = (await _run("list"))["output"]
    for label in ("OpenCode", "Claude Code", "Antigravity"):
        assert label in out
    assert out.index("running") < out.index("done")   # running first
    assert cct.CLI_SECTION_TITLE not in out


async def test_history_line_after_restart(monkeypatch, tmp_path):
    _cli(monkeypatch, installed=False)
    now = time.time()
    with open(jobs.HISTORY_FILE, "w") as f:
        f.write(json.dumps({"id": "old12345", "engine": "opencode", "model": "m", "action": "execute",
                            "status": "done", "started": now - 900, "finished": now - 600}) + "\n")
    out = (await _run("status"))["output"]
    assert "old12345" in out and "OpenCode" in out and "restart" in out


async def test_cap(monkeypatch):
    _cli(monkeypatch, installed=False)
    for i in range(20):
        _job(result={"output": f"r{i}", "exit_code": 0}, age_s=1000 + i)
    out = (await _run("status"))["output"]
    assert out.count("\n- `") == cct.MAX_RUNS_SHOWN


def test_pr_url_detected():
    refs = cct._run_refs("opened https://github.com/o/r/pull/12 done")
    assert refs == ["https://github.com/o/r/pull/12"]


def test_model_facing_text_is_engine_neutral():
    fn = next(t["function"] for t in tool_schemas.FUNCTION_TOOL_SCHEMAS
              if t["function"]["name"] == "claude_code")
    desc = fn["description"]
    assert "It runs the Claude Code agent" not in desc
    assert "OpenCode" in desc and "Antigravity" in desc
    assert "never say you cannot see OpenCode runs" in desc
    assert "OpenCode" in tool_index.BUILTIN_TOOL_DESCRIPTIONS["claude_code"]
    assert "NEVER say you cannot see OpenCode runs" in agent_loop.TOOL_SECTIONS["claude_code"]
