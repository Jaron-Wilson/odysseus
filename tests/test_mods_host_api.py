"""The mod API for the agent, speech and Google (src/mods/host_api.py), and
mods' public paths (src/mods/proxy.py, core/middleware.py).

The Phone & Messages mod is built on these: a text, a phone call and a
Google Meet are chats with the agent, through the server's speech engines,
and the phone's webhooks reach the mod with nobody signed in. Nothing here
reaches a model, a speech engine, Google or a phone: each is faked.
"""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import host_api, routes, signing
from tests.mods_helpers import UvicornThread, add_mod, mark_running, mods_dir  # noqa: F401


class FakeAuth:
    users = {"alice": {}, "bob": {}, "carol": {}}

    def is_admin(self, u):
        return u == "alice"

    def validate_token(self, t):
        return t in ("alice-session",)

    def get_username_for_token(self, t):
        return t.split("-")[0]


class Sess:
    def __init__(self, name, owner, model="qwen"):
        self.name, self.owner, self.model, self.archived = name, owner, model, False


class Mgr:
    def __init__(self):
        self.sessions = {"a1": Sess("Trip plans", "alice"), "a2": Sess("Groceries", "alice"),
                         "b1": Sess("Bob secrets", "bob")}
        self.notes = []

    def get_sessions_for_user(self, user=None):
        return {k: s for k, s in self.sessions.items() if user is None or s.owner == user}

    def get_session(self, sid):
        return self.sessions.get(sid)

    def add_message(self, sid, msg):
        self.notes.append((sid, msg.role, msg.content, msg.metadata))

    def save_sessions(self):
        pass


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def api(mods_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from src.mods import access
    access.set_auth_manager(FakeAuth())
    mgr = Mgr()
    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: mgr)
    app = FastAPI()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    routes._calls.clear()
    _, tok = add_mod(permissions=["chats", "speech", "google.calendar", "calendar.read", "users", "browser"],
                     access="users")
    _, bare = add_mod(permissions=[], mod_id="other-mod")
    yield TestClient(app), tok, bare, mgr
    access.set_auth_manager(None)


@pytest.mark.parametrize("method,path,perm", [
    ("get", "/api/mods/v1/chats?user=alice", "chats"),
    ("post", "/api/mods/v1/chats/a1/turn", "chats"),
    ("get", "/api/mods/v1/models?user=alice", "chats"),
    ("post", "/api/mods/v1/speech/synthesize", "speech"),
    ("get", "/api/mods/v1/browser", "browser"),
    ("post", "/api/mods/v1/google/meet", "google.calendar"),
    ("get", "/api/mods/v1/calendar/meetings?user=alice", "calendar.read"),
    ("get", "/api/mods/v1/users/alice", "users"),
])
def test_each_call_needs_its_permission(api, method, path, perm):
    client, _, bare, _ = api
    kw = {"headers": bearer(bare)}
    if method == "post":
        kw["json"] = {"user": "alice", "message": "hi", "text": "hi"}
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 403 and perm in r.json()["detail"]


def test_chats_are_the_named_users_and_never_a_fallback(api):
    client, tok, _, _ = api
    r = client.get("/api/mods/v1/chats?user=alice", headers=bearer(tok))
    assert r.status_code == 200 and {c["id"] for c in r.json()["chats"]} == {"a1", "a2"}
    assert [c["id"] for c in client.get("/api/mods/v1/chats?user=alice&q=trip", headers=bearer(tok)).json()["chats"]] == ["a1"]
    # No user, or one Odysseus does not know: refused, not the primary admin.
    assert client.get("/api/mods/v1/chats", headers=bearer(tok)).status_code == 403
    assert client.get("/api/mods/v1/chats?user=mallory", headers=bearer(tok)).status_code == 403
    # Bob's chat is not Alice's.
    assert client.get("/api/mods/v1/chats/b1?user=alice", headers=bearer(tok)).status_code == 404
    assert client.get("/api/mods/v1/chats/b1?user=bob", headers=bearer(tok)).json()["name"] == "Bob secrets"


