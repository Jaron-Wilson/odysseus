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

    def create_session(self, session_id, name, endpoint_url, model, rag=False, owner=None, **kw):
        s = Session(id=session_id, name=name, endpoint_url=endpoint_url, model=model, rag=rag, owner=owner)
        self.sessions[session_id] = s
        return s

    def _persist_message(self, sid, msg):
        pass


def _model_db(monkeypatch):
    """The endpoints the model list and the model switch read: Alice's with
    three models, Bob's with one."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import importlib
    import sys
    from core.database import Base, ModelEndpoint
    import core.database
    # The gateway imports these at call time. Some other test files leave a
    # stub, or a copy built over stubbed core modules, in sys.modules; import
    # the real ones for this test (monkeypatch puts theirs back after).
    real = {"src.endpoint_resolver": lambda m: hasattr(m, "build_chat_url"),
            "routes.model_routes": lambda m: getattr(m, "SessionLocal", None) is core.database.SessionLocal,
            "routes.session_routes": lambda m: getattr(m, "SessionLocal", None) is core.database.SessionLocal}
    for name, ok in real.items():
        mod = sys.modules.get(name)
        if mod is not None and not ok(mod):
            monkeypatch.delitem(sys.modules, name)
    model_routes = importlib.import_module("routes.model_routes")
    session_routes = importlib.import_module("routes.session_routes")

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db_factory = sessionmaker(bind=engine, autoflush=False)
    db = db_factory()
    db.add(ModelEndpoint(id="ep-a", name="Alice box", base_url="http://10.9.0.1:8000/v1", owner="alice",
                         cached_models='["qwen", "llama", "mistral-small-24b"]'))
    db.add(ModelEndpoint(id="ep-b", name="Bob box", base_url="http://10.9.0.2:8000/v1", owner="bob",
                         cached_models='["gpt"]'))
    db.commit()
    db.close()
    monkeypatch.setattr(model_routes, "SessionLocal", db_factory)
    monkeypatch.setattr(session_routes, "SessionLocal", db_factory)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    sms_routes._LAST_LIST.clear()
    sms_routes._LAST_MODELS.clear()
    _model_db(monkeypatch)

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

    events = []
    import src.event_bus
    monkeypatch.setattr(src.event_bus, "fire_event", lambda name, owner=None: events.append((name, owner)))

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
            numbers = number if isinstance(number, list) else [number]
            assert client.put("/api/sms/config", headers=h,
                              json={"numbers": numbers, "reply_url": reply_url}).status_code == 200
            return client.post("/api/sms/secret", headers=h).json()["secret"]

        def text(secret, frm, body):
            r = client.post(f"/api/sms/inbound/{secret}", json={"from": frm, "text": body})
            client.portal.call(sms_routes.wait_pending)
            return r

        yield {"client": client, "setup": setup, "text": text, "llm": llm_calls,
               "posted": posted, "pushed": pushed, "mgr": mgr, "events": events}


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


def _texts(env):
    return [p["textMessage"]["text"] for _, p in env["posted"]]


def test_long_answers_arrive_as_numbered_texts_in_order(env, monkeypatch):
    import src.llm_core
    answer = " ".join(f"Sentence number {i} says a little something about the trip." for i in range(1, 31))

    async def chatty(url, model, messages, **kw):
        return answer

    monkeypatch.setattr(src.llm_core, "llm_call_async", chatty)
    secret = env["setup"]("alice", ALICE_NUM, reply_url="http://phone/message")
    env["text"](secret, ALICE_NUM, "say 1 go on")
    texts = _texts(env)
    n = len(texts)
    assert 2 < n <= sms_routes.MAX_PARTS
    assert [t.split(" ", 1)[0] for t in texts] == [f"({i}/{n})" for i in range(1, n + 1)]
    assert all(len(t) <= sms_routes.REPLY_CHARS for t in texts)
    # Cut between sentences, and nothing lost or reordered.
    assert all(t.endswith(".") for t in texts)
    assert " ".join(t.split(" ", 1)[1] for t in texts) == answer
    # The whole answer is still saved in the chat.
    assert env["mgr"].sessions["a1"].history[-1].content == answer


def test_a_very_long_answer_stops_at_the_cap_and_points_to_the_app(env, monkeypatch):
    import src.llm_core

    async def endless(url, model, messages, **kw):
        return "\n".join(f"Line {i} of a very long answer." for i in range(1, 500))

    monkeypatch.setattr(src.llm_core, "llm_call_async", endless)
    secret = env["setup"]("alice", ALICE_NUM, reply_url="http://phone/message")
    env["text"](secret, ALICE_NUM, "say 1 tell me everything")
    texts = _texts(env)
    assert len(texts) == sms_routes.MAX_PARTS
    assert texts[0].startswith(f"(1/{sms_routes.MAX_PARTS}) Line 1 of")
    assert texts[-1].endswith(sms_routes.CONTINUED)
    assert all(len(t) <= sms_routes.REPLY_CHARS for t in texts)


def test_split_prefers_line_breaks_and_keeps_short_replies_whole():
    assert sms_routes.split_reply("short answer") == ["short answer"]
    text = "\n".join(["a" * 200, "b" * 200, "c" * 200])
    parts = sms_routes.split_reply(text)
    assert parts == ["(1/2) " + "a" * 200 + "\n" + "b" * 200, "(2/2) " + "c" * 200]
    # One unbroken word still splits, at the size.
    parts = sms_routes.split_reply("x" * 1000)
    assert [len(p) <= sms_routes.REPLY_CHARS for p in parts] == [True] * len(parts)
    assert "".join(p.split(" ", 1)[1] for p in parts) == "x" * 1000


# ── Conversations: new / chat <n> / end, then plain texts ─────────────────

def test_new_starts_a_conversation_that_plain_texts_reach_until_end(env):
    secret = env["setup"]("alice", ALICE_NUM)
    r = env["text"](secret, ALICE_NUM, "New")
    assert r.status_code == 200 and r.json()["reply"].startswith("New chat with qwen.")
    new = [s for s in env["mgr"].sessions.values() if s.id not in ("a1", "a2", "b1")]
    assert len(new) == 1 and new[0].owner == "alice" and new[0].model == "qwen"
    assert new[0].endpoint_url == "http://10.9.0.1:8000/v1/chat/completions"
    assert env["events"] == [("session_created", "alice")]

    assert env["text"](secret, ALICE_NUM, "What should I pack?").json()["reply"] == "answer from qwen"
    assert env["text"](secret, ALICE_NUM, "And for rain?").json()["reply"] == "answer from qwen"
    assert [m.content for m in new[0].history] == [
        "What should I pack?", "answer from qwen", "And for rain?", "answer from qwen"]
    assert env["llm"][-1]["url"] == "http://10.9.0.1:8000/v1/chat/completions"
    assert "Now talking to: " in env["text"](secret, ALICE_NUM, "help").json()["reply"]

    assert env["text"](secret, ALICE_NUM, "end").json()["reply"].startswith("Conversation ended.")
    calls = len(env["llm"])
    assert env["text"](secret, ALICE_NUM, "one more thing").json()["reply"] == sms_routes.HINT
    assert len(env["llm"]) == calls and len(new[0].history) == 4


def test_chat_n_picks_a_listed_chat_as_the_conversation(env):
    secret = env["setup"]("alice", ALICE_NUM)
    env["text"](secret, ALICE_NUM, "list")
    r = env["text"](secret, ALICE_NUM, "CHAT 2")
    assert r.json()["reply"].startswith("Now talking to Groceries (llama).")
    assert env["text"](secret, ALICE_NUM, "do we need milk?").json()["reply"] == "answer from llama"
    assert [m.content for m in env["mgr"].sessions["a2"].history] == ["do we need milk?", "answer from llama"]
    assert "Talking to: Groceries (llama)" in env["text"](secret, ALICE_NUM, "status").json()["reply"]
    # stop is end too; "stop" inside a sentence is just a turn.
    assert env["text"](secret, ALICE_NUM, "stop the car?").json()["reply"] == "answer from llama"
    assert env["text"](secret, ALICE_NUM, "stop").json()["reply"].startswith("Conversation ended.")
    assert "no chat 7" in env["text"](secret, ALICE_NUM, "chat 7").json()["reply"]


def test_find_with_one_match_switches_straight_to_it(env):
    secret = env["setup"]("alice", ALICE_NUM)
    r = env["text"](secret, ALICE_NUM, "find trip")
    assert r.json()["reply"].startswith("Now talking to Trip plans (qwen).")
    assert env["text"](secret, ALICE_NUM, "where are we going?").json()["reply"] == "answer from qwen"
    assert [m.content for m in env["mgr"].sessions["a1"].history] == ["where are we going?", "answer from qwen"]


def test_find_with_no_match_says_so(env):
    secret = env["setup"]("alice", ALICE_NUM)
    assert 'No chat matches "zzz"' in env["text"](secret, ALICE_NUM, "find zzz").json()["reply"]
    assert env["llm"] == []


def test_find_with_several_matches_lists_them_and_chat_n_still_works(env):
    env["mgr"].create_session("a3", "Trip to NYC", "http://llm/a", "mixtral", owner="alice")
    secret = env["setup"]("alice", ALICE_NUM)
    reply = env["text"](secret, ALICE_NUM, "find trip").json()["reply"]
    assert 'Chats matching "trip":' in reply
    assert "1. Trip plans" in reply and "2. Trip to NYC" in reply
    r = env["text"](secret, ALICE_NUM, "chat 2")
    assert r.json()["reply"].startswith("Now talking to Trip to NYC (mixtral).")


def test_call_texts_back_a_link_that_opens_the_call_and_never_reaches_the_model(env):
    # A texted "/call" used to go to the model, which said "call started"
    # and nothing rang. Nothing can ring from here: the answer is a link.
    secret = env["setup"]("alice", ALICE_NUM)

    def text(body, host=None):
        h = {"host": host} if host else {}
        r = env["client"].post(f"/api/sms/inbound/{secret}", json={"from": ALICE_NUM, "text": body}, headers=h)
        env["client"].portal.call(sms_routes.wait_pending)
        return r.json()["reply"]

    assert text("call").endswith("http://testserver/?call=new")
    env["text"](secret, ALICE_NUM, "list")
    env["text"](secret, ALICE_NUM, "chat 2")
    reply = text("/call", host="jaron-dev-server.tail1234.ts.net")
    assert reply.startswith("Tap to start a voice call with Groceries (llama):")
    assert reply.endswith("https://jaron-dev-server.tail1234.ts.net/?call=a2#a2")
    assert text("Call me").endswith("/?call=a2#a2")
    # A phone cannot open loopback, so no link to it.
    assert "type /call" in text("call", host="127.0.0.1:7000")
    assert env["llm"] == []
    # "call" inside a sentence is a turn, like "stop".
    assert text("call mom about dinner?") == "answer from llama"
    assert "call - a link" in sms_routes.HELP


def test_models_lists_the_owners_models_and_model_switches_the_chat(env):
    secret = env["setup"]("alice", ALICE_NUM)
    env["text"](secret, ALICE_NUM, "new")
    sid = sms_routes.get_conversation("alice", ALICE_NUM)
    sess = env["mgr"].sessions[sid]
    listing = env["text"](secret, ALICE_NUM, "models").json()["reply"]
    assert listing.splitlines()[:3] == ["1. qwen *", "2. llama", "3. mistral-small-24b"]
    assert "gpt" not in listing   # Bob's endpoint

    assert env["text"](secret, ALICE_NUM, "model 2").json()["reply"].endswith("now uses llama.")
    assert sess.model == "llama" and sess.endpoint_url == "http://10.9.0.1:8000/v1/chat/completions"
    assert env["text"](secret, ALICE_NUM, "hi again").json()["reply"] == "answer from llama"
    assert env["llm"][-1]["model"] == "llama"

    assert env["text"](secret, ALICE_NUM, "Model Mistral Small").json()["reply"].endswith(
        "now uses mistral-small-24b.")
    assert sess.model == "mistral-small-24b"
    assert "No model matches" in env["text"](secret, ALICE_NUM, "model gpt").json()["reply"]
    assert "no model 9" in env["text"](secret, ALICE_NUM, "model 9").json()["reply"]
    assert sess.model == "mistral-small-24b"

    # new takes a model too, and the new chat becomes the conversation.
    assert env["text"](secret, ALICE_NUM, "new llama").json()["reply"].startswith("New chat with llama.")
    other = sms_routes.get_conversation("alice", ALICE_NUM)
    assert other != sid and env["mgr"].sessions[other].model == "llama"


def test_model_without_a_conversation_gets_the_hint(env):
    secret = env["setup"]("alice", ALICE_NUM)
    assert env["text"](secret, ALICE_NUM, "model 1").json()["reply"] == sms_routes.HINT


def test_conversations_are_per_sender_and_per_owner_and_kept_in_prefs(env, tmp_path):
    alice_work = "+15550104000"
    secret = env["setup"]("alice", [ALICE_NUM, alice_work])
    bob_secret = env["setup"]("bob", BOB_NUM)
    env["text"](secret, ALICE_NUM, "new")
    # Alice's other number has no conversation of its own.
    assert env["text"](secret, alice_work, "hello?").json()["reply"] == sms_routes.HINT
    # Bob's "new" is his chat, on his endpoint, and Alice's stays hers.
    assert env["text"](bob_secret, BOB_NUM, "new").json()["reply"].startswith("New chat with gpt.")
    assert env["text"](bob_secret, BOB_NUM, "hey").json()["reply"] == "answer from gpt"
    alice_sid = sms_routes.get_conversation("alice", ALICE_NUM)
    bob_sid = sms_routes.get_conversation("bob", BOB_NUM)
    assert env["mgr"].sessions[alice_sid].owner == "alice" and env["mgr"].sessions[bob_sid].owner == "bob"
    assert sms_routes.get_conversation("alice", BOB_NUM) == "" and sms_routes.get_conversation("bob", ALICE_NUM) == ""
    # Saved in the prefs file, so a restart keeps it; Settings saves keep it too.
    stored = __import__("json").loads((tmp_path / "user_prefs.json").read_text())
    assert list(stored["_users"]["alice"]["sms_gateway"]["conversations"]) == [ALICE_NUM]
    env["client"].put("/api/sms/config", headers={"x-test-user": "alice"},
                      json={"numbers": [ALICE_NUM, alice_work], "reply_url": ""})
    assert sms_routes.get_conversation("alice", ALICE_NUM) == alice_sid
    assert env["text"](secret, ALICE_NUM, "still there?").json()["reply"] == "answer from qwen"
    assert "conversations" not in env["client"].get("/api/sms/config", headers={"x-test-user": "alice"}).json()


def test_a_conversation_on_a_chat_that_is_gone_is_forgotten(env):
    secret = env["setup"]("alice", ALICE_NUM)
    env["text"](secret, ALICE_NUM, "chat 1")
    del env["mgr"].sessions["a1"]
    assert env["text"](secret, ALICE_NUM, "are you there?").json()["reply"] == sms_routes.HINT
    assert sms_routes.get_conversation("alice", ALICE_NUM) == ""
    assert env["llm"] == []


def test_plain_or_unknown_text_without_a_conversation_gets_the_hint(env):
    secret = env["setup"]("alice", ALICE_NUM)
    for body in ("hello", "flarb 7", "chat", "end"):
        r = env["text"](secret, ALICE_NUM, body)
        assert r.status_code == 200
    assert _pushed_bodies(env)[:3] == [sms_routes.HINT] * 3
    assert _pushed_bodies(env)[3] == "You are not talking to a chat."
    assert env["llm"] == []


def _pushed_bodies(env):
    return [p["body"] for p in env["pushed"]]


def test_say_still_goes_to_the_listed_chat_during_a_conversation(env):
    secret = env["setup"]("alice", ALICE_NUM)
    env["text"](secret, ALICE_NUM, "chat 1")
    assert env["text"](secret, ALICE_NUM, "say 2 just this once").json()["reply"] == "answer from llama"
    assert [m.content for m in env["mgr"].sessions["a2"].history] == ["just this once", "answer from llama"]
    assert sms_routes.get_conversation("alice", ALICE_NUM) == "a1"
    assert env["text"](secret, ALICE_NUM, "back to trips").json()["reply"] == "answer from qwen"


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


def test_test_reply_reaches_every_configured_number_with_a_reply_url(env):
    h = {"x-test-user": "alice"}
    env["setup"]("alice", [ALICE_NUM, BOB_NUM], reply_url="http://sms:pw@100.64.0.9:8080/message")
    r = env["client"].post("/api/sms/test", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert [x["to"] for x in body["results"]] == [ALICE_NUM, BOB_NUM]
    assert [p["phoneNumbers"] for _, p in env["posted"]] == [[ALICE_NUM], [BOB_NUM]]


def test_test_reply_pushes_only_once_for_multiple_numbers_without_a_reply_url(env):
    h = {"x-test-user": "alice"}
    env["setup"]("alice", [ALICE_NUM, BOB_NUM])
    r = env["client"].post("/api/sms/test", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert [x["to"] for x in body["results"]] == [ALICE_NUM]
    assert len(env["pushed"]) == 1


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
