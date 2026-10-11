"""Self-hosted Ollama gets a real num_ctx.

Ollama runs a model at its server default window (4096 tokens on 0.6+)
unless each request sends options.num_ctx, and its OpenAI-style /v1 surface
ignores that option. Odysseus sends chat calls for an Ollama /v1 URL to the
native /api/chat endpoint, with num_ctx from the model's /api/show
context_length, capped (ODYSSEUS_OLLAMA_MAX_CTX / ollama_max_num_ctx, 32768
by default). Company runs go stream_agent_loop -> stream_llm, the path tested
here. No model server is contacted.
"""
import asyncio

import httpx
import pytest

from src import llm_core
from src import model_context as mc

V1 = "http://100.102.86.125:11434/v1/chat/completions"
NATIVE = "http://100.102.86.125:11434/api/chat"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    mc._ollama_show_cache.clear()
    monkeypatch.delenv("ODYSSEUS_OLLAMA_MAX_CTX", raising=False)
    monkeypatch.delenv("ODYSSEUS_OLLAMA_V1_NATIVE", raising=False)
    monkeypatch.setattr(mc, "_configured_endpoint_kind", lambda url: None)
    yield
    mc._ollama_show_cache.clear()


def _fake_show(monkeypatch, ctx_by_model, calls=None):
    def fake_post(url, json=None, timeout=None, **kw):
        if calls is not None:
            calls.append((url, json))
        req = httpx.Request("POST", url)
        ctx = ctx_by_model.get((json or {}).get("model"))
        if ctx is None:
            return httpx.Response(404, request=req, json={"error": "model not found"})
        return httpx.Response(200, request=req, json={"model_info": {
            "general.architecture": "qwen3", "qwen3.context_length": ctx, "qwen3.embedding_length": 4096}})
    monkeypatch.setattr(mc.httpx, "post", fake_post)


def test_num_ctx_comes_from_api_show_capped_at_the_max(monkeypatch):
    calls = []
    _fake_show(monkeypatch, {"qwen3:8b": 40960, "tiny:1b": 8192}, calls)
    assert mc.ollama_num_ctx(V1, "qwen3:8b") == 32768               # 40960 capped at the default max
    assert calls[0] == ("http://100.102.86.125:11434/api/show", {"model": "qwen3:8b"})
    assert mc.ollama_num_ctx(NATIVE, "tiny:1b") == 8192             # below the cap: the model's own
    monkeypatch.setenv("ODYSSEUS_OLLAMA_MAX_CTX", "65536")
    assert mc.ollama_num_ctx(V1, "qwen3:8b") == 40960
    # Cached: /api/show is asked once per model.
    assert len([c for c in calls if c[1]["model"] == "qwen3:8b"]) == 1


def test_max_can_come_from_settings(monkeypatch):
    _fake_show(monkeypatch, {"qwen3:8b": 40960})
    from src import settings
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: 16384 if k == "ollama_max_num_ctx" else d)
    assert mc.ollama_max_ctx() == 16384
    assert mc.ollama_num_ctx(V1, "qwen3:8b") == 16384


def test_unknown_model_without_show_sends_nothing(monkeypatch):
    _fake_show(monkeypatch, {})
    assert mc.ollama_num_ctx(V1, "mystery-model") is None
    # Known table as a fallback, still capped.
    assert mc.ollama_num_ctx(V1, "qwen3:14b") == 32768


def test_get_context_length_reports_the_window_ollama_really_gets(monkeypatch):
    _fake_show(monkeypatch, {"qwen3:8b": 40960})
    assert mc.get_context_length(V1, "qwen3:8b") == 32768
    assert mc.get_context_length(NATIVE, "qwen3:8b") == 32768


def test_ollama_url_detection():
    assert mc.is_local_ollama_url(V1) and mc.is_local_ollama_url(NATIVE)
    assert mc.is_local_ollama_url("http://localhost:11434")
    assert not mc.is_local_ollama_url("http://localhost:8000/v1/chat/completions")   # vLLM / llama.cpp
    assert not mc.is_local_ollama_url("https://ollama.com/api/chat")
    assert not mc.is_local_ollama_url("https://api.openai.com/v1/chat/completions")
    assert mc.ollama_root("http://h:11434/v1/chat/completions") == "http://h:11434"
    assert mc.ollama_root("http://h:11434/proxy/api/chat") == "http://h:11434/proxy"


def _stream_capture(monkeypatch, url, model="qwen3:8b"):
    """Run stream_llm up to the HTTP call and return what it would send."""
    seen = {}
    real = llm_core._build_ollama_payload

    def spy(*a, **kw):
        p = real(*a, **kw)
        seen["payload"] = p
        return p

    def dead(target):
        seen["url"] = target
        return True
    monkeypatch.setattr(llm_core, "_build_ollama_payload", spy)
    monkeypatch.setattr(llm_core, "_is_host_dead", dead)

    async def go():
        return [c async for c in llm_core.stream_llm(url, model, [{"role": "user", "content": "hi"}],
                                                     max_tokens=7)]
    asyncio.run(go())
    return seen


def test_stream_to_ollama_v1_goes_native_with_num_ctx(monkeypatch):
    _fake_show(monkeypatch, {"qwen3:8b": 40960})
    seen = _stream_capture(monkeypatch, V1)
    assert seen["url"] == NATIVE
    assert seen["payload"]["options"]["num_ctx"] == 32768
    assert seen["payload"]["think"] is False             # same thinking suppression as the /v1 path


def test_stream_to_native_ollama_sends_num_ctx(monkeypatch):
    _fake_show(monkeypatch, {"qwen3:8b": 40960})
    seen = _stream_capture(monkeypatch, NATIVE)
    assert seen["url"] == NATIVE and seen["payload"]["options"]["num_ctx"] == 32768


def test_other_v1_servers_are_left_alone(monkeypatch):
    _fake_show(monkeypatch, {"qwen3:8b": 40960})
    seen = _stream_capture(monkeypatch, "http://localhost:8000/v1/chat/completions")
    assert seen["url"] == "http://localhost:8000/v1/chat/completions" and "payload" not in seen


def test_native_routing_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_OLLAMA_V1_NATIVE", "0")
    _fake_show(monkeypatch, {"qwen3:8b": 40960})
    seen = _stream_capture(monkeypatch, V1)
    assert seen["url"] == V1 and "payload" not in seen


def test_llm_call_async_to_ollama_v1_goes_native(monkeypatch):
    _fake_show(monkeypatch, {"qwen3:8b": 40960})
    sent = {}

    class FakeClient:
        async def post(self, url, headers=None, json=None, timeout=None):
            sent["url"], sent["json"] = url, json
            return httpx.Response(200, request=httpx.Request("POST", url),
                                  json={"message": {"content": "OK"}, "done": True,
                                        "prompt_eval_count": 12, "eval_count": 1})
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: FakeClient())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    out = asyncio.run(llm_core.llm_call_async(V1, "qwen3:8b", [{"role": "user", "content": "unique-ctx-test"}]))
    assert out == "OK"
    assert sent["url"] == NATIVE and sent["json"]["options"]["num_ctx"] == 32768
