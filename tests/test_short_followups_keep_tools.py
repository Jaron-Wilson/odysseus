"""A short follow-up after a detailed message gets that message's tools.

Seen live on 2026-09-27: after a brief describing a script-driven video
project (scripts, scp, SSH), "okay please make it! please make a 5 min video
..." was judged low-signal: retrieval was skipped and bash, the tool the job
needed, was not offered.
"""
from src.agent_loop import _classify_agent_request

BRIEF = ("Scripts live in ~/costa-rica-video on the server. Copy task-args.txt over with "
         "scp, then run schtasks over SSH to build the timeline and render it.")


def test_video_work_offers_the_shell():
    msg = "please make a 5 min video a 30 second video and a 1 min video"
    r = _classify_agent_request([{"role": "user", "content": msg}], msg)
    assert not r["low_signal"] and "files" in r["domains"]


def test_a_short_follow_up_uses_the_previous_reply():
    msgs = [{"role": "assistant", "content": BRIEF},
            {"role": "user", "content": "okay please make it!"}]
    r = _classify_agent_request(msgs, "okay please make it!")
    assert not r["low_signal"] and "files" in r["domains"]
    assert "scp" in r["retrieval_query"]


def test_small_talk_stays_low_signal():
    assert _classify_agent_request([{"role": "user", "content": "hi"}], "hi")["low_signal"]
    msgs = [{"role": "assistant", "content": "Glad that helped!"},
            {"role": "user", "content": "cool"}]
    assert _classify_agent_request(msgs, "cool")["low_signal"]


def test_coding_tasks_default_to_local_opencode(tmp_path, monkeypatch):
    """Seen live: drafting the video cuts went to Claude Code (the user's
    Claude plan) when the local model via OpenCode would have done."""
    import asyncio, json, os, stat, sys
    from src import claude_code_approvals as approvals, claude_code_jobs as jobs
    from src.agent_tools import claude_code_tool as cct
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    import src.doc_pdf as doc_pdf

    async def no_pdf(*a, **k):
        return None, "skipped"
    monkeypatch.setattr(doc_pdf, "render_markdown_pdf", no_pdf)
    jobs._JOBS.clear()
    argv = tmp_path / "argv.txt"
    bindir = tmp_path / "bin"; bindir.mkdir()
    fake = bindir / "opencode"
    fake.write_text(f"#!{sys.executable}\nimport json,sys\nopen({str(argv)!r},'w').write(json.dumps(sys.argv[1:]))\n"
                    "print(json.dumps({'type':'text','sessionID':'ses_abc','part':{'text':'A plan.'}}))\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "opencode")
    out = asyncio.run(cct.ClaudeCodeTool().execute(
        json.dumps({"prompt": "draft the cuts", "cwd": str(tmp_path)}), {}))
    assert json.loads(argv.read_text())[0] == "run"                  # opencode, not claude
    assert "runs on OpenCode · local default" in out["approval"]["approve"]
    assert approvals.get(out["session_id"])["engine"] == "opencode"


def test_background_chip_and_watch_are_wired():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(here, "static", "js", "bgTasks.js"), encoding="utf-8").read()
    assert "export async function watchInChat(" in js and "function _renderChip(" in js
    assert 'id="bg-chip"' in open(os.path.join(here, "static", "index.html"), encoding="utf-8").read()
