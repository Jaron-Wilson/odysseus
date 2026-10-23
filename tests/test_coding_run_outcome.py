"""Honest run outcomes (done / incomplete / failed) and OpenCode's scratch dir.

Seen 2026-10-23: an OpenCode run exited 1 right after a "Where we are" note and
was recorded as done; another exited 0 mid-work after OpenCode auto-rejected
/tmp. See classify_outcome, settle and scratch_env in claude_code_tool.
"""
import json
import os
import time

import pytest

from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

NOTE = "## Where we are\n\nIn progress: wiring the routes. Next: tests."


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(jobs, "_chat_name", lambda sid: "")


def _run(code, state=None, detail="", text="", err="", reason="stop", action="execute"):
    j = jobs.register(job_id="", chat_session_id="c1", owner="", action=action,
                      cwd="/p", model="m", engine="opencode", prompt="do it")
    os.makedirs(j.run_dir, exist_ok=True)
    with open(os.path.join(j.run_dir, "exit"), "w") as f:
        f.write(str(code))
    with open(os.path.join(j.run_dir, "err.log"), "w") as f:
        f.write(err)
    if state:
        j.agent_status = {"state": state, "detail": detail, "at": time.time()}
    j.cli_session_id = "ses_abc"
    s = cct._Stream("opencode")
    s.feed(json.dumps({"type": "text", "part": {"text": text}}))
    s.feed(json.dumps({"type": "step_finish", "part": {"reason": reason}}))
    result = {"output": text, "exit_code": 0}
    return j, s, result


def test_exit1_after_progress_note_is_failed_not_done():
    j, s, res = _run(1, "working", "Wiring routes", NOTE)
    assert cct.settle(j, s, res) == "failed"
    assert j.exit_code == 1 and res["exit_code"] == 1
    assert "did NOT finish" in res["output"] and "Where we are" in res["output"]
    assert "action 'execute'" in res["next_step"] and "ses_abc" in res["next_step"]
    j.status = "failed"
    assert jobs.ended_early_line(j) == "ended early (exit 1), last state: working: Wiring routes"


def test_exit0_still_working_is_incomplete():
    j, s, res = _run(0, "working", "Verifying", "Let me verify the JavaScript files...")
    assert cct.settle(j, s, res) == "incomplete"
    assert res["incomplete"] and "exit 0" in res["ended_early"]


def test_exit0_done_state_is_done():
    j, s, res = _run(0, "done", "All merged", "Finished. PR opened.")
    assert cct.settle(j, s, res) == "done"
    assert "incomplete" not in res


def test_exit0_working_but_real_final_answer_is_done():
    j, s, res = _run(0, "working", "x", "All four routes now return 200 and the tests pass.")
    assert cct.settle(j, s, res) == "done"


def test_exit0_no_state_progress_note_is_incomplete():
    j, s, res = _run(0, None, "", NOTE)
    assert cct.settle(j, s, res) == "incomplete"


def test_last_event_tool_calls_is_incomplete():
    j, s, res = _run(0, "working", "x", "All done here.", reason="tool-calls")
    assert cct.settle(j, s, res) == "incomplete"


def test_non_execute_runs_keep_old_status():
    j, s, res = _run(0, "working", "x", NOTE, action="ask")
    assert cct.settle(j, s, res) == "done"


def test_refused_path_surfaced_and_recorded():
    err = "permission requested: external_directory (/tmp/*); auto-rejecting\n"
    j, s, res = _run(0, "done", "ok", "Finished.", err=err)
    assert cct.settle(j, s, res) == "done"
    assert j.outcome["refused"] == ["/tmp/*"]
    assert "OpenCode refused access outside the project: /tmp/*" in res["output"]


def test_status_and_list_show_early_end():
    j, s, res = _run(1, "working", "Wiring routes", NOTE)
    jobs.finish(j, cct.settle(j, s, res), res)
    assert j.status == "failed"
    text = "\n".join(cct._run_lines(j, "c1"))
    assert "ended early (exit 1), last state: working: Wiring routes" in text
    assert "Where we are" in text and "did NOT finish" in text
    assert "action 'execute'" in text
    pub = j.public()
    assert pub["exit_code"] == 1 and pub["ended_early"].startswith("ended early")
    with open(jobs.HISTORY_FILE) as f:
        rec = json.loads(f.read().splitlines()[-1])
    assert rec["status"] == "failed" and rec["exit_code"] == 1


def test_incomplete_status_line_and_unknown_status_safe():
    j, s, res = _run(0, "working", "Verifying", "Let me check:")
    jobs.finish(j, cct.settle(j, s, res), res)
    assert j.status == "incomplete"
    assert "incomplete -" in cct._run_lines(j, "c1")[0]
    j.status = "something_new"          # an unknown string must not break listing
    cct._run_lines(j, "c1")
    j.public()


def test_scratch_env_is_narrow(tmp_path):
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps({"provider": {"p": {}}})}
    scratch = cct.scratch_env(env, str(tmp_path / "run1"))
    assert os.path.isdir(scratch) and scratch.endswith("/run1/tmp")
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == scratch
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["provider"] == {"p": {}}
    assert cfg["permission"]["external_directory"] == {scratch + "/*": "allow"}


def test_scratch_env_without_existing_config(tmp_path):
    env = {}
    scratch = cct.scratch_env(env, str(tmp_path / "r"))
    perm = json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]["external_directory"]
    assert perm == {scratch + "/*": "allow"}
