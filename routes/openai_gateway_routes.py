"""OpenAI-compatible gateway: GET /v1/models and POST /v1/chat/completions.

Point any OpenAI-compatible client at ``<odysseus>/v1`` with a ``ody_`` API token (Settings > API tokens, "chat" scope)
and it can use every chat model added in Settings > Models. The model list and routing are in src/openai_gateway.py;
this file only checks the token and gets the answer back to the caller: passed straight through for servers that speak
the OpenAI format, or translated (src/openai_translate.py) for Anthropic, ChatGPT sign-in, Copilot, native Ollama and
add-on models, which go through the same provider code a chat turn uses.
Requests are never logged with their content, and the caller's token is never sent upstream.
"""
import asyncio
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from src import openai_gateway as gw
from src.openai_translate import Translator
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15
READ_TIMEOUT_S = 900     # a long generation on a slow local model can be quiet for a while


def _error(status: int, message: str, code: str = None):
    kind = "invalid_request_error" if status < 500 else "api_error"
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": kind, "code": code}})


def _allowed(request: Request) -> bool:
    """A bearer token needs the chat scope; a signed-in browser session is fine."""
    if getattr(request.state, "api_token", False):
        return "chat" in set(getattr(request.state, "api_token_scopes", []) or [])
    return True


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S), follow_redirects=False)


def _llm_stream(*args, **kwargs):
    """Odysseus's provider layer; one place to replace in tests."""
    from src.llm_core import stream_llm
    return stream_llm(*args, **kwargs)


def _num(v, cast):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


async def _translated(url: str, headers: dict, model: str, payload: dict, owner):
    """Answer a request for a model that does not take a plain OpenAI request."""
    opts = payload.get("stream_options")
    tr = Translator(str(payload.get("model") or model), include_usage=isinstance(opts, dict) and bool(opts.get("include_usage")))
    kw = {"headers": headers, "owner": owner or None}
    if payload.get("tools"):
        kw["tools"] = payload["tools"]
    temp = _num(payload.get("temperature"), float)
    if temp is not None:
        kw["temperature"] = temp
    mt = _num(payload.get("max_completion_tokens") or payload.get("max_tokens"), int)
    if mt:
        kw["max_tokens"] = mt
    agen = _llm_stream(url, model, payload["messages"], **kw)

    async def close():
        try:
            await agen.aclose()
        except Exception:
            pass

    if not payload.get("stream"):
        try:
            async for chunk in agen:
                tr.feed(chunk)
        finally:
            await close()
        if tr.error and not (tr.text or tr.calls):
            return _error(tr.error["status"], tr.error["message"], "upstream_error")
        tr.finish()
        return JSONResponse(tr.completion())

    first = []
    try:
        async for chunk in agen:
            first = tr.feed(chunk)
            if tr.error and not tr.started:
                await close()
                return _error(tr.error["status"], tr.error["message"], "upstream_error")
            if first:
                break
    except Exception:
        await close()
        logger.warning("gateway: translated stream failed before output")
        return _error(502, "the model did not answer", "upstream_unreachable")

    async def body():
        try:
            for line in first:
                yield line
            if not tr.done:
                async for chunk in agen:
                    for line in tr.feed(chunk):
                        yield line
                    if tr.done:
                        break
            for line in tr.finish():
                yield line
        finally:
            await close()
    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def setup_openai_gateway_routes() -> APIRouter:
    router = APIRouter(tags=["openai_gateway"])

    @router.get("/v1/models")
    async def list_models(request: Request):
        if not _allowed(request):
            return _error(403, "this token needs the 'chat' scope", "insufficient_scope")
        items = await asyncio.to_thread(gw.catalog, effective_user(request))
        return {"object": "list", "data": [{"id": i["id"], "object": "model", "created": 0, "owned_by": i["endpoint_name"]}
                                           for i in items]}

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        if not _allowed(request):
            return _error(403, "this token needs the 'chat' scope", "insufficient_scope")
        try:
            payload = await request.json()
        except Exception:
            return _error(400, "the body must be JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return _error(400, "send a JSON object with 'model' and 'messages'")
        try:
            url, headers, model, translate = await asyncio.to_thread(gw.resolve, str(payload.get("model") or ""), effective_user(request))
        except LookupError as e:
            return _error(404, str(e), "model_not_found")
        if translate:
            return await _translated(url, headers, model, payload, effective_user(request))
        upstream = dict(payload, model=model)
        want_stream = bool(upstream.get("stream"))
        client = _client()
        try:
            resp = await client.send(client.build_request("POST", url, json=upstream, headers=headers), stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            logger.warning("gateway: %s unreachable: %s", url, type(e).__name__)
            return _error(502, f"the model's server did not answer ({type(e).__name__})", "upstream_unreachable")
        ctype = resp.headers.get("content-type") or ("text/event-stream" if want_stream else "application/json")
        if not want_stream:
            try:
                data = await resp.aread()
            finally:
                await resp.aclose()
                await client.aclose()
            return Response(content=data, status_code=resp.status_code, media_type=ctype)

        async def body():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()
        return StreamingResponse(body(), status_code=resp.status_code, media_type=ctype,
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