def test_an_admin_only_mod_serves_only_admins(api):
    client, _, _, _ = api
    _, tok = add_mod(permissions=["chats"], mod_id="admin-mod")
    assert client.get("/api/mods/v1/chats?user=alice", headers=bearer(tok)).status_code == 200
    assert client.get("/api/mods/v1/chats?user=bob", headers=bearer(tok)).status_code == 403


def test_with_sign_in_off_there_is_one_user(api, monkeypatch):
    client, tok, _, _ = api
    monkeypatch.setenv("AUTH_ENABLED", "false")
    r = client.get("/api/mods/v1/chats?user=local", headers=bearer(tok))
    assert {c["id"] for c in r.json()["chats"]} == {"a1", "a2", "b1"}


def test_new_chat_on_the_default_model(api, monkeypatch):
    client, tok, _, mgr = api
    from routes import model_routes, session_routes
    monkeypatch.setattr(model_routes, "resolve_default_chat", lambda u, a: {"model": "qwen", "endpoint_id": "ep"})
    made = []

    def fake_create(sm, owner, model, endpoint_id, name=""):
        made.append((owner, model, endpoint_id, name))
        mgr.sessions["n1"] = Sess(name, owner, model)
        return "n1", mgr.sessions["n1"]
    monkeypatch.setattr(session_routes, "create_direct_chat", fake_create)
    r = client.post("/api/mods/v1/chats", headers=bearer(tok), json={"user": "bob", "name": "Phone call 14:05"})
    assert r.status_code == 200 and r.json()["id"] == "n1"
    assert made == [("bob", "qwen", "ep", "Phone call 14:05")]
    monkeypatch.setattr(model_routes, "resolve_default_chat", lambda u, a: {})
    assert client.post("/api/mods/v1/chats", headers=bearer(tok), json={"user": "bob"}).status_code == 409


@pytest.fixture
def fake_run(monkeypatch):
    """chat_queue.run_headless and agent_runs.subscribe, without a model."""
    from src import agent_runs, chat_queue
    sent = []

    async def run_headless(sid, text, source="queued", voice_call=False, note="", **kw):
        sent.append({"sid": sid, "text": text, "source": source, "voice_call": voice_call, "note": note})
        return True

    async def subscribe(sid):
        for ev in ('data: {"delta": "thinking...", "thinking": true}\n\n', 'data: {"delta": "Hello "}\n\n',
                   'data: {"delta": "there."}\n\n', "data: [DONE]\n\n"):
            yield ev
    monkeypatch.setattr(chat_queue, "run_headless", run_headless)
    monkeypatch.setattr(agent_runs, "subscribe", subscribe)
    monkeypatch.setattr(agent_runs, "is_active", lambda sid: False)
    return sent


def test_a_turn_answers_whole(api, fake_run):
    client, tok, _, _ = api
    r = client.post("/api/mods/v1/chats/a1/turn", headers=bearer(tok),
                    json={"user": "alice", "message": "hi", "source": "sms"})
    assert r.json() == {"ok": True, "text": "Hello there.", "error": ""}
    assert fake_run == [{"sid": "a1", "text": "hi", "source": "sms", "voice_call": False, "note": ""}]
    assert client.post("/api/mods/v1/chats/b1/turn", headers=bearer(tok),
                       json={"user": "alice", "message": "hi"}).status_code == 404


