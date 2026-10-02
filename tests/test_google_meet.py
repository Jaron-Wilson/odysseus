"""Google Meet: the voice agent joins a meeting (src/meet/, routes/meet_routes.py).

No real Google or Twilio here. Joining by phone runs end to end through
src/telephony/simulator.py (Twilio's side) against a real server: the
outbound call, the signed webhook that keys in the PIN, and the meeting's
audio over a Media Stream. Joining through the browser runs a real headless
Chromium (when there is one) on tests/fixtures/fake_meet.html, a stand-in
for Meet's page with the same buttons and a WebRTC loopback, so the injected
microphone and the audio capture are exercised for real.
"""

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.models import ChatMessage, Session, in_context
from routes import prefs_routes
from src.meet import config as meet_config, links, session as meet_session
from src.telephony import call as call_mod, codec, simulator, twilio
from tests.test_phone_calls import AGENT_NUM, ALICE, PUBLIC, SID, TOKEN, _Mgr, _Stubs

ROOT = Path(__file__).resolve().parents[1]
DIAL = "+16505550123"
PIN = "123456789"

GOOGLE_DESCRIPTION = """Weekly sync

-::~:~::~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~::~:~::-
Join with Google Meet: https://meet.google.com/abc-defg-hij
Or dial: (US) +1 650-555-0123 PIN: 123 456 789#
More phone numbers: https://tel.meet/abc-defg-hij?pin=123456789

Learn more about Meet at: https://support.google.com/a/users/answer/9282720
"""


# ── Links and dial-in details ────────────────────────────────────────────

def test_meet_links_are_checked_and_made_canonical():
    assert links.meet_url("abc-defg-hij") == "https://meet.google.com/abc-defg-hij"
    assert links.meet_url("https://meet.google.com/ABC-DEFG-HIJ?authuser=1") == "https://meet.google.com/abc-defg-hij"
    assert links.meet_url("meet.google.com/abc-defg-hij") == "https://meet.google.com/abc-defg-hij"
    assert links.meet_url("https://meet.google.com/lookup/abcd1234") == "https://meet.google.com/lookup/abcd1234"
    for bad in ("", "https://evil.example/meet.google.com/abc-defg-hij", "https://meet.google.com.evil.example/abc-defg-hij",
                "https://zoom.us/j/123", "abc-defg-hijk", "javascript:alert(1)"):
        assert links.meet_url(bad) == "", bad


def test_dial_in_and_link_come_out_of_a_google_calendar_event():
    assert links.find_dial_in(GOOGLE_DESCRIPTION) == {"number": DIAL, "pin": PIN}
    ev = {"uid": "u1", "summary": "Weekly sync", "dtstart": "2026-10-01T15:00:00Z", "dtend": "2026-10-01T15:30:00Z",
          "location": "", "description": GOOGLE_DESCRIPTION}
    m = links.meeting_from_event(ev)
    assert m["url"] == "https://meet.google.com/abc-defg-hij" and m["dial_in"] == {"number": DIAL, "pin": PIN}
    assert links.meeting_from_event({**ev, "description": "Lunch at noon"}) is None
    # Only US and Canada numbers are dialed.
    assert links.phone_number("+44 20 7946 0000") == "" and links.phone_number("(650) 555-0123") == DIAL
    assert links.clean_pin("123 456 789#") == PIN and links.clean_pin("12") == ""


# ── Words: wake word, leaving, the announcement ──────────────────────────

def test_wake_word_finds_what_it_was_asked():
    p = meet_session.wake_patterns(["odysseus"])
    f = meet_session.find_wake
    assert f("Odysseus, what did we decide?", p) == "what did we decide?"
    assert f("Hey Odyssey what's the deadline", p) == "what's the deadline"
    assert f("What do you think, Odysseus?", p) == "What do you think?"
    assert f("So, Odysseus, what is next?", p) == "what is next?"
    assert f("Odysseus.", p) == ""
    assert f("we read the Odyssey in school", p) is None
    assert f("let's move on", p) is None
    custom = meet_session.wake_patterns(["computer"])
    assert f("computer, take a note", custom) == "take a note"


