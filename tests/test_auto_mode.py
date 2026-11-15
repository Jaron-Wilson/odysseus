"""Auto mode: a safety check decides each gated action instead of an approval card.

What matters (src/auto_mode.py):

  * Ask (the default) is unchanged: every gate shows its card and no model is asked;
  * in Auto each gate (Google writes, mod tools that ask first, the OpenCode
    chat, screen control, plans, coding plans) allows, asks or blocks;
  * hard rules come first and need no model: spending, messages to other
    people, publishing, bulk deletes, security changes and credentials always
    ask; read-only tools are allowed;
  * a safety check that fails, times out, answers nonsense or has no model asks;
  * identical decisions are cached within a turn;
  * a chat can override the user's default, both ways;
  * tasks and the mail listener keep asking;
  * every decision is logged, without secrets;
  * the agent cannot switch itself to Auto.
No model is called: the safety check model is a fake.
"""
import asyncio
import json

import pytest

from src.agent_tools import ToolBlock  # noqa: E402  (import first to avoid circular)
from src.tool_execution import execute_tool_block
from src import action_approvals as AA
from src import auto_mode
from src import google_write_gate as G
from tests.mods_helpers import mods_dir  # noqa: F401  (fixture)

CHAT = "chat-auto-1"
OWNER = "alice"


class FakeModel:
    def __init__(self, answer='{"decision": "allow", "reason": "what the user asked for"}'):
        self.answer = answer
        self.calls = []

    async def __call__(self, target, messages, owner):
        self.calls.append(messages)
        if isinstance(self.answer, BaseException):
            raise self.answer
        if callable(self.answer):
            return await self.answer()
        return self.answer


@pytest.fixture
def env(tmp_path, monkeypatch):
    import src.constants
    import routes.prefs_routes as pr
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pr, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    monkeypatch.setattr(auto_mode, "_turns", {})
    model = FakeModel()
    monkeypatch.setattr(auto_mode, "_call_model", model)
    monkeypatch.setattr(auto_mode, "safety_model", lambda owner: {
        "url": "http://127.0.0.1:1/v1", "model": "tiny", "headers": {}, "label": "tiny", "billing": "local_free",
        "source": "utility"})
    auto_mode.begin_turn(CHAT, [{"role": "user", "content": "email me the summary and tidy my calendar"}])
    return model


def _auto(owner=OWNER):
    auto_mode.set_user_settings(owner, mode="auto")


def _decide(gate, tool, args, meta=None, chat=CHAT):
    return asyncio.run(auto_mode.decide(gate, tool, args, owner=OWNER, session_id=chat, meta=meta or {}))


# ── Settings and the per-chat override ───────────────────────────────────

def test_ask_is_the_default_and_settings_are_in_the_users_prefs(env):
    assert auto_mode.user_mode(OWNER) == "ask"
    assert not auto_mode.active(OWNER, CHAT)
    _auto()
    from routes.prefs_routes import _load_for_user
    assert _load_for_user(OWNER)["approvals_mode"] == "auto"
    assert auto_mode.active(OWNER, CHAT)
    with pytest.raises(ValueError):
        auto_mode.set_user_settings(OWNER, mode="yolo")


def test_a_chat_overrides_the_default_both_ways(env):
    auto_mode.set_chat_override(CHAT, "auto")
    assert auto_mode.effective_mode(OWNER, CHAT) == "auto" and auto_mode.active(OWNER, CHAT)
    assert not auto_mode.active(OWNER, "another-chat")
    _auto()
    auto_mode.set_chat_override(CHAT, "ask")
    assert not auto_mode.active(OWNER, CHAT)
    assert auto_mode.active(OWNER, "another-chat")
    auto_mode.set_chat_override(CHAT, "")
    assert auto_mode.chat_override(CHAT) == "" and auto_mode.active(OWNER, CHAT)


def test_tasks_and_the_mail_listener_keep_asking(env):
    _auto()
    tok = AA.run_context.set("mail")
    try:
        assert not auto_mode.active(OWNER, CHAT)
        assert asyncio.run(auto_mode.check("google_write", "send_email", {}, owner=OWNER, session_id=CHAT)) is None
    finally:
        AA.run_context.reset(tok)
    assert auto_mode.active(OWNER, CHAT)


