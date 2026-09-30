"""Texting Odysseus: the phone's forwarder posts each SMS to
/api/sms/inbound/<secret>, and the reply goes back over the phone's send
endpoint (or web push).

That path is open to the internet by design, so most of these are about who
it will not listen to: a stranger's number, a wrong or rotated secret, and
another user's chats.
"""
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.models import Session
from routes import prefs_routes, sms_routes

ALICE_NUM = "+15550102000"
BOB_NUM = "+15550103000"


class _Mgr:
    def __init__(self, sessions):
        self.sessions = {s.id: s for s in sessions}

    def get_sessions_for_user(self, username=None):
        if username is None:
            return self.sessions
        return {k: s for k, s in self.sessions.items() if s.owner == username}

    def get_session(self, sid):
        return self.sessions.get(sid)

    def _persist_message(self, sid, msg):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    sms_routes._LAST_LIST.clear()

    mgr = _Mgr([
        Session(id="a1", name="Trip plans", endpoint_url="http://llm/a", model="qwen", owner="alice"),
        Session(id="a2", name="Groceries", endpoint_url="http://llm/a", model="llama", owner="alice"),
        Session(id="b1", name="Bob secrets", endpoint_url="http://llm/b", model="gpt", owner="bob"),
    ])
    from src import ai_interaction
    import core.models as cm
    monkeypatch.setattr(ai_interaction, "_session_manager", mgr)
    monkeypatch.setattr(cm, "_SESSION_MANAGER_INSTANCE", mgr)

    llm_calls = []

    async def fake_llm(url, model, messages, **kw):
        llm_calls.append({"url": url, "model": model, "messages": messages})
        return f"answer from {model}"

    import src.llm_core
    monkeypatch.setattr(src.llm_core, "llm_call_async", fake_llm)

    posted, pushed = [], []

    async def fake_post(url, payload):
        posted.append((url, payload))
        return 200

    monkeypatch.setattr(sms_routes, "_post_reply", fake_post)

    from src import webpush
    monkeypatch.setattr(webpush, "load_subscriptions", lambda: [
        {"endpoint": "https://push/alice", "owner": "alice"},
        {"endpoint": "https://push/bob", "owner": "bob"},
    ])

    async def fake_send(title, body, **kw):
        pushed.append({"body": body, **kw})
        return {"sent": 1, "failed": 0}

    monkeypatch.setattr(webpush, "send", fake_send)

    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(sms_routes.setup_sms_routes(mgr))
    with TestClient(app) as client:
        def setup(user, number, reply_url=""):
            h = {"x-test-user": user}
            assert client.put("/api/sms/config", headers=h,
                              json={"numbers": [number], "reply_url": reply_url}).status_code == 200
            return client.post("/api/sms/secret", headers=h).json()["secret"]

        def text(secret, frm, body):
            r = client.post(f"/api/sms/inbound/{secret}", json={"from": frm, "text": body})
            client.portal.call(sms_routes.wait_pending)
            return r

        yield {"client": client, "setup": setup, "text": text, "llm": llm_calls,
               "posted": posted, "pushed": pushed, "mgr": mgr}


def test_wrong_number_is_a_generic_404_and_runs_nothing(env):
    secret = env["setup"]("alice", ALICE_NUM)
    r = env["text"](secret, "+15559999999", "say 1 delete everything")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}
    assert env["llm"] == [] and env["posted"] == [] and env["pushed"] == []
    assert env["mgr"].sessions["a1"].history == []


def test_bad_secret_is_the_same_404(env):
    env["setup"]("alice", ALICE_NUM)
    wrong = env["text"]("x" * 43, ALICE_NUM, "list")
    assert wrong.status_code == 404 and wrong.json() == {"detail": "Not Found"}
    # Indistinguishable from the wrong-number case and from an unknown path.
    assert env["client"].post("/api/sms/inbound/short", json={}).status_code == 404
    assert env["posted"] == [] and env["pushed"] == []