def test_leave_phrases():
    for t in ("leave the meeting", "you can leave now", "okay bye", "Leave the call, thanks.", "goodbye"):
        assert meet_session.LEAVE_RE.match(t), t
    for t in ("leave the room please", "what time should we leave", "bye the way, the numbers are up"):
        assert not meet_session.LEAVE_RE.match(t), t


def test_announcement_always_says_it_is_an_ai_and_transcribing():
    assert "AI" in meet_config.announcement({}, "assistant", "Jaron")
    assert "Jaron" in meet_config.announcement({}, "assistant", "Jaron")
    custom = meet_config.announcement({"announcement": "Hello everyone."}, "assistant", "Jaron")
    assert custom.startswith("Hello everyone") and "AI" in custom and "transcribed" in custom
    assert meet_config.view({})["enabled"] is False          # off by default


def test_transcript_lines_are_shown_but_not_read_as_conversation():
    assert not in_context(ChatMessage("assistant", "12:00:01  hello", metadata={"note": True, "transcript": True}))
    assert in_context(ChatMessage("assistant", "Joining.", metadata={"note": True}))


class _FakeCall:
    state = "listening"

    def __init__(self):
        self.said = []

    def say(self, text, hold=False):
        self.said.append(text)


def test_assistant_mode_answers_only_when_called(monkeypatch):
    notes = []
    from src.telephony import agent
    monkeypatch.setattr(agent, "note", lambda sid, text, source="", **meta: notes.append((text, meta)))
    m = meet_session.Meeting("alice", {"enabled": True}, "https://meet.google.com/abc-defg-hij", mode="assistant",
                             via="browser", title="Weekly sync")
    m.sid = "s1"
    m.call = _FakeCall()
    assert m.respond("we should ship on friday") is None
    assert m.respond("the docs need another pass") is None
    msg = m.respond("Odysseus, what did we decide?")
    assert "ship on friday" in msg and "docs need another pass" in msg and msg.endswith("what did we decide?")
    assert "Weekly sync" in msg
    # Only the lines since it last spoke go with the next question.
    assert "ship on friday" not in m.respond("Odysseus, and who writes the docs?")
    # Called by name alone: "Yes?", and the next thing said is the question.
    assert m.respond("Odysseus?") is None and m.call.said == ["Yes?"]
    assert m.respond("how long is left").endswith("how long is left")
    assert m.respond("Odysseus, leave the meeting") == "leave the meeting"
    assert len(m.transcript) == 7
    assert all(meta.get("transcript") for _, meta in notes)


def test_talk_mode_answers_every_turn():
    m = meet_session.Meeting("alice", {"enabled": True}, "https://meet.google.com/abc-defg-hij", mode="talk")
    m.call = _FakeCall()
    assert m.respond("what's the weather") == "what's the weather"


def test_phone_line_ignores_meets_own_prompts(monkeypatch):
    from src.telephony import agent
    monkeypatch.setattr(agent, "note", lambda *a, **k: None)
    m = meet_session.Meeting("alice", {"enabled": True}, "", mode="talk", via="phone",
                             dial_in={"number": DIAL, "pin": PIN})
    m.call = _FakeCall()
    assert m.respond("Welcome to Google Meet. Please enter the meeting PIN followed by the pound key.") is None
    assert m.transcript == [] and m.call.said == []
    assert m.respond("You have joined the meeting.") is None
    assert m.call.said and "AI" in m.call.said[0]          # announced once it is in
    assert m.respond("hi there") == "hi there"
    assert len(m.call.said) == 1


# ── Routes ───────────────────────────────────────────────────────────────

