"""Plans on OpenCode, a week to answer them, and a notification (and the
desktop overlay) when one is waiting.

Asked for on 2026-09-27: "when making plans it should use opencode not
claude code. also expiring is too short when i dont get a notification to
approve or deny". Seen live: the agent asked for Claude and opus on its own
for a plan, and a plan approved the next day had expired.
"""
import asyncio
import json
import os
import stat
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.models import ChatMessage
from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

REAL_NOTIFY = cct._notify_plan
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Sess:
    def __init__(self, msgs):
        self.history = [ChatMessage("user", m) for m in msgs]


class _SM:
    def __init__(self, sessions):
        self.sessions = sessions

    def get_session(self, sid):
        return self.sessions[sid]


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv = tmp_path / "argv.txt"
    for name, line in (("opencode", "print(json.dumps({'type':'text','sessionID':'ses_x1','part':{'text':'Local plan.'}}))"),
                       ("claude", "sys.stdin.read(); print(json.dumps({'type':'result','result':'Claude plan.','is_error':False}))")):
        exe = bindir / name
        exe.write_text(f"#!{sys.executable}\nimport json, sys\n"
                       f"open({str(argv)!r}, 'w').write(json.dumps([{name!r}] + sys.argv[1:]))\n{line}\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "appr.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    from src import claude_code_agents as agents_reg
    monkeypatch.setattr(agents_reg, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agents_reg, "AGENTS_FILE", str(tmp_path / "agents.json"))
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "opencode")

    async def no_pdf(*a, **k):
        return None, "skipped"
    import src.doc_pdf as doc_pdf
    monkeypatch.setattr(doc_pdf, "render_markdown_pdf", no_pdf)
    sent = []

    async def fake_notify(*a, **k):
        sent.append((a, k))
    monkeypatch.setattr(cct, "_notify_plan", fake_notify)
    sm = _SM({"chat-plain": _Sess(["build the pastor notes backend"]),
              "chat-claude": _Sess(["plan it with claude code please"])})
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    jobs._JOBS.clear()
    return argv, sent


def _plan(chat, **extra):
    return asyncio.run(cct.ClaudeCodeTool().execute(json.dumps(
        {"action": "plan", "prompt": "plan it", "cwd": os.getcwd(), **extra}), {"session_id": chat}))


def test_a_plan_asked_for_on_claude_runs_on_opencode(env):
    argv, sent = env
    out = _plan("chat-plain", engine="claude", model="opus",
                session_id="94b83923-d37f-42a8-9c52-8a6ba9232724")
    cmd = json.loads(argv.read_text())
    assert cmd[0] == "opencode" and "opus" not in cmd and "--session" not in cmd
    assert "runs on OpenCode" in out["approval"]["approve"] and "OpenCode" in out["engine_note"]
    assert approvals.get(out["session_id"])["engine"] == "opencode"
    assert sent, "no plan notification was started"


def test_claude_when_the_user_names_it(env):
    argv, _ = env
    out = _plan("chat-claude", engine="claude")
    assert json.loads(argv.read_text())[0] == "claude"
    assert "runs on Claude Code" in out["approval"]["approve"]
    assert cct._user_named_claude("chat-claude") and not cct._user_named_claude("chat-plain")
    # The tool's own name is not the user asking for Claude.
    import src.ai_interaction as ai
    ai.get_session_manager().sessions["chat-tool"] = _Sess(["use claude_code for this"])
    assert not cct._user_named_claude("chat-tool")


def test_plans_last_a_week_and_are_listed(env):
    approvals.record_plan("p-old", cwd="/x", plan="old", owner="jaron")
    approvals.record_plan("p-new", cwd="/x", plan="# Step one\nthen more", owner="jaron",
                          engine="opencode", chat_session_id="chat-plain")
    data = approvals._load()
    data["p-old"]["created"] = time.time() - 3 * 24 * 3600      # three days: still open
    approvals._save(data)
    ids = [p["session_id"] for p in approvals.pending_for("jaron")]
    assert ids == ["p-new", "p-old"]
    assert approvals.pending_for("someone-else") == []
    data["p-old"]["created"] = time.time() - 8 * 24 * 3600
    approvals._save(data)
    assert [p["session_id"] for p in approvals.pending_for("jaron")] == ["p-new"]


def test_the_notification_waits_and_skips_answered_plans(env, monkeypatch):
    real = REAL_NOTIFY                               # the fixture replaced it
    from src import agent_runs, chat_queue
    sent = []

    async def fake_send(sid, notify, heading, body, **kw):
        sent.append((sid, heading, body, kw.get("kind")))
    monkeypatch.setattr(chat_queue, "send_notification", fake_send)
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "Pastor notes")
    monkeypatch.setattr(agent_runs, "has_watchers", lambda sid: False)
    approvals.record_plan("p1", cwd="/x", plan="## Build the API\nmore", chat_session_id="c1")
    asyncio.run(real("p1", "c1", "OpenCode · local default", "## Build the API\nmore"))
    assert sent == [("c1", "Plan ready: Pastor notes",
                     "Approve or deny (runs on OpenCode · local default). Build the API", "plan")]
    approvals.set_status("p1", "approved")
    asyncio.run(real("p1", "c1", "x", "y"))
    assert len(sent) == 1                               # answered: nothing more


def test_overlay_lists_and_answers_plans(env, monkeypatch, tmp_path):
    started = []
    import src.screen_control_resume as scr
    monkeypatch.setattr(scr, "start_turn", lambda sid, prompt, **kw: started.append(sid) or True)
    import routes.overlay_routes as ovr
    monkeypatch.setattr(ovr, "_owner", lambda request: "jaron")
    from src import chat_queue
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "Pastor notes")
    app = FastAPI()
    app.include_router(ovr.setup_overlay_routes())
    c = TestClient(app)
    approvals.record_plan("plan-a", cwd=str(tmp_path), plan="Do the thing", owner="jaron",
                          engine="opencode", chat_session_id="chat-9")
    plans = c.get("/api/overlay/inbox").json()["plans"]
    assert plans[0]["id"] == "plan-a" and plans[0]["chat"] == "Pastor notes"
    assert plans[0]["runs_on"].startswith("OpenCode")
    r = c.post("/api/overlay/plan/plan-a/approve").json()
    assert r["status"] == "approved" and r["resuming"] and started == ["chat-9"]
    assert c.get("/api/overlay/inbox").json()["plans"] == []
    assert c.post("/api/overlay/plan/plan-a/deny").status_code == 409
    assert c.post("/api/overlay/plan/../approve").status_code in (400, 404)


def test_overlay_shows_plans_above_the_player():
    ov = open(os.path.join(HERE, "tools", "music_overlay", "music_overlay.py"), encoding="utf-8").read()
    assert "self.msg_above = self.ay >= MSG_H" in ov and "def _sync_plans(self, plans):" in ov
    assert 'api/overlay/plan/{ev[\'plan_id\']}/{verb}' in ov and "self.dismissed_plan" in ov
    loop = open(os.path.join(HERE, "src", "agent_loop.py"), encoding="utf-8").read()
    assert "PLANS always run on OpenCode" in loop