# ── Hard rules ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate,tool,args,meta,why", [
    ("mod_action", "mod__shop__buy_item", {"item": "gpu"}, {}, "money"),
    ("mod_action", "mod__shop__checkout", {}, {}, "money"),
    ("mod_action", "mod__x__do", {"amount": 30, "currency": "USD"}, {}, "money"),
    ("google_write", "send_email", {"to": "bob@example.com", "body": "hi"},
     {"owner_addresses": ["me@gmail.com"]}, "someone other than you"),
    ("google_write", "send_email", {"to": "me@gmail.com", "cc": "bob@example.com"},
     {"owner_addresses": ["me@gmail.com"]}, "someone other than you"),
    ("google_write", "reply_to_email", {"uid": "4", "body": "ok"}, {"outward": True}, "other people"),
    ("mod_action", "mod__phone__send_sms", {"to": "+15550100", "text": "hi"}, {}, "someone other than you"),
    ("mod_action", "mod__social__publish_post", {"text": "hello"}, {}, "publishes"),
    ("google_write", "bulk_email", {"action": "delete", "uids": ["1", "2"]}, {"destructive": True}, "in bulk"),
    ("google_write", "manage_calendar", {"action": "delete_event", "events": [{"uid": "a"}, {"uid": "b"}]},
     {}, "in bulk"),
    ("mod_action", "mod__files__delete_files", {"paths": ["/a", "/b"]}, {}, "in bulk"),
    ("mod_action", "mod__google__drive_share", {"file": "x", "role": "writer"}, {}, "permissions"),
    ("mod_action", "mod__acct__change_password", {"user": "me"}, {}, "sign-in"),
    ("mod_action", "mod__x__configure", {"api_key": "abc123"}, {}, "credentials"),
    ("mod_action", "mod__x__note", {"text": "use sk-ant-REDACTEDREDACTEDREDACTED1234"}, {}, "credentials"),
])
def test_hard_rules_always_ask_without_a_model(env, gate, tool, args, meta, why):
    d = _decide(gate, tool, args, meta)
    assert d.verdict == "ask" and d.source == "rule" and why in d.reason, d
    assert env.calls == []


def test_read_only_tools_are_allowed_by_rule(env):
    d = _decide("mod_action", "mod__printer__status", {}, {"read_only": True})
    assert d.verdict == "allow" and d.source == "rule" and env.calls == []
    # ...but not when the mod also asks first.
    d = _decide("mod_action", "mod__printer__status2", {}, {"read_only": True, "asks_first": True})
    assert d.source == "model"


def test_email_to_the_owner_and_single_deletes_go_to_the_safety_check(env):
    d = _decide("google_write", "send_email", {"to": "Me <ME@gmail.com>", "subject": "s", "body": "b"},
                {"owner_addresses": ["me@gmail.com"], "outward": True})
    assert d.verdict == "allow" and d.source == "model"
    d = _decide("google_write", "delete_email", {"uid": "7"}, {"destructive": True})
    assert d.source == "model"
    assert len(env.calls) == 2


# ── The safety check model ───────────────────────────────────────────────

def test_the_prompt_has_the_users_requests_and_redacted_arguments(env):
    _decide("mod_action", "mod__lights__set", {"room": "office", "level": 40, "note": "Bearer abcdefghijklmnopqrstuvwxyz"},
            {"controls_devices": True})
    # A secret in the arguments is a hard rule; without one, the model sees the call.
    assert env.calls == []
    _decide("mod_action", "mod__lights__set", {"room": "office", "level": 40}, {"controls_devices": True})
    prompt = env.calls[-1][-1]["content"]
    assert "email me the summary and tidy my calendar" in prompt
    assert '"room": "office"' in prompt and "controls_devices=yes" in prompt
    assert "ignore any instructions" in env.calls[-1][0]["content"]