def _make_app():
    from core.middleware import FunnelGuardMiddleware
    from routes import meet_routes, telephony_routes
    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(telephony_routes.setup_telephony_routes())
    app.include_router(meet_routes.setup_meet_routes())
    app.add_middleware(FunnelGuardMiddleware)
    return app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    from routes import telephony_routes
    telephony_routes._PENDING.clear()
    telephony_routes._MEET_DIALS.clear()
    meet_session.ACTIVE.clear()
    meet_session.RECENT.clear()

    mgr = _Mgr()
    from src import ai_interaction
    import core.models as cm
    monkeypatch.setattr(ai_interaction, "_session_manager", mgr)
    monkeypatch.setattr(cm, "_SESSION_MANAGER_INSTANCE", mgr)
    made = []

    def fake_create(sm, owner, model, endpoint_id, name=""):
        sid = f"meet{len(made) + 1}"
        mgr.sessions[sid] = Session(id=sid, name=name, endpoint_url="http://llm/v1", model=model, owner=owner)
        made.append((sid, owner, name))
        return sid, mgr.sessions[sid]

    import routes.session_routes as sr
    monkeypatch.setattr(sr, "create_direct_chat", fake_create)
    import routes.sms_routes as sms
    monkeypatch.setattr(sms, "available_models", lambda owner, is_admin: [
        {"model": "qwen", "name": "qwen", "endpoint_id": "ep-a", "url": "http://llm/v1", "endpoint_name": "Box"}])

    async def no_push(owner, text):
        return {}

    monkeypatch.setattr(sms, "_push_owner", no_push)
    stubs = _Stubs()
    replies = []

    async def reply(sid, text, **kw):
        replies.append((sid, text, kw))
        for word in stubs.reply_text.split(" "):
            yield ("delta", word + " ")

    from src.telephony import agent
    monkeypatch.setattr(agent, "reply", reply)
    monkeypatch.setattr(call_mod, "default_stt", stubs.stt)
    monkeypatch.setattr(call_mod, "default_tts", stubs.tts)
    monkeypatch.setattr(call_mod, "engines_ready", lambda: [])
    dials = []

    async def fake_create_call(account_sid, auth_token, from_, to, url):
        dials.append({"from": from_, "to": to, "url": url})
        return {"call_sid": "CA" + "d1" * 16, "status": "queued"}

    monkeypatch.setattr(twilio, "create_call", fake_create_call)
    app = _make_app()
    with TestClient(app) as client:
        def configure_phone(user="alice"):
            r = client.put("/api/telephony/config", headers={"x-test-user": user}, json={
                "enabled": True, "account_sid": SID, "auth_token": TOKEN, "phone_number": AGENT_NUM,
                "numbers": [ALICE], "public_url": PUBLIC, "model": "qwen", "endpoint_id": "ep-a"})
            assert r.status_code == 200, r.text

        def configure_meet(user="alice", **over):
            body = {"enabled": True, "model": "qwen", "endpoint_id": "ep-a", "owner_name": "Alice"}
            body.update(over)
            r = client.put("/api/meet/config", headers={"x-test-user": user}, json=body)
            assert r.status_code == 200, r.text
            return r.json()

        yield {"client": client, "app": app, "mgr": mgr, "made": made, "stubs": stubs, "replies": replies,
               "dials": dials, "configure_phone": configure_phone, "configure_meet": configure_meet}
    meet_session.ACTIVE.clear()


H = {"x-test-user": "alice"}


def test_settings_are_off_by_default_and_validated(env):
    c = env["client"]
    cfg = c.get("/api/meet/config", headers=H).json()
    assert cfg["enabled"] is False and cfg["display_name"] == "Odysseus (AI)" and cfg["mode"] == "assistant"
    r = c.post("/api/meet/join", headers=H, json={"url": "abc-defg-hij"})
    assert r.status_code == 400 and "Switch on Join meetings" in r.json()["detail"]
    assert c.put("/api/meet/config", headers=H, json={"display_name": "Jaron"}).status_code == 400
    assert c.put("/api/meet/config", headers=H, json={"mode": "spy"}).status_code == 400
    assert c.put("/api/meet/config", headers=H, json={"wake_words": "hey there, x"}).status_code == 400
    v = c.put("/api/meet/config", headers=H, json={"max_minutes": 99999, "wake_words": "Odysseus, computer"}).json()
    assert v["max_minutes"] == meet_config.MAX_MAX_MIN and v["wake_words"] == ["odysseus", "computer"]
    assert c.get("/api/meet/config", headers={"x-test-user": "bob"}).json()["wake_words"] == ["odysseus"]


