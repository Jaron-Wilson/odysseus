"""OpenCode runs: the label names the model really used, and a run that only
produces empty steps is stopped.

2026-11-01: OpenCode ran with no model passed and fell back to the global
default "odysseus/auto", the Odysseus gateway, which answered the unauthenticated
call with a redirect. OpenCode recorded an empty step (0 tokens) and retried
4,878 times in 27 minutes while the job stayed "running" and the screen said
"local default (vllm3090/qwen3.8-27b)", a hard-coded label.
"""
import asyncio
import json
import os
import stat
import sys
import time

import pytest

from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src import opencode_providers as op
from src.agent_tools import claude_code_tool as cct


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".config" / "opencode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    monkeypatch.setattr(op, "_endpoints", lambda: [])
    return h


def _cfg(home, name="opencode.json", text="{}"):
    (home / ".config" / "opencode" / name).write_text(text)


# ── the label ────────────────────────────────────────────────────────────

def test_label_is_the_global_default_model(home):
    _cfg(home, text=json.dumps({"model": "odysseus/auto"}))
    assert cct.opencode_default_label({}) == "odysseus/auto"


def test_label_prefers_the_model_in_the_config_content(home):
    _cfg(home, text=json.dumps({"model": "odysseus/auto"}))
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps({"model": "ody-box/qwen", "provider": {}})}
    assert cct.opencode_default_label(env) == "ody-box/qwen"


def test_label_reads_jsonc_with_comments_and_trailing_commas(home):
    _cfg(home, "opencode.jsonc", '{\n  // my default\n  "url": "http://x//y", /* c */\n  "model": "a/b",\n}\n')
    assert cct.opencode_default_label({}) == "a/b"


def test_label_follows_opencode_config_env(home, tmp_path):
    _cfg(home, text=json.dumps({"model": "global/one"}))
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"model": "custom/two"}))
    assert cct.opencode_default_label({"OPENCODE_CONFIG": str(other)}) == "custom/two"
    other.write_text("{}")                          # no model there: the global one stays
    assert cct.opencode_default_label({"OPENCODE_CONFIG": str(other)}) == "global/one"


def test_label_falls_back_when_nothing_is_set_or_the_file_is_bad(home):
    assert cct.opencode_default_label({}) == "opencode default"
    _cfg(home, text="{not json")
    assert cct.opencode_default_label({}) == "opencode default"
    assert cct.opencode_default_label({"OPENCODE_CONFIG_CONTENT": "nope",
                                       "OPENCODE_CONFIG": "/nonexistent.json"}) == "opencode default"


def test_runs_on_never_invents_a_model(home):
    _cfg(home, text=json.dumps({"model": "odysseus/auto"}))
    assert cct.runs_on("opencode", "") == "OpenCode · odysseus/auto"
    assert cct.runs_on("opencode", "ody-x/m") == "OpenCode · ody-x/m"


# ── the watchdog ─────────────────────────────────────────────────────────

