"""Tidy: write a full chat's notes, then prune what they cover (src/chat_tidy.py).

Asked for on 2026-09-29: "when i get the note that the conversation is over
the token count, could i enable a sub agent to go through the chat, make
necessary notes, then purge or prune the chat?"
"""
import asyncio
import json
import os
import uuid

import pytest

from core.database import init_db, SessionLocal, ChatMessage as DbChatMessage
from core.models import ChatMessage
from core.session_manager import SessionManager
from src import chat_memory, chat_prefs, chat_tidy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    init_db()
    sm = SessionManager()
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "_session_manager", sm)
    monkeypatch.setattr(chat_memory, "MEMORY_FILE", str(tmp_path / "chat_memory.json"), raising=False)
    if hasattr(chat_memory, "DATA_DIR"):
        monkeypatch.setattr(chat_memory, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(chat_prefs, "PREFS_FILE", str(tmp_path / "chat_prefs.json"))
    import src.endpoint_resolver as er
    monkeypatch.setattr(er, "resolve_endpoint", lambda kind, owner=None: (None, None, None))
    calls = []
    reply = {"text": json.dumps({"notes": [
        "The user's Worker is at https://odysseus-mail.example.workers.dev and forwards to Gmail.",
        "Decided: submissions@clevernode.org starts a task only for alerts@clevernode.org.",
        "API key is sk-abcdefghijklmnopqrstuvwx for the gateway.",
    ]})}
    import src.llm_core as llm

    async def fake_call(url, model, messages, **kw):
        calls.append((url, model, messages))
        return reply["text"]
    monkeypatch.setattr(llm, "llm_call_async", fake_call)
    sid = str(uuid.uuid4())
    sm.create_session(sid, "Long chat", "http://localhost:8000/v1", "qwen3:8b", owner="jaron")
    for i in range(12):
        sm.add_message(sid, ChatMessage("user" if i % 2 == 0 else "assistant", f"message {i} " + "x" * 50))
    yield sm, sid, calls, reply


def _db_meta(sid):
    db = SessionLocal()
    try:
        rows = db.query(DbChatMessage).filter(DbChatMessage.session_id == sid).order_by(DbChatMessage.timestamp).all()
        return [json.loads(r.meta_data) if r.meta_data else {} for r in rows]
    finally:
        db.close()


def test_notes_first_then_older_messages_leave_context(env):
    sm, sid, calls, _ = env
    sess = sm.get_session(sid)
    r = asyncio.run(chat_tidy.tidy(sess, owner="jaron"))
    assert r["tidied"] and r["pruned"] == 12 - chat_tidy.KEEP_RECENT
    # The key the model copied out of the chat is not kept.
    assert len(r["notes"]) == 2 and not any("sk-" in n for n in r["notes"])
    items = [i["text"] for i in chat_memory.get(sid)["items"]]
    assert any("odysseus-mail.example.workers.dev" in t for t in items)
    # The model read the older messages, not the newest.
    sent = calls[0][2][1]["content"]
    assert "message 0 " in sent and "message 11 " not in sent
    # Pruned, not deleted: still in the history, out of context, and saved so.
    assert len(sess.history) == 13                                  # 12 + the notice
    ctx = [m["content"] for m in sess.get_context_messages()]
    assert not any(c.startswith("message 0 ") for c in ctx)
    assert any(c.startswith("message 11 ") for c in ctx)
    meta = _db_meta(sid)
    assert sum(1 for m in meta if m.get("excluded")) == 12 - chat_tidy.KEEP_RECENT
    assert "Chat tidied" in sess.history[-1].content
    # Tidying again finds nothing left to do.
    assert not asyncio.run(chat_tidy.tidy(sess, owner="jaron"))["tidied"]


def test_no_notes_means_nothing_is_pruned(env):
    sm, sid, calls, reply = env
    reply["text"] = "Sorry, I can't help with that."
    sess = sm.get_session(sid)
    r = asyncio.run(chat_tidy.tidy(sess, owner="jaron"))
    assert not r["tidied"] and "nothing was pruned" in r["reason"]
    assert not any(m.get("excluded") for m in _db_meta(sid))
    assert len(sess.get_context_messages()) == 12


def test_full_notes_means_nothing_is_pruned(env, monkeypatch):
    sm, sid, calls, _ = env
    monkeypatch.setattr(chat_memory, "MAX_ITEMS", 0)
    sess = sm.get_session(sid)
    r = asyncio.run(chat_tidy.tidy(sess, owner="jaron"))
    assert not r["tidied"]
    assert not any(m.get("excluded") for m in _db_meta(sid))


def test_short_chats_are_left_alone(env):
    sm, _, calls, _ = env
    sid = str(uuid.uuid4())
    sm.create_session(sid, "Short", "http://localhost:8000/v1", "qwen3:8b", owner="jaron")
    for i in range(5):
        sm.add_message(sid, ChatMessage("user", f"m{i}"))
    assert not asyncio.run(chat_tidy.tidy(sm.get_session(sid)))["tidied"]
    assert not calls


def test_parse_notes_handles_thinking_and_junk():
    assert chat_tidy.parse_notes('<think>{"notes":["no"]}</think> {"notes": ["A real note here.", "x"]}') == ["A real note here."]
    assert chat_tidy.parse_notes("not json") == []
    assert chat_tidy.parse_notes('{"notes": "a string"}') == []


def test_automatic_tidy_only_when_switched_on_and_full(env, monkeypatch):
    sm, sid, calls, _ = env
    sess = sm.get_session(sid)
    started = []
    monkeypatch.setattr(chat_tidy.asyncio, "create_task", lambda coro: started.append(coro) or coro.close())
    full = {"input_tokens": 9000, "context_length": 10000}
    assert not chat_tidy.schedule_if_full(sess, full)                   # off by default
    chat_prefs.set_pref(sid, "tidy", True)
    assert not chat_tidy.schedule_if_full(sess, {"input_tokens": 1000, "context_length": 10000})
    assert chat_tidy.schedule_if_full(sess, full) and len(started) == 1


def test_popup_and_route_are_wired():
    js = open(os.path.join(ROOT, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert "window.chatTidy.attach(popup)" in js
    chat = open(os.path.join(ROOT, "static", "js", "chat.js"), encoding="utf-8").read()
    assert "import './chatTidy.js';" in chat
    routes = open(os.path.join(ROOT, "routes", "history_routes.py"), encoding="utf-8").read()
    assert '@router.post("/api/session/{session_id}/tidy")' in routes
    helpers = open(os.path.join(ROOT, "routes", "chat_helpers.py"), encoding="utf-8").read()
    assert "chat_tidy.schedule_if_full(sess, last_metrics" in helpers
