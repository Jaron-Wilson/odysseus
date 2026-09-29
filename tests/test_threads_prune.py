"""Prune, references and side threads inside a chat.

Asked for on 2026-09-28: "I should be able to prune the messages, or like
basically fork it but keep it in same chat or like a thread ... a side chat
and then I can press merge to go back to main chat, or use as reference in
main chat", and "if I prune the chat, I can scroll up and still see it, but
then I can press use this for reference on the latest message".
"""
import json
import os
import uuid

import pytest

from core.database import SessionLocal, ChatMessage as DbChatMessage, init_db
from core.models import ChatMessage, in_context, set_session_manager_instance, get_session_manager_instance
from core.session_manager import SessionManager
import routes.history_routes as history_routes
import routes.thread_routes as thread_routes
from src import chat_threads

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def env(monkeypatch):
    init_db()
    sm = SessionManager()
    prev = get_session_manager_instance()
    set_session_manager_instance(sm)            # the app does this at startup
    sid = "p-" + uuid.uuid4().hex[:8]
    s = sm.create_session(sid, "Deploy Erik", "http://x/v1", "qwen3.8-27b")
    for role, text in [("user", "old question"), ("assistant", "old answer"),
                       ("user", "how do we host Erik?"), ("assistant", "One worker, stub API."),
                       ("user", "latest question")]:
        s.add_message(ChatMessage(role, text))
    for mod in (history_routes, thread_routes):
        monkeypatch.setattr(mod, "_verify_session_owner", lambda *a, **k: None)
    routers = [history_routes.setup_history_routes(sm), thread_routes.setup_thread_routes(sm)]
    yield sm, s, _Client(routers)
    set_session_manager_instance(prev)


class _Resp:
    def __init__(self, status_code, data):
        self.status_code, self._data = status_code, data

    def json(self):
        return self._data


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {"content-type": "application/json"}

    async def json(self):
        return self._body


class _Client:
    """Calls the route handlers in this thread. The test database is an
    in-memory SQLite, which a TestClient's worker thread cannot see."""

    def __init__(self, routers):
        self.routes = [r for router in routers for r in router.routes]

    def _call(self, method, url, body):
        import asyncio
        import re
        from fastapi import HTTPException
        for r in self.routes:
            if method not in getattr(r, "methods", set()):
                continue
            pattern = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", r.path) + "$"
            m = re.match(pattern, url)
            if m:
                try:
                    return _Resp(200, asyncio.run(r.endpoint(_Req(body), **m.groupdict())))
                except HTTPException as e:
                    return _Resp(e.status_code, {"detail": e.detail})
        raise AssertionError(f"no route for {method} {url}")

    def post(self, url, json=None):
        return self._call("POST", url, json or {})

    def get(self, url):
        return self._call("GET", url, None)


def _ids(s):
    return [m.metadata["_db_id"] for m in s.history]


def _row_meta(msg_id):
    db = SessionLocal()
    try:
        row = db.query(DbChatMessage).filter(DbChatMessage.id == msg_id).first()
        return json.loads(row.meta_data or "{}")
    finally:
        db.close()


def test_pruned_messages_leave_the_context_but_stay_in_the_chat(env):
    sm, s, c = env
    ids = _ids(s)
    r = c.post(f"/api/session/{s.id}/prune", json={"msg_ids": [ids[0]]}).json()
    assert r["changed"] == [ids[0]]
    assert [m["content"] for m in s.get_context_messages()][0] == "old answer"
    assert len(s.history) == 5                                     # still in the chat
    assert _row_meta(ids[0])["excluded"] is True                   # survives a reload
    c.post(f"/api/session/{s.id}/prune", json={"msg_ids": [ids[0]], "excluded": False})
    assert len(s.get_context_messages()) == 5 and "excluded" not in _row_meta(ids[0])


def test_prune_everything_above(env):
    sm, s, c = env
    ids = _ids(s)
    r = c.post(f"/api/session/{s.id}/prune", json={"above_msg_id": ids[2]}).json()
    assert r["changed"] == ids[:2]
    assert [m["content"] for m in s.get_context_messages()] == [
        "how do we host Erik?", "One worker, stub API.", "latest question"]
    assert c.post(f"/api/session/{s.id}/prune", json={"above_msg_id": "nope"}).status_code == 404


def test_compaction_keeps_pruned_messages_and_cuts_by_what_the_model_saw(env):
    from src.context_compactor import _update_session_history
    sm, s, c = env
    ids = _ids(s)
    c.post(f"/api/session/{s.id}/prune", json={"msg_ids": [ids[1]]})
    # The model saw 4 messages; summarise the first 2 of those (ids 0 and 2).
    _update_session_history(s, 2, "they talked about hosting", system_msg_count=0)
    contents = [m.content for m in s.history]
    assert "old answer" in contents                                # the pruned one is kept, still visible
    assert contents[-2:] == ["One worker, stub API.", "latest question"]
    assert any(m.metadata.get("compacted") for m in s.history)
    assert not in_context(next(m for m in s.history if m.content == "old answer"))


