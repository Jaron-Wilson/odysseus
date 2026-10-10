"""`/claude attach 7238cfa3`: a chat carries one of the user's own Claude
Code sessions on (src/claude_attach.py, routes/claude_sessions_routes.py,
the claude_code tool).

Asked for: "I want to be able to in a chat do: claude attach 7238cfa3 and
then it can now use that chat." What matters:

  * the start of an id names exactly one session, or says why not (none,
    several, or not an id at all), and never looks outside the projects dir;
  * the attachment is kept per chat, shows in the chat's agents, and
    detaching drops both;
  * the chat's coding agent then resumes THAT session in its own folder on
    Claude Code (stopping its parked agent first), unless told otherwise;
  * only an admin, on their own chat, can attach.

The claude CLI here is a fake: nothing runs against a real session.
"""
import asyncio
import json
import os
import stat
import sys
import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from routes import claude_sessions_routes
from src import claude_agent_view as view
from src import claude_attach
from src import claude_code_agents
from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src import claude_code_sessions as ccs
from src.agent_tools import claude_code_tool as cct

SID = "7238cfa3-e12f-4867-bccd-ba64f3e00751"
TWIN_A = "abcdef12-0000-4000-8000-000000000001"
TWIN_B = "abcdef12-0000-4000-8000-000000000002"
PROJ = "-tmp-proj-a"
OTHER = "-tmp-proj-b"


def _write_session(root, project, sid, cwd, title="Fix the login page"):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "user", "sessionId": sid, "cwd": cwd, "gitBranch": "feature-x",
         "message": {"role": "user", "content": "Look at the login page"},
         "timestamp": "2026-10-04T12:00:00.000Z"},
        {"type": "ai-title", "aiTitle": title, "sessionId": sid},
    ]
    path = d / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.setenv("CLAUDE_JOBS_DIR", str(tmp_path / "jobs"))
    proj_a, proj_b = tmp_path / "proj-a", tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    _write_session(root, PROJ, SID, str(proj_a))
    _write_session(root, OTHER, TWIN_A, str(proj_b), title="Twin one")
    _write_session(root, OTHER, TWIN_B, str(proj_b), title="Twin two")
    monkeypatch.setattr(claude_attach, "ATTACH_FILE", str(tmp_path / "data" / "claude_attached.json"))
    monkeypatch.setattr(claude_code_agents, "AGENTS_FILE", str(tmp_path / "data" / "agents.json"))
    monkeypatch.setattr(claude_code_agents, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(claude_code_agents, "_chat_name", lambda c: "Login work")
    with ccs._LOCK:
        ccs._CACHE.clear()
    return root, proj_a, proj_b


# ── Resolving a prefix ──────────────────────────────────────────────────

def test_a_unique_prefix_names_one_session(home):
    root, proj_a, _ = home
    for prefix in ("7238cf", "7238cfa3", "7238CFA3", "7238cfa3-e12f", SID):
        s = claude_attach.resolve(prefix)
        assert s["id"] == SID and s["project"] == PROJ
        assert s["cwd"] == str(proj_a) and s["git_branch"] == "feature-x"
        assert s["title"] == "Fix the login page"


def test_an_ambiguous_prefix_lists_the_candidates(home):
    with pytest.raises(claude_attach.AttachError) as e:
        claude_attach.resolve("abcdef12")
    assert e.value.status == 409
    assert {c["id"] for c in e.value.candidates} == {TWIN_A, TWIN_B}
    assert claude_attach.resolve("abcdef12-0000-4000-8000-000000000002")["id"] == TWIN_B


def test_no_match_is_a_clear_error(home):
    with pytest.raises(claude_attach.AttachError) as e:
        claude_attach.resolve("deadbeef")
    assert e.value.status == 404 and "deadbeef" in str(e.value)


@pytest.mark.parametrize("prefix", ["", "7238c", "7238cfaz", "../../etc", "7238cfa3/..",
                                    "7238cfa3e12f", "-7238cfa", "*", "7238cfa3-e12f-4867-bccd-ba64f3e007511"])
def test_anything_but_the_start_of_an_id_is_refused(home, prefix):
    assert ccs.resolve_prefix(prefix) == []
    with pytest.raises(claude_attach.AttachError) as e:
        claude_attach.resolve(prefix)
    assert e.value.status == 400


def test_links_out_of_the_projects_dir_are_not_followed(home, tmp_path):
    root, _, _ = home
    outside = tmp_path / "outside"
    outside.mkdir()
    sid = "99999999-0000-4000-8000-000000000009"
    (outside / f"{sid}.jsonl").write_text("{}\n")
    os.symlink(outside, root / "-linked-project")
    (root / PROJ / "88888888-0000-4000-8000-000000000008.jsonl").symlink_to(outside / f"{sid}.jsonl")
    assert ccs.resolve_prefix("999999") == []
    assert ccs.resolve_prefix("888888") == []


# ── Attaching and detaching ─────────────────────────────────────────────

def test_attach_is_kept_per_chat_and_shows_as_its_agent(home):
    _, proj_a, _ = home
    out = claude_attach.attach("chat-1", "7238cfa3", by="jaron")
    assert out["id"] == SID and out["cwd"] == str(proj_a) and out["title"] == "Fix the login page"
    assert claude_attach.get("chat-1")["session_id"] == SID
    assert claude_attach.get("chat-2") is None
    saved = json.loads(open(claude_attach.ATTACH_FILE).read())
    assert saved["chat-1"]["project"] == PROJ and saved["chat-1"]["attached_by"] == "jaron"
    agent = claude_code_agents.for_chat("chat-1", cwd=str(proj_a))
    assert agent and agent["session_id"] == SID and agent["engine"] == "claude"


def test_detach_drops_the_attachment_and_the_agent(home):
    _, proj_a, _ = home
    claude_attach.attach("chat-1", "7238cfa3")
    was = claude_attach.detach("chat-1")
    assert was["session_id"] == SID
    assert claude_attach.get("chat-1") is None
    assert claude_code_agents.for_chat("chat-1", cwd=str(proj_a)) is None
    assert claude_attach.detach("chat-1") is None


def test_attaching_another_session_replaces_the_first(home):
    claude_attach.attach("chat-1", "7238cfa3")
    claude_attach.attach("chat-1", TWIN_A)
    assert claude_attach.get("chat-1")["session_id"] == TWIN_A


def test_a_session_whose_transcript_is_gone_is_not_attached(home):
    root, _, _ = home
    claude_attach.attach("chat-1", "7238cfa3")
    os.unlink(root / PROJ / f"{SID}.jsonl")
    assert claude_attach.get("chat-1") is None
    assert claude_attach.context_note("chat-1") == ""


def test_the_model_is_told_briefly(home):
    _, proj_a, _ = home
    assert claude_attach.context_note("chat-1") == ""
    claude_attach.attach("chat-1", "7238cfa3")
    note = claude_attach.context_note("chat-1")
    assert "7238cfa3" in note and str(proj_a) in note and "feature-x" in note
    assert "claude_code" in note and len(note) < 700
    from src import agent_loop
    assert agent_loop._attached_claude_note("chat-1") == note


def test_the_odysseus_install_is_warned_about(home):
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(claude_attach.__file__)))
    assert "Odysseus install" in claude_attach.cwd_warning(os.path.join(root_dir, "src"))
    assert "not on this host" in claude_attach.cwd_warning("/no/such/folder/here")
    assert claude_attach.cwd_warning(str(home[1])) == ""


