"""The OpenAI-compatible gateway over Settings > Models: which models are listed, how a request is routed, and that
the answer (plain or streamed) comes back as the model's own server sent it. In-memory database and a fake upstream;
no network."""
import datetime
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ModelEndpoint
from routes import openai_gateway_routes as routes
from src import openai_gateway as gw
from src.openai_translate import Translator


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(gw, "SessionLocal", factory)
    s = factory()
    s.add_all([
        ModelEndpoint(id="ep-ollama", name="Local Ollama", base_url="http://10.0.0.5:11434/v1", api_key=None,
                      created_at=datetime.datetime(2026, 1, 1), updated_at=datetime.datetime(2026, 1, 1),
                      cached_models=json.dumps(["qwen3:8b", "nomic-embed-text", "llama3.1:latest"]),
                      hidden_models=json.dumps(["llama3.1:latest"])),
        ModelEndpoint(id="ep-router", name="OpenRouter", base_url="https://openrouter.ai/api/v1", api_key="sk-or-secret",
                      cached_models=json.dumps(["openai/gpt-4o"]), pinned_models=json.dumps(["meta/llama-3"])),
        ModelEndpoint(id="ep-claude", name="Anthropic", base_url="https://api.anthropic.com/v1", api_key="sk-ant",
                      cached_models=json.dumps(["claude-x"])),
        ModelEndpoint(id="ep-off", name="Disabled box", base_url="http://10.0.0.9/v1", is_enabled=False,
                      cached_models=json.dumps(["nope"])),
        ModelEndpoint(id="ep-img", name="Images", base_url="http://10.0.0.7/v1", model_type="image",
                      cached_models=json.dumps(["sdxl"])),
        ModelEndpoint(id="ep-bob", name="Bob private", base_url="http://10.0.0.8/v1", owner="bob",
                      cached_models=json.dumps(["bobs-model"])),
        ModelEndpoint(id="ep-dup", name="local ollama", base_url="http://10.0.0.6:11434/v1",
                      cached_models=json.dumps(["qwen3:8b"])),
    ])
    s.commit()
    s.close()
    yield factory
    engine.dispose()


def ids(owner=None):
    return [i["id"] for i in gw.catalog(owner)]


def test_catalog_lists_only_usable_chat_models(db):
    got = ids("alice")
    assert "local-ollama:qwen3:8b" in got
    assert "openrouter:openai/gpt-4o" in got and "openrouter:meta/llama-3" in got      # cached and pinned
    assert not any("llama3.1" in i for i in got)             # hidden on the endpoint
    assert not any("embed" in i for i in got)                # not a chat model
    assert "anthropic:claude-x" in got                        # translated, so listed too
    assert not any(i.startswith(("disabled-box:", "images:", "bob-private:")) for i in got)
    assert "bob-private:bobs-model" in ids("bob")             # a private endpoint is its owner's only
    slugs = {i.split(":")[0] for i in got}
    assert len([s for s in slugs if s.startswith("local-ollama")]) == 2    # same name twice: second gets a suffix


def test_resolve_routes_to_the_endpoint_with_its_own_key(db):
    url, headers, model, translate = gw.resolve("openrouter:openai/gpt-4o", "alice")
    assert url == "https://openrouter.ai/api/v1/chat/completions" and model == "openai/gpt-4o" and not translate
    assert headers["Authorization"] == "Bearer sk-or-secret"
    url, headers, model, translate = gw.resolve("local-ollama:qwen3:8b", "alice")
    assert url == "http://10.0.0.5:11434/v1/chat/completions" and model == "qwen3:8b" and "Authorization" not in headers
    assert not translate
    assert gw.resolve("anthropic:claude-x", "alice")[3] is True               # needs translating


def test_resolve_bare_names_and_errors(db):
    assert gw.resolve("openai/gpt-4o", "alice")[2] == "openai/gpt-4o"      # only one endpoint has it
    with pytest.raises(LookupError, match="several endpoints"):
        gw.resolve("qwen3:8b", "alice")                                      # two endpoints have it
    assert gw.resolve("claude-x", "alice")[2] == "claude-x"                  # Anthropic is listed now
    with pytest.raises(LookupError, match="no model"):
        gw.resolve("not-a-model", "alice")
    with pytest.raises(LookupError):
        gw.resolve("bobs-model", "alice")                                    # someone else's endpoint
    with pytest.raises(LookupError):
        gw.resolve("", "alice")


class Upstream:
    """A fake model server: records the request and answers with a fixed reply."""
    def __init__(self, stream=False):
        self.seen = []
        self.stream = stream

    def __call__(self, request: httpx.Request):
        self.seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        if self.stream:
            sse = b'data: {"choices":[{"delta":{"content":"he"}}]}\n\ndata: {"choices":[{"delta":{"content":"llo"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "x", "choices": [{"message": {"role": "assistant", "content": "hello"}}]})


