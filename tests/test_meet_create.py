"""Making Google Meet meetings from the user's Google account
(src/meet/google_calendar.py, POST /api/meet/create, the google_meet tool),
reusing a meeting's chat, the fast "guest turned away" message, and routing
Meet follow-ups to the Meet tool.

Google is never reached: every call goes to an httpx.MockTransport that
plays the token endpoint, Calendar and the Meet REST API.
"""

import asyncio
import json
import os
import urllib.parse
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import google_oauth
from src.meet import config as meet_config, google_calendar, session as meet_session
from tests.test_google_meet import H, env  # noqa: F401  (the fixture)

ROOT = Path(__file__).resolve().parents[1]
REFRESH = "1//refresh-token-never-shown"
ACCESS = "ya29.access-token"
CODE = "abc-defg-hij"


def _id_token(email):
    import base64
    body = base64.urlsafe_b64encode(json.dumps({"email": email, "sub": "1"}).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class FakeGoogle:
    """The token endpoint, Calendar events and Meet spaces, recorded."""

    def __init__(self):
        self.calls = []
        self.pending = 0                  # events.insert answers "pending" this many times
        self.meet_api = "ok"              # "ok", "disabled"
        self.refresh = "ok"               # "ok", "invalid_grant"
        self.access_type = "OPEN"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url, request.content.decode() if request.content else ""))
        if url.startswith(google_oauth.TOKEN_URL):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            if form.get("grant_type") == "authorization_code":
                return httpx.Response(200, json={
                    "access_token": ACCESS, "expires_in": 3600, "refresh_token": REFRESH,
                    "scope": f"openid https://www.googleapis.com/auth/userinfo.email "
                             f"{google_calendar.CALENDAR_SCOPE} {google_calendar.MEET_SETTINGS_SCOPE}",
                    "id_token": _id_token("jaron@example.com")})
            if self.refresh == "invalid_grant":
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert form.get("refresh_token") == REFRESH
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        if url.startswith(google_oauth.REVOKE_URL):
            return httpx.Response(200)
        assert request.headers.get("authorization") == f"Bearer {ACCESS}"
        if url.startswith(google_calendar.EVENTS_URL):
            ev = {"id": "ev1", "htmlLink": "https://calendar.google.com/event?eid=ev1",
                  "conferenceData": {"conferenceId": CODE, "createRequest": {"status": {"statusCode": "success"}}},
                  "hangoutLink": f"https://meet.google.com/{CODE}"}
            if self.pending > 0:
                self.pending -= 1
                ev.pop("hangoutLink")
                ev["conferenceData"] = {"createRequest": {"status": {"statusCode": "pending"}}}
            return httpx.Response(200, json=ev)
        if url.startswith(google_calendar.MEET_API):
            if self.meet_api == "disabled":
                return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "x",
                                                           "details": [{"reason": "SERVICE_DISABLED"}]}})
            if request.method == "GET":
                return httpx.Response(200, json={"name": "spaces/jQCFfuBOdN5z", "meetingCode": CODE})
            return httpx.Response(200, json={"name": "spaces/jQCFfuBOdN5z",
                                             "config": {"accessType": self.access_type}})
        return httpx.Response(404)


