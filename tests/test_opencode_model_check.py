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
import os
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:], "extra": os.environ.get("OPENCODE_CONFIG_CONTENT", "")}}) + "\n")
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
    from src import opencode_providers
    monkeypatch.setattr(opencode_providers, "_endpoints", lambda: [])
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


# "when approving plans it wont allow me to pick a different server for the
# open code": Odysseus's own servers are offered to OpenCode too.
SERVERS = [
    {"id": "06e32a54", "name": "totoro:114335", "base_url": "http://100.102.86.125:11435/v1",
     "models": ["qwen3.8:27b", "gpt-oss:20b"], "api_key": ""},
    {"id": "3d297b8d", "name": "3090", "base_url": "http://192.168.100.101:8114/v1/",
     "models": ["qwen3.8-27b"], "api_key": ""},
]


def test_odysseus_servers_opencode_lacks_are_added(env, monkeypatch):
    from src import opencode_providers as op
    own = {"provider": {"vllm3090": {"options": {"baseURL": "http://192.168.100.101:8114/v1"},
                                     "models": {"qwen3.8-27b": {}}}}}
    monkeypatch.setattr(op, "_own_config", lambda: own)
    monkeypatch.setattr(op, "_endpoints", lambda: SERVERS)
    prov = op.providers()
    assert list(prov) == ["ody-totoro-114335"]                 # the 3090 is already OpenCode's
    assert prov["ody-totoro-114335"]["options"]["baseURL"] == "http://100.102.86.125:11435/v1"
    ids = [m for m, _ in op.models()[0]]
    assert ids == ["vllm3090/qwen3.8-27b", "ody-totoro-114335/qwen3.8:27b", "ody-totoro-114335/gpt-oss:20b"]
    assert json.loads(op.config_content())["provider"]["ody-totoro-114335"]["models"]["gpt-oss:20b"]


def test_a_run_on_an_odysseus_server_gets_it_handed_to_opencode(env, monkeypatch):
    work, log = env
    from src import opencode_providers as op
    monkeypatch.setattr(op, "_endpoints", lambda: SERVERS[:1])
    out = _plan(work, "ody-totoro-114335/qwen3.8:27b")
    assert out.get("exit_code") != 2, out
    call = _calls(log)[-1]
    assert call["argv"][call["argv"].index("--model") + 1] == "ody-totoro-114335/qwen3.8:27b"
    assert "ody-totoro-114335" in json.loads(call["extra"])["provider"]


def test_the_approve_dialog_lists_them(env, monkeypatch):
    from src import opencode_providers as op
    import routes.claude_code_routes as ccr
    monkeypatch.setattr(op, "_endpoints", lambda: SERVERS[:1])
    ids = [m["id"] if isinstance(m, dict) else m[0] for m in
           next(e for e in ccr.run_options("", {})["engines"] if e["id"] == "opencode")["models"]]
    assert "ody-totoro-114335/qwen3.8:27b" in ids
