"""Choose the coder and model when approving a plan.

Asked for on 2026-09-28: "when approving the plans, let me manually switch
from each type of coder, ie: opencode, claude code, and the models too, make
that popup look nicer when I press approve, and then it gives me the options."
"""
import asyncio
import json
import os
import stat
import sys

import pytest
from fastapi import HTTPException

from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Records what it was started with, then answers like the real CLI.
FAKE = r'''#!{py}
import json, sys
stdin = sys.stdin.read()
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin}}) + "\n")
print(json.dumps({{"type": "result", "result": "Done as planned.", "is_error": False}}), flush=True)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    for name in ("claude", "opencode"):
        exe = bindir / name
        exe.write_text(FAKE.format(py=sys.executable, log=str(log)))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    jobs._JOBS.clear()
    work = tmp_path / "work"
    work.mkdir()
    return tmp_path, work, log


def _calls(log):
    return [json.loads(l) for l in open(log)] if os.path.exists(log) else []


def _approve(sid, work, *, engine, model, run_engine="", run_model=""):
    approvals.record_plan(sid, cwd=str(work), plan="1. Build the preview\n2. Deploy it", owner="",
                          model=model, engine=engine, chat_session_id="")
    approvals.set_status(sid, "approved", owner="")
    approvals.set_run_choice(sid, run_engine, run_model)


def _execute(sid, work):
    return asyncio.run(cct.ClaudeCodeTool().execute(
        json.dumps({"action": "execute", "session_id": sid, "cwd": str(work),
                    "prompt": "Carry out the approved plan."}), {"session_id": ""}))


def test_an_opencode_plan_runs_on_claude_code_when_chosen(env):
    tmp, work, log = env
    _approve("ses_plan0001aaaa", work, engine="opencode", model="vllm3090/qwen3.8-27b",
             run_engine="claude", run_model="haiku")
    out = _execute("ses_plan0001aaaa", work)
    assert out["exit_code"] == 0, out
    call = _calls(log)[-1]
    argv = call["argv"]
    assert "--session-id" in argv and "--resume" not in argv          # fresh: the plan's session is OpenCode's
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "<approved-plan>" in call["stdin"] and "2. Deploy it" in call["stdin"]
    assert approvals.get("ses_plan0001aaaa")["status"] == "used"


def test_same_engine_carries_on_the_plan_session_with_the_chosen_model(env):
    tmp, work, log = env
    _approve("11111111-2222-3333-4444-555555555555", work, engine="claude", model="opus",
             run_engine="claude", run_model="sonnet")
    _execute("11111111-2222-3333-4444-555555555555", work)
    argv = _calls(log)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == "11111111-2222-3333-4444-555555555555"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "<approved-plan>" not in _calls(log)[-1]["stdin"]            # it already knows the plan


def test_a_claude_run_never_gets_an_opencode_model_name(env):
    tmp, work, log = env
    _approve("ses_plan0002bbbb", work, engine="opencode", model="vllm3090/qwen3.8-27b", run_engine="claude")
    _execute("ses_plan0002bbbb", work)
    argv = _calls(log)[-1]["argv"]
    assert argv[argv.index("--model") + 1] == cct.DEFAULT_MODEL


def test_approve_route_records_the_choice_and_respects_the_chat(env, monkeypatch):
    import routes.claude_code_routes as ccr
    from src import chat_prefs
    tmp, work, log = env
    monkeypatch.setattr(ccr, "start_turn", lambda *a, **k: False, raising=False)
    import src.screen_control_resume as scr
    monkeypatch.setattr(scr, "start_turn", lambda *a, **k: False)
    approvals.record_plan("ses_plan0003cccc", cwd=str(work), plan="p", owner="", model="", engine="opencode",
                          chat_session_id="chat-9")
    with pytest.raises(HTTPException) as e:
        ccr.approve_plan("ses_plan0003cccc", "", engine="gpt")
    assert e.value.status_code == 400
    monkeypatch.setattr(chat_prefs, "engine_allowed", lambda sid, eng: eng != "claude")
    with pytest.raises(HTTPException) as e:
        ccr.approve_plan("ses_plan0003cccc", "", engine="claude")
    assert e.value.status_code == 409 and approvals.get("ses_plan0003cccc")["status"] == "pending"
    r = ccr.approve_plan("ses_plan0003cccc", "", engine="opencode", model="ollama-desktop/qwen3:8b")
    entry = approvals.get("ses_plan0003cccc")
    assert r["status"] == "approved" and entry["run_engine"] == "opencode"
    assert entry["run_model"] == "ollama-desktop/qwen3:8b"


def test_run_options_list_both_engines_and_their_models(tmp_path, monkeypatch):
    import routes.claude_code_routes as ccr
    cfg = tmp_path / ".config" / "opencode"
    cfg.mkdir(parents=True)
    (cfg / "opencode.json").write_text(json.dumps({
        "provider": {"vllm3090": {"name": "3090 vLLM", "models": {"qwen3.8-27b": {"name": "Qwen3 27B"}}},
                     "ollama-desktop": {"name": "Ollama", "models": {"qwen3:8b": {"name": "Qwen3 8B"}}}},
        "model": "vllm3090/qwen3.8-27b"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    opts = ccr.run_options("", {"engine": "opencode", "model": ""})
    oc, cl = opts["engines"]
    assert [m["id"] for m in oc["models"]] == ["vllm3090/qwen3.8-27b", "ollama-desktop/qwen3:8b"]
    assert oc["default_model"] == "vllm3090/qwen3.8-27b" and oc["allowed"] and cl["allowed"]
    assert [m["id"] for m in cl["models"]] == ["opus", "sonnet", "haiku"]
    assert opts["plan_engine"] == "opencode"


def test_the_dialog_sends_the_choice():
    js = open(os.path.join(HERE, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert "export function showRunLimits(anchor, onApprove, planId)" in js
    assert "/api/claude_code/plan/${encodeURIComponent(planId)}/run_options" in js
    assert "JSON.stringify({ limits: a._runLimits, engine: a._runEngine || '', model: a._runModel || '' })" in js
    assert "}, planId);" in js
