"""Sign in with ChatGPT: the bearer must reach every request.

The plan's access token is short-lived, so sessions never persist it
(``sessions.headers`` stays ``{}``) and ``SessionManager.get_session``
reloads ``sess.headers`` from that row on every call. A token set on the
session for one request is gone as soon as anything else reads the session,
so the chat stream, agent rounds, closing words, auto-name and skill
extraction all reached ``_stream_chatgpt_plan`` with no Authorization header
and fell back to another model ("Sign in with ChatGPT ..."). llm_core now
resolves the owner's token when the request is sent.

Everything here is mocked: no request leaves the machine.
"""

import asyncio
import json
import time
import types
import uuid

import pytest

from src import chatgpt_plan as cgp
from src import llm_core

ACCESS = "eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.c2lnbmF0dXJlLWFjY2Vzcw"
PLAN_URL = cgp.ENDPOINT_BASE + "/responses"
MODEL = "gpt-5.6-terra"

_DONE_SSE = [
    "event: response.output_text.delta",
    'data: {"type":"response.output_text.delta","delta":"Short title"}',
    "",
    "event: response.completed",
    'data: {"type":"response.completed","response":{}}',
    "",
]


class _FakeStream:
    def __init__(self, lines):
        self.status_code = 200
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, lines=None):
        self.lines = lines or _DONE_SSE
        self.calls = []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        return _FakeStream(self.lines)


def _auth(call):
    return call["headers"].get("Authorization")


@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    """alice is signed in with a token that does not need a refresh."""
    monkeypatch.setattr(cgp, "STORE_DIR", tmp_path / "chatgpt_plan")
    monkeypatch.setenv("ODYSSEUS_CHATGPT_LOOPBACK_LISTENER", "0")

    def _no_network(*a, **k):
        raise AssertionError("no real OpenAI calls in tests")

    monkeypatch.setattr(cgp.httpx, "post", _no_network)
    cgp._refresh_locks.clear()
    cgp.save_credentials("alice", {
        "access_token": ACCESS,
        "refresh_token": "rt_test_refresh_value",
        "client_id": "oaiapp_test",
        "expires_at": int(time.time()) + 86400,
        "earliest_refresh_at": 0,
    })
    client = _FakeClient()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_response_cache", {})
    return client


def _collect(agen):
    async def go():
        return [c async for c in agen]
    return asyncio.run(go())


# ── llm_core resolves the token at send time ──────────────────────────────

def test_stream_with_empty_session_headers_sends_the_owner_token(signed_in):
    chunks = _collect(llm_core.stream_llm_with_fallback(
        [(PLAN_URL, MODEL, {})], [{"role": "user", "content": "hi"}], owner="alice",
    ))
    assert len(signed_in.calls) == 1
    assert _auth(signed_in.calls[0]) == f"Bearer {ACCESS}"
    assert not any(c.startswith("event: error") for c in chunks)


def test_blocking_call_with_empty_headers_sends_the_owner_token(signed_in, monkeypatch):
    sent = {}

    class _SyncStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            yield 'data: {"type":"response.output_text.delta","delta":"ok"}'
            yield 'data: {"type":"response.completed","response":{}}'

    def fake_stream(method, url, json=None, headers=None, timeout=None):
        sent.update(headers=dict(headers or {}))
        return _SyncStream()

    monkeypatch.setattr(llm_core.httpx, "stream", fake_stream)
    out = llm_core.llm_call(PLAN_URL, MODEL, [{"role": "user", "content": "sort " + uuid.uuid4().hex}],
                            headers={}, owner="alice")
    assert out == "ok"
    assert sent["headers"]["Authorization"] == f"Bearer {ACCESS}"


def test_another_user_is_not_signed_in(signed_in):
    chunks = _collect(llm_core.stream_llm(PLAN_URL, MODEL, [{"role": "user", "content": "x"}],
                                          headers={}, owner="bob"))
    assert signed_in.calls == []
    assert "Sign in with ChatGPT" in chunks[0]


def test_a_stale_bearer_is_dropped_after_sign_out(signed_in):
    cgp.delete_credentials("alice")
    chunks = _collect(llm_core.stream_llm(PLAN_URL, MODEL, [{"role": "user", "content": "x"}],
                                          headers={"Authorization": "Bearer old"}, owner="alice"))
    assert signed_in.calls == []
    assert "Sign in with ChatGPT" in chunks[0]


