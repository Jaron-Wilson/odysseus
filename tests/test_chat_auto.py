"""Odysseus's own chat on the auto model (src/chat_auto.py): the picker offers it only when the router is on, a message
is routed by the gateway's router with the label kept per chat (keyed on the chat's id, not on the messages), the turn
runs on the chosen model's endpoint like any picked model, the choice is saved with the reply for the label under it,
and a router or endpoint failure is shown rather than answered by another model. No network: the embeddings, the
models and the endpoints are stand-ins."""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.models import ChatMessage, Session
from routes import chat_routes, model_routes, openai_gateway_routes as gw_routes
from src import chat_auto
from src import gateway_router as R
from src import openai_gateway as gw

EASY, HARD, CHAT, MIDDLE = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.6, 0.0, 0.8], [0.7071, 0.7072, 0.0]
FAST, STRONG = R.DEFAULTS["fast"], R.DEFAULTS["strong"]

# where each gateway model id lives
ENDPOINTS = {
    R.DEFAULTS["chat"]: ("http://tiny/v1/chat/completions", {}, "gemma3:1b"),
    FAST: ("http://fast/v1/chat/completions", {}, "qwen3:8b"),
    STRONG: ("http://strong/v1/chat/completions", {"Authorization": "Bearer strong-key"}, "qwen3.8:27b"),
    "chatgpt:gpt-6.1-sol": ("https://chatgpt.test/backend-api/codex", {"Authorization": "Bearer chatgpt"}, "gpt-6.1-sol"),
    "chatgpt:gpt-6.1-terra": ("https://chatgpt.test/backend-api/codex", {"Authorization": "Bearer chatgpt"}, "gpt-6.1-terra"),
    "chatgpt:gpt-6-luna": ("https://chatgpt.test/backend-api/codex", {"Authorization": "Bearer chatgpt"}, "gpt-6-luna"),
}


async def fake_embed(cfg, owner, texts):
    out = []
    for t in texts:
        if t in R.DEFAULTS["examples"]["chat"] or t.startswith("chat"):
            out.append(CHAT)
        elif t in R.DEFAULTS["examples"]["easy"] or t.startswith("easy"):
            out.append(EASY)
        elif t in R.DEFAULTS["examples"]["hard"] or t.startswith("hard"):
            out.append(HARD)
        else:
            out.append(MIDDLE)
    return out


def fake_resolve(model_id, owner):
    if model_id not in ENDPOINTS:
        raise LookupError(f"no model '{model_id}'; GET /v1/models lists what is available")
    url, headers, model = ENDPOINTS[model_id]
    return url, dict(headers), model, "chatgpt" in url


async def fake_complete(model_id, messages, owner, max_tokens):
    return "HARD"


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    R._sticky.clear(); R._inflight.clear(); R._vision.clear(); R._online.clear(); R._example_vecs.clear()
    monkeypatch.setattr(R, "_embed", fake_embed)
    monkeypatch.setattr(R, "_reachable", lambda url: "offline" not in url)
    monkeypatch.setitem(R.DEFAULTS, "unavailable", {})     # the lab's hours would make these depend on the clock

    async def url_of(mid, owner):
        return fake_resolve(mid, owner)[0]
    monkeypatch.setattr(R, "_url_of", url_of)
    monkeypatch.setattr(gw, "resolve", fake_resolve)
    monkeypatch.setattr(gw_routes, "_complete", fake_complete)
    yield tmp_path


def settings(tmp_path, **values):
    (tmp_path / "gateway_router.json").write_text(json.dumps(values))


def chat_session(model="auto", sid="chat-1", url=chat_auto.AUTO_URL, history=()):
    s = Session(id=sid, name="", endpoint_url=url, model=model, owner="jaron")
    for role, text in history:
        s.history.append(ChatMessage(role, text))
    return s


def turn(sess, message, **kw):
    return asyncio.run(chat_auto.route_turn(sess, "jaron", message, fake_complete, **kw))


# ── the picker ────────────────────────────────────────────────────────────