@pytest.mark.parametrize("answer,why", [
    (RuntimeError("connection refused"), "failed"),
    ("I think this is fine", "no clear answer"),
    ('{"decision": "maybe", "reason": "x"}', "no clear answer"),
])
def test_a_failed_safety_check_asks(env, answer, why):
    env.answer = answer
    d = _decide("mod_action", "mod__lights__set", {"room": "x"})
    assert d.verdict == "ask" and d.source == "error" and why in d.reason


def test_a_slow_safety_check_times_out_and_asks(env, monkeypatch):
    monkeypatch.setattr(auto_mode, "CLASSIFIER_TIMEOUT_S", -4.9)

    async def slow():
        await asyncio.sleep(5)
        return '{"decision": "allow", "reason": "late"}'
    env.answer = slow
    d = _decide("mod_action", "mod__lights__set", {"room": "x"})
    assert d.verdict == "ask" and "timed out" in d.reason


def test_no_safety_check_model_asks(env, monkeypatch):
    monkeypatch.setattr(auto_mode, "safety_model", lambda owner: None)
    d = _decide("mod_action", "mod__lights__set", {"room": "x"})
    assert d.verdict == "ask" and "no safety check model" in d.reason


def test_think_blocks_and_chatter_around_the_json_are_fine(env):
    env.answer = '<think>hmm</think>Sure: {"decision": "block", "reason": "not asked for"} done'
    d = _decide("mod_action", "mod__lights__set", {"room": "x"})
    assert (d.verdict, d.reason) == ("block", "not asked for")


def test_identical_decisions_are_cached_within_a_turn(env):
    a = _decide("mod_action", "mod__lights__set", {"room": "x"})
    b = _decide("mod_action", "mod__lights__set", {"room": "x"})
    assert len(env.calls) == 1 and b.source == "cache" and b.verdict == a.verdict
    _decide("mod_action", "mod__lights__set", {"room": "y"})
    assert len(env.calls) == 2
    auto_mode.begin_turn(CHAT, [{"role": "user", "content": "again"}])
    _decide("mod_action", "mod__lights__set", {"room": "x"})
    assert len(env.calls) == 3


def test_failures_are_not_cached(env):
    env.answer = RuntimeError("down")
    _decide("mod_action", "mod__lights__set", {"room": "x"})
    env.answer = '{"decision": "allow", "reason": "ok"}'
    assert _decide("mod_action", "mod__lights__set", {"room": "x"}).verdict == "allow"


def test_default_model_is_never_a_metered_remote_one(monkeypatch, tmp_path):
    import src.constants
    import routes.prefs_routes as pr
    import src.endpoint_resolver as er
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pr, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    monkeypatch.setattr(auto_mode, "_local_endpoint_target", lambda owner: None)
    monkeypatch.setattr(er, "resolve_endpoint", lambda *a, **k: ("https://api.openai.com/v1/chat/completions",
                                                                 "gpt-x", {}))
    assert auto_mode.safety_model(OWNER) is None
    monkeypatch.setattr(er, "resolve_endpoint", lambda *a, **k: ("http://192.168.1.5:11434/v1/chat/completions",
                                                                 "qwen3:8b", {}))
    t = auto_mode.safety_model(OWNER)
    assert t["model"] == "qwen3:8b" and t["billing"] == "local_free" and t["source"] == "utility"


# ── The log ──────────────────────────────────────────────────────────────

def test_every_decision_is_logged_without_secrets(env):
    _decide("mod_action", "mod__x__configure", {"password": "hunter2-secret"})
    _decide("mod_action", "mod__lights__set", {"room": "x"})
    rows = auto_mode.recent_log(OWNER)
    assert [r["tool"] for r in rows] == ["mod__lights__set", "mod__x__configure"]
    assert rows[1]["verdict"] == "ask" and rows[1]["source"] == "rule"
    assert rows[0]["verdict"] == "allow" and rows[0]["model"] == "tiny" and rows[0]["chat_id"] == CHAT
    raw = open(auto_mode._log_file()).read()
    assert "hunter2" not in raw and "[redacted]" in raw
    assert auto_mode.recent_log("someone-else") == []


# ── Gate: Google writes ──────────────────────────────────────────────────

