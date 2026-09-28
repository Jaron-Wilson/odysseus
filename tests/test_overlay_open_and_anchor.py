"""The desktop overlay: the player stays put, and Open brings up the tab.

Asked for on 2026-09-27: "the music popout keeps moving during each ask or
question or completion ... keep it there and just expand upwards", and "open
should pull the tab up, not make a new tab, if i have an odysseus with same
chat lets open that one".
"""
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _client(monkeypatch):
    import routes.overlay_routes as ovr
    monkeypatch.setattr(ovr, "_owner", lambda request: "jaron")
    ovr._OPEN_REQUESTS.clear()
    app = FastAPI()
    app.include_router(ovr.setup_overlay_routes())
    return TestClient(app), ovr


def test_open_reaches_only_pages_on_the_same_machine(monkeypatch):
    c, ovr = _client(monkeypatch)
    pc = {"X-Forwarded-For": "100.102.86.125"}
    phone = {"X-Forwarded-For": "100.64.0.9"}
    d = c.post("/api/overlay/open", json={"session_id": "chat-1"}, headers=pc).json()
    assert d["marker"] == f"[{d['nonce']}]"
    assert c.get("/api/overlay/page/open-request", headers=phone).json() == {}   # not the phone
    req = c.get("/api/overlay/page/open-request", headers=pc).json()
    assert req == {"nonce": d["nonce"], "session_id": "chat-1"}
    assert c.get(f"/api/overlay/open/{d['nonce']}", headers=pc).json()["acks"] == []
    c.post(f"/api/overlay/page/open-request/{d['nonce']}/ack", json={"visible": False}, headers=pc)
    assert c.get(f"/api/overlay/open/{d['nonce']}", headers=pc).json()["acks"][0]["visible"] is False
    ovr._OPEN_REQUESTS["jaron"]["ts"] -= 60                                     # stale
    assert c.get("/api/overlay/page/open-request", headers=pc).json() == {}
    assert c.post("/api/overlay/open", json={}, headers=pc).status_code == 400


def test_page_and_overlay_are_wired():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    assert "import './openRequests.js';" in read("static", "js", "chat.js")
    js = read("static", "js", "openRequests.js")
    assert "new Worker('/static/js/openRequestsWorker.js')" in js and "document.title = `[${nonce}] ${title}`" in js
    assert "/api/overlay/page/open-request" in read("static", "js", "openRequestsWorker.js")
    ov = read("tools", "music_overlay", "music_overlay.py")
    assert "self._open_chat(self.current[\"session_id\"])" in ov and "def focus_marked_tab(marker):" in ov
    assert "UIA_SelectionItemPatternId" in ov


def test_the_player_is_placed_from_its_anchor_only():
    ov = open(os.path.join(HERE, "tools", "music_overlay", "music_overlay.py"), encoding="utf-8").read()
    assert "winfo_x()" not in ov and "winfo_y()" not in ov        # no read-back positions
    assert 'self.root.geometry(f"{W}x{H}+{self.ax}+{self.ay}")' in ov
    assert "self.ay - MSG_H if self.msg_above else self.ay" in ov
    assert '"launch_handler"' in open(os.path.join(HERE, "static", "manifest.json")).read()