def test_the_picker_lists_the_four_auto_models_with_the_routers_descriptions():
    p = chat_auto.picker()
    assert p["url"] == chat_auto.AUTO_URL
    assert [m["id"] for m in p["models"]] == ["auto", "auto:fast", "auto:hard", "auto:remote"]
    assert [m["name"] for m in p["models"]] == ["Auto", "Auto (fast)", "Auto (hard)", "Auto (remote)"]
    assert [m["description"] for m in p["models"]] == [what for _, what in R.model_ids()]


def test_the_picker_offers_nothing_when_the_router_is_off(world):
    settings(world, enabled=False)
    assert chat_auto.picker() is None


def _models_client(monkeypatch):
    monkeypatch.setattr(model_routes, "fetch_models", lambda owner="", is_admin=False: {"hosts": [], "items": [
        {"url": "http://fast/v1/chat/completions", "models": ["qwen3:8b"], "endpoint_id": "ep1",
         "endpoint_name": "Fast", "category": "local"}]})
    app = FastAPI()
    app.include_router(model_routes.setup_model_routes(model_discovery=None))
    return TestClient(app)


def test_api_models_carries_the_auto_entries_beside_the_endpoint_list(monkeypatch):
    client = _models_client(monkeypatch)
    r = client.get("/api/models")
    assert r.status_code == 200
    data = r.json()
    assert [i["endpoint_name"] for i in data["items"]] == ["Fast"]          # the endpoint list is untouched
    assert [m["id"] for m in data["auto"]["models"]] == ["auto", "auto:fast", "auto:hard", "auto:remote"]
    assert "auto" in client.get("/api/models").json()                        # a cached answer has them too


def test_api_models_has_no_auto_entries_when_the_router_is_off(monkeypatch, world):
    settings(world, enabled=False)
    assert "auto" not in _models_client(monkeypatch).get("/api/models").json()


# ── routing a chat's message ──────────────────────────────────────────────

def test_a_message_is_routed_and_resolved_to_the_chosen_models_endpoint():
    sess = chat_session("auto")
    routed = turn(sess, "hard: find out why the server keeps crashing")
    assert routed.model == "qwen3.8:27b" and routed.endpoint_url == "http://strong/v1/chat/completions"
    assert routed.headers == {"Authorization": "Bearer strong-key"}
    assert routed.model_id == STRONG
    assert routed.auto_meta["text"] == "Auto -> qwen3.8:27b (hard, judged from your message)"
    # the chat itself is still on auto, and reads and writes go to it
    assert sess.model == "auto" and sess.endpoint_url == chat_auto.AUTO_URL
    assert routed.id == "chat-1" and routed.get_context_messages() == sess.get_context_messages()
    routed.add_message(ChatMessage("user", "x"))
    assert sess.history[-1].content == "x"
    routed.name = "Named"
    assert sess.name == "Named"
    easy = turn(chat_session("auto", "chat-2"), "easy: rename this variable")
    assert easy.model == "qwen3:8b" and easy.endpoint_url == "http://fast/v1/chat/completions"


def test_the_label_is_kept_per_chat_session_not_per_message_text():
    # chat-1 goes hard, and keeps it though the next message and the history are different (a message hash would
    # start over); a new chat with the same first message is its own conversation
    first = turn(chat_session("auto", "chat-1"), "hard: refactor the payment module")
    assert first.model_id == STRONG
    later = turn(chat_session("auto", "chat-1", history=[("user", "something else entirely"), ("assistant", "ok")]),
                 "easy: what port is this?")
    assert later.model_id == STRONG
    assert later.auto_meta["text"] == "Auto -> qwen3.8:27b (hard, kept for this conversation)"
    other = turn(chat_session("auto", "chat-2"), "easy: what port is this?")
    assert other.model_id == FAST
    assert any(k == "chat:chat-1" for k in R._sticky) and any(k == "chat:chat-2" for k in R._sticky)


def test_a_label_only_moves_up_and_auto_fast_never_uses_the_strong_model():
    a = turn(chat_session("auto:fast", "c"), "easy: what port is this?")
    assert a.model_id == FAST
    b = turn(chat_session("auto:fast", "c"), "hard: refactor the payment module")
    assert b.model_id == FAST                                     # capped
    assert b.auto_meta["text"] == "Auto (fast) -> qwen3:8b (easy, capped at easy by auto:fast, judged from your message)"
    c = turn(chat_session("auto", "c"), "easy: what port is this?")      # the chat now sits at hard
    assert c.model_id == STRONG