def test_references_are_read_for_that_turn_only(env):
    sm, s, c = env
    ids = _ids(s)
    c.post(f"/api/session/{s.id}/prune", json={"above_msg_id": ids[4]})
    turn = s.get_context_messages()
    assert [m["content"] for m in turn] == ["latest question"]
    labels = chat_threads.apply_references(
        s, turn, json.dumps([{"kind": "message", "id": ids[3]}]), session_manager=sm)
    assert labels[0]["label"].startswith("Assistant: One worker")
    assert "One worker, stub API." in turn[-1]["content"] and chat_threads.REFERENCE_HEADER in turn[-1]["content"]
    assert s.history[-1].content == "latest question"               # the saved message is unchanged
    assert s.history[-1].metadata["references"] == labels
    assert _row_meta(ids[4])["references"] == labels
    assert chat_threads.apply_references(s, turn, "", session_manager=sm) == []


def test_side_thread_start_list_merge(env):
    sm, s, c = env
    ids = _ids(s)
    t = c.post(f"/api/session/{s.id}/threads", json={"anchor_msg_id": ids[3]}).json()
    thread = sm.get_session(t["id"])
    assert thread.parent_session_id == s.id and thread.thread_anchor_id == ids[3]
    assert [m.content for m in thread.history] == ["how do we host Erik?", "One worker, stub API."]
    assert all(m.metadata.get("thread_seed") for m in thread.history)
    assert len(thread.get_context_messages()) == 2                  # small: not the whole chat
    listed = c.get(f"/api/session/{s.id}/threads").json()["threads"]
    assert [(x["id"], x["message_count"], x["merged"]) for x in listed] == [(t["id"], 0, False)]
    assert c.post(f"/api/session/{t['id']}/merge").status_code == 409  # nothing said yet

    thread.add_message(ChatMessage("user", "what about the map key?"))
    thread.add_message(ChatMessage("assistant", "<think>check</think>It is keyless."))
    r = c.post(f"/api/session/{t['id']}/merge").json()
    assert r["parent_session_id"] == s.id and r["merged_count"] == 2
    merged = s.history[-1]
    assert merged.metadata["source"] == "thread_merge" and merged.content.startswith("[Side thread merged ·")
    assert "User: what about the map key?" in merged.content and "Assistant: It is keyless." in merged.content
    assert "One worker, stub API." not in merged.content and "<think>" not in merged.content   # seed not repeated
    assert c.get(f"/api/session/{s.id}/threads").json()["threads"][0]["merged"] is True

    text, labels = chat_threads.resolve_references(s, [{"kind": "thread", "id": t["id"]}], sm)
    assert "It is keyless." in text and labels[0]["kind"] == "thread"
    # No threads inside threads.
    tid = thread.history[-1].metadata["_db_id"]
    assert c.post(f"/api/session/{t['id']}/threads", json={"anchor_msg_id": tid}).status_code == 400


def test_the_page_wires_it_in():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    chat = read("static", "js", "chat.js")
    assert "import './chatThreads.js';" in chat
    assert "window.chatThreads.takeReferences(streamSessionId)" in chat and "fd.append('references', _refsField)" in chat
    renderer = read("static", "js", "chatRenderer.js")
    assert renderer.count("...(window.chatThreads ? window.chatThreads.actions(msgElement) : []),") == 2
    assert renderer.count("if (metadata?.excluded) wrap.classList.add('msg-excluded');") == 2
    sessions = read("static", "js", "sessions.js")
    assert "!s.archived && !s.parent_session_id && s.folder !== 'Assistant'" in sessions
    routes = read("routes", "chat_routes.py")
    assert "chat_threads.apply_references(sess, ctx.messages, form_data.get(\"references\")" in routes


def test_session_head_reports_the_newest_shown_message(env, monkeypatch):
    sm, s, c = env
    from src import agent_runs
    ids = _ids(s)
    s.add_message(ChatMessage("system", "[Conversation summary] x", metadata={"compacted": True, "hidden": True}))
    monkeypatch.setattr(agent_runs, "is_active", lambda sid: False)
    d = c.get(f"/api/session/{s.id}/head").json()
    assert d["last"]["id"] == ids[-1] and d["last"]["role"] == "user" and d["running"] is False
    monkeypatch.setattr(agent_runs, "is_active", lambda sid: sid == s.id)
    assert c.get(f"/api/session/{s.id}/head").json()["running"] is True
