"""OpenCode runs only with a model OpenCode knows, and says which ones it does.

Seen 2026-09-29: an OpenCode plan with model "totoro/qwen3.8:27b" (an Odysseus
endpoint OpenCode has no provider for) exited 1, and the error added "valid
values are 'opus', 'sonnet', 'haiku'" (Claude's names). The agent then
switched the server's default model to get one that worked.
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

FAKE = r'''#!{py}
import json, sys
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:]}}) + "\n")
print(json.dumps({{"type": "text", "part": {{"text": "Plan: 1. look 2. do"}}}}), flush=True)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    exe = bindir / "opencode"
    exe.write_text(FAKE.format(py=sys.executable, log=str(log)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text(json.dumps({
        "model": "vllm3090/qwen3.8-27b",
        "provider": {
            "vllm3090": {"models": {"qwen3.8-27b": {}}},
            "ollama-desktop": {"models": {"qwen3:8b": {}, "qwen2.5vl:7b": {}}},
        }}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    jobs._JOBS.clear()
    work = tmp_path / "work"
    work.mkdir()
    return work, log


def _plan(work, model):
    args = {"action": "plan", "engine": "opencode", "cwd": str(work), "prompt": "Plan the CI previews."}
    if model:
        args["model"] = model
    return asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(args), {"session_id": ""}))


def _calls(log):
    return [json.loads(l) for l in open(log)] if os.path.exists(log) else []


def test_the_models_come_from_opencodes_own_config(env):
    assert cct.opencode_model_ids() == ["vllm3090/qwen3.8-27b", "ollama-desktop/qwen3:8b",
                                        "ollama-desktop/qwen2.5vl:7b"]


def test_an_unknown_model_is_refused_before_anything_runs(env):
    work, log = env
    out = _plan(work, "totoro/qwen3.8:27b")
    assert out["exit_code"] == 2 and not _calls(log)
    err = out["error"]
    assert "totoro/qwen3.8:27b" in err and "vllm3090/qwen3.8-27b" in err
    assert "opus" not in err                                   # not Claude's names
    assert "Do not change Odysseus settings" in err


def test_a_known_model_or_none_runs(env):
    work, log = env
    _plan(work, "ollama-desktop/qwen3:8b")
    argv = _calls(log)[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "ollama-desktop/qwen3:8b"
    _plan(work, "")
    assert "--model" not in _calls(log)[-1]["argv"]


def test_a_failed_opencode_run_gets_opencode_advice():
    src = open(cct.__file__, encoding="utf-8").read()
    i = src.index('if spec.get("args_model") and spec.get("engine", job.engine) == "opencode":')
    assert "OpenCode's models are" in src[i:i + 400]
