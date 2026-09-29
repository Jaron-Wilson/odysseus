"""A check is not a plan, and a used approval does not start another plan.

Seen 2026-09-29 ("its asked me about 12 times for plans"): one chat made 13
plans. Read-only status checks were sent as plans, each asking the user to
approve a plan that changed nothing, and a second execute on an approval
already spent answered "plan again for a new run", so the agent planned the
same work again.
"""
import asyncio
import json

from src import claude_code_approvals as approvals
from src.agent_tools import claude_code_tool as cct

CHECKS = [
    "READ-ONLY status check, answer 4 questions, do not write anything:  1. On the laptop ...",
    "Read-only check, no changes. On the laptop (ssh jaron@100.103.158.40) run: gh pr list ...",
]
PLANS = [
    "READ-ONLY planning pass. Task: design a new branch `jaron-ci-previews` ...",
    "Plan (READ-ONLY recon, then the execution is just 3 gh commands) to open three pull requests ...",
    "Add a **music progress bar** (elapsed / total time + a fill bar) to all three music surfaces ...",
    "PLAN MODE ONLY: read-only recon, then produce a concrete build plan. Write nothing, deploy nothing.",
]


def test_checks_are_told_apart_from_plans():
    assert all(cct._is_read_only_check(p) for p in CHECKS)
    assert not any(cct._is_read_only_check(p) for p in PLANS)


FAKE = r'''#!{py}
import json, sys
open({log!r}, "a").write(json.dumps(sys.argv[1:]) + "\n")
print(json.dumps({{"type": "text", "part": {{"text": "Facts: all 4 answered."}}}}), flush=True)
'''


def _run(tmp_path, monkeypatch, prompt):
    import os, stat, sys
    from src import claude_code_jobs as jobs
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.jsonl"
    exe = bindir / "opencode"
    exe.write_text(FAKE.format(py=sys.executable, log=str(log)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    from src import opencode_providers
    monkeypatch.setattr(opencode_providers, "_endpoints", lambda: [])
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    args = {"action": "plan", "engine": "opencode", "cwd": str(work), "prompt": prompt}
    out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(args), {"session_id": ""}))
    argv = json.loads(open(log).read().splitlines()[-1])
    pending = [e for e in json.loads((tmp_path / "a.json").read_text() or "{}").values()] \
        if (tmp_path / "a.json").exists() else []
    return out, argv, pending


def test_a_check_sent_as_a_plan_runs_as_ask(monkeypatch, tmp_path):
    out, argv, pending = _run(tmp_path, monkeypatch, CHECKS[0])
    assert argv[argv.index("--agent") + 1] == "plan"           # still read-only
    assert pending == []                                        # and nothing to approve


def test_a_real_plan_still_asks_for_approval(monkeypatch, tmp_path):
    out, argv, pending = _run(tmp_path, monkeypatch, PLANS[2])
    assert [e["status"] for e in pending] == ["pending"]


def test_a_used_approval_says_not_to_plan_again(monkeypatch, tmp_path):
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    (tmp_path / "a.json").write_text(json.dumps({"ses_1": {
        "status": "used", "cwd": str(tmp_path), "run_id": "0aa7bf85", "created": 9e9, "chat_session_id": "c"}}))
    ok, why = approvals.consume_approval("ses_1", str(tmp_path))
    assert not ok
    assert "do NOT plan again" in why and "0aa7bf85" in why and "'status'" in why
    assert "plan again for a new run" not in why


def test_the_tool_tells_the_model_to_ask_for_checks():
    from src import tool_schemas
    text = json.dumps(tool_schemas.__dict__.get("TOOL_SCHEMAS") or open(tool_schemas.__file__, encoding="utf-8").read())
    assert "never send a check as a plan" in text