@pytest.fixture
def client(db, monkeypatch):
    holder = {}

    def mk(handler):
        monkeypatch.setattr(routes, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        holder["handler"] = handler

    app = FastAPI()
    app.include_router(routes.setup_openai_gateway_routes())

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        if scopes is not None:
            request.state.api_token = True
            request.state.api_token_scopes = [s for s in scopes.split(",") if s]
            request.state.api_token_owner = request.headers.get("x-test-owner") or None
        return await call_next(request)

    c = TestClient(app)
    c.mk = mk
    return c


def test_models_endpoint_and_scope(client):
    r = client.get("/v1/models", headers={"x-test-scopes": "chat", "x-test-owner": "alice"})
    assert r.status_code == 200 and r.json()["object"] == "list"
    got = {m["id"]: m["owned_by"] for m in r.json()["data"]}
    assert got["openrouter:openai/gpt-4o"] == "OpenRouter"
    r = client.get("/v1/models", headers={"x-test-scopes": "todos:read"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
    assert client.get("/v1/models").status_code == 200                       # a browser session needs no scope


def test_chat_is_passed_on_with_the_endpoints_key_not_the_callers(client):
    up = Upstream()
    client.mk(up)
    body = {"model": "openrouter:openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}], "temperature": 0.2}
    r = client.post("/v1/chat/completions", json=body, headers={"x-test-scopes": "chat", "authorization": "Bearer ody_callerstoken"})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello"
    url, headers, sent = up.seen[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["authorization"] == "Bearer sk-or-secret"                  # the endpoint's key; ody_ token never goes out
    assert sent["model"] == "openai/gpt-4o" and sent["tools"] == body["tools"] and sent["temperature"] == 0.2


def test_streaming_comes_back_as_it_was_sent(client):
    client.mk(Upstream(stream=True))
    r = client.post("/v1/chat/completions", headers={"x-test-scopes": "chat"},
                    json={"model": "local-ollama:qwen3:8b", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.text.count("data:") == 3 and r.text.rstrip().endswith("[DONE]")


def test_errors_are_openai_shaped(client):
    client.mk(Upstream())
    h = {"x-test-scopes": "chat"}
    r = client.post("/v1/chat/completions", headers=h, json={"model": "nope", "messages": []})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    assert client.post("/v1/chat/completions", headers=h, json={"model": "x"}).status_code == 400
    assert client.post("/v1/chat/completions", headers=h, content=b"not json").status_code == 400
    assert client.post("/v1/chat/completions", headers={"x-test-scopes": "memory:read"},
                       json={"model": "x", "messages": []}).status_code == 403

    def down(request):
        raise httpx.ConnectError("refused")
    client.mk(down)
    r = client.post("/v1/chat/completions", headers=h, json={"model": "local-ollama:qwen3:8b", "messages": []})
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unreachable"


def test_an_upstream_error_status_passes_through(client):
    client.mk(lambda req: httpx.Response(429, json={"error": {"message": "slow down"}}))
    r = client.post("/v1/chat/completions", headers={"x-test-scopes": "chat"},
                    json={"model": "local-ollama:qwen3:8b", "messages": []})
    assert r.status_code == 429 and r.json()["error"]["message"] == "slow down"


# ---- translated models: Anthropic, ChatGPT sign-in, Copilot, native Ollama, add-ons ----

def sse(*events):
    return [e if isinstance(e, str) else "data: " + json.dumps(e) + "\n\n" for e in events]


def fake_llm(monkeypatch, chunks):
    seen = {}

    async def gen(url, model, messages, **kw):
        seen.update(url=url, model=model, messages=messages, kw=kw)
        for c in chunks:
            yield c

    monkeypatch.setattr(routes, "_llm_stream", gen)
    return seen


H = {"x-test-scopes": "chat", "x-test-owner": "alice"}


def test_translator_streams_text_thinking_tools_and_usage():
    t = Translator("m", include_usage=True)
    out = []
    for c in sse({"delta": "why", "thinking": True}, {"delta": "hi"},
                 {"type": "tool_calls", "calls": [{"id": "c1", "name": "f", "arguments": "{\"a\":1}"}]},
                 {"type": "usage", "data": {"input_tokens": 3, "output_tokens": 4}}, "data: [DONE]\n\n"):
        out += t.feed(c)
    objs = [json.loads(o[6:]) for o in out if not o.startswith("data: [DONE]")]
    assert objs[0]["choices"][0]["delta"]["role"] == "assistant"
    assert objs[1]["choices"][0]["delta"] == {"reasoning_content": "why"}
    assert objs[2]["choices"][0]["delta"] == {"content": "hi"}
    call = objs[3]["choices"][0]["delta"]["tool_calls"][0]
    assert call["index"] == 0 and call["id"] == "c1" and call["function"] == {"name": "f", "arguments": "{\"a\":1}"}
    assert objs[4]["choices"][0]["finish_reason"] == "tool_calls"
    assert objs[5]["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert out[-1] == "data: [DONE]\n\n"


def test_translated_chat_non_streaming(client, monkeypatch):
    seen = fake_llm(monkeypatch, sse({"delta": "he"}, {"delta": "llo"},
                                     {"type": "usage", "data": {"input_tokens": 1, "output_tokens": 2}}, "data: [DONE]\n\n"))
    body = {"model": "anthropic:claude-x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50,
            "temperature": 0.1, "tools": [{"type": "function", "function": {"name": "f"}}]}
    r = client.post("/v1/chat/completions", json=body, headers=H)
    assert r.status_code == 200
    j = r.json()
    assert j["object"] == "chat.completion" and j["choices"][0]["message"]["content"] == "hello"
    assert j["choices"][0]["finish_reason"] == "stop" and j["usage"]["total_tokens"] == 3
    assert seen["model"] == "claude-x" and seen["url"].startswith("https://api.anthropic.com")
    assert seen["kw"]["max_tokens"] == 50 and seen["kw"]["temperature"] == 0.1 and seen["kw"]["owner"] == "alice"
    assert seen["kw"]["tools"] == body["tools"]
    assert "ody_" not in json.dumps(seen["kw"]["headers"])


def test_translated_chat_streaming(client, monkeypatch):
    fake_llm(monkeypatch, sse({"delta": "he"}, {"delta": "llo"}, "data: [DONE]\n\n"))
    r = client.post("/v1/chat/completions", headers=H,
                    json={"model": "anthropic:claude-x", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert '"content": "he"' in r.text and '"content": "llo"' in r.text and r.text.rstrip().endswith("[DONE]")
    assert r.text.count("[DONE]") == 1


def test_translated_error_before_output_is_an_http_error(client, monkeypatch):
    fake_llm(monkeypatch, ['event: error\ndata: {"status": 401, "text": "bad key"}\n\n'])
    for stream in (False, True):
        r = client.post("/v1/chat/completions", headers=H,
                        json={"model": "anthropic:claude-x", "messages": [{"role": "user", "content": "hi"}], "stream": stream})
        assert r.status_code == 401 and r.json()["error"]["message"] == "bad key"


def test_translated_error_mid_stream_ends_cleanly(client, monkeypatch):
    fake_llm(monkeypatch, sse({"delta": "part"}, 'event: error\ndata: {"status": 500, "text": "boom"}\n\n'))
    r = client.post("/v1/chat/completions", headers=H,
                    json={"model": "anthropic:claude-x", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 200 and '"message": "boom"' in r.text and r.text.rstrip().endswith("[DONE]")


def test_auto_is_listed_and_routed_to_the_model_the_router_picks(client, monkeypatch, tmp_path):
    """The auto model (src/gateway_router.py): listed first, and sent where the router says, with the reason."""
    import src.constants as const
    from src import gateway_router as router
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))

    async def fake_route(payload, owner, complete, **kw):
        return dict(payload, model="local-ollama:qwen3:8b"), {"model": "local-ollama:qwen3:8b", "reason": "easy (embeddings)"}
    monkeypatch.setattr(router, "route", fake_route)
    up = Upstream()
    client.mk(up)
    ids_ = [m["id"] for m in client.get("/v1/models", headers={"x-test-scopes": "chat"}).json()["data"]]
    assert ids_[0] == "auto"
    r = client.post("/v1/chat/completions", headers={"x-test-scopes": "chat"},
                    json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.headers["x-odysseus-route"].startswith("local-ollama:qwen3:8b; easy")
    assert up.seen[0][2]["model"] == "qwen3:8b"
    assert router._inflight.get("local-ollama") == 0          # counted while it ran, released after


def test_gateway_is_exempt_from_the_45_second_request_limit():
    """A model can take longer than 45s to send its first byte (loading, reading a long prompt); app.py's hard request
    timeout must not cut /v1 requests off. Read from the source so the test does not have to boot the whole app."""
    import ast
    import pathlib
    tree = ast.parse((pathlib.Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(x, "id", "") == "_TIMEOUT_EXEMPT_PREFIXES" for x in node.targets):
            assert "/v1/" in [e.value for e in node.value.elts]
            return
    raise AssertionError("_TIMEOUT_EXEMPT_PREFIXES not found in app.py")
