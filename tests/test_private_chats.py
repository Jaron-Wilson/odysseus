"""Private chats (src/private_chats.py): never sent to a model, never read
from another chat.

The Phone & Messages mod mirrors his business SIP chat into "Text:
jarondynamics" and makes it private. Company and legal content there must
not reach a model or another chat: search_chats and the search box, the
agent's list_sessions and send_to_session, a fork, a new chat's "Recent
chats", the folder sort, tidy, memory suggestions and every turn leave it
alone. The chat-level switch is the rule; the name is the fallback for a
chat made before it.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src import chat_prefs, private_chats


@pytest.fixture(autouse=True)
def prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_prefs, "PREFS_FILE", str(tmp_path / "chat_prefs.json"))
    yield


class Sess:
    def __init__(self, name, owner="alice", parent=None):
        self.name, self.owner, self.parent_session_id = name, owner, parent
        self.model, self.archived, self.message_count = "m", False, 2
        self.endpoint_url, self.headers = "http://x", {}

    def get_context_messages(self):
        return [{"role": "user", "content": "NDA terms for Acme"}]


class Mgr:
    def __init__(self, sessions):
        self.sessions = sessions

    def get_session(self, sid):
        return self.sessions.get(sid)

    def get_sessions_for_user(self, user=None):
        return {k: s for k, s in self.sessions.items() if user is None or s.owner == user}


@pytest.fixture
def mgr(monkeypatch):
    m = Mgr({"biz": Sess("Text: jarondynamics"), "flag": Sess("Contracts"), "open": Sess("Groceries"),
             "kid": Sess("thread", parent="flag")})
    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: m)
    monkeypatch.setattr(ai_interaction, "_session_manager", m, raising=False)
    monkeypatch.setattr(private_chats, "private_ids", lambda db=None: {
        sid for sid in m.sessions if private_chats.is_private(sid)})
    private_chats.set_private("flag", True)
    return m


def test_a_chat_is_private_by_its_switch_its_name_or_its_parent(mgr):
    assert private_chats.is_private("flag")                       # the switch
    assert private_chats.is_private("biz")                        # the name fallback
    assert private_chats.is_private("kid")                        # a side thread of a private chat
    assert not private_chats.is_private("open")
    assert private_chats.is_private(None, "  text:   JaronDynamics ")
    assert chat_prefs.get("flag")["private"] is True and chat_prefs.get("open")["private"] is False


# ── Search ────────────────────────────────────────────────────────────

def _db(with_fts):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    if with_fts:
        db.connection().exec_driver_sql(
            "CREATE VIRTUAL TABLE chat_messages_fts USING fts5(content, message_id UNINDEXED, "
            "session_id UNINDEXED, role UNINDEXED)")
    return db


@pytest.mark.parametrize("with_fts", (True, False))
def test_search_never_finds_a_private_chat(with_fts):
    from core.database import ChatMessage as M, Session as S
    from src.session_search import search_session_messages
    db = _db(with_fts)
    try:
        base = datetime(2026, 11, 16, 9)
        rows = [("biz", "Text: jarondynamics", None), ("flag", "Contracts", None),
                ("kid", "thread", "flag"), ("open", "Groceries", None)]
        for i, (sid, name, parent) in enumerate(rows):
            db.add(S(id=sid, name=name, endpoint_url="http://x", model="m", owner="alice", archived=False,
                     message_count=1, parent_session_id=parent))
            db.add(M(id=f"m{i}", session_id=sid, role="user", content=f"acme nda clause {sid}",
                     timestamp=base + timedelta(minutes=i)))
            if with_fts:
                db.connection().exec_driver_sql(
                    "INSERT INTO chat_messages_fts(content, message_id, session_id, role) VALUES (?, ?, ?, ?)",
                    (f"acme nda clause {sid}", f"m{i}", sid, "user"))
        db.commit()
        private_chats.set_private("flag", True)
        assert private_chats.private_ids(db) == {"biz", "flag", "kid"}
        found = search_session_messages("acme", owner="alice", db=db)
        assert [r.session_id for r in found] == ["open"]
    finally:
        db.close()


# ── The agent's tools ─────────────────────────────────────────────────

def test_list_sessions_hides_and_send_to_session_and_fork_refuse(mgr, monkeypatch):
    from src import ai_interaction
    from core import database

    class Q:
        def all(self):
            return []

    class DB:
        def query(self, *a):
            return Q()

        def close(self):
            pass
    monkeypatch.setattr(database, "SessionLocal", lambda: DB())
    out = asyncio.run(ai_interaction.do_list_sessions("", owner="alice"))["results"]
    assert "Groceries" in out and "jarondynamics" not in out and "Contracts" not in out and "thread" not in out

    called = []

    async def llm(*a, **kw):
        called.append(a)
        return "leaked"
    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", llm)
    for sid in ("biz", "flag", "kid"):
        r = asyncio.run(ai_interaction.do_send_to_session(f"{sid}\nsummarize this chat", owner="alice"))
        assert "error" in r and "response" not in r
    assert called == []


def test_a_new_chat_is_never_shown_a_private_chats_items(mgr, tmp_path, monkeypatch):
    from src import chat_memory as cm
    monkeypatch.setattr(cm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "MEMORY_FILE", str(tmp_path / "chat_memory.json"))
    cm.add("biz", "Acme NDA renewal due Friday", by="you", owner="alice")
    cm.add("open", "buy milk", by="you", owner="alice")
    assert [r["session_id"] for r in cm.recent(exclude="new", owner="alice")] == ["open"]


def test_tidy_refuses_a_private_chat(mgr):
    from src import chat_tidy
    s = mgr.sessions["biz"]
    s.id, s.history = "biz", []
    r = asyncio.run(chat_tidy.tidy(s, owner="alice"))
    assert r["tidied"] is False and "private" in r["reason"]


# ── Turns ─────────────────────────────────────────────────────────────

def test_a_run_in_a_private_chat_never_reaches_the_model(mgr):
    from src import agent_runs
    reached = []

    async def agen():
        reached.append(True)
        yield "data: {\"delta\": \"leaked\"}\n\n"

    async def go():
        run = agent_runs.start("biz", agen())
        await run.task
        return run

    run = asyncio.run(go())
    text = "".join(run.buffer)
    assert reached == [] and "private" in text and "leaked" not in text


def test_the_refusal_says_why():
    evs = asyncio.run(_collect(private_chats.refusal_stream("biz")))
    assert "never sent to a model" in evs[0] and evs[-1] == "data: [DONE]\n\n"


async def _collect(agen):
    return [e async for e in agen]