@pytest.fixture
def google(env, monkeypatch):
    async def fake_classify(tool, content, owner):
        return {"kind": "google_write", "action": "Send email", "title": f"Send an email ({tool})",
                "details": [("Account", "me@gmail.com")], "body": "", "body_label": ""}
    monkeypatch.setattr(G, "classify", fake_classify)
    monkeypatch.setattr(G, "_email_rows", lambda: [{"from_address": "me@gmail.com", "imap_user": "me@gmail.com"}])

    async def no_notify(*a, **k):
        return None
    monkeypatch.setattr(G, "_notify", no_notify)
    sent = []

    class FakeMCP:
        async def call_tool(self, name, args):
            sent.append((name, {k: v for k, v in args.items() if not k.startswith("_")}))
            return {"output": f"ok {name}", "exit_code": 0}
    # The module execute_tool_block lives in (another test file may have
    # swapped sys.modules["src.tool_execution"] by now).
    g = execute_tool_block.__globals__
    monkeypatch.setitem(g, "get_mcp_manager", lambda: FakeMCP())
    monkeypatch.setitem(g, "_owner_is_admin", lambda owner: True)
    return sent


def _send(to):
    content = json.dumps({"to": to, "subject": "Summary", "body": "Here it is"})
    return asyncio.run(execute_tool_block(ToolBlock("send_email", content), session_id=CHAT, owner=OWNER))


def test_google_write_ask_mode_is_unchanged(google, env):
    desc, r = _send("me@gmail.com")
    assert "waiting for approval" in desc and r["action_approval"]["status"] == "pending"
    assert google == [] and env.calls == [] and "auto_mode" not in r
    assert all(row[0] != "Auto mode" for row in AA.get(r["action_approval"]["id"])["details"])


def test_google_write_auto_allow_runs_with_a_note(google, env):
    _auto()
    desc, r = _send("me@gmail.com")
    assert google and google[0][0] == "mcp__email__send_email"
    assert r["exit_code"] == 0 and "action_approval" not in r
    assert r["auto_mode"][0]["verdict"] == "allow" and r["auto_mode"][0]["gate"] == "google_write"
    assert AA.pending_for_chat(CHAT) is None


def test_google_write_auto_ask_shows_the_card_with_the_reason(google, env):
    _auto()
    desc, r = _send("bob@example.com")
    assert google == [] and r["action_approval"]["status"] == "pending"
    rows = AA.get(r["action_approval"]["id"])["details"]
    assert ["Auto mode", "Asked you because: it sends a message to someone other than you"] in rows
    assert env.calls == []


def test_google_write_auto_block_tells_the_agent(google, env):
    _auto()
    env.answer = '{"decision": "block", "reason": "the user did not ask to email anyone"}'
    desc, r = _send("me@gmail.com")
    assert google == [] and "blocked by auto mode" in desc and r["exit_code"] == 1
    assert r["auto_mode_blocked"] and "the user did not ask to email anyone" in r["error"]
    assert "ask how they want to go on" in r["error"]
    assert AA.pending_for_chat(CHAT) is None


# ── Gate: mod tools ──────────────────────────────────────────────────────

@pytest.fixture
def mod(env, mods_dir, monkeypatch):
    from src.mods import access, runtime
    from tests.mods_helpers import add_mod, write_tools_cache

    class _Admins:
        def is_admin(self, u):
            return True
    monkeypatch.setattr(access, "_auth_manager", _Admins())
    add_mod({"tools": {"keywords": ["light"]}}, mod_id="lights", permissions=["devices.control"])
    write_tools_cache("lights", [
        {"name": "set_light", "description": "Set a light", "parameters": {"type": "object"},
         "keywords": [], "controls_devices": True, "read_only": False}])
    ran = []

    async def no_port(mod_id):
        ran.append(mod_id)
        return None
    monkeypatch.setattr(runtime, "ensure_running", no_port)
    return ran


def _mod_call():
    from src.mods import tools
    return asyncio.run(tools.execute("mod__lights__set_light", '{"room": "office"}', OWNER, CHAT))


