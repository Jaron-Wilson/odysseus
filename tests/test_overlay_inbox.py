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