def test_a_turn_streams_for_a_call(api, fake_run):
    client, tok, _, _ = api
    with client.stream("POST", "/api/mods/v1/chats/a1/turn", headers=bearer(tok),
                       json={"user": "alice", "message": "hi", "voice_call": True, "stream": True,
                             "note": "On a phone call", "source": "phone call!"}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert events == [{"delta": "Hello "}, {"delta": "there."}, {"done": True, "text": "Hello there.", "ok": True}]
    assert fake_run[0]["voice_call"] is True and fake_run[0]["note"] == "On a phone call"
    assert fake_run[0]["source"] == "phonecall"


def test_a_busy_chat_is_refused_unless_it_interrupts(api, fake_run, monkeypatch):
    client, tok, _, _ = api
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "is_active", lambda sid: True)
    r = client.post("/api/mods/v1/chats/a1/turn", headers=bearer(tok),
                    json={"user": "alice", "message": "hi", "interrupt": False})
    assert r.json()["code"] == "busy" and fake_run == []


def test_notes_are_saved_as_assistant_lines(api):
    client, tok, _, mgr = api
    r = client.post("/api/mods/v1/chats/a1/note", headers=bearer(tok),
                    json={"user": "alice", "text": "Call ended.", "source": "phone_call",
                          "meta": {"excluded": True, "nested": {"no": 1}}})
    assert r.status_code == 200
    sid, role, text, meta = mgr.notes[0]
    assert (sid, role, text) == ("a1", "assistant", "Call ended.")
    assert meta == {"source": "phone_call", "note": True, "excluded": True}


def test_speech_round_trip(api, monkeypatch):
    client, tok, _, _ = api

    class STT:
        def transcribe(self, audio):
            return "heard" if audio.startswith(b"RIFF") else None

    class TTS:
        def synthesize(self, text, use_cache=True, response_format="mp3"):
            assert response_format == "wav"
            return b"RIFF" + text.encode()
    import services.stt as stt
    import services.tts as tts
    monkeypatch.setattr(stt, "get_stt_service", lambda: STT())
    monkeypatch.setattr(tts, "get_tts_service", lambda: TTS())
    r = client.post("/api/mods/v1/speech/transcribe", headers={**bearer(tok), "Content-Type": "audio/wav"},
                    content=b"RIFF....")
    assert r.json() == {"text": "heard"}
    r = client.post("/api/mods/v1/speech/synthesize", headers=bearer(tok), json={"text": "Hi."})
    assert r.headers["content-type"] == "audio/wav" and r.content == b"RIFFHi."
    assert client.post("/api/mods/v1/speech/synthesize", headers=bearer(tok), json={"text": ""}).status_code == 400


def test_making_a_meet_goes_through_the_users_google_connection(api, monkeypatch):
    client, tok, _, _ = api
    from src.google_workspace import meet as gmeet
    monkeypatch.setattr(gmeet, "status", lambda u: {"connected": u == "alice", "configured": True})
    calls = []

    async def create(user, **kw):
        calls.append((user, kw["title"], kw["attendees"]))
        return {"url": "https://meet.google.com/abc-defg-hij", "title": kw["title"], "open_access": True}
    monkeypatch.setattr(gmeet, "create_meeting", create)
    r = client.post("/api/mods/v1/google/meet", headers=bearer(tok),
                    json={"user": "alice", "title": "Sync", "attendees": "bob@x.com"})
    assert r.status_code == 200 and r.json()["url"].endswith("abc-defg-hij") and r.json()["starts_now"] is True
    assert calls == [("alice", "Sync", ["bob@x.com"])]
    r = client.post("/api/mods/v1/google/meet", headers=bearer(tok), json={"user": "bob", "title": "x"})
    assert r.status_code == 400 and "not connected" in r.json()["detail"]
    r = client.post("/api/mods/v1/google/meet", headers=bearer(tok), json={"user": "alice", "attendees": "nope"})
    assert r.status_code == 400 and "email address" in r.json()["detail"]


def test_admin_lookup(api):
    client, tok, _, _ = api
    assert client.get("/api/mods/v1/users/alice", headers=bearer(tok)).json() == {"user": "alice", "admin": True}
    assert client.get("/api/mods/v1/users/bob", headers=bearer(tok)).json()["admin"] is False