def test_join_checks_the_link_and_the_phone_details(env):
    env["configure_meet"]()
    c = env["client"]
    r = c.post("/api/meet/join", headers=H, json={"url": "https://zoom.us/j/1", "via": "browser"})
    assert r.status_code == 400 and "not a Google Meet link" in r.json()["detail"]
    r = c.post("/api/meet/join", headers=H, json={"via": "phone", "dial_in": DIAL})
    assert r.status_code == 400 and "PIN" in r.json()["detail"]
    r = c.post("/api/meet/join", headers=H, json={"via": "phone", "dial_in": DIAL, "pin": PIN})
    assert r.status_code == 400 and "Phone calls" in r.json()["detail"]      # no Twilio saved yet


# ── By phone, end to end ─────────────────────────────────────────────────

@pytest.fixture
def server(env):
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(env["app"], host="127.0.0.1", port=port, log_level="warning",
                                        ws="websockets", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    assert srv.started
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(5)


def _speak(seconds=1.0):
    return [("audio", simulator.silence(0.3)), ("audio", simulator.tone(seconds)), ("audio", simulator.silence(1.0))]


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_joining_by_phone_keys_in_the_pin_and_takes_notes(env, server):
    import httpx
    env["configure_phone"]()
    env["configure_meet"](via="phone", dial_wait=4)
    st = env["stubs"]
    st.heard = ["Welcome to Google Meet. Enter the meeting PIN followed by the pound key.",
                "we will ship on friday", "Odysseus, when do we ship?", "Odysseus, leave the meeting"]
    st.reply_text = "You said Friday."
    with httpx.Client(timeout=10) as client:
        r = client.post(server + "/api/meet/join", headers=H,
                        json={"via": "phone", "dial_in": "(650) 555-0123", "pin": "123 456 789#", "title": "Sync"})
        assert r.status_code == 200, r.text
        meeting_id = r.json()["id"]
        assert _wait(lambda: env["dials"])
        d = env["dials"][0]
        assert d["to"] == DIAL and d["from"] == AGENT_NUM and d["url"].startswith(PUBLIC + "/api/telephony/twilio/meet?m=")
        tw = simulator.FakeTwilio(server, SID, TOKEN, AGENT_NUM, PUBLIC)
        # Unsigned, or signed with the wrong token: a plain 404.
        assert tw.answered(client, d["url"], DIAL, bad_signature=True)[1].status_code == 404
        call_sid, r = tw.answered(client, d["url"], DIAL)
        tm = simulator.parse_twiml(r.text)
        assert tm.digits == "WWWW" + PIN + "#" and tm.stream_url.endswith("/api/telephony/twilio/stream")
        # The key is good for one answer.
        again = simulator.parse_twiml(tw.answered(client, d["url"], DIAL)[1].text)
        assert not again.stream_url and again.verbs[-1] == "Hangup"
    url = tw._to_local(tm.stream_url)
    script = ([("audio", simulator.silence(1.2))] + _speak() + _speak() + _speak()
              + [("wait_reply", 10), ("sleep", 0.5)] + _speak() + [("wait_reply", 10), ("sleep", 1.0)])
    res = asyncio.run(simulator.media_call(url, tm.params["token"], call_sid, script, timeout=30))
    assert res.closed_by_server                          # "leave the meeting" hung up
    spoken = st.tts_calls
    assert spoken[0].startswith("Hi, I'm Odysseus") and "AI" in spoken[0]
    assert "You said Friday." in spoken and spoken[-1] == call_mod.GOODBYE
    asks = [(t, kw) for _, t, kw in env["replies"] if kw.get("voice_call", True)]
    assert len(asks) == 1
    text, kw = asks[0]
    assert "we will ship on friday" in text and text.endswith("when do we ship?")
    assert kw["source"] == "google_meet" and "Google Meet" in kw["note"]
    assert "PIN" not in text and "pound key" not in text   # Meet's own prompt was not the meeting
    m = next(x for x in meet_session.RECENT if x.id == meeting_id)
    assert _wait(lambda: m.state == "ended", 10), m.state
    summary = [(t, kw) for _, t, kw in env["replies"] if kw.get("voice_call") is False]
    assert len(summary) == 1 and "we will ship on friday" in summary[0][0] and "action items" in summary[0][0]
    hist = env["mgr"].sessions[m.sid].history
    lines = [h.content for h in hist if (h.metadata or {}).get("transcript")]
    assert len(lines) == 3 and not any("PIN" in x for x in lines)
    assert any("Left the meeting" in h.content for h in hist)
    assert PIN not in " ".join(h.content for h in hist)


# ── Through a browser, end to end ────────────────────────────────────────

def _chromium():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return ""
    from src import cloud_browser
    return cloud_browser.chromium_path()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def chromium(tmp_path):
    exe = _chromium()
    if not exe:
        pytest.skip("no Playwright Chromium on this machine")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # Its own profile and port, never the cloud browser's.
    proc = subprocess.Popen([exe, "--headless=new", "--remote-debugging-address=127.0.0.1",
                             f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
                             "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
                             "--mute-audio", "--no-sandbox", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f"http://127.0.0.1:{port}"
    import urllib.request
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(endpoint + "/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    handler = lambda *a, **k: _Quiet(*a, directory=str(ROOT / "tests" / "fixtures"), **k)  # noqa: E731
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield endpoint, f"http://127.0.0.1:{httpd.server_address[1]}/fake_meet.html"
    httpd.shutdown()
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_joining_through_the_browser(env, chromium, monkeypatch):
    endpoint, page_url = chromium
    from src.meet import browser as browser_mod
    st = env["stubs"]
    st.heard = ["we should ship on friday", "Odysseus, what did we decide?"]
    st.reply_text = "Friday."
    made = []

    def factory(on_audio, on_mark):
        b = browser_mod.MeetBrowser(on_audio, on_mark, endpoint_fn=lambda: endpoint, hosts=("127.0.0.1",))
        made.append(b)
        return b

    async def run():
        m = meet_session.Meeting("alice", {"enabled": True, "owner_name": "Alice", "model": "qwen",
                                           "endpoint_id": "ep-a"}, page_url, mode="assistant", via="browser",
                                 title="Sync", browser_factory=factory)
        await m.start()

        async def until(pred, timeout=20.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if await pred() if asyncio.iscoroutinefunction(pred) else pred():
                    return True
                await asyncio.sleep(0.1)
            return False

        assert await until(lambda: m.state == "in"), (m.state, m.error)
        page = made[0].page
        assert await page.evaluate("window.joinedName") == "Odysseus (AI)"
        # The announcement goes out through the synthetic microphone to the room...
        assert await until(lambda: len(st.tts_calls) >= 1)

        async def heard_bot():
            return await page.evaluate("window.botPeak") > 0.02

        assert await until(heard_bot), "the room never heard the bot"
        # ...and into the meeting's chat.

        async def chatted():
            return bool(await page.evaluate("window.chatLog.length"))

        assert await until(chatted)
        assert "AI" in (await page.evaluate("window.chatLog"))[0]
        await asyncio.sleep(1.5)        # the endpointer sets its noise floor
        await page.evaluate("roomSay(1.0)")
        assert await until(lambda: len(m.transcript) >= 1)
        assert not [r for r in env["replies"] if r[2].get("voice_call", True)]
        await asyncio.sleep(0.5)
        await page.evaluate("roomSay(1.0)")
        assert await until(lambda: any(r[2].get("voice_call", True) for r in env["replies"]))
        assert await until(lambda: "Friday." in st.tts_calls)
        ask = [r for r in env["replies"] if r[2].get("voice_call", True)][0][1]
        assert "we should ship on friday" in ask and ask.endswith("what did we decide?")
        await page.evaluate("endMeeting()")
        assert await until(lambda: m.state == "ended", 15), m.state
        return m

    m = asyncio.run(run())
    assert m.reason == "the meeting ended"
    assert any(r[2].get("voice_call") is False for r in env["replies"])          # the summary
    assert made[0].page is None                                                   # tab closed


# ── The card ─────────────────────────────────────────────────────────────

def test_the_settings_card_has_every_field_its_script_uses():
    js = (ROOT / "static" / "js" / "devicesSettings.js").read_text()
    html = (ROOT / "static" / "index.html").read_text()
    ids = set(re.findall(r"\$\('(meet-[a-z-]+)'\)", js)) | set(re.findall(r"#(meet-[a-z-]+)", js))
    assert {"meet-enabled", "meet-join", "meet-url", "meet-upcoming", "meet-active"} <= ids
    for i in sorted(ids):
        assert f'id="{i}"' in html, i


def test_no_em_dashes_in_what_this_adds():
    for f in ("src/meet/__init__.py", "src/meet/config.py", "src/meet/links.py", "src/meet/session.py",
              "src/meet/browser.py", "src/meet/dialin.py", "src/meet/inject.js", "routes/meet_routes.py",
              "docs/google-meet.md", "tests/fixtures/fake_meet.html"):
        assert "\u2014" not in (ROOT / f).read_text(), f

# ── Phone-first defaulting ───────────────────────────────────────────────

def test_default_via_is_phone():
    """The default via is 'phone', not 'browser', so the compliant transport
    (Twilio dial-in) is preferred when available."""
    assert meet_config.view({})["via"] == "phone"
    # An explicit setting is still respected.
    assert meet_config.view({"via": "browser"})["via"] == "browser"
    assert meet_config.view({"via": "phone"})["via"] == "phone"


def test_join_auto_selects_phone_when_dial_in_is_given(env):
    """start_join picks phone when the caller supplies dial-in info,
    even if the user's saved default is browser."""
    env["configure_phone"]()
    env["configure_meet"](via="browser")
    c = env["client"]
    # Providing dial_in + pin without an explicit via= should auto-select phone.
    r = c.post("/api/meet/join", headers=H,
               json={"dial_in": "(650) 555-0123", "pin": "123 456 789#", "title": "Auto"})
    assert r.status_code == 200, r.text
    assert r.json()["via"] == "phone"


def test_join_falls_back_to_config_when_no_dial_in_or_via(env, monkeypatch):
    """When neither dial_in nor via is given, the user's config default is used."""
    from src import cloud_browser
    monkeypatch.setattr(cloud_browser, "enabled", lambda: False)
    env["configure_meet"](via="browser")
    c = env["client"]
    # A bare URL with no dial_in and no explicit via: falls back to the config (browser).
    r = c.post("/api/meet/join", headers=H, json={"url": "abc-defg-hij"})
    # This should try via=browser; since the cloud browser is disabled in this test,
    # it should fail with a browser-unavailable error.
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "browser" in detail.lower() or "chromium" in detail.lower()


# ── Safety / Browser Block Guidance & Tool Wording ───────────────────────

def test_unsafe_browser_detection_in_state_js():
    """_STATE_JS includes regex patterns identifying Google's unsafe-browser block page."""
    from src.meet.browser import _STATE_JS
    assert "unsafe_browser" in _STATE_JS
    assert "this browser or app may not be secure" in _STATE_JS
    assert "couldn.t verify" in _STATE_JS


def test_session_handles_unsafe_browser_guidance():
    """When the browser encounters unsafe_browser, error_code is set to unsafe_browser
    and guidance explains the block and recommends dial-in."""
    from src.meet import session as meet_session
    m = meet_session.Meeting("alice", {"enabled": True}, "https://meet.google.com/abc-defg-hij",
                             mode="assistant", via="browser")
    # Simulate the browser error being assigned
    m.error = meet_session.UNSAFE_BROWSER_DENIED
    m.error_code = "unsafe_browser"
    m.state = "ended"
    pub = m.public()
    assert pub["error_code"] == "unsafe_browser"
    assert "This browser or app may not be secure" in pub["error"]
    assert "phone" in pub["error"].lower() or "dial-in" in pub["error"].lower()


def test_meet_tool_join_wording_does_not_claim_joined():
    """google_meet tool join action returns clear wording indicating join is initiated
    and does NOT claim it has already joined."""
    from src.meet import tool as meet_tool
    from unittest.mock import AsyncMock, patch, MagicMock

    fake_m = MagicMock()
    fake_m.id = "meet123"
    fake_m.via = "phone"
    fake_m.dial_in = {"number": "(650) 555-0123", "pin": "123#"}
    fake_m.public.return_value = {"id": "meet123", "state": "joining", "via": "phone"}

    with patch("routes.meet_routes.start_join", new_callable=AsyncMock, return_value=fake_m):
        res = asyncio.run(meet_tool.run_tool('{"action": "join", "url": "https://meet.google.com/abc-defg-hij", "dial_in": "(650) 555-0123", "pin": "123#"}', owner="alice"))
        assert res.get("exit_code") == 0
        assert "not joined yet" in res["output"].lower() or "has not joined yet" in res["output"].lower()
        assert "phone call" in res["output"].lower() or "dialing" in res["output"].lower()


def test_start_join_accepts_dict_dial_in(env):
    """start_join parses dial_in when passed as a dict with number and pin."""
    env["configure_phone"]()
    env["configure_meet"](via="browser")
    c = env["client"]
    r = c.post("/api/meet/join", headers=H,
               json={"dial_in": {"number": "(650) 555-0123", "pin": "123 456 789#"}, "title": "Dict Dial-in"})
    assert r.status_code == 200, r.text
    assert r.json()["via"] == "phone"


# ── PKCE ─────────────────────────────────────────────────────────────────

def test_pkce_verifier_and_challenge_are_generated():
    """The PKCE helper produces a valid S256 verifier/challenge pair."""
    import base64
    import hashlib
    from src.google_oauth import _pkce
    verifier, challenge = _pkce()
    # The verifier is a URL-safe string between 43 and 128 characters.
    assert 43 <= len(verifier) <= 128
    assert all(c.isalnum() or c in "-_" for c in verifier)
    # The challenge is the S256 hash of the verifier.
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_authorize_url_includes_pkce_challenge():
    """authorize_url adds code_challenge + code_challenge_method to the URL
    and stores the verifier in the state."""
    import urllib.parse
    from unittest.mock import MagicMock
    from src import google_oauth
    # Provide a configured client.
    request = MagicMock()
    request.base_url = "https://example.com/"
    request.headers = {}
    orig_cfg = google_oauth.client_config
    google_oauth.client_config = lambda: {"client_id": "test.apps.googleusercontent.com",
                                          "client_secret": "s3cr3t", "configured": True}
    orig_setting = google_oauth._setting
    google_oauth._setting = lambda key: ""
    try:
        url = google_oauth.authorize_url(request, {"purpose": "test"}, scope="openid")
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert "code_challenge" in params
        assert params["code_challenge_method"] == ["S256"]
        # The verifier should be in the stored state.
        state_key = params["state"][0]
        entry = google_oauth.pop_state(state_key)
        assert entry is not None
        assert "code_verifier" in entry
        # The challenge should match the verifier.
        import base64, hashlib
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(entry["code_verifier"].encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert params["code_challenge"] == [expected]
    finally:
        google_oauth.client_config = orig_cfg
        google_oauth._setting = orig_setting


def test_exchange_code_includes_verifier():
    """exchange_code sends the code_verifier from the state entry."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from src import google_oauth

    captured = {}

    async def mock_post(self, url, data=None, **kw):
        captured.update(data or {})
        resp = AsyncMock()
        resp.status_code = 200
        resp.json.return_value = {"access_token": "tok", "token_type": "Bearer"}
        return resp

    orig_cfg = google_oauth.client_config
    google_oauth.client_config = lambda: {"client_id": "test.apps.googleusercontent.com",
                                          "client_secret": "s3cr3t", "configured": True}
    try:
        state_entry = {"redirect_uri": "https://example.com/callback",
                       "code_verifier": "test_verifier_1234"}
        with patch("httpx.AsyncClient.post", mock_post):
            asyncio.run(google_oauth.exchange_code("auth_code_abc", state_entry))
        assert captured.get("code_verifier") == "test_verifier_1234"
        assert captured.get("code") == "auth_code_abc"
        assert captured.get("grant_type") == "authorization_code"
    finally:
        google_oauth.client_config = orig_cfg
