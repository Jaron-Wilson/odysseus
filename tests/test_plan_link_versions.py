"""An older plan's Approve or Deny cannot answer a newer plan in the same chat.

Seen 2026-09-29: "i denied a chat and now its saying its approved". A chat
carries on one coder session, so each new plan reused the session id of the
last, and the links (#claudecode-approve-<session id>) of every plan in the
chat answered the newest one; the page also settled all of them at once.
"""
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import claude_code_approvals as approvals

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    import routes.claude_code_routes as ccr
    monkeypatch.setattr(ccr, "get_current_user", lambda request: "jaron")
    app = FastAPI()
    app.include_router(ccr.setup_claude_code_routes())
    return TestClient(app), tmp_path


def test_each_plan_gets_its_own_version(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    v1 = approvals.record_plan("ses_x", cwd=str(tmp_path), plan="first")
    import time
    time.sleep(0.005)
    v2 = approvals.record_plan("ses_x", cwd=str(tmp_path), plan="second")
    assert v1 != v2
    assert approvals.stale("ses_x", v1) and not approvals.stale("ses_x", v2)
    assert not approvals.stale("ses_x", "")                  # links from before versions
    assert approvals.split_ref(f"ses_x~{v2}") == ("ses_x", v2)


def test_an_older_plans_deny_does_not_answer_the_newer_one(client):
    c, tmp = client
    old = approvals.record_plan("ses_y", cwd=str(tmp), plan="old")
    import time
    time.sleep(0.005)
    new = approvals.record_plan("ses_y", cwd=str(tmp), plan="new")
    r = c.post(f"/api/claude_code/deny/ses_y~{old}")
    assert r.status_code == 409 and "earlier plan" in r.json()["detail"]
    assert approvals.get("ses_y")["status"] == "pending"
    r = c.post(f"/api/claude_code/deny/ses_y~{new}")
    assert r.status_code == 200 and approvals.get("ses_y")["status"] == "denied"


def test_links_carry_the_version_and_the_page_settles_only_them():
    src = open(os.path.join(ROOT, "src", "agent_tools", "claude_code_tool.py"), encoding="utf-8").read()
    assert "#claudecode-approve-{plan_ref}" in src and "#claudecode-deny-{plan_ref}" in src
    assert "#claudecode-approve-{session_id}" not in src
    js = open(os.path.join(ROOT, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert "Replaced by a newer plan" in js


def test_tidy_is_in_every_replys_menu():
    js = open(os.path.join(ROOT, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert js.count("id: 'tidy'") == 2
    tidy = open(os.path.join(ROOT, "static", "js", "chatTidy.js"), encoding="utf-8").read()
    assert "window.chatTidy = { attach, tidyNow, run }" in tidy