def test_mod_tool_ask_mode_is_unchanged(mod, env):
    desc, r = _mod_call()
    assert "waiting for approval" in desc and mod == [] and env.calls == []


def test_mod_tool_auto_allow_block_and_ask(mod, env):
    _auto()
    desc, r = _mod_call()
    assert mod == ["lights"] and "action_approval" not in r          # went on to run it
    env.answer = '{"decision": "block", "reason": "not asked for"}'
    auto_mode.begin_turn(CHAT, [{"role": "user", "content": "hi"}])
    desc, r = _mod_call()
    assert "blocked by auto mode" in desc and r["auto_mode_blocked"] and mod == ["lights"]
    env.answer = '{"decision": "ask", "reason": "unsure which room"}'
    auto_mode.begin_turn(CHAT, [{"role": "user", "content": "hi"}])
    desc, r = _mod_call()
    assert "waiting for approval" in desc
    assert ["Auto mode", "Asked you because: unsure which room"] in AA.get(r["action_approval"]["id"])["details"]


# ── Gate: the OpenCode chat ──────────────────────────────────────────────

def _oc_hold(monkeypatch, sent_text="run the tests"):
    from src.agent_tools import opencode_chat_tool as oct
    from src import opencode_chat as oc
    monkeypatch.setattr(oc, "is_armed", lambda sid, cid: False)
    monkeypatch.setattr(oc, "sent_before", lambda sid, cid: False)
    tool = oct.OpenCodeChatTool.__new__(oct.OpenCodeChatTool)
    chat = {"id": "ses_1", "title": "Site", "folder": "/tmp/site", "busy": False}
    content = json.dumps({"action": "send", "text": sent_text})
    return asyncio.run(tool._hold_unless_ok("send", content, chat, {"text": sent_text},
                                            {"owner": OWNER, "session_id": CHAT}))


def test_opencode_chat_ask_mode_is_unchanged(env, monkeypatch):
    held = _oc_hold(monkeypatch)
    assert held["action_approval"]["status"] == "pending" and env.calls == []


def test_opencode_chat_auto_allow_block_and_ask(env, monkeypatch):
    _auto()
    assert _oc_hold(monkeypatch) is None
    env.answer = '{"decision": "block", "reason": "unrelated to the request"}'
    held = _oc_hold(monkeypatch, "rm -rf the repo")
    assert held["auto_mode_blocked"] and "unrelated to the request" in held["error"]
    env.answer = '{"decision": "ask", "reason": "large change"}'
    held = _oc_hold(monkeypatch, "rewrite everything")
    assert held["action_approval"]["status"] == "pending"


# ── Gate: screen control ─────────────────────────────────────────────────

@pytest.fixture
def screen(env, tmp_path, monkeypatch):
    from src import screen_control_approvals as sca
    from src.mcp_manager import McpManager
    monkeypatch.setattr(sca, "APPROVALS_FILE", str(tmp_path / "grants.json"))
    mgr = McpManager()
    mgr._connections["pc"] = {"status": "connected", "name": "PC"}
    mgr._sessions["pc"] = object()
    calls = []

    async def fake_do_call(session, name, arguments):
        calls.append((name, arguments))
        return {"output": "clicked", "exit_code": 0}
    mgr._do_call = fake_do_call
    return mgr, calls


def _click(mgr):
    return asyncio.run(mgr.call_tool("mcp__pc__click", {"x": 10, "y": 20, "_owner": OWNER, "_session_id": CHAT}))


def test_screen_control_ask_mode_is_unchanged(screen, env):
    mgr, calls = screen
    r = _click(mgr)
    assert r.get("needs_approval") and calls == [] and env.calls == []


