"""Native plans only: the chat model writes its plan itself, the user answers in the chat.

Asked for: "i want it so that the model never uses anything external to make
the plans, only using the native markdown planner, so instead of sending it
out it writes the md file in the chat so that i can approve or deny it in the
chat." What matters:

  * propose_plan saves an ordinary Plans document, linked to the chat, pending;
  * approve / deny (the user's own request) record the answer and start the
    chat again with the right message; the card's state survives a reload;
  * claude_code's "plan" action is refused for a model, and "execute" needs an
    approved native plan for the chat, runs once, and skips the coding agent's
    own plan phase;
  * while a plan waits, delegation tools are refused;
  * plan mode keeps propose_plan.
No real model or coding agent runs: the LLM stream and the CLI are fakes.
"""
import asyncio
import json
import os
import stat
import sys

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.agent_tools import ToolBlock, TOOL_TAGS  # noqa: E402  (import first to avoid circular)
from src.tool_execution import execute_tool_block
from src import native_plans, plans

CHAT = "chat-np-1"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    return tmp_path


def _call(tool, args, chat=CHAT, owner=None):
    content = json.dumps(args) if isinstance(args, dict) else args
    return asyncio.run(execute_tool_block(ToolBlock(tool, content), session_id=chat, owner=owner))


def _propose(chat=CHAT, title="Add a dark mode", body=None, **extra):
    body = body or ("## Goal\nA dark theme.\n\n## Steps\n- [ ] Add CSS variables\n- [ ] Add a toggle\n\n"
                    "## Risks\nContrast.\n\n## On approval\nOpenCode in /tmp/site")
    return _call("propose_plan", {"title": title, "plan": body, **extra}, chat=chat)


# ── The tool ─────────────────────────────────────────────────────────────

def test_propose_plan_saves_a_plans_document_linked_to_the_chat(data_dir):
    desc, out = _propose(engine="opencode", cwd="/tmp/site")
    assert out["exit_code"] == 0, out
    np = out["native_plan"]
    assert np["status"] == "pending" and np["title"] == "Add a dark mode"
    # An ordinary Plans document, in the owner's folder, that the editor lists.
    doc = plans.get_plan(None, np["name"])
    assert doc["content"].startswith("# Add a dark mode\n")
    assert "- [ ] Add CSS variables" in doc["content"]
    assert [p["name"] for p in plans.list_plans(None)] == [np["name"]]
    entry = native_plans.get(np["id"])
    assert entry["chat_session_id"] == CHAT and entry["status"] == "pending"
    assert entry["engine"] == "opencode" and entry["cwd"] == "/tmp/site"
    assert native_plans.pending_for_chat(CHAT)["id"] == np["id"]
    assert "Do not start the work" in out["output"]


def test_propose_plan_needs_a_title_and_a_body(data_dir):
    _, out = _call("propose_plan", {"title": "x"})
    assert out["exit_code"] == 1 and "plan" in out["error"]


def test_a_new_plan_replaces_a_pending_one_and_a_revision_keeps_the_old(data_dir):
    first = _propose()[1]["native_plan"]
    second = _propose(body="- [ ] smaller step")[1]["native_plan"]
    assert native_plans.get(first["id"])["status"] == "superseded"
    assert second["revision"] == 2 and native_plans.get(second["id"])["revises"] == first["id"]
    # Both files are kept.
    assert len(plans.list_plans(None)) == 2


def test_registered_everywhere():
    from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_policy import _COMMON_TOOL_NAMES
    assert "propose_plan" in TOOL_TAGS and "propose_plan" in ALWAYS_AVAILABLE
    assert "propose_plan" in BUILTIN_TOOL_DESCRIPTIONS and "propose_plan" in _COMMON_TOOL_NAMES
    assert "propose_plan" in {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}


# ── Approve and deny, from the user's page ──────────────────────────────

@pytest.fixture
def client(data_dir, monkeypatch):
    from routes import native_plans_routes, plans_routes
    resumed = []

    def fake_start_turn(chat_id, prompt, *, note_source, reply_source, agent_loop=None):
        resumed.append({"chat": chat_id, "prompt": prompt, "source": note_source})
        return True
    import src.screen_control_resume as scr
    monkeypatch.setattr(scr, "start_turn", fake_start_turn)

    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(native_plans_routes.setup_native_plans_routes())
    app.include_router(plans_routes.setup_plans_routes())
    with TestClient(app) as c:
        c.resumed = resumed
        yield c


ANN = {"x-test-user": "ann"}
BOB = {"x-test-user": "bob"}


