"""Reading and answering plans from the desktop overlay, and the "plan
waiting" notification arriving only once the chat shows the plan.

Asked for on 2026-09-27: the notification said a plan was waiting but the
chat it opened looked empty until the reply landed two minutes later; "add
an open plan pdf for overlay and let me approve or deny it while I'm in a
game"; two messages at once should queue; two plans should sit side by side.
"""
import asyncio
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import claude_code_approvals as approvals
from src.agent_tools import claude_code_tool as cct

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "appr.json"))
    import src.doc_pdf as doc_pdf
    monkeypatch.setattr(doc_pdf, "PDF_DIR", str(tmp_path))
    import routes.overlay_routes as ovr
    monkeypatch.setattr(ovr, "_owner", lambda request: "jaron")
    from src import chat_queue
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "Pastor notes")
    app = FastAPI()
    app.include_router(ovr.setup_overlay_routes())
    return TestClient(app), tmp_path


def test_the_plan_and_its_pages_can_be_read(client):
    c, tmp = client
    import pymupdf as fitz
    doc = fitz.open()
    for n in range(2):
        doc.new_page().insert_text((72, 72), f"Plan page {n + 1}")
    doc.save(str(tmp / "plan-p1.pdf"))
    approvals.record_plan("p1", cwd="/x", plan="# Build it", owner="jaron", engine="opencode",
                          chat_session_id="chat-1")
    d = c.get("/api/overlay/plan/p1").json()
    assert d["pages"] == 2 and d["chat"] == "Pastor notes" and d["plan"] == "# Build it"
    assert d["runs_on"].startswith("OpenCode") and d["pdf_url"].endswith("/pdf?inline=1")
    png = c.get("/api/overlay/plan/p1/page/2.png?w=500")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert c.get("/api/overlay/plan/p1/page/3.png").status_code == 404
    approvals.record_plan("p2", cwd="/x", plan="text only", owner="someone-else")
    assert c.get("/api/overlay/plan/p2").status_code == 404                 # not theirs
    assert c.get("/api/overlay/plan/..%2Fx").status_code in (400, 404, 405)


def test_no_pdf_means_the_text(client):
    c, _ = client
    approvals.record_plan("p3", cwd="/x", plan="Just words", owner="jaron")
    d = c.get("/api/overlay/plan/p3").json()
    assert d["pages"] == 0 and d["pdf_url"] == "" and d["plan"] == "Just words"


def test_the_notification_waits_for_the_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "appr.json"))
    from src import agent_runs, chat_queue
    active = {"n": 3}

    def is_active(sid):
        active["n"] -= 1
        return active["n"] > 0
    sent = []

    async def fake_send(sid, notify, heading, body, **kw):
        sent.append(active["n"])
    monkeypatch.setattr(agent_runs, "is_active", is_active)
    monkeypatch.setattr(agent_runs, "has_watchers", lambda sid: False)
    monkeypatch.setattr(chat_queue, "send_notification", fake_send)
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "x")
    slept = []

    async def fast_sleep(s):
        slept.append(s)
    monkeypatch.setattr(cct.asyncio, "sleep", fast_sleep)
    approvals.record_plan("p9", cwd="/x", plan="p", chat_session_id="c9")
    asyncio.run(cct._notify_plan("p9", "c9", "OpenCode", "p"))
    assert sent == [0] and len(slept) == 2                    # waited out the turn, then sent


def test_overlay_queues_messages_and_reads_plans_side_by_side():
    ov = open(os.path.join(HERE, "tools", "music_overlay", "music_overlay.py"), encoding="utf-8").read()
    for needle in ("def _enqueue(self, ev):", "def _step_msg(self, delta):", "class PlanReader:",
                   "self.ids = list(plan_ids)[:3]", 'button(f"Read {len(plans)} plans"',
                   "api/overlay/plan/{pid}/page/{n}.png", "room_above = top - 16", "a = self.alert = tk.Toplevel(self.root)"):
        assert needle in ov, needle
