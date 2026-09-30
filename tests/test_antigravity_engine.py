"""Google Antigravity (the `agy` CLI) as a third coding-agent engine.

Asked for on 2026-09-30: "can we add google antigravity to the agents_code
please? I have a googel subscription that has alot of credits and models."

The fake agy below answers like `agy -p ... --output-format stream-json`
does (antigravity.google/docs/cli/headless): an init event with the
conversation id, step_update events, and a result.
"""
import asyncio
import json
import os
import stat
import sys

import pytest

from src import chat_prefs
from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONV = "c3b66b04-872b-4fbe-a3a4-058a026ef20a"

FAKE_AGY = r'''#!{py}
import json, sys
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:]}}) + "\n")
if sys.argv[1:2] == ["models"]:
    print("gemini-3.8-flash-high   Gemini 3.8 Flash (high)")
    print("claude-sonnet-4-6       Claude Sonnet 4.6")
    sys.exit(0)
conv = sys.argv[sys.argv.index("--conversation") + 1] if "--conversation" in sys.argv else {conv!r}
emit = lambda e: print(json.dumps(e), flush=True)
emit({{"event": "init", "conversation_id": conv, "init": {{"cwd": "/w", "permission_mode": "request-review"}}}})
# Shapes as seen from agy 1.2.14 (2026-09-30).
emit({{"event": "step_update", "step_update": {{"conversation_id": conv, "step_index": 0, "state": "DONE",
      "step_type": "user_input"}}}})
emit({{"event": "step_update", "step_update": {{"conversation_id": conv, "step_index": 1, "state": "DONE",
      "step_type": "tool", "tool_name": "run_command",
      "tool_info": {{"name": "run_command", "parameters": {{"CommandLine": "ls src"}}, "output": "2 files"}}}}}})
emit({{"event": "step_update", "step_update": {{"conversation_id": conv, "step_index": 2, "state": "RUNNING",
      "step_type": "agent_response", "text_delta": "The app has "}}}})
emit({{"event": "step_update", "step_update": {{"conversation_id": conv, "step_index": 2, "state": "DONE",
      "step_type": "agent_response", "text_delta": "two modules.", "usage": {{"thinking_tokens": 40}}}}}})
emit({{"event": "result", "result": {{"conversation_id": conv, "status": "SUCCESS",
      "response": "The app has two modules.", "num_turns": 2, "usage": {{"output_tokens": 12}}}}}})
'''

