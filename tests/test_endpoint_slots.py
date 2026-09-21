"""Do not pile more generations onto a model server that is already full.

The 27b was not crashed, it was maxed out -- and Odysseus had nothing
to stop it adding to that. httpx is configured with
max_connections=100 and nothing limited how many generations were in
flight against one endpoint. A single GPU serving a large model at a
long context has room for very few concurrent sequences; past that the
server accepts the request and queues it, which is indistinguishable
from working right up until the tokens never arrive.

Slots make that visible: over the limit a caller waits briefly and then
gets a plain "busy" error. It is emitted as a pre-content error, the
shape the fallback chain already retries on, so a busy primary moves to
the next candidate instead of stalling the turn.
"""
import asyncio
import inspect
import json

import pytest

import src.llm_core as llm


def test_one_semaphore_per_host():
    a = llm._endpoint_slot("http://10.0.0.1:8114/v1/chat/completions")
    b = llm._endpoint_slot("http://10.0.0.1:8114/v1/models")
    c = llm._endpoint_slot("http://10.0.0.2:8114/v1/chat/completions")
    assert a is b, "paths on one host must share the host's slots"
    assert a is not c, "separate hosts must not share a budget"


def test_the_limit_is_not_one():
    """Compare panes and background extraction are legitimately parallel."""
    assert llm.MAX_CONCURRENT_PER_ENDPOINT >= 2


def test_every_caller_goes_through_the_gate():
    """Wrapped at stream_llm, not at one call site.

    Background extraction passes use the same door as a chat turn, and
    they are part of what fills a local server up; gating only the chat
    path would leave the rest uncounted.
    """
    assert inspect.isasyncgenfunction(llm.stream_llm)
    assert inspect.isasyncgenfunction(llm._stream_llm_inner)
    src = inspect.getsource(llm.stream_llm)
    assert "_endpoint_slot(url)" in src
    assert "sem.release()" in src and "finally:" in src, (
        "a slot not released on every exit leaks until restart"
    )


def _drain(agen):
    async def go():
        return [c async for c in agen]
    return asyncio.run(go())


def test_a_busy_endpoint_reports_instead_of_queueing(monkeypatch):
    url = "http://10.0.0.9:8114/v1/chat/completions"
    # Empty the host's budget and make the wait instant.
    monkeypatch.setattr(llm, "ENDPOINT_SLOT_WAIT_S", 0.01)
    sem = asyncio.Semaphore(0)
    monkeypatch.setitem(llm._ENDPOINT_SLOTS, llm._host_key(url), sem)

    async def _never(*a, **k):
        raise AssertionError("upstream was called despite no free slot")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "_stream_llm_inner", _never)

    chunks = _drain(llm.stream_llm(url, "m", [{"role": "user", "content": "hi"}]))
    assert chunks, "a busy endpoint returned nothing at all"
    assert chunks[0].startswith("event: error"), (
        "busy must be a pre-content error so the fallback chain retries"
    )
    payload = json.loads(chunks[0].split("data: ", 1)[1])
    assert payload["status"] == 503
    assert "busy" in payload["error"].lower()


def test_the_slot_is_released_after_a_normal_stream(monkeypatch):
    url = "http://10.0.0.10:8114/v1/chat/completions"
    llm._ENDPOINT_SLOTS.pop(llm._host_key(url), None)

    async def _ok(*a, **k):
        yield 'data: {"delta": "hi"}\n\n'

    monkeypatch.setattr(llm, "_stream_llm_inner", _ok)
    out = _drain(llm.stream_llm(url, "m", []))
    assert out == ['data: {"delta": "hi"}\n\n']
    sem = llm._endpoint_slot(url)
    assert sem._value == llm.MAX_CONCURRENT_PER_ENDPOINT, (
        "the slot was not returned; the endpoint leaks capacity per call"
    )


def test_the_slot_is_released_when_the_stream_raises(monkeypatch):
    """A failing generation must not permanently cost the endpoint a slot."""
    url = "http://10.0.0.11:8114/v1/chat/completions"
    llm._ENDPOINT_SLOTS.pop(llm._host_key(url), None)

    async def _boom(*a, **k):
        raise RuntimeError("upstream died")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "_stream_llm_inner", _boom)
    with pytest.raises(RuntimeError):
        _drain(llm.stream_llm(url, "m", []))
    sem = llm._endpoint_slot(url)
    assert sem._value == llm.MAX_CONCURRENT_PER_ENDPOINT, (
        "a crashed generation leaked its slot"
    )
