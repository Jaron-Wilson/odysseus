"""Each chat's Claude Code agent: parked in `claude agents`, carried on in any folder.

Asked for 2026-09-30: "for the claude code, i was hoping for agents, not just
local dir .claude stuff, so a chat can keep going cause it just opens that
chats agents and finds it there". The user chose both "real agents in
`claude agents`" and "a chat keeps its agent, any folder", and said yes to
marking the folders it runs in as trusted.

The fake claude below behaves as the real one did when tried (2.1.285):
`--bg --resume <id>` with no prompt parks the session under its own id,
`agents --json --all` lists it with a pid while it is alive, `stop` ends the
process but keeps the conversation.
"""
import asyncio
import json
import os
import stat
import sys

import pytest

from src import claude_agent_view as view
from src import claude_code_agents
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

FAKE = r'''#!{py}
import json, os, sys
log, state = {log!r}, {state!r}
argv = sys.argv[1:]
agents = json.load(open(state)) if os.path.exists(state) else []
def save(): json.dump(agents, open(state, "w"))
open(log, "a").write(json.dumps({{"argv": argv, "cwd": os.getcwd()}}) + "\n")
if argv[:1] == ["agents"]:
    print(json.dumps(agents)); sys.exit(0)
if argv[:1] == ["stop"]:
    for a in agents:
        if a["id"] == argv[1]: a.pop("pid", None); a["status"] = None
    save(); print("stopped " + argv[1]); sys.exit(0)
if "--bg" in argv:
    sid = argv[argv.index("--resume") + 1]
    agents[:] = [a for a in agents if a["sessionId"] != sid]
    agents.append({{"id": sid[:8], "sessionId": sid, "cwd": os.getcwd(), "kind": "background",
                   "pid": 4242, "status": "idle", "state": "blocked"}})
    save(); print("backgrounded · " + sid[:8] + " (idle — send a prompt to start)"); sys.exit(0)
sys.stdin.read()
print(json.dumps({{"type": "result", "result": "Looked at it.", "is_error": False}}), flush=True)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log, state = tmp_path / "calls.jsonl", tmp_path / "agents.json"
    exe = bindir / "claude"
    exe.write_text(FAKE.format(py=sys.executable, log=str(log), state=str(state)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(view, "ENABLED", True)
    monkeypatch.setattr(view, "_LIST_TTL_S", 0.0)
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"numStartups": 7, "projects": {"/elsewhere": {"hasTrustDialogAccepted": True}}}))
    monkeypatch.setattr(view, "CLAUDE_JSON", str(cj))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(claude_code_agents, "AGENTS_FILE", str(tmp_path / "agents_reg.json"))
    monkeypatch.setattr(claude_code_agents, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(claude_code_agents, "_chat_name", lambda c: "Fix the login page")
    jobs._JOBS.clear()
    a, b = tmp_path / "proj-a", tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    return tmp_path, a, b, log, state, cj


def _calls(log, want=None):
    rows = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
    return [r for r in rows if want is None or want(r["argv"])]


def _turns(log):
    return _calls(log, lambda a: a[:1] == ["-p"])


def _ask(cwd, prompt="What does it do?", chat="chat-1"):
    return asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(
        {"action": "ask", "engine": "claude", "cwd": str(cwd), "prompt": prompt}), {"session_id": chat}))


def test_marking_a_folder_trusted_changes_only_its_flag(env):
    tmp, a, b, log, state, cj = env
    assert not view.is_trusted(str(a))
    assert view.ensure_trusted(str(a)) and view.is_trusted(str(a))
    data = json.loads(cj.read_text())
    assert data["numStartups"] == 7 and data["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": True}
    assert data["projects"][str(a)] == {"hasTrustDialogAccepted": True}
    before = cj.stat().st_mtime_ns
    assert view.ensure_trusted(str(a)) and cj.stat().st_mtime_ns == before       # no rewrite when already trusted


def test_a_turn_parks_the_chats_agent_named_after_the_chat(env):
    tmp, a, b, log, state, cj = env
    out = _ask(a)
    assert out["exit_code"] == 0, out
    turn = _turns(log)[-1]["argv"]
    assert turn[turn.index("-n") + 1] == "Odysseus: Fix the login page"
    park = _calls(log, lambda x: "--bg" in x)[-1]
    sid = park["argv"][park["argv"].index("--resume") + 1]
    assert park["argv"] == ["--bg", "--resume", sid]                 # flag-less: a flag would start a copy
    assert park["cwd"] == str(a) and view.is_trusted(str(a))        # --bg needs a trusted folder
    assert out["claude_agent"] == {"id": sid[:8], "name": "Odysseus: Fix the login page",
                                   "attach": f"claude attach {sid[:8]}"}


def test_the_next_turn_stops_the_parked_agent_then_carries_it_on(env):
    tmp, a, b, log, state, cj = env
    first = _ask(a)
    sid = first["session_id"]
    _ask(a, "And the tests?")
    stop = _calls(log, lambda x: x[:1] == ["stop"])
    assert stop and stop[-1]["argv"] == ["stop", sid[:8]]          # before the turn, not after
    turn = _turns(log)[-1]["argv"]
    assert turn[turn.index("--resume") + 1] == sid                   # the same agent, not a new one
    order = [r["argv"][0] if r["argv"][0] != "--bg" else "park" for r in _calls(log)
             if r["argv"][:1] in (["stop"], ["-p"]) or "--bg" in r["argv"]]
    assert order[-3:] == ["stop", "-p", "park"]


def test_a_chat_carries_its_agent_on_in_another_folder(env):
    tmp, a, b, log, state, cj = env
    sid = _ask(a)["session_id"]
    out = _ask(b, "Now look at the other project")
    assert out["exit_code"] == 0, out
    call = _turns(log)[-1]
    argv = call["argv"]
    assert argv[argv.index("--resume") + 1] == sid                   # this chat's agent from proj-a
    assert call["cwd"] == str(a)                                     # where Claude keeps that session
    assert argv[argv.index("--add-dir") + 1] == str(b)               # with the folder asked for added
    assert "runs there with" in out.get("agent", "")


def test_a_new_agent_is_still_possible(env):
    tmp, a, b, log, state, cj = env
    sid = _ask(a)["session_id"]
    out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(
        {"action": "ask", "engine": "claude", "cwd": str(b), "prompt": "fresh", "new_agent": True}),
        {"session_id": "chat-1"}))
    argv = _turns(log)[-1]["argv"]
    assert "--resume" not in argv and "--add-dir" not in argv and out["session_id"] != sid


def test_a_busy_agent_open_in_a_terminal_is_not_taken_over(env):
    tmp, a, b, log, state, cj = env
    sid = _ask(a)["session_id"]
    agents = json.loads(state.read_text())
    agents[0]["status"] = "busy"                                    # someone ran `claude attach` and typed
    state.write_text(json.dumps(agents))
    out = _ask(a, "again")
    assert out["exit_code"] == 1 and f"claude attach {sid[:8]}" in out["error"]
    assert len(_turns(log)) == 1                                     # nothing new ran


def test_the_added_folder_is_bound_like_cwd(env, monkeypatch):
    tmp, a, b, log, state, cj = env
    _ask(a)                                                          # the chat's agent lives in proj-a
    root = str(cct.Path(cct.__file__).resolve().parents[2])           # the running install
    out = _ask(root, "look")                                          # carried on with root added: refused
    assert out["exit_code"] == 1 and "running Odysseus install" in out["error"]
    assert len(_turns(log)) == 1


def test_switched_off_it_parks_nothing(env, monkeypatch):
    tmp, a, b, log, state, cj = env
    monkeypatch.setattr(view, "ENABLED", False)
    out = _ask(a)
    assert out["exit_code"] == 0 and "claude_agent" not in out
    assert not _calls(log, lambda x: "--bg" in x or x[:1] in (["agents"], ["stop"]))
    assert not view.is_trusted(str(a))