FAKE_OTHER = r'''#!{py}
import json, sys
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:], "cli": sys.argv[0]}}) + "\n")
print(json.dumps({{"type": "result", "result": "Done.", "is_error": False}}), flush=True)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    for name, body in (("agy", FAKE_AGY), ("opencode", FAKE_OTHER), ("claude", FAKE_OTHER)):
        exe = bindir / name
        exe.write_text(body.format(py=sys.executable, log=str(log), conv=CONV))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(chat_prefs, "PREFS_FILE", str(tmp_path / "chat_prefs.json"))
    monkeypatch.setitem(cct._AGY_MODELS, "at", 0.0)
    monkeypatch.setitem(cct._AGY_CHECK, "at", 0.0)
    jobs._JOBS.clear()
    work = tmp_path / "work"
    work.mkdir()
    return tmp_path, work, log


def _calls(log, cli=None):
    rows = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
    # `agy models` and `agy --version` are checks, not runs.
    return [r for r in rows if r["argv"][:1] not in (["models"], ["--version"])
            and (cli is None or cli in r.get("cli", "agy"))]


def _run(args, chat=""):
    return asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(args), {"session_id": chat}))


def _approve(sid, work, *, engine, model="", run_engine="", run_model=""):
    approvals.record_plan(sid, cwd=str(work), plan="1. Add the page\n2. Test it", owner="",
                          model=model, engine=engine, chat_session_id="")
    approvals.set_status(sid, "approved", owner="")
    approvals.set_run_choice(sid, run_engine, run_model)


def test_it_is_a_named_engine():
    assert "antigravity" in cct.ENGINES
    assert cct.engine_label("antigravity") == "Antigravity"
    assert cct.ENGINE_BINARIES["antigravity"] == "agy"
    assert cct.engine_label("claude") == "Claude Code" and cct.engine_label("opencode") == "OpenCode"


def test_an_ask_is_read_only_and_reads_the_stream(env):
    tmp, work, log = env
    out = _run({"action": "ask", "engine": "antigravity", "cwd": str(work),
                "prompt": "What does this app do?"})
    assert out["exit_code"] == 0, out
    argv = _calls(log)[-1]["argv"]
    assert argv[0] == "-p" and argv[1].startswith("What does this app do?")
    assert "view_file, list_dir, find_by_name" in argv[1]     # read-only: its own file tools
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--sandbox" in argv and "--dangerously-skip-permissions" not in argv   # nothing it may write
    assert "--print-timeout" in argv                          # not agy's own 5 minutes
    assert out["session_id"] == CONV                          # from the init event
    assert "The app has two modules." in out["output"]
    assert "● run_command(ls src) → 2 files" in out["console"]
    assert out["engine_label"] == "Antigravity"


def test_an_approved_antigravity_plan_carries_on_its_conversation(env):
    tmp, work, log = env
    _approve(CONV, work, engine="antigravity", run_engine="antigravity", run_model="gemini-3.8-flash-high")
    out = _run({"action": "execute", "session_id": CONV, "cwd": str(work), "prompt": "Carry out the approved plan."})
    assert out["exit_code"] == 0, out
    argv = _calls(log)[-1]["argv"]
    assert argv[argv.index("--conversation") + 1] == CONV
    assert "--dangerously-skip-permissions" in argv and "--sandbox" not in argv
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-high"
    assert approvals.get(CONV)["status"] == "used"


def test_an_opencode_plan_can_run_on_antigravity_fresh(env):
    tmp, work, log = env
    _approve("ses_plan0009zzzz", work, engine="opencode", model="vllm3090/qwen3.8-27b",
             run_engine="antigravity")
    out = _run({"action": "execute", "session_id": "ses_plan0009zzzz", "cwd": str(work),
                "prompt": "Carry out the approved plan."})
    assert out["exit_code"] == 0, out
    argv = _calls(log)[-1]["argv"]
    assert "--conversation" not in argv                       # OpenCode's session is not agy's
    assert "<approved-plan>" in argv[1] and "2. Test it" in argv[1]
    assert "--model" not in argv                              # no OpenCode model name for agy


def test_an_unknown_model_is_refused_with_the_real_ones(env):
    tmp, work, log = env
    out = _run({"action": "ask", "engine": "antigravity", "model": "gemini-9-ultra",
                "cwd": str(work), "prompt": "hi"})
    assert out["exit_code"] == 2
    assert "gemini-3.8-flash-high" in out["error"] and "claude-sonnet-4-6" in out["error"]
    assert not _calls(log)                                    # nothing was started


def test_the_stream_parser_joins_the_text_of_a_step():
    s = cct._Stream("antigravity")
    lines = []
    for ev in ({"event": "init", "conversation_id": CONV},
               {"event": "step_update", "step_update": {"step_index": 1, "state": "RUNNING",
                                                         "step_type": "agent_response", "text_delta": "Hello "}},
               {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE",
                                                         "step_type": "agent_response", "text_delta": "world"}},
               {"event": "result", "result": {"status": "ERROR", "error": "quota exceeded", "response": ""}}):
        lines += s.feed(json.dumps(ev))
    assert s.session_id == CONV
    assert "Hello world" in lines and lines.count("Hello world") == 1     # written once, when done
    assert s.is_error and "quota exceeded" in s.final_text


def test_agy_is_found_in_local_bin_without_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    agy = home / ".local" / "bin" / "agy"
    agy.write_text("#!/bin/sh\n")
    agy.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(cct.Path, "home", classmethod(lambda cls: home))
    assert cct.engine_cli("antigravity") == str(agy)
    assert cct.engine_cli("opencode") is None


def test_antigravity_off_moves_a_plan_to_opencode(env):
    tmp, work, log = env
    chat_prefs.set_pref("chat-g", "antigravity", False)
    out = _run({"action": "plan", "engine": "antigravity", "cwd": str(work),
                "prompt": "Add a settings page to the app"}, chat="chat-g")
    assert "switched off" in out.get("engine_note", "") and "OpenCode" in out.get("engine_note", "")
    assert not [c for c in _calls(log) if "agy" in c.get("cli", "agy") and c["argv"][:1] == ["-p"]]


def test_bash_cannot_run_agy_when_it_is_off(env):
    from src import tool_execution as te
    chat_prefs.set_pref("chat-g", "antigravity", False)

    class B:
        tool_type = "bash"
        content = "cd /tmp && agy -p 'do it'"
    desc, out = asyncio.run(te.execute_tool_block(B(), session_id="chat-g"))
    assert desc == "bash: refused" and "Antigravity is switched off" in out["error"]
    assert not te._CODE_CLI_RE.search("cat legacy.md")       # 'agy' inside a word is not the CLI


def test_the_approve_dialog_offers_it_once_installed(env, monkeypatch):
    import routes.claude_code_routes as r
    monkeypatch.setattr("src.opencode_providers.models", lambda: ([("vllm3090/qwen3.8-27b", "qwen")], ""))
    monkeypatch.setattr("src.opencode_providers.in_use", lambda ids: {})
    opts = r.run_options("chat-x", {"engine": "opencode"})
    g = next(e for e in opts["engines"] if e["id"] == "antigravity")
    assert g["label"] == "Antigravity" and g["allowed"] and g["default_label"]
    assert {m["id"] for m in g["models"]} == {"gemini-3.8-flash-high", "claude-sonnet-4-6"}
    monkeypatch.setattr(cct, "engine_cli", lambda e: None)
    assert "antigravity" not in [e["id"] for e in r.run_options("chat-x", {})["engines"]]


def test_devops_reads_antigravity_runs(tmp_path):
    from src import devops_stats
    run = tmp_path / "run1"
    run.mkdir()
    (run / "out.jsonl").write_text("\n".join(json.dumps(e) for e in (
        {"event": "init", "conversation_id": CONV},
        {"event": "result", "result": {"status": "SUCCESS", "duration_seconds": 6.5,
                                       "usage": {"output_tokens": 589}}})) + "\n")
    info = devops_stats._parse_run_log(str(run))
    assert info["engine"] == "antigravity" and info["status"] == "done" and info["out"] == 589
    assert devops_stats.ENGINE_NAMES["antigravity"] == "Antigravity"


def test_the_ui_knows_it():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    assert "antigravity: 'Antigravity'" in read("static", "js", "bgTasks.js")
    assert "/^\\$ agy\\b/m" in read("static", "js", "chatRenderer.js")
    assert "e.default_label" in read("static", "js", "chatRenderer.js")
    schema = read("src", "tool_schemas.py")
    assert '"enum": ["opencode", "claude", "antigravity"]' in schema


def test_an_agy_that_cannot_run_on_this_cpu_is_explained_not_offered(tmp_path, monkeypatch):
    # Seen 2026-09-30: this server is a KVM guest on QEMU's generic CPU, and
    # agy dies at start (exit 132) for want of PCLMUL.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    agy = bindir / "agy"
    agy.write_text("#!/bin/sh\necho 'FATAL ERROR: This binary was compiled with pclmul enabled, but this "
                   "feature is not available on this processor (go/sigill-fail-fast).' >&2\nexit 132\n")
    agy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setitem(cct._AGY_CHECK, "at", 0.0)
    monkeypatch.setitem(cct._AGY_MODELS, "at", 0.0)
    real_which = cct.shutil.which
    monkeypatch.setattr(cct.shutil, "which", lambda n: None if n == "qemu-x86_64" else real_which(n))
    work = tmp_path / "work"
    work.mkdir()
    problem = cct.antigravity_problem()
    assert "pclmul" in problem and "sudo apt install qemu-user" in problem and "CPU type to 'host'" in problem
    out = _run({"action": "ask", "engine": "antigravity", "cwd": str(work), "prompt": "hi"})
    assert out["exit_code"] == 1 and "cannot run on this host" in out["error"]
    import routes.claude_code_routes as r
    monkeypatch.setattr("src.opencode_providers.models", lambda: ([], ""))
    monkeypatch.setattr("src.opencode_providers.in_use", lambda ids: {})
    assert "antigravity" not in [e["id"] for e in r.run_options("", {})["engines"]]


def test_under_qemu_when_the_cpu_lacks_pclmul(tmp_path, monkeypatch):
    # The fix that needed no VM restart: QEMU user mode with a full CPU model.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    agy = bindir / "agy"
    agy.write_text(f"""#!{sys.executable}
import json, os, sys
if not os.environ.get("UNDER_QEMU"):
    sys.stderr.write("FATAL ERROR: This binary was compiled with pclmul enabled, but this feature is "
                     "not available on this processor (go/sigill-fail-fast).\\n"); sys.exit(132)
open({str(log)!r}, "a").write(json.dumps({{"argv": sys.argv[1:]}}) + "\\n")
if sys.argv[1:2] == ["--version"]: print("1.2.14"); sys.exit(0)
print(json.dumps({{"event": "init", "conversation_id": "q-1"}}))
print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": "emulated ok"}}}}))
""")
    agy.chmod(0o755)
    qemu = bindir / "qemu-x86_64"
    qemu.write_text(f"""#!/bin/sh
[ "$1" = "-cpu" ] && [ "$2" = "max" ] || exit 9
shift 2
UNDER_QEMU=1 exec "$@"
""")
    qemu.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setitem(cct._AGY_CHECK, "at", 0.0)
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    work = tmp_path / "work"
    work.mkdir()
    assert cct.antigravity_problem() == ""
    assert cct.agy_argv() == [str(qemu), "-cpu", "max", str(agy)]
    out = _run({"action": "ask", "engine": "antigravity", "cwd": str(work), "prompt": "hi"})
    assert out["exit_code"] == 0, out
    assert "emulated ok" in out["output"] and out["session_id"] == "q-1"
    runs = [json.loads(l) for l in open(log) if '"-p"' in l]
    assert runs and runs[-1]["argv"][0] == "-p"


def test_a_denied_tool_is_shown_and_an_empty_answer_says_why(env, monkeypatch):
    # Seen live 2026-09-30: asked what a file does, agy tried `find` (a shell
    # command), the read-only run denied it, and it ended with no answer. The
    # card showed only the banner, as if that were the reply.
    line = cct._summarize_antigravity({"event": "step_update", "step_update": {
        "step_index": 3, "state": "ERROR", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": 'find . -name "calc.py"'},
                      "error": {"type": "TOOL_ERROR", "message": "permission check failed for command"}}}})
    assert line.startswith('✗ run_command(find . -name "calc.py")') and "permission check failed" in line
    tmp, work, log = env
    agy = tmp / "bin" / "agy"
    agy.write_text(f"""#!{sys.executable}
import json, sys
if sys.argv[1:2] in (["--version"], ["models"]): sys.exit(0)
print(json.dumps({{"event": "init", "conversation_id": "e-1"}}))
print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": ""}}}}))
sys.stderr.write("jetski: no output produced — a tool required the \\\\"command\\\\" permission that headless mode cannot prompt for, so it was auto-denied.\\\\n")
""")
    out = _run({"action": "ask", "engine": "antigravity", "cwd": str(work), "prompt": "What is in calc.py?"})
    assert out["output"].startswith("Antigravity finished without an answer.") and "auto-denied" in out["output"]