def test_other_providers_keep_their_headers():
    from src.endpoint_resolver import request_scoped_headers

    h = {"Authorization": "Bearer sk-static"}
    assert request_scoped_headers("https://api.openai.com/v1/chat/completions", h, "alice") is h
    assert request_scoped_headers("http://localhost:11434/v1/chat/completions", None, "alice") is None


def test_token_is_not_logged(signed_in, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    _collect(llm_core.stream_llm(PLAN_URL, MODEL, [{"role": "user", "content": "x"}],
                                 headers={}, owner="alice"))
    assert ACCESS not in caplog.text


# ── Session paths ─────────────────────────────────────────────────────────

def test_session_reload_drops_the_request_local_bearer():
    """The root cause: get_session() resyncs headers from the DB row, which
    is {} for a ChatGPT plan chat, wiping a token set earlier in the request."""
    from core.database import SessionLocal, Session as DbSession
    from core.session_manager import SessionManager

    sid = "plan-" + uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        db.add(DbSession(id=sid, name="chat", endpoint_url=PLAN_URL, model=MODEL,
                         owner="alice", headers={}))
        db.commit()
    finally:
        db.close()
    try:
        mgr = SessionManager.__new__(SessionManager)
        mgr.sessions = {}
        mgr._load_session_from_db = lambda _sid: mgr.sessions.setdefault(
            _sid, types.SimpleNamespace(id=_sid, history=[], message_count=0, headers={},
                                        endpoint_url=PLAN_URL, model=MODEL, owner="alice"))
        mgr._touch_session = lambda _sid: None
        sess = mgr.get_session(sid)
        sess.headers = {"Authorization": "Bearer request-local"}
        assert mgr.get_session(sid).headers == {}
    finally:
        db = SessionLocal()
        try:
            db.query(DbSession).filter(DbSession.id == sid).delete()
            db.commit()
        finally:
            db.close()


def _plan_endpoint(owner="alice", supports_tools=None):
    from core.database import SessionLocal, ModelEndpoint

    db = SessionLocal()
    try:
        ep = ModelEndpoint(id="cgp" + uuid.uuid4().hex[:5], name="ChatGPT",
                           base_url=cgp.ENDPOINT_BASE, owner=owner, is_enabled=True,
                           provider_auth_id=cgp.PROVIDER_AUTH_MARKER, api_key=None,
                           cached_models=json.dumps([MODEL]), supports_tools=supports_tools)
        db.add(ep)
        db.commit()
        return ep.id
    finally:
        db.close()


def _drop_endpoint(ep_id):
    from core.database import SessionLocal, ModelEndpoint

    db = SessionLocal()
    try:
        db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).delete()
        db.commit()
    finally:
        db.close()


def test_switching_a_chat_to_the_plan_model_then_streaming(signed_in):
    from core.database import SessionLocal, Session as DbSession
    from routes.session_routes import switch_session_model

    ep_id = _plan_endpoint()
    sid = "plan-" + uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        db.add(DbSession(id=sid, name="chat", endpoint_url="http://localhost:11435/v1/chat/completions",
                         model="qwen", owner="alice", headers={}))
        db.commit()
    finally:
        db.close()
    try:
        sess = types.SimpleNamespace(model="qwen", endpoint_url="", headers={"Authorization": "Bearer x"})
        url = switch_session_model(sess, sid, MODEL, "", ep_id, "alice")
        assert url == PLAN_URL
        # Nothing secret is stored on the session or its row.
        assert not any(k.lower() == "authorization" for k in sess.headers)
        db = SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == sid).first()
            assert not any(k.lower() == "authorization" for k in (row.headers or {}))
        finally:
            db.close()
        _collect(llm_core.stream_llm_with_fallback(
            [(sess.endpoint_url, sess.model, sess.headers)],
            [{"role": "user", "content": "hi"}], owner="alice",
        ))
        assert _auth(signed_in.calls[0]) == f"Bearer {ACCESS}"
    finally:
        _drop_endpoint(ep_id)
        db = SessionLocal()
        try:
            db.query(DbSession).filter(DbSession.id == sid).delete()
            db.commit()
        finally:
            db.close()


