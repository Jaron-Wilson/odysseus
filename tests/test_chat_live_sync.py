"""An open chat shows replies saved on the server while it was not attached.

Seen live on 2026-09-28: "the coding agent is done but I don't see anything
else" - an approved run had finished and its report was saved, but the open
chat showed none of it until a reload.
"""
import os

from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_stamp_counts_the_chats_messages(monkeypatch):
    from fastapi import FastAPI
    import routes.session_routes as sr

    class _Sess:
        def __init__(self, owner=None):
            self.history, self.owner = [], owner

    class _SM:
        def __init__(self):
            self.s = {"chat-1": _Sess(), "theirs": _Sess(owner="someone-else")}

        def get_session(self, sid):
            return self.s[sid]
    sm = _SM()
    monkeypatch.setattr(sr, "effective_user", lambda request: "jaron")
    sr.setup_session_routes(sm, {})
    app = FastAPI()
    app.include_router(sr.router)
    c = TestClient(app)
    assert c.get("/api/session/chat-1/stamp").json()["message_count"] == 0
    sm.s["chat-1"].history.append("a reply saved by a background run")
    assert c.get("/api/session/chat-1/stamp").json()["message_count"] == 1
    assert c.get("/api/session/nope/stamp").status_code == 404
    assert c.get("/api/session/theirs/stamp").status_code == 404


def test_the_page_checks_and_rerenders():
    js = open(os.path.join(HERE, "static", "js", "chatLiveSync.js"), encoding="utf-8").read()
    assert "/stamp`" in js and "cm.hasActiveStream(sid)" in js and "select(sid, { keepSidebar: true })" in js
    assert "import './chatLiveSync.js';" in open(os.path.join(HERE, "static", "js", "chat.js")).read()