def _ann_plan(**extra):
    _, out = asyncio.run(execute_tool_block(ToolBlock("propose_plan", json.dumps({
        "title": "Fix the login bug", "plan": "- [ ] Find it\n- [ ] Fix it", **extra})),
        session_id=CHAT, owner="ann"))
    return out["native_plan"]


def test_approve_records_it_and_resumes_the_chat_with_the_plan(client):
    np = _ann_plan(engine="opencode", cwd="/tmp/proj")
    r = client.post(f"/api/native_plans/{np['id']}/approve", headers=ANN)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "approved" and d["approved_at"] and d["resuming"] is True
    (call,) = client.resumed
    assert call["chat"] == CHAT and call["source"] == "native_plan_approved"
    assert 'The user approved plan "Fix the login bug"; carry it out' in call["prompt"]
    assert "- [ ] Find it" in call["prompt"]                         # the spec
    assert f'"plan_id": "{np["id"]}"' in call["prompt"] and '"action": "execute"' in call["prompt"]
    # Once only.
    assert client.post(f"/api/native_plans/{np['id']}/approve", headers=ANN).status_code == 409


def test_deny_records_the_note_and_tells_the_model_to_revise_or_stop(client):
    np = _ann_plan()
    r = client.post(f"/api/native_plans/{np['id']}/deny", headers=ANN, json={"note": "Too broad"})
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert r.json()["deny_note"] == "Too broad"
    (call,) = client.resumed
    assert call["source"] == "native_plan_denied"
    assert "denied plan \"Fix the login bug\"" in call["prompt"] and "Too broad" in call["prompt"]
    assert "propose_plan" in call["prompt"] and f"revises '{np['id']}'" in call["prompt"]
    # A revision is a new version; the denied one is kept.
    rev = _ann_plan(revises=np["id"])
    assert rev["revision"] == 2 and native_plans.get(np["id"])["status"] == "denied"


def test_the_card_state_survives_a_reload(client):
    np = _ann_plan()
    assert client.get(f"/api/native_plans/{np['id']}", headers=ANN).json()["status"] == "pending"
    client.post(f"/api/native_plans/{np['id']}/approve", headers=ANN)
    again = client.get(f"/api/native_plans/{np['id']}", headers=ANN).json()
    assert again["status"] == "approved" and "- [ ] Find it" in again["content"]
    listed = client.get(f"/api/native_plans?chat={CHAT}", headers=ANN).json()["plans"]
    assert [p["id"] for p in listed] == [np["id"]]
    # The Plans list shows it was proposed in a chat and approved.
    rows = client.get("/api/plans", headers=ANN).json()["plans"]
    assert rows[0]["proposal"]["status"] == "approved"


def test_edits_in_plans_are_what_gets_approved(client):
    np = _ann_plan()
    doc = client.get(f"/api/plans/{np['name']}", headers=ANN).json()
    edited = doc["content"] + "- [ ] Add a test\n"
    assert client.put(f"/api/plans/{np['name']}", headers=ANN,
                      json={"content": edited, "base_version": doc["version"]}).status_code == 200
    assert "Add a test" in client.get(f"/api/native_plans/{np['id']}", headers=ANN).json()["content"]
    client.post(f"/api/native_plans/{np['id']}/approve", headers=ANN)
    assert "Add a test" in native_plans.get(np["id"])["approved_content"]
    assert "Add a test" in client.resumed[0]["prompt"]


def test_a_renamed_plan_stays_linked(client):
    np = _ann_plan()
    r = client.post(f"/api/plans/{np['name']}/rename", headers=ANN, json={"title": "Login fix"})
    assert r.status_code == 200
    assert client.get(f"/api/native_plans/{np['id']}", headers=ANN).json()["name"] == "Login fix"


def test_another_user_cannot_see_or_answer_it(client):
    np = _ann_plan()
    assert client.get(f"/api/native_plans/{np['id']}", headers=BOB).status_code == 404
    assert client.post(f"/api/native_plans/{np['id']}/approve", headers=BOB).status_code == 404
    assert native_plans.get(np["id"])["status"] == "pending"
    assert client.get("/api/native_plans/../../etc", headers=ANN).status_code in (400, 404)


# ── claude_code: no plan phase for models, execute needs an approved plan ──

FAKE_OPENCODE = r'''#!{py}
import json, sys
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:]}}) + "\n")
print(json.dumps({{"type": "text", "part": {{"text": "Done: dark mode added."}}}}), flush=True)
'''