def test_auto_name_uses_the_owner_token(signed_in, monkeypatch):
    from routes import chat_helpers

    renamed = {}
    mgr = types.SimpleNamespace(update_session_name=lambda sid, title: renamed.update({sid: title}))
    sess = types.SimpleNamespace(
        id="s1", owner="alice", endpoint_url=PLAN_URL, model=MODEL, headers={},
        history=[types.SimpleNamespace(role="user", content="ping google " + uuid.uuid4().hex)],
    )
    asyncio.run(chat_helpers.auto_name_session(mgr, sess))
    assert signed_in.calls and _auth(signed_in.calls[0]) == f"Bearer {ACCESS}"
    assert renamed == {"s1": "Short title"}


def test_skill_extraction_uses_the_owner_token(signed_in):
    from services.memory.skill_extractor import maybe_extract_skill

    signed_in.lines = [
        'data: {"type":"response.output_text.delta","delta":"null"}',
        'data: {"type":"response.completed","response":{}}',
    ]
    sess = types.SimpleNamespace(get_context_messages=lambda: [
        {"role": "user", "content": "ping google.com " + uuid.uuid4().hex},
        {"role": "assistant", "content": "done"},
    ])
    asyncio.run(maybe_extract_skill(sess, object(), PLAN_URL, MODEL, {}, 2, 2, owner="alice"))
    assert signed_in.calls and _auth(signed_in.calls[0]) == f"Bearer {ACCESS}"


# ── Agent mode ────────────────────────────────────────────────────────────

def _agent_common(monkeypatch):
    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    return al


def test_agent_rounds_send_the_token_and_native_tools(signed_in, monkeypatch):
    al = _agent_common(monkeypatch)
    ep_id = _plan_endpoint(supports_tools=True)
    try:
        signed_in.lines = [
            'data: {"type":"response.output_text.delta","delta":"Pong."}',
            'data: {"type":"response.completed","response":{}}',
        ]
        chunks = _collect(al.stream_agent_loop(
            PLAN_URL, MODEL, [{"role": "user", "content": "search the web for odysseus"}],
            headers={}, owner="alice", max_rounds=1,
            relevant_tools={"web_search", "web_fetch"},
        ))
    finally:
        _drop_endpoint(ep_id)
    plan_calls = [c for c in signed_in.calls if c["url"] == cgp.RESPONSES_URL]
    assert plan_calls, chunks
    assert all(_auth(c) == f"Bearer {ACCESS}" for c in plan_calls)
    tools = plan_calls[0]["json"].get("tools") or []
    assert "web_search" in {t.get("name") for t in tools}
    # Our schemas have optional properties, which strict mode rejects.
    assert all(t.get("strict") is False for t in tools)
    assert not any("fell back" in c for c in chunks)


def test_closing_words_use_the_owner_token(signed_in):
    import src.agent_loop as al

    text = asyncio.run(al._closing_words(
        [{"role": "user", "content": "ping " + uuid.uuid4().hex}],
        [{"tool": "bash"}], endpoint_url=PLAN_URL, model=MODEL, headers={},
        max_tokens=100, owner="alice",
    ))
    assert text == "Short title"
    assert _auth(signed_in.calls[0]) == f"Bearer {ACCESS}"


# ── Native tools default ──────────────────────────────────────────────────

def test_plan_endpoints_saved_without_tools_are_upgraded_once(signed_in):
    from core.database import SessionLocal, ModelEndpoint

    ep_id = _plan_endpoint(supports_tools=False)
    try:
        assert cgp.enable_native_tools_once(SessionLocal) >= 1
        db = SessionLocal()
        try:
            assert db.get(ModelEndpoint, ep_id).supports_tools is True
            # The user turns them off again; the upgrade does not repeat.
            db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).update({"supports_tools": False})
            db.commit()
        finally:
            db.close()
        assert cgp.enable_native_tools_once(SessionLocal) == 0
        db = SessionLocal()
        try:
            assert db.get(ModelEndpoint, ep_id).supports_tools is False
        finally:
            db.close()
    finally:
        _drop_endpoint(ep_id)
