"""Every plan waiting for Approve / Deny, in one place.

Asked for on 2026-09-28: "I don't see those requests, they are not showing
up, can we add a tab that shows all pending tasks?" A plan's links lived only
in its chat reply; a brought-back plan's reply had none, and older plans were
left waiting with no way to answer them.
"""
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import claude_code_approvals as approvals

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_endpoint_lists_every_waiting_plan(tmp_path, monkeypatch):
    import routes.claude_code_routes as ccr
    import src.chat_queue as cq
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(ccr, "_auth_disabled", lambda: True)
    monkeypatch.setattr(ccr, "get_current_user", lambda r: "")
    monkeypatch.setattr(cq, "_session_title", lambda sid: "Deploy to Cloudflare")
    for i, engine in enumerate(("opencode", "opencode", "claude")):
        approvals.record_plan(f"ses_{i}", cwd=f"/w/p{i}", plan=f"plan {i}", owner="",
                              model="qwen3.8-27b" if engine == "opencode" else "opus",
                              engine=engine, chat_session_id="chat-1")
    approvals.set_status("ses_1", "denied")
    app = FastAPI()
    app.include_router(ccr.setup_claude_code_routes())
    plans = TestClient(app).get("/api/claude_code/pending").json()["plans"]
    assert sorted(p["id"] for p in plans) == ["ses_0", "ses_2"]           # denied is not waiting
    by = {p["id"]: p for p in plans}
    assert by["ses_2"]["runs_on"] == "Claude Code · opus" and by["ses_0"]["runs_on"].startswith("OpenCode")
    assert by["ses_0"]["chat_name"] == "Deploy to Cloudflare" and by["ses_0"]["has_pdf"] is False


def test_the_panel_offers_the_same_links_as_the_chat():
    js = open(os.path.join(HERE, "static", "js", "bgTasks.js"), encoding="utf-8").read()
    # The chat's own approve/deny handler (run limits, retries) takes these.
    assert 'href="#claudecode-approve-${_esc(p.id)}"' in js and 'href="#claudecode-deny-${_esc(p.id)}"' in js
    # Redrawn only when the list changes, or an Approve in progress is lost.
    assert "if (key === _pendingKey) return;" in js
    assert "_pending.length ? `${_pending.length} waiting for your approval` : ''" in js