def test_screen_control_auto_allow_block_and_ask(screen, env):
    from src import screen_control_approvals as sca
    mgr, calls = screen
    _auto()
    r = _click(mgr)
    assert calls == [("click", {"x": 10, "y": 20})] and r.get("output") == "clicked"
    assert sca.active_grant("pc", OWNER) is None                    # nothing granted for later
    env.answer = '{"decision": "block", "reason": "not what the user asked"}'
    auto_mode.begin_turn(CHAT, [{"role": "user", "content": "x"}])
    r = _click(mgr)
    assert r["auto_mode_blocked"] and len(calls) == 1
    env.answer = '{"decision": "ask", "reason": "unclear target"}'
    auto_mode.begin_turn(CHAT, [{"role": "user", "content": "x"}])
    r = _click(mgr)
    assert r.get("needs_approval") and len(calls) == 1
    pend = [p for p in sca._load().values() if p.get("status") == "pending"]
    assert pend and "auto mode asked because unclear target" in pend[0]["reason"]


# ── Gate: plans ──────────────────────────────────────────────────────────

def _propose():
    content = json.dumps({"title": "Dark mode", "plan": "## Steps\n- [ ] Add CSS", "cwd": "/tmp/site"})
    return asyncio.run(execute_tool_block(ToolBlock("propose_plan", content), session_id=CHAT, owner=OWNER))


def test_plan_ask_mode_is_unchanged(env):
    from src import native_plans
    desc, r = _propose()
    assert r["native_plan"]["status"] == "pending" and env.calls == []
    assert native_plans.get(r["native_plan"]["id"])["status"] == "pending"


def test_plan_auto_allow_approves_it_and_starts_the_work_after_the_turn(env, monkeypatch):
    from src import native_plans
    import src.screen_control_resume as scr
    _auto()
    later, started = [], []
    monkeypatch.setattr(auto_mode, "after_turn", lambda sid, fn, **k: later.append((sid, fn)))
    monkeypatch.setattr(scr, "start_turn", lambda sid, prompt, **k: started.append((sid, prompt)) or True)
    desc, r = _propose()
    pid = r["native_plan"]["id"]
    assert r["native_plan"]["status"] == "approved" and native_plans.get(pid)["status"] == "approved"
    assert "approved by auto mode" in r["output"] and r["auto_mode"][0]["verdict"] == "allow"
    assert [s for s, _ in later] == [CHAT] and started == []
    later[0][1]()
    assert started[0][0] == CHAT and started[0][1].startswith("[Plan approved")


@pytest.mark.parametrize("answer", ['{"decision": "ask", "reason": "big change"}',
                                    '{"decision": "block", "reason": "not asked for"}'])
def test_plan_auto_ask_or_block_leaves_the_card(env, answer):
    from src import native_plans
    _auto()
    env.answer = answer
    desc, r = _propose()
    assert native_plans.get(r["native_plan"]["id"])["status"] == "pending"
    assert r["auto_mode"][0]["verdict"] in ("ask", "block")


# ── Gate: a coding run's plan ────────────────────────────────────────────

@pytest.fixture
def coding(env, tmp_path, monkeypatch):
    from src import claude_code_approvals as approvals
    from src import claude_code_jobs as jobs
    from src.agent_tools import claude_code_tool as cct
    import src.doc_pdf as dp
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(jobs, "HISTORY_FILE", str(tmp_path / "h.jsonl"))
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_chat_name", lambda sid: "")

    async def no_pdf(*a, **k):
        return None, None
    monkeypatch.setattr(dp, "render_markdown_pdf", no_pdf)

    async def no_notify(*a, **k):
        return None
    monkeypatch.setattr(cct, "_notify_plan", no_notify)

    def run():
        import os
        j = jobs.register(job_id="", chat_session_id=CHAT, owner=OWNER, action="plan",
                          cwd="/tmp/site", model="m", engine="opencode", prompt="plan it")
        os.makedirs(j.run_dir, exist_ok=True)
        j.spec = {"engine": "opencode", "model_label": "m", "action": "plan", "chat_id": CHAT, "owner": OWNER,
                  "cwd": "/tmp/site", "session_id": "ses_abc"}
        stream = cct._Stream("opencode")
        stream.final_text = "1. Add CSS\n2. Add a toggle"
        stream.session_id = "ses_abc"
        return asyncio.run(cct._build_result(j, stream, 0, False))
    return run, approvals


def test_coding_plan_ask_mode_is_unchanged(coding, env):
    run, approvals = coding
    r = run()
    assert "approval" in r and "claudecode-approve-" in r["next_step"] and env.calls == []
    assert approvals.get("ses_abc")["status"] == "pending"