# ── Routes ──────────────────────────────────────────────────────────────

class _Auth:
    is_configured = True

    def __init__(self, admins):
        self.admins = admins

    def is_admin(self, user):
        return user in self.admins


class _Sess:
    def __init__(self, owner):
        self.owner = owner
        self.name = "Login work"


class _Mgr:
    chats = {"chat-1": _Sess("jaron"), "theirs": _Sess("someone")}

    def get_session(self, cid):
        return self.chats[cid]


def _client(monkeypatch, user="jaron", admins=("jaron", "someone")):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: _Mgr())
    app = FastAPI()
    app.state.auth_manager = _Auth(set(admins))

    @app.middleware("http")
    async def _who(request, call_next):
        request.state.current_user = user
        return await call_next(request)

    app.include_router(claude_sessions_routes.setup_claude_sessions_routes())
    return TestClient(app)


def test_the_routes_attach_show_and_detach(home, monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/claude_attach/chat-1").json()["attached"] is None
    r = c.post("/api/claude_attach/chat-1", json={"id": "7238cfa3"})
    assert r.status_code == 200, r.text
    assert r.json()["attached"]["id"] == SID and r.json()["warning"] == ""
    got = c.get("/api/claude_attach/chat-1").json()["attached"]
    assert got["title"] == "Fix the login page" and got["project"] == PROJ
    assert c.delete("/api/claude_attach/chat-1").json()["detached"]["session_id"] == SID
    assert c.get("/api/claude_attach/chat-1").json()["attached"] is None


def test_the_routes_explain_bad_prefixes(home, monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/claude_attach/chat-1", json={"id": "abcdef12"})
    assert r.status_code == 409 and len(r.json()["detail"]["candidates"]) == 2
    assert c.post("/api/claude_attach/chat-1", json={"id": "deadbeef"}).status_code == 404
    assert c.post("/api/claude_attach/chat-1", json={"id": "../etc"}).status_code == 400
    assert c.get("/api/claude_sessions/resolve?prefix=7238cf").json()["session"]["id"] == SID
    assert c.get("/api/claude_sessions/resolve?prefix=abcdef").status_code == 409


def test_only_an_admin_on_their_own_chat(home, monkeypatch):
    c = _client(monkeypatch, user="guest")
    assert c.post("/api/claude_attach/chat-1", json={"id": "7238cfa3"}).status_code == 403
    assert c.get("/api/claude_sessions/resolve?prefix=7238cf").status_code == 403
    c = _client(monkeypatch, user="jaron")
    assert c.post("/api/claude_attach/theirs", json={"id": "7238cfa3"}).status_code == 404
    assert c.post("/api/claude_attach/nope", json={"id": "7238cfa3"}).status_code == 404
    assert claude_attach.get("theirs") is None


# ── The coding tool carries the attached session on ─────────────────────

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
    save(); print("backgrounded · " + sid[:8]); sys.exit(0)
sys.stdin.read()
print(json.dumps({{"type": "result", "result": "Carried on.", "is_error": False}}), flush=True)
'''


@pytest.fixture
def cli(home, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log, state = tmp_path / "calls.jsonl", tmp_path / "agents_view.json"
    exe = bindir / "claude"
    exe.write_text(FAKE.format(py=sys.executable, log=str(log), state=str(state)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(view, "ENABLED", True)
    monkeypatch.setattr(view, "_LIST_TTL_S", 0.0)
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"projects": {}}))
    monkeypatch.setattr(view, "CLAUDE_JSON", str(cj))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "data" / "approvals.json"))
    monkeypatch.setattr(cct, "_user_named_claude", lambda chat_id: False)
    jobs._JOBS.clear()
    # The attached session is parked in Claude's agent view, as after a turn.
    state.write_text(json.dumps([{"id": SID[:8], "sessionId": SID, "cwd": str(home[1]),
                                  "kind": "background", "pid": 4242, "status": "idle"}]))
    return log


def _calls(log, want=None):
    rows = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
    return [r for r in rows if want is None or want(r["argv"])]


def _run(args, chat="chat-1"):
    return asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(args), {"session_id": chat}))


def test_an_ask_resumes_the_attached_session_in_its_folder(home, cli):
    _, proj_a, proj_b = home
    claude_attach.attach("chat-1", "7238cfa3")
    out = _run({"action": "ask", "prompt": "Where did we leave off?"})
    assert out["exit_code"] == 0, out
    turn = _calls(cli, lambda a: a[:1] == ["-p"])[-1]
    assert turn["argv"][turn["argv"].index("--resume") + 1] == SID
    assert turn["cwd"] == str(proj_a)
    assert "attached to this chat" in out.get("agent", "")
    order = [r["argv"][0] if r["argv"][0] != "--bg" else "park" for r in _calls(cli)
             if r["argv"][:1] in (["stop"], ["-p"]) or "--bg" in r["argv"]]
    assert order == ["stop", "-p", "park"]                     # unparked first: no forked copy


def test_a_plan_stays_on_claude_for_the_attached_session(home, cli):
    _, proj_a, proj_b = home
    claude_attach.attach("chat-1", "7238cfa3")
    out = _run({"prompt": "Finish the login fix", "cwd": str(proj_b)})
    assert out["exit_code"] == 0, out
    turn = _calls(cli, lambda a: a[:1] == ["-p"])[-1]["argv"]
    assert turn[turn.index("--resume") + 1] == SID
    assert turn[turn.index("--add-dir") + 1] == str(proj_b)   # the folder asked for, added


def test_other_requests_leave_the_attached_session_alone(home, cli, monkeypatch):
    claude_attach.attach("chat-1", "7238cfa3")
    _run({"action": "ask", "prompt": "Fresh look", "cwd": str(home[1]), "engine": "claude",
          "new_agent": True})
    turn = _calls(cli, lambda a: a[:1] == ["-p"])[-1]["argv"]
    assert "--resume" not in turn and "--session-id" in turn
    _run({"action": "ask", "prompt": "Another chat", "cwd": str(home[1]), "engine": "claude"},
         chat="chat-2")
    turn = _calls(cli, lambda a: a[:1] == ["-p"])[-1]["argv"]
    assert "--resume" not in turn


def test_the_attachment_follows_a_new_session_id(home):
    root, proj_a, _ = home
    copy = "7238cfa3-0000-4000-8000-00000000c0de"              # the CLI went on under a new id
    _write_session(root, PROJ, copy, str(proj_a))
    claude_attach.attach("chat-1", SID)
    claude_attach.follow("chat-1", SID, copy)
    assert claude_attach.get("chat-1")["session_id"] == copy
    claude_attach.follow("chat-1", SID, TWIN_B)                # not the attached one: ignored
    assert claude_attach.get("chat-1")["session_id"] == copy