def test_list_numbers_the_owners_chats_only(env):
    secret = env["setup"]("alice", ALICE_NUM)
    r = env["text"](secret, "(555) 010-2000", "  LIST  ")   # 10-digit US form, any case
    assert r.status_code == 200
    reply = r.json()["reply"]
    assert r.json()["ok"] is True
    assert "1. Trip plans (qwen, " in reply and "2. Groceries (llama, " in reply
    assert "Bob" not in reply


def test_say_reaches_the_numbered_session(env):
    secret = env["setup"]("alice", ALICE_NUM)
    env["text"](secret, ALICE_NUM, "list")
    r = env["text"](secret, ALICE_NUM, "Say 2   what do we need?")
    assert r.status_code == 200
    assert r.json()["reply"] == "answer from llama"
    assert env["llm"][-1]["url"] == "http://llm/a"
    assert env["llm"][-1]["messages"][-1] == {"role": "user", "content": "what do we need?"}
    hist = env["mgr"].sessions["a2"].history
    assert [m.content for m in hist] == ["what do we need?", "answer from llama"]
    assert env["mgr"].sessions["a1"].history == []


def test_long_answers_are_truncated_with_a_marker(env, monkeypatch):
    import src.llm_core

    async def chatty(url, model, messages, **kw):
        return "word " * 400

    monkeypatch.setattr(src.llm_core, "llm_call_async", chatty)
    secret = env["setup"]("alice", ALICE_NUM)
    reply = env["text"](secret, ALICE_NUM, "say 1 go on").json()["reply"]
    assert len(reply) <= sms_routes.REPLY_CHARS and reply.endswith(sms_routes.TRUNCATED)


def test_unknown_number_and_other_owners_chat_are_refused_politely(env):
    secret = env["setup"]("alice", ALICE_NUM)
    env["setup"]("bob", BOB_NUM)
    r = env["text"](secret, ALICE_NUM, "say 9 hello")
    assert r.status_code == 200 and "no chat 9" in r.json()["reply"]
    # A remembered list that somehow points at Bob's chat still cannot reach it.
    sms_routes._LAST_LIST["alice"] = (__import__("time").time(), ["b1"])
    r = env["text"](secret, ALICE_NUM, "say 1 show me")
    assert r.status_code == 200 and "no chat 1" in r.json()["reply"]
    assert env["llm"] == [] and env["mgr"].sessions["b1"].history == []
    # Bob's number with Alice's secret is not Bob, and not Alice either.
    assert env["text"](secret, BOB_NUM, "list").status_code == 404


def test_reply_goes_to_the_reply_url_when_set(env):
    secret = env["setup"]("alice", ALICE_NUM, reply_url="http://sms:pw@100.64.0.9:8080/message")
    env["text"](secret, ALICE_NUM, "help")
    assert len(env["posted"]) == 1 and env["pushed"] == []
    url, payload = env["posted"][0]
    assert url == "http://sms:pw@100.64.0.9:8080/message"
    assert payload["phoneNumbers"] == [ALICE_NUM]
    assert "list" in payload["textMessage"]["text"]


def test_reply_falls_back_to_the_owners_web_push(env):
    secret = env["setup"]("alice", ALICE_NUM)
    env["text"](secret, ALICE_NUM, "status")
    assert env["posted"] == []
    assert [p["endpoint"] for p in env["pushed"]] == ["https://push/alice"]
    assert env["pushed"][0]["body"] == "Nothing is running."


def test_slow_say_answers_later_through_the_reply_channel(env, monkeypatch):
    import asyncio
    import src.llm_core

    async def slow(url, model, messages, **kw):
        await asyncio.sleep(0.3)
        return "late answer"

    monkeypatch.setattr(src.llm_core, "llm_call_async", slow)
    monkeypatch.setattr(sms_routes, "SAY_INLINE_WAIT", 0.05)
    secret = env["setup"]("alice", ALICE_NUM, reply_url="http://phone/message")
    r = env["text"](secret, ALICE_NUM, "say 1 hi")
    assert r.status_code == 200 and "will follow" in r.json()["reply"]
    assert [p["textMessage"]["text"] for _, p in env["posted"]] == ["late answer"]