@pytest.fixture
def google(tmp_path, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(google_calendar, "_transport", httpx.MockTransport(fake))
    monkeypatch.setattr(google_calendar, "_dir", lambda: str(tmp_path / "google_calendar"))

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(google_calendar, "_sleep", no_sleep)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(google_oauth, "_setting", lambda key: "")
    google_calendar._access.clear()
    google_oauth._states.clear()
    yield fake
    google_calendar._access.clear()


def _connect(user="alice"):
    ok, msg = asyncio.run(google_calendar.finish_connect(user, "the-code", "https://box.ts.net/api/auth/google/callback"))
    assert ok, msg
    return msg


# ── Connecting ───────────────────────────────────────────────────────────

class _Req:
    def __init__(self, base="http://box.ts.net/", proto="https"):
        self.base_url = base
        self.headers = {"x-forwarded-proto": proto}


def test_connect_asks_for_offline_calendar_access_through_the_shared_callback(google):
    url = google_calendar.connect_url(_Req(), "alice")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert url.startswith(google_oauth.AUTH_URL)
    assert q["redirect_uri"] == "https://box.ts.net/api/auth/google/callback"     # https behind Serve
    assert q["access_type"] == "offline" and q["prompt"] == "consent" and q["include_granted_scopes"] == "true"
    assert google_calendar.CALENDAR_SCOPE in q["scope"].split() and "email" in q["scope"].split()
    entry = google_oauth.pop_state(q["state"])
    assert entry["purpose"] == "calendar" and entry["user"] == "alice"
    assert google_oauth.pop_state(q["state"]) is None                               # used once


def test_the_refresh_token_is_kept_encrypted_and_never_shown(google, tmp_path):
    assert google_calendar.status("alice")["connected"] is False
    msg = _connect()
    assert "jaron@example.com" in msg
    st = google_calendar.status("alice")
    assert st["connected"] and st["email"] == "jaron@example.com" and st["can_open_access"]
    assert REFRESH not in json.dumps(st)
    files = list((tmp_path / "google_calendar").iterdir())
    assert len(files) == 1 and REFRESH not in files[0].read_text()
    if os.name == "posix":
        assert (files[0].stat().st_mode & 0o777) == 0o600
    assert google_calendar.status("bob")["connected"] is False                     # per user
    asyncio.run(google_calendar.disconnect("alice"))
    assert google_calendar.status("alice")["connected"] is False
    assert any(c[1].startswith(google_oauth.REVOKE_URL) for c in google.calls)


# ── Making a meeting ─────────────────────────────────────────────────────

def test_create_inserts_an_event_with_a_meet_and_opens_its_access(google):
    _connect()
    google_calendar._access.clear()                       # forces a refresh from the saved token
    google.pending = 1
    from datetime import datetime, timezone
    made = asyncio.run(google_calendar.create_meeting(
        "alice", title="Quick sync", start=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc), minutes=30,
        attendees=["bob@x.com"]))
    assert made["url"] == f"https://meet.google.com/{CODE}" and made["open_access"] is True
    assert made["end"].startswith("2026-10-02T15:30")
    method, url, body = next(c for c in google.calls if c[0] == "POST" and c[1].startswith(google_calendar.EVENTS_URL))
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q == {"conferenceDataVersion": "1", "sendUpdates": "all"}
    ev = json.loads(body)
    assert ev["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert ev["attendees"] == [{"email": "bob@x.com"}] and ev["summary"] == "Quick sync"
    assert any(c[0] == "GET" and "/events/ev1" in c[1] for c in google.calls)       # asked again while pending
    patch = next(c for c in google.calls if c[0] == "PATCH")
    assert "spaces/jQCFfuBOdN5z" in patch[1] and "updateMask=config.accessType" in patch[1]
    assert json.loads(patch[2]) == {"config": {"accessType": "OPEN"}}


def test_no_invites_without_people_and_access_failure_is_explained(google):
    _connect()
    google.meet_api = "disabled"
    made = asyncio.run(google_calendar.create_meeting("alice"))
    url = next(c[1] for c in google.calls if c[0] == "POST" and c[1].startswith(google_calendar.EVENTS_URL))
    assert "sendUpdates=none" in url
    assert made["open_access"] is False and "Meet REST API is not enabled" in made["access_note"]
    assert "set Meeting access to Open" in google_calendar.access_hint(made)


def test_a_revoked_connection_is_forgotten_with_a_way_back(google):
    _connect()
    google_calendar._access.clear()
    google.refresh = "invalid_grant"
    with pytest.raises(google_calendar.GoogleError) as e:
        asyncio.run(google_calendar.create_meeting("alice"))
    assert "Connect Google Calendar again" in str(e.value)
    assert google_calendar.status("alice")["connected"] is False


def test_attendees_and_start_times_are_checked(google):
    assert google_calendar.clean_attendees("Bob@X.com, amy@y.org; bob@x.com") == ["bob@x.com", "amy@y.org"]
    with pytest.raises(google_calendar.GoogleError):
        google_calendar.clean_attendees(["bob"])
    assert google_calendar.parse_start("2026-10-02T15:00:00+00:00").hour == 15
    with pytest.raises(google_calendar.GoogleError):
        google_calendar.parse_start("whenever-ish")


# ── The routes ───────────────────────────────────────────────────────────

class _Admins:
    def is_admin(self, user):
        return user == "alice"


def test_card_status_says_what_to_do_without_a_client(env, google, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID")
    c = env["client"]
    g = c.get("/api/meet/google", headers=H).json()
    assert g["configured"] is False and g["redirect_uri"].endswith("/api/auth/google/callback")
    assert "refresh_token" not in json.dumps(g)
    r = c.get("/api/meet/google/connect", headers=H, follow_redirects=False)
    assert r.status_code == 400 and "OAuth client" in r.json()["detail"]
    # Only an admin saves the server's client.
    r = c.put("/api/meet/google/client", headers=H, json={"client_id": "1.apps.googleusercontent.com",
                                                         "client_secret": "s"})
    assert r.status_code == 403
    saved = {}
    monkeypatch.setattr(google_oauth, "save_client", lambda cid, sec: saved.update(cid=cid, sec=sec))
    env["app"].state.auth_manager = _Admins()
    assert c.put("/api/meet/google/client", headers=H, json={"client_id": "nope", "client_secret": "s"}).status_code == 400
    r = c.put("/api/meet/google/client", headers=H, json={"client_id": "1.apps.googleusercontent.com",
                                                         "client_secret": "s"})
    assert r.status_code == 200 and saved == {"cid": "1.apps.googleusercontent.com", "sec": "s"}
    assert "client_secret" not in r.json()


def test_connect_redirects_to_google(env, google):
    r = env["client"].get("/api/meet/google/connect", headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(google_oauth.AUTH_URL)


def test_create_now_needs_no_join_switch_but_joining_does(env, google):
    c = env["client"]
    r = c.post("/api/meet/create", headers=H, json={"start": "now"})
    assert r.status_code == 400 and "not connected" in r.json()["detail"]
    _connect()
    r = c.post("/api/meet/create", headers=H, json={"start": "now", "attendees": "bob@x.com", "title": "Sync"})
    assert r.status_code == 200, r.text
    made = r.json()
    assert made["url"].endswith(CODE) and made["joined"] is None
    assert "Switch on Join meetings at the top of the Google Meet card" in made["join_error"]
    assert "Google sent invites to bob@x.com" in made["message"]


def test_create_now_joins_and_says_to_admit_it(env, google, monkeypatch):
    from routes import meet_routes
    env["configure_meet"]()
    _connect()
    monkeypatch.setattr(meet_routes, "_readiness", lambda user, cfg: {"engines": [], "browser": True, "phone": []})
    started = []

    async def fake_start(self, is_admin=False):
        self._open_chat(is_admin)
        started.append(self)

    monkeypatch.setattr(meet_session.Meeting, "start", fake_start)
    made = env["client"].post("/api/meet/create", headers=H, json={}).json()
    assert started and started[0].url == f"https://meet.google.com/{CODE}"
    assert made["joined"]["sid"] and "admit Odysseus (AI)" in made["message"]
    # Its chat is recorded, so joining it again goes on in the same chat.
    assert meet_config.chat_for("alice", started[0].chat_key()) == made["joined"]["sid"]


def test_scheduling_later_does_not_join(env, google):
    _connect()
    c = env["client"]
    r = c.post("/api/meet/create", headers=H, json={"start": "2099-01-01T10:00:00+00:00", "join": True})
    assert r.status_code == 400 and "starts now" in r.json()["detail"]
    made = c.post("/api/meet/create", headers=H, json={"start": "2099-01-01T10:00:00+00:00", "minutes": 45}).json()
    assert made["joined"] is None and not made["join_error"] and "Google Calendar" in made["message"]


def test_the_join_switch_error_points_at_the_switch(env):
    r = env["client"].post("/api/meet/join", headers=H, json={"url": CODE})
    assert r.status_code == 400 and "Switch on Join meetings" in r.json()["detail"]


def test_the_shared_callback_finishes_a_calendar_connection(tmp_path, google, monkeypatch):
    from routes import auth_routes
    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)

    class _Mgr:
        def get_username_for_token(self, t):
            return None

    app = FastAPI()
    app.include_router(auth_routes.setup_auth_routes(_Mgr()))
    google_oauth.remember_state("st1", {"purpose": "calendar", "user": "alice",
                                        "redirect_uri": "https://box.ts.net/api/auth/google/callback"})
    with TestClient(app) as c:
        r = c.get("/api/auth/google/callback?state=st1&code=the-code", follow_redirects=False)
    assert r.status_code == 200 and "Google Calendar connected" in r.text
    assert REFRESH not in r.text and "set-cookie" not in {k.lower() for k in r.headers}
    assert google_calendar.status("alice")["email"] == "jaron@example.com"


# ── The agent tool ───────────────────────────────────────────────────────

def test_the_tool_starts_a_meeting_and_reports_status(env, google):
    from src.meet import tool as meet_tool
    out = asyncio.run(meet_tool.run_tool('{"action": "status"}', owner="alice"))
    assert "not connected" in out["output"] and "Join meetings switch" in out["output"]
    _connect()
    out = asyncio.run(meet_tool.run_tool(json.dumps({"action": "start", "attendees": ["bob@x.com"]}), owner="alice"))
    assert out["exit_code"] == 0 and out["meeting"]["url"].endswith(CODE)
    assert "refresh" not in json.dumps(out).lower()
    out = asyncio.run(meet_tool.run_tool('{"action": "schedule"}', owner="alice"))
    assert out["exit_code"] == 1
    out = asyncio.run(meet_tool.run_tool("https://meet.google.com/abc-defg-hij", owner="alice"))
    assert out["exit_code"] == 1 and "Switch on Join meetings" in out["error"]


# ── One chat per meeting ─────────────────────────────────────────────────

def test_joining_the_same_meeting_again_reuses_its_chat(env, monkeypatch):
    notes = []
    from src.telephony import agent
    monkeypatch.setattr(agent, "note", lambda sid, text, **k: notes.append((sid, text)))
    cfg = {"enabled": True, "model": "qwen", "endpoint_id": "ep-a"}
    a = meet_session.Meeting("alice", cfg, "https://meet.google.com/abc-defg-hij", via="browser")
    a._open_chat(False)
    b = meet_session.Meeting("alice", cfg, "https://meet.google.com/abc-defg-hij", via="browser")
    b._open_chat(False)
    assert a.sid and b.sid == a.sid and len(env["made"]) == 1
    assert notes[-1][1].startswith("Joining again")
    c = meet_session.Meeting("alice", cfg, "https://meet.google.com/xyz-abcd-efg", via="browser")
    c._open_chat(False)
    assert c.sid != a.sid and len(env["made"]) == 2
    # Someone else's join of the same link is their own chat.
    d = meet_session.Meeting("bob", cfg, "https://meet.google.com/abc-defg-hij", via="browser")
    d._open_chat(False)
    assert d.sid not in (a.sid, c.sid)
    # A deleted chat is not reused.
    env["mgr"].sessions.pop(a.sid)
    e = meet_session.Meeting("alice", cfg, "https://meet.google.com/abc-defg-hij", via="browser")
    e._open_chat(False)
    assert e.sid not in (a.sid, c.sid, d.sid)


# ── A guest turned away at once ──────────────────────────────────────────

class _DeniedBrowser:
    def __init__(self, *a):
        self.page = None

    async def open(self, *a, **k):
        pass

    async def status(self):
        return {"state": "denied"}

    async def leave(self):
        pass

    async def close(self):
        pass


def test_a_guest_turned_away_at_once_gets_the_meeting_access_advice(env, monkeypatch):
    from src.telephony import agent
    monkeypatch.setattr(agent, "note", lambda *a, **k: None)

    async def run():
        m = meet_session.Meeting("alice", {"enabled": True, "model": "qwen", "endpoint_id": "ep-a"},
                                 "https://meet.google.com/abc-defg-hij", via="browser",
                                 browser_factory=lambda on_audio, on_mark: _DeniedBrowser())
        m.sid = "s"
        await m._run_browser()
        return m

    m = asyncio.run(run())
    pub = m.public()
    assert pub["error_code"] == "guest_denied"
    assert "Signed in" in pub["error"] and "separate Google account" in pub["error"] and "Trusted" in pub["error"]


# ── Routing Meet requests to the Meet tool ───────────────────────────────

def test_meet_follow_ups_select_the_meet_tool():
    from src.agent_loop import _classify_agent_request, _DOMAIN_TOOL_MAP
    from src.tool_index import ToolIndex
    for text in ("join again", "try joining through my browser", "rejoin the meeting",
                 "start a meet with bob@x.com", "set up a meeting tomorrow at 3"):
        dom = _classify_agent_request([{"role": "user", "content": text}], text)["domains"]
        assert "meet" in dom, text
        assert "google_meet" in _DOMAIN_TOOL_MAP["meet"]
        hinted = set()
        for kws, tools in ToolIndex._KEYWORD_HINTS.items():
            if any(k in text.lower() for k in kws):
                hinted |= tools
        assert "google_meet" in hinted, text


def test_a_meeting_chat_always_has_the_meet_tool(env, monkeypatch):
    from src import agent_loop
    meet_config.remember_chat("alice", "meet:abc-defg-hij", "chat-1")
    assert agent_loop._is_meet_chat("chat-1", "alice")
    assert not agent_loop._is_meet_chat("chat-2", "alice")
    from core.models import Session
    env["mgr"].sessions["chat-3"] = Session(id="chat-3", name="Google Meet 14:05 (abc-defg-hij)",
                                            endpoint_url="http://llm/v1", model="qwen", owner="alice")
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: env["mgr"])
    assert agent_loop._is_meet_chat("chat-3", "alice")


def test_the_tool_description_keeps_it_off_the_desktop():
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    d = BUILTIN_TOOL_DESCRIPTIONS["google_meet"]
    assert "cloud browser" in d and "never needs the user's desktop" in d
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "google_meet")
    assert "never needs the user's desktop" in schema["function"]["description"]


# ── The card and the text ────────────────────────────────────────────────

def test_the_card_has_the_make_a_meeting_controls():
    js = (ROOT / "static" / "js" / "devicesSettings.js").read_text()
    html = (ROOT / "static" / "index.html").read_text()
    for i in ("meet-google-connect", "meet-google-disconnect", "meet-google-redirect", "meet-create-now",
              "meet-create-schedule", "meet-google-cid", "meet-google-secret"):
        assert f'id="{i}"' in html and i in js, i
    assert "/api/meet/google/connect" in html and "/api/meet/create" in js


def test_no_em_dashes_in_what_this_adds():
    for f in ("src/google_oauth.py", "src/meet/google_calendar.py", "src/meet/tool.py", "src/meet/session.py",
              "src/meet/config.py", "routes/meet_routes.py", "docs/google-meet.md", "tests/test_meet_create.py"):
        assert chr(0x2014) not in (ROOT / f).read_text(), f