@pytest.fixture
def coder(data_dir, monkeypatch):
    from src import claude_code_approvals as approvals
    from src import claude_code_jobs as jobs
    bindir = data_dir / "bin"
    bindir.mkdir()
    log = data_dir / "calls.jsonl"
    exe = bindir / "opencode"
    exe.write_text(FAKE_OPENCODE.format(py=sys.executable, log=str(log)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    home = data_dir / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text(json.dumps({"model": "vllm3090/qwen3.8-27b"}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(data_dir / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(data_dir / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(data_dir / "jobs.json"))
    jobs._JOBS.clear()
    from src import opencode_providers
    monkeypatch.setattr(opencode_providers, "_endpoints", lambda: [])
    work = data_dir / "work"
    work.mkdir()

    def calls():
        return [json.loads(l)["argv"] for l in open(log)] if log.exists() else []
    return work, calls


def test_the_plan_action_is_refused_for_a_model(coder):
    work, calls = coder
    for args in ({"action": "plan", "prompt": "Add a dark mode to the site", "cwd": str(work)},
                 {"prompt": "Add a dark mode to the site", "cwd": str(work)}):       # plan was the default
        _, out = _call("claude_code", args)
        assert out["exit_code"] == 1 and "propose_plan" in out["error"], out
    assert calls() == []


def test_the_plan_action_is_gone_from_what_the_model_sees():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    cc = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "claude_code")
    props = cc["function"]["parameters"]["properties"]
    assert "plan" not in props["action"]["enum"] and "ask" in props["action"]["enum"]
    assert "plan_id" in props
    assert "propose_plan" in cc["function"]["description"]
    from src.agent_loop import TOOL_SECTIONS
    assert "ALWAYS plan first" not in TOOL_SECTIONS["claude_code"]
    assert "propose_plan" in TOOL_SECTIONS["claude_code"]


def test_execute_without_an_approved_plan_is_refused(coder):
    work, calls = coder
    _, out = _call("claude_code", {"action": "execute", "prompt": "go", "cwd": str(work)})
    assert out["exit_code"] == 1 and "propose_plan" in out["error"]
    np = _propose(cwd=str(work))[1]["native_plan"]
    # Pending: the plan is waiting, so claude_code is blocked outright.
    _, out = _call("claude_code", {"action": "execute", "plan_id": np["id"], "prompt": "go", "cwd": str(work)})
    assert out["exit_code"] == 1 and "waiting for the user" in out["error"]
    native_plans.deny(np["id"], "")
    _, out = _call("claude_code", {"action": "execute", "plan_id": np["id"], "prompt": "go", "cwd": str(work)})
    assert out["exit_code"] == 1 and "denied" in out["error"]
    _, out = _call("claude_code", {"action": "execute", "plan_id": "0123456789ab", "prompt": "go", "cwd": str(work)})
    assert out["exit_code"] == 1
    assert calls() == []


def test_execute_with_an_approved_plan_runs_once_and_skips_the_plan_phase(coder):
    work, calls = coder
    np = _propose(engine="opencode", cwd=str(work))[1]["native_plan"]
    native_plans.approve(np["id"], "")
    # From another chat: refused.
    _, other = _call("claude_code", {"action": "execute", "plan_id": np["id"], "prompt": "go",
                                     "cwd": str(work)}, chat="another-chat")
    assert other["exit_code"] == 1 and "another chat" in other["error"]
    _, out = _call("claude_code", {"action": "execute", "plan_id": np["id"],
                                   "prompt": "Carry out the approved plan."})
    assert out.get("exit_code") == 0, out
    (argv,) = calls()
    # Straight to the writing agent: no "plan" agent, no resumed plan session.
    assert argv[argv.index("--agent") + 1] == "build"
    assert "--session" not in argv
    prompt = argv[-1]
    assert "<approved-plan>" in prompt and "- [ ] Add CSS variables" in prompt
    assert "do not plan" in prompt.lower()
    e = native_plans.get(np["id"])
    assert e["executed_at"] and e["run_job_id"]
    # A second execute of the same approval is refused.
    _, again = _call("claude_code", {"action": "execute", "plan_id": np["id"], "prompt": "go", "cwd": str(work)})
    assert again["exit_code"] == 1 and "already started" in again["error"]
    assert len(calls()) == 1


def test_execute_must_use_the_folder_the_plan_names(coder, tmp_path):
    work, calls = coder
    np = _propose(cwd=str(work))[1]["native_plan"]
    native_plans.approve(np["id"], "")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _, out = _call("claude_code", {"action": "execute", "plan_id": np["id"], "prompt": "go", "cwd": str(elsewhere)})
    assert out["exit_code"] == 1 and "cwd does not match" in out["error"]
    assert calls() == []
    assert not native_plans.get(np["id"]).get("executed_at")


# ── Delegation is held while a plan waits ───────────────────────────────

def test_delegation_tools_are_blocked_while_a_plan_is_pending(data_dir):
    _propose()
    for tool, args in (("chat_with_model", {"model": "x", "message": "write me a plan"}),
                       ("ask_teacher", "write me a plan"),
                       ("spawn_subagent", {"task": "draft the plan"}),
                       ("pipeline", {"steps": []}),
                       ("claude_code", {"action": "ask", "prompt": "what is here", "cwd": "/tmp"})):
        desc, out = _call(tool, args)
        assert "BLOCKED" in desc and "waiting for the user" in out["error"], (tool, out)
    # Reads of claude_code's own runs are not delegation.
    desc, out = _call("claude_code", {"action": "status"})
    assert "BLOCKED" not in desc
    # Another chat is not held up.
    desc, _ = _call("chat_with_model", {"model": "x", "message": "hi"}, chat="other-chat")
    assert "BLOCKED" not in desc


def test_once_answered_delegation_is_allowed_again(data_dir):
    np = _propose()[1]["native_plan"]
    native_plans.deny(np["id"], "", "no")
    from src import tool_execution
    assert tool_execution._pending_plan_block("chat_with_model", "{}", CHAT) == ""


# ── The agent loop: rule in the prompt, card event, turn ends ───────────

def _run_loop(monkeypatch, text, *, plan_mode=False):
    import src.agent_loop as al
    seen = []
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def fake_stream(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    async def collect():
        return [c async for c in al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "plan a dark mode for my site"}],
            max_rounds=3, relevant_tools={"propose_plan", "read_file"}, session_id=CHAT,
            plan_mode=plan_mode)]
    chunks = asyncio.run(collect())
    events = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                events.append(json.loads(c[6:]))
            except Exception:
                pass
    return seen, events


PLAN_BLOCK = ("```propose_plan\n" + json.dumps({"title": "Dark mode", "plan": "- [ ] Add CSS\n- [ ] Add toggle"})
              + "\n```")


def test_the_loop_ends_the_turn_on_a_plan_and_saves_its_id(data_dir, monkeypatch):
    seen, events = _run_loop(monkeypatch, PLAN_BLOCK)
    assert len(seen) == 1, "the turn must end after propose_plan"
    system = seen[0][0]["content"]
    assert "PLANS ARE WRITTEN HERE, BY YOU" in system and "propose_plan" in system
    out = [e for e in events if e.get("type") == "tool_output" and e.get("tool") == "propose_plan"]
    assert out and out[0]["exit_code"] == 0 and native_plans.valid_id(out[0]["native_plan_id"])
    metrics = next(e for e in events if e.get("type") == "metrics")["data"]
    ev = [t for t in metrics.get("tool_events", []) if t.get("tool") == "propose_plan"]
    assert not metrics.get("tool_events") or ev[0]["native_plan_id"] == out[0]["native_plan_id"]
    assert any("Plan proposed: **Dark mode**" in (e.get("delta") or "") for e in events)


def test_the_pending_plan_is_named_in_the_next_turns_prompt(data_dir, monkeypatch):
    np = _propose()[1]["native_plan"]
    seen, _ = _run_loop(monkeypatch, "Waiting for your answer.")
    assert "A PLAN IS WAITING FOR THE USER" in seen[0][0]["content"] and np["id"] in seen[0][0]["content"]
    native_plans.approve(np["id"], "")
    seen, _ = _run_loop(monkeypatch, "ok")
    assert "APPROVED PLAN" in seen[0][0]["content"] and f"plan_id '{np['id']}'" in seen[0][0]["content"]


def test_plan_mode_keeps_the_plan_tool(data_dir, monkeypatch):
    from src.tool_security import plan_mode_disabled_tools, PLAN_MODE_READONLY_TOOLS
    assert "propose_plan" in PLAN_MODE_READONLY_TOOLS
    assert "propose_plan" not in plan_mode_disabled_tools()
    seen, events = _run_loop(monkeypatch, PLAN_BLOCK, plan_mode=True)
    assert "propose_plan" in seen[0][0]["content"].split("## PLAN MODE")[1][:2000]
    out = [e for e in events if e.get("type") == "tool_output" and e.get("tool") == "propose_plan"]
    assert out and out[0]["exit_code"] == 0
    assert native_plans.pending_for_chat(CHAT)