def test_auto_hard_always_uses_the_strong_model():
    r = turn(chat_session("auto:hard"), "easy: what port is this?")
    assert r.model_id == STRONG and "always the strong model" in r.auto_meta["text"]


def test_a_plain_question_may_use_the_chat_model_only_when_the_turn_has_no_tools_or_files():
    q = "chat: what does git rebase do?"
    assert turn(chat_session("auto", "a"), q).model_id == R.DEFAULTS["chat"]
    agent = turn(chat_session("auto", "b"), q, tools=True)
    assert agent.model_id == FAST and "this turn uses tools" in agent.auto_meta["text"]
    files = turn(chat_session("auto", "c"), q, has_attachments=True)
    assert files.model_id == FAST and "this turn has attachments" in files.auto_meta["text"]


def test_the_remote_level_resolves_the_chatgpt_model_and_carries_its_reasoning_effort():
    r = turn(chat_session("auto:remote"), "hard: refactor the payment module")
    assert r.model_id == "chatgpt:gpt-6.1-sol" and r.model == "gpt-6.1-sol"
    assert r.effort == "high"
    assert r.auto_meta["text"] == "Auto (remote) -> gpt-6.1-sol (hard, judged from your message, high effort)"
    assert r.auto_meta["effort"] == "high"
    assert turn(chat_session("auto:remote", "z"), "easy: rename this variable").effort == "low"


def test_the_turn_is_counted_against_the_chosen_endpoint_while_it_runs():
    r = turn(chat_session("auto"), "hard: refactor the payment module")
    assert R._inflight.get(R.slug_of(STRONG), 0) == 0
    r.begin(); r.begin()                                     # counted once
    assert R._inflight[R.slug_of(STRONG)] == 1
    r.end(); r.end()
    assert R._inflight[R.slug_of(STRONG)] == 0


def test_a_busy_strong_endpoint_is_skipped_by_the_routers_own_fallback_and_the_label_says_so():
    R.begin(STRONG)                                          # arrietty's one slot is taken
    r = turn(chat_session("auto"), "hard: refactor the payment module")
    assert r.model_id == FAST
    assert r.auto_meta["text"].endswith("a-liberty-11435 is busy)")


# ── failures are shown, never answered by another model ──────────────────

def test_a_model_the_router_picks_that_cannot_be_resolved_is_an_error_naming_it(monkeypatch):
    def lookup(model_id, owner):
        if model_id == STRONG:
            raise LookupError("could not get credentials for that endpoint: gone")
        return fake_resolve(model_id, owner)
    monkeypatch.setattr(gw, "resolve", lookup)
    with pytest.raises(chat_auto.AutoError) as e:
        turn(chat_session("auto:hard"), "hi")
    assert STRONG in str(e.value) and "could not get credentials" in str(e.value) and e.value.status == 502


def test_a_router_failure_is_an_error():
    async def broken(*a, **k):
        raise RuntimeError("boom")
    with pytest.raises(chat_auto.AutoError, match=r"Auto \(fast\) could not choose a model"):
        asyncio.run(chat_auto.route_turn(chat_session("auto:fast"), "jaron", "hi", fake_complete, route=broken))


def test_a_turned_off_router_is_a_clear_error(world):
    settings(world, enabled=False)
    with pytest.raises(chat_auto.AutoError, match="turned off") as e:
        turn(chat_session("auto"), "hi")
    assert e.value.status == 400


def test_only_a_chat_on_the_auto_endpoint_is_an_auto_chat():
    assert chat_auto.is_auto_session(chat_session("auto:hard"))
    assert not chat_auto.is_auto_session(chat_session("auto", url="http://real/v1/chat/completions"))   # a real model named auto
    assert not chat_auto.is_auto_session(chat_session("qwen3:8b"))


def test_rewriting_a_reply_uses_the_model_that_wrote_it_without_routing():
    s = chat_session("auto")
    s.history.append(ChatMessage("assistant", "hi", metadata={"auto": {"model": FAST}}))
    p = chat_auto.pinned_session(s, "jaron")
    assert p.model == "qwen3:8b" and p.endpoint_url == "http://fast/v1/chat/completions"
    assert R._sticky == {}


# ── the chat route, end to end ────────────────────────────────────────────

