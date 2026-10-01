"""Sign in with ChatGPT (src/chatgpt_plan.py): OpenAI's ChatGPT plan usage
flow for open-source apps, https://developers.openai.com/siwc/token-sharing-open-source.

Everything here is mocked: no request leaves the machine. The one real socket
is the loopback listener test, which talks to 127.0.0.1 only.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import stat
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ModelEndpoint
from src import chatgpt_plan as cgp

ACCESS = "eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.c2lnbmF0dXJlLWFjY2Vzcw"
REFRESH = "rt_refresh_secret_value_0001"
ISSUED_CLIENT = "oaiapp_issued_client_123"


def _jwt(claims):
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{seg}.c2ln"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def plan(tmp_path, monkeypatch):
    monkeypatch.setattr(cgp, "STORE_DIR", tmp_path / "chatgpt_plan")
    monkeypatch.setenv("ODYSSEUS_CHATGPT_LOOPBACK_LISTENER", "0")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(cgp, "_session_local", lambda: TestSession)
    monkeypatch.setattr(cgp, "_invalidate_models_cache", lambda: None)
    cgp._pending.clear()
    cgp._refresh_locks.clear()
    cgp._session = TestSession
    yield cgp
    cgp._pending.clear()


def _posts(monkeypatch, responder):
    calls = []

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        calls.append({"url": url, "data": dict(data or {}), "headers": headers})
        return responder(url, dict(data or {}))

    monkeypatch.setattr(cgp.httpx, "post", fake_post)
    return calls


def _token_body(nonce, email="jaron@example.com", access=ACCESS, refresh=REFRESH, **extra):
    body = {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": _jwt({"email": email, "nonce": nonce, "iss": "https://auth.openai.com"}),
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": cgp.SCOPE,
        "earliest_refresh_at": int(time.time()) + 1800,
    }
    body.update(extra)
    return body


def _sign_in(plan, monkeypatch, owner="alice", email="jaron@example.com"):
    start = plan.start_sign_in(owner)
    q = parse_qs(urlparse(start["authorize_url"]).query)
    state = q["state"][0]
    nonce = q["nonce"][0]
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, _token_body(nonce, email=email)))
    cb = f"http://127.0.0.1:1455/auth/callback?code=AUTHCODE&scope=openid&state={state}&client_id={ISSUED_CLIENT}"
    result = plan.complete_sign_in(cb, owner=owner)
    return start, q, calls, result


# ── Authorize URL ────────────────────────────────────────────────────────

def test_first_sign_in_builds_the_documented_authorize_url(plan):
    start = plan.start_sign_in("alice")
    parsed = urlparse(start["authorize_url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.openai.com/api/accounts/authorize"
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q["client_id"] == "dynamic_agent_client"
    assert q["agent_name_hint"] == "Odysseus"
    assert q["ext_agent_host_id"].startswith("urn:uuid:")
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert q["scope"] == "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
    assert q["resource"] == "https://api.openai.com/v1"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32 and q["state"] != q["nonce"]
    # PKCE: the challenge is base64url(SHA-256(verifier)) without padding.
    verifier = plan._pending[q["state"]]["verifier"]
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected
    assert start["first_sign_in"] is True


def test_host_id_is_created_once_and_kept(plan):
    first = plan.host_id()
    assert plan.host_id() == first
    q1 = parse_qs(urlparse(plan.start_sign_in("alice")["authorize_url"]).query)
    q2 = parse_qs(urlparse(plan.start_sign_in("bob")["authorize_url"]).query)
    assert q1["ext_agent_host_id"] == q2["ext_agent_host_id"] == [first]


def test_later_sign_ins_use_the_issued_client_id_and_hints(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    again = plan.start_sign_in("alice")
    q = {k: v[0] for k, v in parse_qs(urlparse(again["authorize_url"]).query).items()}
    assert q["client_id"] == ISSUED_CLIENT
    assert "agent_name_hint" not in q
    assert q.get("id_token_hint") or q.get("login_hint")
    assert again["first_sign_in"] is False
    # Another Odysseus user still registers their own client.
    other = parse_qs(urlparse(plan.start_sign_in("bob")["authorize_url"]).query)
    assert other["client_id"] == ["dynamic_agent_client"]


# ── Pasted redirect URL ──────────────────────────────────────────────────

def test_pasted_url_exchanges_the_code_with_the_documented_form(plan, monkeypatch):
    start, q, calls, result = _sign_in(plan, monkeypatch)
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    verifier_challenge = q["code_challenge"][0]
    form = call["data"]
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == ISSUED_CLIENT
    assert form["code"] == "AUTHCODE"
    assert form["redirect_uri"] == q["redirect_uri"][0]
    assert form["resource"] == "https://api.openai.com/v1"
    assert "client_secret" not in form
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == verifier_challenge
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert result["signed_in"] is True and result["email"] == "jaron@example.com"
    assert result["plan_usage_enabled"] is True


def test_a_wrong_state_is_rejected_without_calling_openai(plan, monkeypatch):
    plan.start_sign_in("alice")
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, {}))
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in("http://127.0.0.1:1455/auth/callback?code=X&state=not-the-state", owner="alice")
    assert calls == []
    assert plan.status("alice")["signed_in"] is False


def test_another_users_state_is_rejected(plan, monkeypatch):
    start = plan.start_sign_in("alice")
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, {}))
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in(f"http://127.0.0.1:1455/auth/callback?code=X&state={state}", owner="bob")
    assert calls == []


def test_a_state_can_only_be_used_once_and_expires(plan, monkeypatch):
    start, q, calls, _ = _sign_in(plan, monkeypatch)
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in(f"http://127.0.0.1:1455/auth/callback?code=AUTHCODE&state={q['state'][0]}",
                              owner="alice")
    start = plan.start_sign_in("carol")
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
    plan._pending[state]["created"] -= plan.PENDING_TTL_SECONDS + 1
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in(f"?code=X&state={state}", owner="carol")


def test_declined_consent_and_garbage_are_explained(plan):
    plan.start_sign_in("alice")
    with pytest.raises(cgp.SignInRejected, match="cancelled|declined"):
        plan.complete_sign_in("http://127.0.0.1:1455/auth/callback?error=access_denied&state=x", owner="alice")
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in("   ", owner="alice")


def test_reauthorization_without_client_id_in_callback_keeps_the_saved_one(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    start = plan.start_sign_in("alice")
    q = parse_qs(urlparse(start["authorize_url"]).query)
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, _token_body(q["nonce"][0])))
    plan.complete_sign_in(f"http://127.0.0.1:1455/auth/callback?code=C2&state={q['state'][0]}", owner="alice")
    assert calls[0]["data"]["client_id"] == ISSUED_CLIENT


def test_a_mismatched_nonce_is_rejected(plan, monkeypatch):
    start = plan.start_sign_in("alice")
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
    _posts(monkeypatch, lambda url, data: FakeResponse(200, _token_body("some-other-nonce")))
    with pytest.raises(cgp.SignInRejected, match="nonce"):
        plan.complete_sign_in(f"?code=C&state={state}&client_id={ISSUED_CLIENT}", owner="alice")
    assert plan.status("alice")["signed_in"] is False


def test_missing_plan_scope_keeps_sign_in_but_flags_plan_usage_off(plan, monkeypatch):
    start = plan.start_sign_in("alice")
    q = parse_qs(urlparse(start["authorize_url"]).query)
    body = _token_body(q["nonce"][0], scope="openid profile email offline_access")
    _posts(monkeypatch, lambda url, data: FakeResponse(200, body))
    st = plan.complete_sign_in(f"?code=C&state={q['state'][0]}&client_id={ISSUED_CLIENT}", owner="alice")
    assert st["signed_in"] is True and st["plan_usage_enabled"] is False


# ── Storage ──────────────────────────────────────────────────────────────

def test_tokens_are_stored_owner_only_and_encrypted(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    path = plan._creds_path("alice")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    raw = path.read_text()
    assert ACCESS not in raw and REFRESH not in raw
    assert plan.load_credentials("alice")["refresh_token"] == REFRESH
    for f in path.parent.iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert plan.load_credentials("bob") == {}


def test_tokens_never_reach_the_logs(plan, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _sign_in(plan, monkeypatch)
    creds = plan.load_credentials("alice")
    creds["expires_at"] = int(time.time()) + 10
    creds["earliest_refresh_at"] = 0
    plan.save_credentials("alice", creds)
    _posts(monkeypatch, lambda url, data: FakeResponse(400, {"error": "invalid_grant"}))
    with pytest.raises(cgp.ReauthRequired):
        plan.get_access_token("alice")
    text = caplog.text
    for secret in (ACCESS, REFRESH, "AUTHCODE"):
        assert secret not in text


def test_redact_scrubs_tokens_from_error_text():
    msg = f"failed Authorization: Bearer {ACCESS} refresh_token={REFRESH} code=abc123"
    out = cgp.redact(msg, REFRESH)
    assert ACCESS not in out and REFRESH not in out and "abc123" not in out


# ── Refresh ──────────────────────────────────────────────────────────────

def _expiring(plan, owner="alice", *, in_seconds=60, earliest=0):
    creds = plan.load_credentials(owner)
    creds["expires_at"] = int(time.time()) + in_seconds
    creds["earliest_refresh_at"] = earliest
    plan.save_credentials(owner, creds)


def test_a_fresh_token_is_used_without_refreshing(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(500, {}))
    assert plan.get_access_token("alice") == ACCESS
    assert calls == []


def test_refresh_before_expiry_rotates_the_refresh_token(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    _expiring(plan)
    new_access = _jwt({"exp": int(time.time()) + 3600, "n": 2})
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, {
        "access_token": new_access, "refresh_token": "rt_rotated_0002", "expires_in": 3600,
        "earliest_refresh_at": int(time.time()) + 1800,
    }))
    assert plan.get_access_token("alice") == new_access
    form = calls[0]["data"]
    assert calls[0]["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    assert form == {"grant_type": "refresh_token", "client_id": ISSUED_CLIENT,
                    "refresh_token": REFRESH, "resource": "https://api.openai.com/v1"}
    creds = plan.load_credentials("alice")
    assert creds["refresh_token"] == "rt_rotated_0002"
    assert creds["access_token"] == new_access
    assert creds["expires_at"] > time.time() + 3000


def test_earliest_refresh_at_is_respected_while_the_token_is_still_valid(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    _expiring(plan, in_seconds=200, earliest=int(time.time()) + 120)
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(500, {}))
    assert plan.get_access_token("alice") == ACCESS
    assert calls == []


def test_concurrent_requests_refresh_once(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    _expiring(plan)
    new_access = _jwt({"exp": int(time.time()) + 3600, "n": 3})
    counter = {"n": 0}

    def responder(url, data):
        counter["n"] += 1
        time.sleep(0.2)
        return FakeResponse(200, {"access_token": new_access, "refresh_token": "rt_rotated_0003",
                                  "expires_in": 3600})

    _posts(monkeypatch, responder)
    results, errors = [], []

    def worker():
        try:
            results.append(plan.get_access_token("alice"))
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert counter["n"] == 1
    assert results == [new_access] * 8


@pytest.mark.parametrize("code", ["invalid_grant", "refresh_token_reused", "token_expired", "invalid_refresh_token"])
def test_a_dead_refresh_token_signs_the_user_out(plan, monkeypatch, code):
    _sign_in(plan, monkeypatch)
    plan.upsert_endpoint("alice", ["gpt-5.5"])
    _expiring(plan)
    _posts(monkeypatch, lambda url, data: FakeResponse(400, {"error": code}))
    with pytest.raises(cgp.ReauthRequired, match="Sign in again"):
        plan.get_access_token("alice")
    st = plan.status("alice")
    assert st["signed_in"] is False and st["needs_sign_in_again"] is True
    assert not plan._creds_path("alice").exists()
    db = plan._session()
    try:
        assert db.query(ModelEndpoint).filter(ModelEndpoint.owner == "alice").count() == 0
    finally:
        db.close()
    with pytest.raises(cgp.NotSignedIn):
        plan.get_access_token("alice")


def test_a_network_failure_during_refresh_keeps_the_credentials(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    _expiring(plan)

    def boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(cgp.httpx, "post", boom)
    with pytest.raises(cgp.ChatGPTPlanError) as info:
        plan.get_access_token("alice")
    assert not isinstance(info.value, cgp.ReauthRequired)
    assert plan.status("alice")["signed_in"] is True


# ── Models ───────────────────────────────────────────────────────────────

def test_the_model_list_keeps_only_visibility_list(plan, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"], seen["headers"] = url, headers
        return FakeResponse(200, {"models": [
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"},
            {"slug": "gpt-5.5-mini", "display_name": "GPT-5.5 mini", "visibility": "list"},
            {"slug": "internal-x", "display_name": "Hidden", "visibility": "hide"},
            {"slug": "no-visibility"},
            {"display_name": "no slug", "visibility": "list"},
        ]})

    monkeypatch.setattr(cgp.httpx, "get", fake_get)
    models = plan.fetch_models(ACCESS)
    assert [m["slug"] for m in models] == ["gpt-5.5", "gpt-5.5-mini"]
    assert models[0]["display_name"] == "GPT-5.5"
    assert seen["url"] == "https://api.openai.com/v1/models"
    assert seen["headers"]["Authorization"] == f"Bearer {ACCESS}"


def test_refresh_models_puts_a_chatgpt_endpoint_in_the_picker(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    monkeypatch.setattr(cgp.httpx, "get", lambda url, headers=None, timeout=None: FakeResponse(
        200, {"models": [{"slug": "gpt-5.5", "visibility": "list"}]}))
    out = plan.refresh_models("alice")
    assert out["endpoint"]["models"] == ["gpt-5.5"]
    db = plan._session()
    try:
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.owner == "alice").one()
        assert ep.name == "ChatGPT" and ep.base_url == cgp.ENDPOINT_BASE
        assert ep.api_key is None and ep.supports_tools is True
    finally:
        db.close()


# ── Routing into the app ─────────────────────────────────────────────────

def test_the_endpoint_routes_to_the_chatgpt_plan_provider():
    from src.llm_core import _detect_provider

    assert _detect_provider(cgp.ENDPOINT_BASE) == "chatgpt-plan"
    assert _detect_provider(cgp.ENDPOINT_BASE + "/responses") == "chatgpt-plan"
    assert _detect_provider("https://api.openai.com/v1") == "openai"
    assert cgp.uses_request_scoped_bearer(cgp.ENDPOINT_BASE + "/responses")
    assert not cgp.uses_request_scoped_bearer("https://api.openai.com/v1/chat/completions")


def test_runtime_resolution_uses_this_users_token(plan, monkeypatch):
    from src.endpoint_resolver import build_headers, resolve_endpoint_runtime

    _sign_in(plan, monkeypatch)
    ep = ModelEndpoint(id="e1", name="ChatGPT", base_url=cgp.ENDPOINT_BASE, owner="alice",
                       provider_auth_id=cgp.PROVIDER_AUTH_MARKER)
    base, key = resolve_endpoint_runtime(ep, owner="alice")
    assert base == cgp.ENDPOINT_BASE and key == ACCESS
    assert build_headers(key, base)["Authorization"] == f"Bearer {ACCESS}"
    with pytest.raises(cgp.NotSignedIn):
        resolve_endpoint_runtime(ep, owner="mallory")


# ── Chat -> Responses translation ────────────────────────────────────────

def test_chat_messages_translate_to_responses_input():
    items = cgp.messages_to_input([
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"}},
        ]},
        {"role": "assistant", "content": "A cat.", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{\"q\":\"cat\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "results"},
        {"role": "user", "content": "Thanks"},
    ])
    assert items[0] == {"role": "system", "content": [{"type": "input_text", "text": "Be brief."}]}
    assert items[1] == {"role": "user", "content": [
        {"type": "input_text", "text": "What is this?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "low"},
    ]}
    assert items[2] == {"role": "assistant", "content": [{"type": "output_text", "text": "A cat."}]}
    assert items[3] == {"type": "function_call", "call_id": "call_1", "name": "search", "arguments": "{\"q\":\"cat\"}"}
    assert items[4] == {"type": "function_call_output", "call_id": "call_1", "output": "results"}
    assert items[5] == {"role": "user", "content": [{"type": "input_text", "text": "Thanks"}]}


def test_payload_always_sends_store_false_and_stream_true():
    tools = [{"type": "function", "function": {"name": "t", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}]
    p = cgp.build_payload("gpt-5.5", [{"role": "user", "content": "hi"}], tools=tools)
    assert p["store"] is False and p["stream"] is True and p["model"] == "gpt-5.5"
    assert p["tools"] == [{"type": "function", "name": "t", "description": "d",
                           "parameters": {"type": "object", "properties": {}}, "strict": False}]
    assert "messages" not in p and "temperature" not in p and "max_output_tokens" not in p
    p2 = cgp.build_payload("gpt-5.5", [])
    assert p2["store"] is False and p2["stream"] is True and "tools" not in p2


# ── Stream translation ───────────────────────────────────────────────────

def _run(events):
    t = cgp.StreamTranslator()
    out = []
    for e in events:
        out.extend(t.feed(e))
    out.extend(t.finish())
    return out


def _data(chunk):
    line = [ln for ln in chunk.splitlines() if ln.startswith("data: ")][0]
    return line[6:] if line[6:] == "[DONE]" else json.loads(line[6:])


def test_text_deltas_stream_and_completed_ends_the_turn():
    out = _run([
        {"type": "response.created"},
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 2}}},
    ])
    assert [_data(c) for c in out] == [
        {"delta": "Hel"}, {"delta": "lo"},
        {"type": "usage", "data": {"input_tokens": 5, "output_tokens": 2}},
        "[DONE]",
    ]


def test_usage_limit_failure_is_shown_plainly():
    out = _run([{"type": "response.failed", "response": {"error": {
        "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}}])
    assert len(out) == 1 and out[0].startswith("event: error")
    body = _data(out[0])
    assert body["text"].startswith("Your ChatGPT plan's usage limit was reached for now")
    assert "https://chatgpt.com/settings/usage" in body["text"]
    assert body["status"] == 429 and body["code"] == "subscription_sharing_usage_limit_exceeded"


def test_other_failures_and_incomplete_and_truncated_streams():
    body = _data(_run([{"type": "response.failed", "response": {"error": {
        "code": "subscription_sharing_usage_unavailable"}}}])[0])
    assert "couldn't check your plan's usage" in body["text"]
    out = _run([{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}])
    assert out[0].startswith("event: error") and "max_output_tokens" in _data(out[0])["text"]
    out = _run([{"type": "response.output_text.delta", "delta": "partial"},
                {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "content_filter"}}}])
    assert _data(out[0]) == {"delta": "partial"} and "content_filter" in _data(out[1])["delta"]
    assert _data(out[-1]) == "[DONE]"
    # No response.completed: not a success.
    out = _run([{"type": "response.output_text.delta", "delta": "cut"}])
    assert out[-1].startswith("event: error")


def test_function_calls_become_the_apps_tool_calls_event():
    out = _run([
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "search"}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{\"q\":"},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "\"cats\"}"},
        {"type": "response.completed", "response": {}},
    ])
    assert _data(out[0]) == {"type": "tool_calls", "calls": [
        {"id": "call_9", "name": "search", "arguments": "{\"q\":\"cats\"}"}]}
    assert _data(out[-1]) == "[DONE]"


def test_http_errors_before_streaming_map_to_messages():
    body = _data(cgp.error_chunk_for_http(429, json.dumps({"error": {
        "code": "subscription_sharing_usage_limit_exceeded", "message": "x"}})))
    assert body["text"].startswith("Your ChatGPT plan's usage limit was reached")
    assert "Sign in again" in _data(cgp.error_chunk_for_http(401, "{}"))["text"]


class _FakeStream:
    def __init__(self, status, lines):
        self.status_code = status
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return "\n".join(self._lines).encode()

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, status, lines):
        self.status, self.lines, self.calls = status, lines, []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return _FakeStream(self.status, self.lines)


def _collect(agen):
    async def go():
        return [c async for c in agen]
    return asyncio.run(go())


def test_stream_llm_posts_to_responses_and_translates(monkeypatch):
    from src import llm_core

    sse = [
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"Hi"}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":{}}',
        "",
    ]
    client = _FakeClient(200, sse)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    chunks = _collect(llm_core.stream_llm(
        cgp.ENDPOINT_BASE + "/responses", "gpt-5.5",
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}],
        headers={"Authorization": f"Bearer {ACCESS}"},
    ))
    assert client.calls[0]["url"] == "https://api.openai.com/v1/responses"
    sent = client.calls[0]["json"]
    assert sent["store"] is False and sent["stream"] is True and sent["model"] == "gpt-5.5"
    assert sent["input"][0]["role"] == "system" and sent["input"][-1]["role"] == "user"
    assert client.calls[0]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert [_data(c) for c in chunks] == [{"delta": "Hi"}, "[DONE]"]


def test_stream_llm_surfaces_the_usage_limit_http_error(monkeypatch):
    from src import llm_core

    client = _FakeClient(429, [json.dumps({"error": {"code": "subscription_sharing_usage_limit_exceeded"}})])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    chunks = _collect(llm_core.stream_llm(cgp.ENDPOINT_BASE + "/responses", "gpt-5.5",
                                          [{"role": "user", "content": "x"}],
                                          headers={"Authorization": "Bearer t"}))
    assert len(chunks) == 1 and "usage limit was reached" in _data(chunks[0])["text"]


def test_stream_llm_without_a_token_asks_to_sign_in(monkeypatch):
    from src import llm_core

    client = _FakeClient(200, [])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    chunks = _collect(llm_core.stream_llm(cgp.ENDPOINT_BASE + "/responses", "gpt-5.5",
                                          [{"role": "user", "content": "x"}], headers={}))
    assert client.calls == [] and "Sign in with ChatGPT" in _data(chunks[0])["text"]


def test_llm_call_async_collects_text_for_background_callers(monkeypatch):
    from src import llm_core

    sse = ['data: {"type":"response.output_text.delta","delta":"Title"}',
           'data: {"type":"response.completed","response":{}}']
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(200, sse))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    out = asyncio.run(llm_core.llm_call_async(cgp.ENDPOINT_BASE + "/responses", "gpt-5.5",
                                              [{"role": "user", "content": "name this chat"}],
                                              headers={"Authorization": "Bearer t"}))
    assert out == "Title"


def test_blocking_llm_call_uses_the_responses_stream(monkeypatch):
    from src import llm_core

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
        sent.update(url=url, json=json, headers=headers)
        return _SyncStream()

    monkeypatch.setattr(llm_core.httpx, "stream", fake_stream)
    out = llm_core.llm_call(cgp.ENDPOINT_BASE + "/responses", "gpt-5.5",
                            [{"role": "user", "content": "sync please"}],
                            headers={"Authorization": "Bearer t"})
    assert out == "ok"
    assert sent["url"] == "https://api.openai.com/v1/responses"
    assert sent["json"]["store"] is False and sent["json"]["stream"] is True


# ── Sign out ─────────────────────────────────────────────────────────────

def test_sign_out_deletes_the_credentials_and_the_endpoint(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    plan.upsert_endpoint("alice", ["gpt-5.5"])
    _sign_in(plan, monkeypatch, owner="bob")
    plan.upsert_endpoint("bob", ["gpt-5.5"])
    st = plan.sign_out("alice")
    assert st["signed_in"] is False and st["needs_sign_in_again"] is False
    assert not plan._creds_path("alice").exists()
    assert plan.load_credentials("alice") == {}
    db = plan._session()
    try:
        owners = [e.owner for e in db.query(ModelEndpoint).all()]
    finally:
        db.close()
    assert owners == ["bob"]
    assert plan.status("bob")["signed_in"] is True
    with pytest.raises(cgp.NotSignedIn):
        plan.get_access_token("alice")


# ── Routes ───────────────────────────────────────────────────────────────

def _client(user="alice"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.chatgpt_plan_routes import setup_chatgpt_plan_routes

    app = FastAPI()

    @app.middleware("http")
    async def as_user(request, call_next):
        request.state.current_user = user
        return await call_next(request)

    app.include_router(setup_chatgpt_plan_routes())
    return TestClient(app)


def test_routes_run_the_whole_paste_flow(plan, monkeypatch):
    c = _client()
    assert c.get("/api/chatgpt-plan/status").json()["signed_in"] is False
    start = c.post("/api/chatgpt-plan/sign-in/start").json()
    q = parse_qs(urlparse(start["authorize_url"]).query)
    _posts(monkeypatch, lambda url, data: FakeResponse(200, _token_body(q["nonce"][0])))
    monkeypatch.setattr(cgp.httpx, "get", lambda url, headers=None, timeout=None: FakeResponse(
        200, {"models": [{"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"}]}))
    bad = c.post("/api/chatgpt-plan/sign-in/complete", json={"redirect_url": "?code=x&state=wrong"})
    assert bad.status_code == 400
    res = c.post("/api/chatgpt-plan/sign-in/complete", json={
        "redirect_url": f"http://127.0.0.1:1455/auth/callback?code=C&state={q['state'][0]}&client_id={ISSUED_CLIENT}"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["signed_in"] is True and body["models"][0]["slug"] == "gpt-5.5"
    assert ACCESS not in res.text and REFRESH not in res.text
    assert c.post("/api/chatgpt-plan/models/refresh").json()["models"][0]["display_name"] == "GPT-5.5"
    assert c.post("/api/chatgpt-plan/sign-out").json()["signed_in"] is False
    assert c.post("/api/chatgpt-plan/models/refresh").status_code == 401


# ── Loopback listener ────────────────────────────────────────────────────

def test_the_loopback_listener_finishes_sign_in_on_the_server(plan, monkeypatch):
    start = plan.start_sign_in("alice", listen=False)
    q = parse_qs(urlparse(start["authorize_url"]).query)
    _posts(monkeypatch, lambda url, data: FakeResponse(200, _token_body(q["nonce"][0])))
    monkeypatch.setattr(cgp, "refresh_models", lambda owner: {"models": []})
    listener = cgp._LoopbackListener()
    assert listener.ensure_running(port=0) is True
    port = listener.port
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/auth/callback",
                      params={"code": "C", "state": q["state"][0], "client_id": ISSUED_CLIENT}, timeout=5)
        assert r.status_code == 200 and "You can close this tab" in r.text
        assert plan.status("alice")["signed_in"] is True
        # Nothing is pending any more, so the listener shuts itself down.
        deadline = time.time() + 5
        while listener.running and time.time() < deadline:
            time.sleep(0.05)
        assert not listener.running
    finally:
        listener.stop()