# ── Public paths ─────────────────────────────────────────────────────────

class CookieAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        tok = request.cookies.get("odysseus_session")
        if tok and FakeAuth().validate_token(tok):
            request.state.current_user = tok.split("-")[0]
        return await call_next(request)


@pytest.fixture
def hook_stack(mods_dir):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.path, self.headers.get("X-Odysseus-User"), self.rfile.read(n)))
            body = b"<Response/>"
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(CookieAuth)
    with UvicornThread(app) as up:
        yield up, srv.server_address[1], seen
    srv.shutdown()


def test_public_paths_reach_a_mod_granted_public_http_with_no_one_signed_in(hook_stack):
    up, port, seen = hook_stack
    _, token = add_mod(permissions=["public.http"])
    mark_running("test-mod", port)
    r = httpx.post(up.url + "/mods/test-mod/public/twilio/voice", data={"From": "+15550102000"},
                   headers={"X-Odysseus-User": "alice", "Sec-Fetch-Dest": "document"})
    assert r.status_code == 200 and r.text == "<Response/>"
    path, user, body = seen[-1]
    assert path == "/public/twilio/voice" and user == signing.PUBLIC_USER and b"From=" in body
    # Signed in or not, a public path is the public identity, and never a redirect to a frame URL.
    r = httpx.get(up.url + "/mods/test-mod/public/sip/x.xml", cookies={"odysseus_session": "alice-session"},
                  headers={"Sec-Fetch-Dest": "document"}, follow_redirects=False)
    assert r.status_code == 200 and seen[-1][1] == signing.PUBLIC_USER
    # Everything else still needs a sign-in.
    assert httpx.get(up.url + "/mods/test-mod/settings").status_code == 401


def test_without_the_permission_public_paths_need_a_sign_in(hook_stack):
    up, port, seen = hook_stack
    add_mod(permissions=[])
    mark_running("test-mod", port)
    assert httpx.post(up.url + "/mods/test-mod/public/twilio/voice").status_code == 401
    assert seen == []


def test_funnel_reaches_only_mods_public_paths():
    from core.middleware import FunnelGuardMiddleware
    got = []

    async def app(scope, receive, send):
        got.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    guard = FunnelGuardMiddleware(app)

    async def hit(path):
        out = []

        async def send(m):
            out.append(m)
        await guard({"type": "http", "path": path, "headers": [(b"tailscale-funnel-request", b"?1")]},
                    None, send)
        return out[0]["status"]
    assert asyncio.run(hit("/mods/phone/public/twilio/voice")) == 200
    for p in ("/api/telephony/twilio/voice", "/mods/phone/settings", "/", "/api/sessions"):
        assert asyncio.run(hit(p)) == 404, p
    assert got == ["/mods/phone/public/twilio/voice"]


def test_the_public_exemption_in_app_py_matches_only_public_paths():
    import os
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py"), encoding="utf-8").read()
    pat = re.compile(re.search(r'_re\.compile\(r"(\^/mods/\[a-z\][^"]+/public/[^"]*)"\)', src).group(1))
    assert pat.match("/mods/phone/public/sms/inbound/abc")
    for p in ("/mods/phone/", "/mods/phone/api/sms/config", "/mods/phone/~t.s/public/x", "/api/mods/phone/public/x"):
        assert not pat.match(p), p


def test_the_new_permissions_are_described():
    from src.mods import manifest as mf
    for p in ("chats", "speech", "browser", "google.calendar", "calendar.read", "users", "public.http"):
        assert mf.PERMISSIONS.get(p), p
    man = mf.validate({"id": "phone", "name": "Phone", "version": "0.1.0",
                       "runtime": {"type": "process", "command": ["python", "-m", "phone_mod"]},
                       "permissions": ["chats", "speech", "public.http"]})
    assert man["permissions"] == ["chats", "public.http", "speech"]
    assert host_api.speech_problems is not None