def _empty_step():
    return json.dumps({"type": "step_finish", "part": {"reason": "unknown", "cost": 0,
                       "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}}})


def _started():
    return json.dumps({"type": "step_start", "part": {}})


@pytest.fixture
def run_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(jobs, "HISTORY_FILE", str(tmp_path / "h.jsonl"))
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_chat_name", lambda sid: "")


def _job():
    j = jobs.register(job_id="", chat_session_id="c1", owner="", action="ask",
                      cwd="/p", model="odysseus/auto", engine="opencode", prompt="hi")
    os.makedirs(j.run_dir, exist_ok=True)
    j.spec = {"engine": "opencode", "model_label": "odysseus/auto", "action": "ask"}
    return j


def _follow_file(job, lines, killed):
    """Run _follow over a fake out.jsonl until the watchdog (or the end) stops it."""
    path = os.path.join(job.run_dir, "out.jsonl")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    stream = cct._Stream("opencode")
    seen = []
    state = {"polls": 0}

    def exited():
        state["polls"] += 1
        return killed() or state["polls"] > 6

    asyncio.run(cct._follow(job, stream, seen.append, exited, cct._FileTail(path)))
    return stream, seen


def test_empty_steps_in_a_row_stop_the_run(run_dirs, monkeypatch):
    j = _job()
    kills = []
    monkeypatch.setattr(jobs, "kill_run", lambda job: kills.append(job.id) or True)
    lines = [ln for _ in range(cct.EMPTY_STEP_LIMIT + 5) for ln in (_started(), _empty_step())]
    stream, seen = _follow_file(j, lines, lambda: bool(kills))
    assert kills == [j.id]
    assert j.spec["empty_loop"] >= cct.EMPTY_STEP_LIMIT
    assert any("empty replies in a row" in ln for ln in seen)

    result = asyncio.run(cct._build_result(j, stream, -9, False))
    assert result["exit_code"] == 1 and result["empty_loop"]
    assert f"OpenCode got empty replies from odysseus/auto {j.spec['empty_loop']} times in a row" in result["error"]
    assert "is the model reachable and the token valid?" in result["error"]
    assert cct.settle(j, stream, result) == "failed"


def test_text_or_a_tool_call_between_empty_steps_resets_the_count(run_dirs, monkeypatch):
    j = _job()
    kills = []
    monkeypatch.setattr(jobs, "kill_run", lambda job: kills.append(job.id) or True)
    n = cct.EMPTY_STEP_LIMIT - 1
    between = [json.dumps({"type": "text", "part": {"text": "thinking"}}),
               json.dumps({"type": "tool", "part": {"tool": "read", "state": {"input": {"filePath": "a"}}}})]
    lines = []
    for mid in between + between:
        lines += [_empty_step() for _ in range(n)] + [mid]
    lines += [_empty_step() for _ in range(n)]
    stream, _ = _follow_file(j, lines, lambda: bool(kills))
    assert not kills and not j.spec.get("empty_loop")
    assert stream.empty_steps == n


def test_a_step_with_output_tokens_is_not_empty():
    s = cct._Stream("opencode")
    for _ in range(cct.EMPTY_STEP_LIMIT - 1):
        s.feed(_empty_step())
    s.feed(json.dumps({"type": "step_finish", "part": {"reason": "tool-calls", "tokens": {"output": 12}}}))
    assert s.empty_steps == 0
    s.feed(json.dumps({"type": "step_finish", "part": {"reason": "stop"}}))     # no counts: unknown
    assert s.empty_steps == 0


def test_a_looping_opencode_is_killed_and_the_job_fails(tmp_path, monkeypatch, run_dirs, home):
    """End to end with a fake opencode that prints empty steps forever."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "opencode"
    exe.write_text(
        f"#!{sys.executable}\nimport json, sys, time\n"
        "while True:\n"
        "    print(json.dumps({'type': 'step_start', 'part': {}}))\n"
        "    print(json.dumps({'type': 'step_finish', 'part': {'reason': 'unknown', 'tokens': "
        "{'input': 0, 'output': 0, 'reasoning': 0}}}))\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.01)\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    _cfg(home, text=json.dumps({"model": "odysseus/auto"}))
    work = tmp_path / "work"
    work.mkdir()
    args = {"action": "ask", "engine": "opencode", "cwd": str(work), "prompt": "What does main.py do?"}
    t0 = time.time()
    out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(args), {"session_id": ""}))
    assert time.time() - t0 < 60
    assert out.get("empty_loop") and out["exit_code"] == 1
    assert "empty replies from odysseus/auto" in out["error"]
    job = next(iter(jobs._JOBS.values()))
    assert job.status == "failed"
    assert job.model == "odysseus/auto"                 # the label is the real model
    time.sleep(0.3)
    assert not jobs.pid_alive(job.pid, job.run_dir)     # the process group is gone
