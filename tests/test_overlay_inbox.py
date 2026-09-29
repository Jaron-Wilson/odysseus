"""The desktop overlay: a frameless always-on-top player on the PC that also
shows what the AI said (over a game, where Windows holds its own
notifications back) and takes a reply."""
import asyncio
import os
import types

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import chat_queue, overlay_inbox

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    overlay_inbox._EVENTS.clear()
    monkeypatch.setattr(overlay_inbox, "_session_owner_and_name", lambda sid: ("jaron", "Costa Rica"))
    monkeypatch.setattr(chat_queue, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(chat_queue, "QUEUE_FILE", str(tmp_path / "q.json"))


def test_record_dedupe_and_since():
    a = overlay_inbox.record("c1", "question", "Question", "Which PR?", options=["PR #3", "PR #4"])
    b = overlay_inbox.record("c1", "question", "Question", "Which PR?")
    assert a["id"] == b["id"] and a["options"] == ["PR #3", "PR #4"]
    assert [e["body"] for e in overlay_inbox.since("jaron", 0)] == ["Which PR?"]
    assert overlay_inbox.since("someone-else", 0) == []
    assert overlay_inbox.since("jaron", a["ts"]) == []


def test_a_reply_that_finishes_unwatched_is_recorded(monkeypatch):
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "has_watchers", lambda sid: False)
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "Costa Rica")
    monkeypatch.setattr(chat_queue, "_last_reply", lambda sid: "All three cuts are rendered.")
    chat_queue.on_run_finished("c1", "done")
    ev = overlay_inbox.since("jaron", 0)[-1]
    assert ev["kind"] == "done" and ev["body"] == "All three cuts are rendered."
    # Sending what is queued is not a finished reply: nothing new recorded.
    chat_queue.schedule_drain("c1")
    assert len(overlay_inbox.since("jaron", 0)) == 1


def _client(state):
    from routes import overlay_routes
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        for k, v in state.items():
            setattr(request.state, k, v)
        return await call_next(request)
    app.include_router(overlay_routes.setup_overlay_routes())
    return TestClient(app)


def test_token_scope_and_reply(monkeypatch):
    overlay_inbox.record("c1", "done", "Reply ready", "Done.")
    ok = {"api_token": True, "api_token_owner": "jaron", "api_token_scopes": ["overlay"], "current_user": "api"}
    c = _client(ok)
    assert [e["body"] for e in c.get("/api/overlay/inbox").json()["events"]] == ["Done."]
    bad = dict(ok, api_token_scopes=["chat"])
    assert _client(bad).get("/api/overlay/inbox").status_code == 403

    sess = types.SimpleNamespace(owner="jaron")
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: types.SimpleNamespace(get_session=lambda sid: sess))
    drained = []
    monkeypatch.setattr(chat_queue, "schedule_drain", lambda sid, status="done": drained.append(sid))
    r = c.post("/api/overlay/reply", json={"session_id": "c1", "text": "PR #3"})
    assert r.status_code == 200 and r.json()["ok"]
    assert [i["text"] for i in chat_queue.get("c1")["items"]] == ["PR #3"] and drained == ["c1"]
    sess.owner = "someone-else"
    assert c.post("/api/overlay/reply", json={"session_id": "c1", "text": "x"}).status_code == 404


def test_only_the_overlay_task_can_be_started():
    from src import machines
    r = asyncio.run(machines.run_user_task({"os": "windows"}, "jaron", "OdysseusDesktopMCP"))
    assert not r["ok"] and "not a task" in r["error"]
    assert "MusicOverlay" in machines.USER_TASKS
    assert not any(t.startswith("Odysseus") for t in machines.USER_TASKS)   # not the services pattern


def test_wiring():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    app = read("tools", "music_overlay", "music_overlay.py")
    assert "overrideredirect(True)" in app and "api/overlay/reply" in app and "_poll_inbox" in app
    assert "winsound.MessageBeep" in app
    assert "/api/media/overlay" in read("static", "js", "musicBar.js")
    assert '@router.post("/overlay")' in read("routes", "media_routes.py")


def test_the_overlay_shows_and_controls_the_phones_song(monkeypatch):
    # Reported: "popup says nothing playing when it's from my phone".
    from routes import overlay_routes
    from src import device_routing, devices
    overlay_routes._REMOTE_CACHE.update(at=0.0, item=None)
    phone = {"name": "pixel-8a", "endpoint": "http://pixel-8a.example:8778", "commands": ["now_playing", "media_control"]}
    monkeypatch.setattr(device_routing, "phone_devices",
                        lambda: [{"server_id": "device:pixel-8a", "name": "pixel-8a"}])
    monkeypatch.setattr(devices, "get", lambda n: phone if n == "pixel-8a" else None)
    sent = []

    async def send(dev, cmd, params=None):
        sent.append((cmd, params))
        if cmd == "now_playing":
            return {"ok": True, "result": {"ok": True, "playing": True, "title": "Happier",
                                           "artist": "Marshmello & Bastille", "art_jpeg_b64": "AAAA"}}
        return {"ok": True, "result": {"ok": True}}
    monkeypatch.setattr(devices, "send_command", send)
    ok = {"api_token": True, "api_token_owner": "jaron", "api_token_scopes": ["overlay"], "current_user": "api"}
    c = _client(ok)
    item = c.get("/api/overlay/remote_media").json()["item"]
    assert item["title"] == "Happier" and item["server_id"] == "device:pixel-8a" and item["art_jpeg_b64"] == "AAAA"
    c.get("/api/overlay/remote_media")
    assert [cmd for cmd, _ in sent].count("now_playing") == 1         # cached between polls
    assert c.post("/api/overlay/remote_media/control",
                  json={"server_id": "device:pixel-8a", "action": "next"}).json()["ok"]
    assert ("media_control", {"action": "next"}) in sent
    assert c.post("/api/overlay/remote_media/control",
                  json={"server_id": "device:pixel-8a", "action": "format"}).status_code == 400
    assert c.post("/api/overlay/remote_media/control",
                  json={"server_id": "19d772b0", "action": "next"}).status_code == 400
    assert _client(dict(ok, api_token_scopes=["chat"])).get("/api/overlay/remote_media").status_code == 403
    app = open(os.path.join(HERE, "tools", "music_overlay", "music_overlay.py"), encoding="utf-8").read()
    assert 'self._api("GET", "api/overlay/remote_media")' in app
    assert '"api/overlay/remote_media/control"' in app and "if self.remote else info[\"artist\"]" in app