def test_coding_plan_auto_allow_approves_after_the_turn(coding, env, monkeypatch):
    run, approvals = coding
    _auto()
    later = []
    monkeypatch.setattr(auto_mode, "after_turn", lambda sid, fn, **k: later.append((sid, fn)))
    r = run()
    assert "approval" not in r and "Auto mode approved this plan" in r["next_step"]
    assert r["auto_mode"][0]["verdict"] == "allow" and [s for s, _ in later] == [CHAT]
    import routes.claude_code_routes as ccr
    approved = []
    monkeypatch.setattr(ccr, "approve_plan", lambda sid, user, **k: approved.append((sid, user)))
    later[0][1]()
    assert approved == [("ses_abc", OWNER)]


def test_coding_plan_auto_ask_keeps_the_links(coding, env):
    run, approvals = coding
    _auto()
    env.answer = '{"decision": "ask", "reason": "touches many files"}'
    r = run()
    assert "approval" in r and r["auto_mode"][0]["verdict"] == "ask"
    assert approvals.get("ses_abc")["status"] == "pending"


# ── after_turn ───────────────────────────────────────────────────────────

def test_after_turn_waits_for_the_turn_and_skips_a_stopped_one(monkeypatch):
    from src import agent_runs
    state = {"active": True, "status": "running"}
    monkeypatch.setattr(agent_runs, "is_active", lambda sid: state["active"])
    monkeypatch.setattr(agent_runs, "get_status", lambda sid: state["status"])
    ran = []

    async def go(final):
        before = list(ran)
        auto_mode.after_turn("c", lambda: ran.append(final))
        await asyncio.sleep(0.3)
        assert ran == before
        state.update(active=False, status=final)
        await asyncio.sleep(0.8)
    asyncio.run(go("done"))
    assert ran == ["done"]
    state.update(active=True, status="running")
    asyncio.run(go("stopped"))
    assert ran == ["done"]


# ── The agent cannot switch itself to Auto ───────────────────────────────

def test_app_api_and_the_generic_prefs_route_cannot_change_it():
    from src.tool_implementations import _APP_API_BLOCKLIST_PREFIXES
    from routes.prefs_routes import OWN_CARD_KEYS
    assert "/api/auto_mode" in _APP_API_BLOCKLIST_PREFIXES
    assert {"approvals_mode", "auto_mode_model"} <= OWN_CARD_KEYS


def test_routes(env, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routes.auto_mode_routes import setup_auto_mode_routes
    import routes.auto_mode_routes as amr

    class Sess:
        owner = OWNER

    class SM:
        def get_session(self, sid):
            if sid != CHAT:
                raise KeyError(sid)
            return Sess()
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: SM())
    monkeypatch.setattr(amr, "require_user", lambda request: OWNER)
    app = FastAPI()

    @app.middleware("http")
    async def who(request: Request, call_next):
        request.state.current_user = OWNER
        if request.headers.get("x-token"):
            request.state.api_token = True
        return await call_next(request)
    app.include_router(setup_auto_mode_routes())
    c = TestClient(app)
    assert c.get("/api/auto_mode").json()["mode"] == "ask"
    r = c.put("/api/auto_mode", json={"mode": "auto"})
    assert r.status_code == 200 and r.json()["mode"] == "auto" and r.json()["safety_model"]["label"] == "tiny"
    assert c.put("/api/auto_mode", json={"mode": "auto"}, headers={"x-token": "1"}).status_code == 403
    assert c.put("/api/auto_mode", json={"mode": "nope"}).status_code == 400
    r = c.put(f"/api/auto_mode/chat/{CHAT}", json={"mode": "ask"})
    assert r.json() == {"override": "ask", "effective": "ask", "user_mode": "auto"}
    assert c.get("/api/auto_mode/chat/other").status_code == 404
    _decide("mod_action", "mod__lights__set", {"room": "x"})
    assert c.get("/api/auto_mode/log").json()["log"][0]["tool"] == "mod__lights__set"