def test_rotating_the_secret_invalidates_the_old_one(env):
    old = env["setup"]("alice", ALICE_NUM)
    new = env["client"].post("/api/sms/secret", headers={"x-test-user": "alice"}).json()["secret"]
    assert new != old
    assert env["text"](old, ALICE_NUM, "list").status_code == 404
    assert env["text"](new, ALICE_NUM, "list").status_code == 200
    env["client"].delete("/api/sms/secret", headers={"x-test-user": "alice"})
    assert env["text"](new, ALICE_NUM, "list").status_code == 404


def test_the_secret_is_never_stored_returned_or_logged(env, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    secret = env["setup"]("alice", ALICE_NUM)
    env["text"](secret, ALICE_NUM, "list")
    env["text"](secret, "+15559999999", "list")
    env["text"](secret + "x", ALICE_NUM, "list")
    # The test client's own httpx logger prints the URL it requested; that is
    # this test calling in, not the server logging.
    server_side = [r.getMessage() for r in caplog.records if not r.name.startswith("httpx")]
    assert server_side and not any(secret in m for m in server_side)
    assert secret not in (tmp_path / "user_prefs.json").read_text()
    cfg = env["client"].get("/api/sms/config", headers={"x-test-user": "alice"}).json()
    assert cfg["has_secret"] is True and secret not in str(cfg)
    # uvicorn's access log line carries the path; it is redacted.
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "POST", f"/api/sms/inbound/{secret}", "1.1", 200), None)
    sms_routes._RedactInboundSecret().filter(rec)
    assert secret not in rec.getMessage() and "<redacted>" in rec.getMessage()


def test_sms_gateway_for_android_payload_is_understood(env):
    secret = env["setup"]("alice", ALICE_NUM)
    r = env["client"].post(f"/api/sms/inbound/{secret}", json={
        "event": "sms:received", "deviceId": "d", "id": "e", "webhookId": "w",
        "payload": {"messageId": "m", "message": "help", "sender": "5550102000",
                    "recipient": "+15550109999",
                    "simNumber": 1, "receivedAt": "2026-09-30T12:00:00.000+00:00"}})
    assert r.status_code == 200 and "say <n> <text>" in r.json()["reply"]


def test_sms_forwarder_default_template_is_understood(env):
    secret = env["setup"]("alice", ALICE_NUM)
    r = env["client"].post(f"/api/sms/inbound/{secret}", json={
        "from": "+15550102000", "text": "help", "sentStamp": 1790000000000,
        "receivedStamp": 1790000001000, "sim": "sim1"})
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.parametrize("raw,want", [
    ("+1 (555) 010-2000", ALICE_NUM), ("555.010.2000", ALICE_NUM), ("15550102000", ALICE_NUM),
    ("+44 7700 900123", "+447700900123"), ("0044 7700 900123", "+447700900123"),
    ("22395", "22395"), ("", ""), ("abc", ""),
])
def test_number_normalization(raw, want):
    assert sms_routes.normalize_number(raw) == want


def test_only_the_inbound_path_is_exempt_from_login():
    import os
    import re
    import secrets
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py"), encoding="utf-8").read()
    pat = re.compile(re.search(r'_re\.compile\(r"(\^/api/sms/inbound/[^"]+)"\)', src).group(1))
    assert pat.match(f"/api/sms/inbound/{secrets.token_urlsafe(32)}")
    for path in ("/api/sms/config", "/api/sms/secret", "/api/sms/test", "/api/sms/inbound/",
                 "/api/sms/inbound/short", f"/api/sms/inbound/{'a' * 43}/extra"):
        assert not pat.match(path)