class FakeSessions:
    def __init__(self, *sessions):
        self.sessions = {s.id: s for s in sessions}
        self.saved = 0

    def get_session(self, sid):
        try:
            return self.sessions[sid]
        except KeyError:
            raise KeyError(sid)

    def save_sessions(self):
        self.saved += 1


class FakeHandler:
    def validate_and_extract_preset(self, preset_id):
        return None, None, None, None

    async def preprocess_message(self, message, att_ids, sess, **kw):
        return message, message, message, [], []

    def update_session_name_if_needed(self, session, message):
        session.name = session.name or "Chat"


class FakeProcessor:
    _last_used_memories = []

    def build_context_preface(self, **kw):
        return [{"role": "system", "content": "You are Odysseus."}], [], []


@pytest.fixture
def app_env(monkeypatch):
    """The real chat routes over fake collaborators; the model call is recorded."""
    from routes import chat_helpers
    calls = []
    during = []

    async def fake_stream(candidates, messages, **kw):
        calls.append({"candidates": list(candidates), "messages": messages, **kw})
        during.append(dict(R._inflight))
        yield 'data: {"delta": "Hello"}\n\n'
        yield 'data: {"delta": " there"}\n\n'
        yield 'data: {"type": "usage", "data": {"input_tokens": 12, "output_tokens": 2}}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_compact(sess, url, model, messages, headers, owner=None):
        return messages, 32768, False

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda request, sid, *a, **k: None)
    monkeypatch.setattr(chat_routes, "set_session_mode", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "get_session_mode", lambda *a, **k: "chat")
    monkeypatch.setattr(chat_routes, "run_post_response_tasks", lambda *a, **k: None)
    monkeypatch.setattr(chat_helpers, "maybe_compact", fake_compact)
    monkeypatch.setattr(chat_helpers, "normalize_model_id", lambda url, model, owner=None: model)
    monkeypatch.setattr(chat_helpers, "_normalize_model_id_from_cache", lambda sess: None)
    monkeypatch.setattr("core.database.update_session_last_accessed", lambda sid: None)

    def make(*sessions):
        sm = FakeSessions(*sessions)
        app = FastAPI()
        app.include_router(chat_routes.setup_chat_routes(sm, FakeHandler(), FakeProcessor(), None, None, None))
        return TestClient(app), sm
    make.calls, make.during = calls, during
    return make


def sse_events(text):
    out = []
    for block in text.split("\n\n"):
        if block.startswith("data: ") and not block.startswith("data: [DONE]"):
            try:
                out.append(json.loads(block[6:]))
            except ValueError:
                pass
    return out


def send(client, sid, message, mode="chat"):
    return client.post("/api/chat_stream", data={"message": message, "session": sid, "mode": mode})


def test_a_chat_on_auto_streams_from_the_chosen_endpoint_and_saves_the_label(app_env):
    sess = chat_session("auto")
    client, sm = app_env(sess)
    r = send(client, "chat-1", "hard: find out why the server keeps crashing")
    assert r.status_code == 200
    events = sse_events(r.text)

    # streamed from the real model's endpoint, exactly as if it had been picked, with no other model to fall to
    call = app_env.calls[0]
    assert call["candidates"] == [("http://strong/v1/chat/completions", "qwen3.8:27b", {"Authorization": "Bearer strong-key"})]
    assert "reasoning_effort" not in call
    assert call["owner"] is None and call["session_id"] == "chat-1"

    # the page is told the model and why, early
    info = next(e for e in events if e.get("type") == "model_info")
    assert info["model"] == "qwen3.8:27b"
    label = next(e for e in events if e.get("type") == "auto_route")["data"]
    assert label["text"] == "Auto -> qwen3.8:27b (hard, judged from your message)"
    assert "".join(e.get("delta", "") for e in events) == "Hello there"

    # usage is attributed to the real model, and the reply is saved with the label
    metrics = next(e for e in events if e.get("type") == "metrics")["data"]
    assert metrics["model"] == "qwen3.8:27b" and metrics["requested_model"] == "qwen3.8:27b"
    reply = sess.history[-1]
    assert reply.role == "assistant" and reply.content == "Hello there"
    assert reply.metadata["auto"]["text"] == label["text"] and reply.metadata["model"] == "qwen3.8:27b"
    assert reply.to_dict()["metadata"]["auto"]["choice"] == "auto"
    assert [m.role for m in sess.history] == ["user", "assistant"]

    # the chat is still on auto, and the turn was counted against the endpoint only while it ran
    assert sess.model == "auto" and sess.endpoint_url == chat_auto.AUTO_URL and sess.headers == {}
    assert app_env.during[0].get("a-liberty-11435") == 1
    assert R._inflight.get("a-liberty-11435", 0) == 0


