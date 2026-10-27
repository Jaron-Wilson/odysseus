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
    assert not any(i.startswith(("anthropic:", "disabled-box:", "images:", "bob-private:")) for i in got)
    assert "bob-private:bobs-model" in ids("bob")             # a private endpoint is its owner's only
    slugs = {i.split(":")[0] for i in got}
    assert len([s for s in slugs if s.startswith("local-ollama")]) == 2    # same name twice: second gets a suffix


def test_resolve_routes_to_the_endpoint_with_its_own_key(db):
    url, headers, model = gw.resolve("openrouter:openai/gpt-4o", "alice")
    assert url == "https://openrouter.ai/api/v1/chat/completions" and model == "openai/gpt-4o"
    assert headers["Authorization"] == "Bearer sk-or-secret"
    url, headers, model = gw.resolve("local-ollama:qwen3:8b", "alice")
    assert url == "http://10.0.0.5:11434/v1/chat/completions" and model == "qwen3:8b" and "Authorization" not in headers


def test_resolve_bare_names_and_errors(db):
    assert gw.resolve("openai/gpt-4o", "alice")[2] == "openai/gpt-4o"      # only one endpoint has it
    with pytest.raises(LookupError, match="several endpoints"):
        gw.resolve("qwen3:8b", "alice")                                      # two endpoints have it
    with pytest.raises(LookupError, match="no model"):
        gw.resolve("claude-x", "alice")                                      # Anthropic is not exposed
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
