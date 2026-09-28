"""Run limits: turns, a budget, or take your time.

Asked for on 2026-09-28: "when I send out a claude agent, I want to be able
to set how many turns it should take, total cost (try to stay under, when hit
finish up right away!) or select take your time and it does unlimited and
unlimited costs."
"""
import asyncio
import json
import os
import stat
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

FAKE = r'''#!{py}
import json, sys, time
args = sys.argv[1:]
open({argv!r}, "a").write(json.dumps(args) + "\n")
prompt = sys.stdin.read()
def out(o): print(json.dumps(o), flush=True)
if prompt.startswith("STOP"):
    out({{"type": "result", "result": "Done: the API worker. Left: the frontend.", "is_error": False,
          "total_cost_usd": 0.01, "num_turns": 1}})
    sys.exit(0)
for i in range(8):
    out({{"type": "assistant", "message": {{"id": f"msg_{{i}}", "model": "claude-sonnet-5",
          "usage": {{"cache_read_input_tokens": {cache}, "output_tokens": 100}},
          "content": [{{"type": "text", "text": f"Step {{i}}"}}]}}}})
    time.sleep(0.35)
out({{"type": "result", "result": "All of it.", "is_error": False, "total_cost_usd": 9.0, "num_turns": 8}})
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv = tmp_path / "argv.jsonl"

    def make(cache=1000):
        exe = bindir / "claude"
        exe.write_text(FAKE.format(py=sys.executable, argv=str(argv), cache=cache))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "claude")
    jobs._JOBS.clear()
    return make, tmp_path, argv


def _ask(tmp_path, **limits):
    return asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(
        {"action": "ask", "prompt": "build it", "cwd": str(tmp_path), **limits}), {}))


def test_the_turn_limit_stops_it_and_it_wraps_up(env):
    make, tmp, argv = env
    make()
    out = _ask(tmp, max_turns=2)
    assert out["limit_hit"] == "turn limit (2 turns)" and out["exit_code"] == 0
    assert out["output"].startswith("**Stopped at the turn limit (2 turns) you set**")
    assert "Left: the frontend." in out["output"]
    calls = [json.loads(l) for l in argv.read_text().splitlines()]
    assert len(calls) == 2 and "--resume" in calls[1]                  # one wrap-up in the same session
    assert calls[1][calls[1].index("--allowedTools") + 1] == "Read"


def test_the_budget_stops_it_before_it_is_spent(env):
    make, tmp, argv = env
    make(cache=1_000_000)                          # ~$0.30 of cache reads per turn (sonnet rates)
    out = _ask(tmp, max_cost_usd=1.0)
    assert out["limit_hit"] == "budget ($1.00)"
    assert out["usage"]["turns"] <= 4                                   # stopped around $0.80
    first = json.loads(argv.read_text().splitlines()[0])
    assert first[first.index("--max-budget-usd") + 1] == "0.80"        # the CLI's own backstop


def test_take_your_time_has_no_limits(env):
    make, tmp, argv = env
    make()
    out = _ask(tmp, take_your_time=True, max_turns=2)
    assert "limit_hit" not in out and out["output"] == "All of it."
    job = jobs.list_jobs()[0]
    assert job.spec["timeout"] == cct.TAKE_YOUR_TIME_TIMEOUT_S


def test_limits_from_approve_are_used_by_the_run(env, monkeypatch):
    make, tmp, argv = env
    import routes.claude_code_routes as ccr
    import src.screen_control_resume as scr
    monkeypatch.setattr(ccr, "_auth_disabled", lambda: True)
    monkeypatch.setattr(ccr, "get_current_user", lambda r: "")
    monkeypatch.setattr(scr, "start_turn", lambda *a, **k: True)
    approvals.record_plan("11111111-2222-3333-4444-555555555555", cwd=str(tmp), plan="p", engine="claude")
    app = FastAPI()
    app.include_router(ccr.setup_claude_code_routes())
    c = TestClient(app)
    r = c.post("/api/claude_code/approve/11111111-2222-3333-4444-555555555555",
               json={"limits": {"max_turns": 3, "max_cost_usd": "2.5"}})
    assert r.status_code == 200
    assert approvals.get("11111111-2222-3333-4444-555555555555")["limits"] == {
        "max_turns": 3, "max_cost_usd": 2.5, "take_your_time": False}


def test_normalize_limits():
    n = cct.normalize_limits
    assert n({"max_turns": "7", "max_cost_usd": "1.234"}) == {"max_turns": 7, "max_cost_usd": 1.23, "take_your_time": False}
    assert n({"max_turns": 0, "max_cost_usd": -1}) == {"max_turns": None, "max_cost_usd": None, "take_your_time": False}
    assert n({"take_your_time": True, "max_turns": 5})["max_turns"] is None
    assert n(None) == {"max_turns": None, "max_cost_usd": None, "take_your_time": False}
