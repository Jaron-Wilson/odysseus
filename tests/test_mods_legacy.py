"""The legacy settings handoff (src/mods/legacy.py) and the old inbound SMS
address (src/mods/legacy_urls.py): a mod imports the settings its feature had
while it was built in, secrets decrypted by the core, once, for the slices it
declared and the admin approved. Nothing here touches a real data folder."""
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src import secret_storage
from src.mods import legacy, legacy_urls, manifest as mf, registry, routes, signing
from tests.mods_helpers import UvicornThread, add_mod, mark_running, mods_dir  # noqa: F401

TWILIO_TOKEN = "twilio-token-ABC123-do-not-leak"
SIP_PW = "sip-password-XYZ789-do-not-leak"
SUNSHINE_PW = "sunshine-pw-QRS456-do-not-leak"
FORWARD_SECRET = "f" * 43


class FakeAuth:
    users = {"alice": {}, "bob": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"


class As(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        u = request.headers.get("x-test-user")
        if u:
            request.state.current_user = u
            request.state.api_token = False
        return await call_next(request)


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def core_data(tmp_path, monkeypatch):
    """A fake Odysseus data folder with its own app key and the old settings."""
    data = tmp_path / "odysseus-data"
    data.mkdir()
    monkeypatch.setattr(secret_storage, "_KEY_PATH", data / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(legacy, "_data_dir", lambda: str(data))
    prefs = {"_users": {
        "alice": {
            "sms_gateway": {"numbers": ["+15550102000"], "reply_url": "http://sms:pw@100.1.1.1:8080/message",
                            "secret_hash": "ab" * 32, "secret_created": 1.0,
                            "conversations": {"+15550102000": "sess-1"}},
            "phone_calls": {"enabled": True, "account_sid": "AC" + "0" * 32, "phone_number": "+15550103000",
                            "auth_token": secret_storage.encrypt(TWILIO_TOKEN),
                            "sip": {"enabled": True, "username": "jaron",
                                    "password": secret_storage.encrypt(SIP_PW)}},
            "google_meet": {"enabled": True, "display_name": "Odysseus (AI)"},
            "google_meet_chats": {"meet:abc-defg-hij": "sess-2"},
            "theme": {"name": "dark"},
        },
        "bob": {"theme": {"name": "light"}},
    }}
    (data / "user_prefs.json").write_text(json.dumps(prefs))
    (data / "games.json").write_text(json.dumps({
        "sunshine_urls": ["https://192.168.1.50:47990"], "username": "sun",
        "password": secret_storage.encrypt(SUNSHINE_PW), "credentials_file": "~/.secrets/sunshine.env",
        "moonlight_url": "/play/", "play_in": "new_tab", "nightly_sync": True,
        "pins": {"192.168.1.50:47990": "-----BEGIN CERTIFICATE-----x"}, "unrelated": "dropped"}))
    return data


@pytest.fixture
def api(mods_dir, core_data, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(legacy, "is_local", lambda request: True)
    routes._calls.clear()
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(As)
    return TestClient(app)


def mod_with(slices, mod_id="phone", granted=None):
    entry, tok = add_mod({"imports": slices}, mod_id=mod_id)
    registry._update(mod_id, granted_imports=list(slices if granted is None else granted))
    return tok


def export(api, tok, name):
    return api.post("/api/mods/v1/legacy/export", headers=bearer(tok), json={"slice": name})


# ── The manifest and the review ──────────────────────────────────────────

def _man(**extra):
    raw = {"id": "x-mod", "name": "X", "version": "1.0.0", "runtime": {"type": "process", "command": ["x"]},
           "tutorial": {"steps": [{"title": "Hi", "body": "Hello"}]}}
    raw.update(extra)
    return raw


def test_imports_are_validated_against_the_fixed_slices():
    assert mf.validate(_man(imports=["telephony", "sms", "sms"]))["imports"] == ["sms", "telephony"]
    assert mf.validate(_man())["imports"] == []
    for bad in (["everything"], "sms", [1], ["sms", "../user_prefs.json"]):
        with pytest.raises(mf.ManifestError):
            mf.validate(_man(imports=bad))


def test_the_install_review_lists_slices_and_what_is_new_on_an_update():
    man = mf.validate(_man(imports=["sms", "telephony"]))
    r = legacy.review(man)
    assert [x["slice"] for x in r] == ["sms", "telephony"] and not any(x["new"] for x in r)
    assert r[1]["secrets"] == ["phone_calls/auth_token", "phone_calls/sip/password"]
    r = legacy.review(man, {"granted_imports": ["sms"]})
    assert {x["slice"]: x["new"] for x in r} == {"sms": False, "telephony": True}


# ── The allowlist ────────────────────────────────────────────────────────

def test_a_mod_cannot_read_a_slice_it_did_not_declare_or_was_not_granted(api):
    tok = mod_with(["sms"])
    assert export(api, tok, "sms").status_code == 200
    assert export(api, tok, "telephony").status_code == 403       # a real slice, but not its own
    assert export(api, tok, "games").status_code == 403
    assert export(api, tok, "user_prefs.json").status_code == 404  # not a slice at all
    assert export(api, tok, "").status_code == 404
    # Declared in the manifest but never approved (an update that added it).
    other = mod_with(["telephony"], mod_id="calls", granted=[])
    assert export(api, other, "telephony").status_code == 403
    # No token.
    assert api.post("/api/mods/v1/legacy/export", json={"slice": "sms"}).status_code == 401


def test_a_slice_returns_only_its_own_keys(api):
    tok = mod_with(["sms"])
    body = export(api, tok, "sms").json()
    alice = body["data"]["_users"]["alice"]
    assert set(alice) == {"sms_gateway"}                           # no theme, no phone_calls
    assert "bob" not in body["data"]["_users"]                     # nothing of bob's under these keys
    tok = mod_with(["games"], mod_id="games")
    data = export(api, tok, "games").json()["data"]
    assert "unrelated" not in data and data["play_in"] == "new_tab"


def test_only_the_local_channel(mods_dir, core_data):
    app = FastAPI()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    tok = mod_with(["sms"])
    r = TestClient(app).post("/api/mods/v1/legacy/export", headers=bearer(tok), json={"slice": "sms"})
    assert r.status_code == 403                                   # TestClient's peer is not loopback


def test_is_local_refuses_proxied_requests():
    class Req:
        def __init__(self, host, headers):
            self.client = type("C", (), {"host": host})()
            self.headers = headers
    assert legacy.is_local(Req("127.0.0.1", {}))
    assert not legacy.is_local(Req("100.64.0.9", {}))
    assert not legacy.is_local(Req("127.0.0.1", {"x-forwarded-for": "100.64.0.9"}))
    assert not legacy.is_local(Req("127.0.0.1", {"tailscale-user-login": "a@b"}))


# ── Decrypt, hand over, never log ────────────────────────────────────────

def test_secrets_are_decrypted_by_the_core_and_never_logged_or_written(api, mods_dir, caplog):
    caplog.set_level(logging.DEBUG)
    tok = mod_with(["telephony"], mod_id="calls")
    r = export(api, tok, "telephony")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    calls = r.json()["data"]["_users"]["alice"]["phone_calls"]
    assert calls["auth_token"] == TWILIO_TOKEN and calls["sip"]["password"] == SIP_PW
    assert r.json()["secrets"] == ["phone_calls/auth_token", "phone_calls/sip/password"]
    done = api.post("/api/mods/v1/legacy/imported", headers=bearer(tok), json={"slice": "telephony"})
    assert done.status_code == 200
    # Not in the core's logs, the audit log, or any file the core wrote.
    assert TWILIO_TOKEN not in caplog.text and SIP_PW not in caplog.text
    lines = [json.loads(x) for x in open(legacy._log_file()).read().splitlines()]
    assert [x["event"] for x in lines] == ["export", "imported"]
    assert lines[0]["secrets"] == ["phone_calls/auth_token", "phone_calls/sip/password"] and lines[0]["users"] == 1
    for root, _, files in os.walk(str(mods_dir)):
        for name in files:
            with open(os.path.join(root, name), "rb") as f:
                blob = f.read()
            assert TWILIO_TOKEN.encode() not in blob and SIP_PW.encode() not in blob, name


def test_the_games_slice_decrypts_the_sunshine_password(api):
    tok = mod_with(["games"], mod_id="games")
    body = export(api, tok, "games").json()
    assert body["data"]["password"] == SUNSHINE_PW and body["secrets"] == ["password"]
    assert body["data"]["sunshine_urls"] == ["https://192.168.1.50:47990"] and body["data"]["username"] == "sun"


def test_a_secret_the_core_cannot_read_is_reported_not_guessed(api, core_data):
    prefs = json.loads((core_data / "user_prefs.json").read_text())
    prefs["_users"]["alice"]["phone_calls"]["auth_token"] = "enc:not-a-valid-token"
    (core_data / "user_prefs.json").write_text(json.dumps(prefs))
    tok = mod_with(["telephony"], mod_id="calls")
    body = export(api, tok, "telephony").json()
    assert body["unreadable"] == ["phone_calls/auth_token"]
    assert body["data"]["_users"]["alice"]["phone_calls"]["auth_token"] == ""
    assert body["data"]["_users"]["alice"]["phone_calls"]["sip"]["password"] == SIP_PW


def test_decryption_works_when_the_secret_store_cannot_be_imported(api, monkeypatch):
    tok = mod_with(["games"], mod_id="games")
    registry._token_index = {tok: "games"}                         # the registry decrypts tokens with that store too

    def boom(_):
        raise ImportError("circular import")
    monkeypatch.setattr(secret_storage, "decrypt", boom)
    r = export(api, tok, "games")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["password"] == SUNSHINE_PW


# ── One time ─────────────────────────────────────────────────────────────

def test_a_slice_closes_once_imported_and_an_admin_can_open_it_again(api):
    tok = mod_with(["sms"])
    st = api.get("/api/mods/v1/legacy", headers=bearer(tok)).json()["slices"][0]
    assert st["available"] is True and st["imported"] is False
    assert export(api, tok, "sms").status_code == 200
    assert export(api, tok, "sms").status_code == 200              # a preview may read it again
    assert api.post("/api/mods/v1/legacy/imported", headers=bearer(tok), json={"slice": "sms"}).status_code == 200
    r = export(api, tok, "sms")
    assert r.status_code == 410 and "alice" not in r.text
    st = api.get("/api/mods/v1/legacy", headers=bearer(tok)).json()["slices"][0]
    assert st["imported"] is True and st["available"] is False
    # Only an admin can open it again.
    assert api.post("/api/mods/phone/legacy/sms/reopen", headers={"x-test-user": "bob"}).status_code in (401, 403)
    assert api.post("/api/mods/phone/legacy/sms/reopen", headers={"x-test-user": "alice"}).json()["reopened"] is True
    assert export(api, tok, "sms").status_code == 200


def test_reinstalling_a_mod_opens_its_slices_again(api):
    tok = mod_with(["sms"])
    assert api.post("/api/mods/v1/legacy/imported", headers=bearer(tok), json={"slice": "sms"}).status_code == 200
    assert export(api, tok, "sms").status_code == 410
    registry._update("phone", installed_at=12345.0)               # removed and installed again
    assert export(api, tok, "sms").status_code == 200


# ── The old inbound SMS address ──────────────────────────────────────────

@pytest.fixture
def fwd_stack(mods_dir):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.path, self.headers.get("X-Odysseus-User"), self.rfile.read(n)))
            body = b'{"ok":true}'
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.include_router(legacy_urls.setup_legacy_url_routes())
    with UvicornThread(app) as up:
        yield up, srv.server_address[1], seen
    srv.shutdown()


def test_the_old_inbound_address_reaches_the_phone_mod(fwd_stack):
    up, port, seen = fwd_stack
    add_mod(permissions=["public.http"], mod_id="phone")
    mark_running("phone", port)
    body = {"from": "+15550102000", "text": "list"}
    r = httpx.post(f"{up.url}/api/sms/inbound/{FORWARD_SECRET}", json=body, follow_redirects=False)
    assert r.status_code == 202 and r.json() == {"ok": True}      # the mod's answer, not a redirect
    path, user, got = seen[-1]
    assert path == f"/public/sms/inbound/{FORWARD_SECRET}" and user == signing.PUBLIC_USER
    assert json.loads(got) == body                                # the same body and secret


def test_the_old_address_checks_the_shape_and_needs_the_mod(fwd_stack):
    up, port, seen = fwd_stack
    assert httpx.post(f"{up.url}/api/sms/inbound/short", json={}).status_code == 404
    assert httpx.post(f"{up.url}/api/sms/inbound/{FORWARD_SECRET}", json={}).status_code == 404   # no mod
    add_mod(permissions=[], mod_id="phone")                       # no public.http
    mark_running("phone", port)
    assert httpx.post(f"{up.url}/api/sms/inbound/{FORWARD_SECRET}", json={}).status_code == 401
    assert seen == []


def test_the_old_address_is_exempt_from_login_in_app_py_and_its_log_line_is_redacted():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")).read()
    m = re.search(r'_re\.compile\(r"(\^/api/sms/inbound/[^"]+)"\)', src)
    pat = re.compile(m.group(1))
    assert pat.match(f"/api/sms/inbound/{FORWARD_SECRET}") and not pat.match("/api/sms/inbound/short")
    assert not pat.match("/api/sms/config")
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/1.1" %d',
                            ("1.2.3.4", "POST", f"/api/sms/inbound/{FORWARD_SECRET}", 202), None)
    legacy_urls._RedactSecrets().filter(rec)
    assert FORWARD_SECRET not in rec.getMessage() and "<redacted>" in rec.getMessage()
    rec = logging.LogRecord("x", logging.INFO, "", 0, "GET /mods/phone/public/sms/inbound/%s" % FORWARD_SECRET, (), None)
    legacy_urls._RedactSecrets().filter(rec)
    assert FORWARD_SECRET not in rec.getMessage()