def test_the_next_message_of_the_chat_keeps_the_label_and_the_model(app_env):
    sess = chat_session("auto")
    client, _ = app_env(sess)
    send(client, "chat-1", "hard: refactor the payment module")
    r = send(client, "chat-1", "easy: what port is this?")
    label = next(e for e in sse_events(r.text) if e.get("type") == "auto_route")["data"]
    assert label["text"] == "Auto -> qwen3.8:27b (hard, kept for this conversation)"
    assert app_env.calls[1]["candidates"][0][1] == "qwen3.8:27b"
    assert [m.metadata.get("auto", {}).get("text") for m in sess.history if m.role == "assistant"] == [
        "Auto -> qwen3.8:27b (hard, judged from your message)",
        "Auto -> qwen3.8:27b (hard, kept for this conversation)"]


def test_the_remote_level_streams_with_its_reasoning_effort(app_env):
    client, _ = app_env(chat_session("auto:remote"))
    send(client, "chat-1", "hard: refactor the payment module")
    call = app_env.calls[0]
    assert call["candidates"][0][:2] == ("https://chatgpt.test/backend-api/codex", "gpt-6.1-sol")
    assert call["reasoning_effort"] == "high"


def test_agent_mode_runs_the_agent_loop_on_the_chosen_model(app_env, monkeypatch):
    seen = {}

    async def fake_loop(url, model, messages, **kw):
        seen.update(url=url, model=model, kw=kw)
        yield 'data: {"delta": "done"}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(chat_routes, "stream_agent_loop", fake_loop)
    sess = chat_session("auto:remote")
    client, _ = app_env(sess)
    r = send(client, "chat-1", "chat: what does git rebase do?", mode="agent")
    assert r.status_code == 200
    assert seen["url"] == "https://chatgpt.test/backend-api/codex" and seen["model"] == "gpt-6.1-terra"   # not the chat model: tools
    assert seen["kw"]["reasoning_effort"] == "low" and seen["kw"]["fallbacks"] == []
    assert seen["kw"]["headers"] == {"Authorization": "Bearer chatgpt"}
    assert "this turn uses tools" in sess.history[-1].metadata["auto"]["text"]


def test_a_failed_router_or_endpoint_is_an_error_and_nothing_else_answers(app_env, monkeypatch):
    sess = chat_session("auto:hard")
    client, _ = app_env(sess)

    def lookup(model_id, owner):
        raise LookupError("no model; the endpoint is gone")
    monkeypatch.setattr(gw, "resolve", lookup)
    r = send(client, "chat-1", "hello")
    msg = r.json()["detail"]["message"]
    assert r.status_code == 502 and r.json()["detail"]["error"] == "AUTO_MODEL"
    assert "cannot be used" in msg and "the endpoint is gone" in msg
    assert app_env.calls == [] and sess.history == []               # no model was asked, and the message was not taken

    monkeypatch.setattr(gw, "resolve", fake_resolve)

    async def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(R, "route", broken)
    r = send(client, "chat-1", "hello")
    assert r.status_code == 502 and "could not choose a model" in r.json()["detail"]["message"]
    assert app_env.calls == [] and R._inflight.get("a-liberty-11435", 0) == 0


def test_the_router_turned_off_is_a_clear_400(app_env, world):
    settings(world, enabled=False)
    client, _ = app_env(chat_session("auto"))
    r = send(client, "chat-1", "hello")
    assert r.status_code == 400 and "turned off" in r.json()["detail"]["message"]


def test_an_ordinary_chat_is_untouched(app_env):
    sess = chat_session("qwen3:8b", url="http://fast/v1/chat/completions")
    client, _ = app_env(sess)
    r = send(client, "chat-1", "hello")
    assert r.status_code == 200
    assert app_env.calls[0]["candidates"][0][:2] == ("http://fast/v1/chat/completions", "qwen3:8b")
    assert not any(e.get("type") == "auto_route" for e in sse_events(r.text))
    assert "auto" not in (sess.history[-1].metadata or {}) and R._sticky == {}


def test_an_auto_chat_is_not_cleared_as_an_orphan_of_a_deleted_endpoint():
    sess = chat_session("auto")
    assert chat_routes._clear_orphaned_session_endpoint(sess, owner="jaron") is False
    assert sess.model == "auto" and sess.endpoint_url == chat_auto.AUTO_URL


def test_a_non_admin_may_pick_auto_without_a_registered_endpoint():
    from fastapi import HTTPException
    from routes import session_routes as sr

    class Req:
        class app:
            class state:
                auth_manager = None
    sr._reject_raw_endpoint_url_for_non_admin(Req, "alice", "", chat_auto.AUTO_URL)          # allowed
    with pytest.raises(HTTPException):
        sr._reject_raw_endpoint_url_for_non_admin(Req, "alice", "", "http://10.0.0.1/v1")    # a raw URL still is not


def test_the_gateway_still_routes_by_message_hash_and_logs_the_same_decision(world):
    """/v1 keeps its conversation key (the first system prompt and user message); nothing it sends changes."""
    payload = {"model": "auto", "messages": [{"role": "system", "content": "opencode"},
                                             {"role": "user", "content": "hard: fix it"}]}
    out, d = asyncio.run(R.route(payload, "jaron", fake_complete))
    assert out["model"] == STRONG and d["conversation"] == R.conversation_key(payload["messages"])[:12]
    assert R.conversation_key(payload["messages"]) in R._sticky


# ── the label shown under a reply, and the reasoning effort the provider layer sends ──

def test_the_picker_and_the_reply_label_are_wired_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    picker = (root / "static/js/modelPicker.js").read_text(encoding="utf-8")
    models = (root / "static/js/models.js").read_text(encoding="utf-8")
    renderer = (root / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    chat = (root / "static/js/chat.js").read_text(encoding="utf-8")
    assert "data.auto" in models and "getAutoModels" in models
    assert "_addSection('Auto')" in picker and "getAutoModels" in picker
    # Auto is shown above Favorites, and the "first model" defaults never pick it (it is not in the endpoint list)
    assert picker.index("_addSection('Auto')") < picker.index("_addSection('Favorites')")
    assert "export function setAutoRouteNote" in renderer
    assert "setAutoRouteNote(wrap, metadata.auto)" in renderer and "setAutoRouteNote(firstMsgAi, metadata.auto)" in renderer
    assert "json.type === 'auto_route'" in chat and "setAutoRouteNote(holder, json.data)" in chat
    # a failure of the Auto model is shown as the server words it, not as "this model doesn't support agent tools"
    assert "j.detail.error === 'AUTO_MODEL'" in chat and "!_autoModelError && (errText.includes('tool')" in chat


def test_reasoning_effort_reaches_the_provider_payloads(monkeypatch):
    from src import llm_core

    sent = {}

    class Resp:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class Client:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            sent["url"], sent["json"] = url, json
            return Resp()

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: Client())

    async def run(url, model, effort):
        async for _ in llm_core.stream_llm(url, model, [{"role": "user", "content": "hi"}], reasoning_effort=effort,
                                           headers={"Authorization": "Bearer k"}):
            pass
    asyncio.run(run("http://strong/v1/chat/completions", "qwen3.8:27b", "high"))
    assert sent["json"]["reasoning_effort"] == "high"
    asyncio.run(run("http://strong/v1/chat/completions", "qwen3.8:27b", None))
    assert "reasoning_effort" not in sent["json"]


def test_a_conversation_too_long_for_every_model_is_an_error_for_the_person():
    class Sess:
        id, model, history, context = "c-long", "auto", [], []

    async def router(payload, owner, complete, **kw):
        return payload, {"error": "This conversation is about 300000 tokens, more than the maximum context length",
                         "status": 400, "code": "context_length_exceeded", "model": ""}
    with pytest.raises(chat_auto.AutoError) as e:
        asyncio.run(chat_auto.route_turn(Sess(), "jaron", "hi", None, route=router))
    assert e.value.status == 400 and "maximum context length" in str(e.value)
