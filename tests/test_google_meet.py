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


# ── Through a browser, the name box and Join button use real X11 input ──

def test_meet_browser_fills_and_clicks_through_x11_not_cdp(monkeypatch):
    """The same Google CDP-Input block that stops sign-in (confirmed
    2026-10-02, src/cloud_browser.py) stops the automated Meet join too: a
    signed-out guest was turned away at "Join now" from a CDP-driven Chrome
    even on headful Xvfb (session.py's GUEST_DENIED, tested 2026-10-01,
    before the X11 input path existed). So the name box and Join button go
    through real X11 input against the real on-screen window (a guest join's
    own context is a separate, differently sized and positioned real Chrome
    window, not the cloud browser's main one), not Playwright's locator
    fill()/click(), whenever the bot is actually driving the cloud browser's
    own headful Chrome. The name itself goes in one keystroke at a time (not
    xdotool's own near-instant default pace: typed that fast, Meet's own
    join-time check still kicked it, 2026-10-02), and Join is not pressed
    until the box reports back the text that was actually typed."""
    from src import cloud_browser
    from src.meet import browser as browser_mod

    name = "Odysseus (AI) Test"
    calls = []
    typed = {"value": ""}

    async def fake_xdotool(display, *args):
        calls.append((display, args))
        if args and args[0] == "type":
            typed["value"] += args[-1]
        elif args and args[0] == "key" and args[-1] == "ctrl+a":
            typed["value"] = ""     # select-all: the next keystroke replaces it
        return True

    monkeypatch.setattr(cloud_browser, "_xdotool", fake_xdotool)
    monkeypatch.setattr(browser_mod.cloud_browser, "x11_display_for_input", lambda: ":321")
    monkeypatch.setattr(browser_mod.MeetBrowser, "_TYPE_DELAY_S", (0.0, 0.0))

    class _FakeLocator:
        def __init__(self, box=None, visible=True):
            self._box = box
            self._visible = visible
            self.first = self

        async def count(self):
            return 1 if self._box else 0

        async def is_visible(self):
            return self._visible

        async def bounding_box(self):
            return self._box

        async def click(self, timeout=None):
            raise AssertionError("CDP click used instead of X11")

        async def fill(self, text, timeout=None):
            raise AssertionError("CDP fill used instead of X11")

        async def input_value(self):
            return typed["value"]

    name_box = {"x": 10, "y": 8, "width": 180, "height": 20}
    join_box = {"x": 200, "y": 8, "width": 70, "height": 20}

    class _FakePage:
        def is_closed(self):
            return False

        def locator(self, sel):
            if sel == browser_mod.SELECTORS["name_input"]:
                return _FakeLocator(name_box)
            return _FakeLocator(None, visible=False)

        def get_by_role(self, role, name=None):
            if name is browser_mod.SELECTORS["join"]:
                return _FakeLocator(join_box)
            return _FakeLocator(None, visible=False)

        async def title(self):
            return "Meet - fake"

        async def bring_to_front(self):
            pass

        async def evaluate(self, js, *a):
            return 712 if "innerHeight" in js else None

    mb = browser_mod.MeetBrowser(lambda b: None, lambda n: None)
    mb.page = _FakePage()
    assert mb._x11_eligible is True     # endpoint_fn is None: this is the cloud browser itself

    async def fixed_origin(display):
        return 20.0, 20.0, 800.0        # a guest join's own window, not at (0, 0)

    monkeypatch.setattr(mb, "_window_origin", fixed_origin)

    asyncio.run(mb.join(name))

    args = [c[1] for c in calls]
    kinds = [a[0] for a in args]
    assert kinds.count("mousemove") == 2 and kinds.count("click") == 2
    assert kinds.count("key") == 1 and ("key", "--clearmodifiers", "ctrl+a") in args
    type_calls = [a for a in args if a[0] == "type"]
    assert len(type_calls) == len(name)                        # one xdotool call per keystroke
    assert all(a[:3] == ("type", "--clearmodifiers", "--") for a in type_calls)
    assert "".join(a[3] for a in type_calls) == name
    assert typed["value"] == name          # settled before Join was pressed, below
    assert all(c[0] == ":321" for c in calls)                  # targeted the real Xvfb display
    moves = [a for a in args if a[0] == "mousemove"]
    # the name box's center (10+90, 8+10), at window origin (20, 20) plus the
    # tab strip/omnibox chrome (800 window height - 712 reported viewport):
    assert moves[0][-2:] == ("120", "126")
    # the Join button's center (200+35, 8+10), same window and chrome offset:
    assert moves[1][-2:] == ("255", "126")


def test_meet_browser_types_the_name_slowly_with_jitter(monkeypatch):
    """xdotool's own default pace still got a guest kicked at "Join now"
    (2026-10-01); typing the same name by hand, slowly, in a take-over never
    did (2026-10-02). So each keystroke waits a human-sized, jittered gap,
    not a fixed one."""
    from src.meet import browser as browser_mod

    typed = []
    sleeps = []

    async def fake_xdotool(display, *args):
        if args and args[0] == "type":
            typed.append(args[-1])
        return True

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(browser_mod.cloud_browser, "_xdotool", fake_xdotool)
    monkeypatch.setattr(browser_mod.asyncio, "sleep", fake_sleep)

    mb = browser_mod.MeetBrowser(lambda b: None, lambda n: None)
    asyncio.run(mb._x11_type_slowly(":321", "Hi!"))

    assert typed == ["H", "i", "!"]
    assert len(sleeps) == 3                            # one gap before each keystroke
    lo, hi = browser_mod.MeetBrowser._TYPE_DELAY_S
    assert all(lo <= s <= hi for s in sleeps)
    assert len(set(sleeps)) > 1                         # jittered, not the same gap every time


def test_meet_browser_settle_waits_for_the_box_to_catch_up(monkeypatch):
    """Join is not pressed off a fixed wait: it waits for the box's own
    value to actually become what was typed (a React-controlled input can
    lag the last keystroke by a frame or two), and gives up if it never
    does rather than hanging the join forever."""
    from src.meet import browser as browser_mod

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(browser_mod.asyncio, "sleep", fake_sleep)

    class _LaggyLocator:
        def __init__(self, delay_reads: int, value: str):
            self._left = delay_reads
            self._value = value

        async def input_value(self):
            if self._left > 0:
                self._left -= 1
                return ""
            return self._value

    mb = browser_mod.MeetBrowser(lambda b: None, lambda n: None)
    asyncio.run(mb._settle(_LaggyLocator(2, "Odysseus (AI)"), "Odysseus (AI)"))
    assert len(sleeps) == 2                             # polled twice before it caught up


def test_meet_browser_settle_gives_up_rather_than_hang(monkeypatch):
    """A box that never settles (Meet's own layout never got to it, say)
    must not hang the join forever: a real, short timeout, with the real
    clock, so it is not entangled with asyncio's own internal scheduling the
    way monkeypatching time.monotonic() would be."""
    from src.meet import browser as browser_mod

    class _StuckLocator:
        async def input_value(self):
            return ""

    mb = browser_mod.MeetBrowser(lambda b: None, lambda n: None)
    started = time.monotonic()
    asyncio.run(mb._settle(_StuckLocator(), "Odysseus (AI)", timeout=0.15))
    assert time.monotonic() - started < 2.0              # gave up, did not hang


def test_meet_browser_falls_back_to_cdp_when_not_the_cloud_browser(monkeypatch):
    """A caller-supplied endpoint (the test fixture's own throwaway Chromium,
    say) is not the cloud browser's Xvfb display: X11 input would hit
    whatever happens to be on screen there instead, so this must stay on
    Playwright's own input regardless of whether Xvfb is up for something
    else entirely."""
    from src import cloud_browser
    from src.meet import browser as browser_mod

    monkeypatch.setattr(browser_mod.cloud_browser, "x11_display_for_input", lambda: ":99")

    clicked = []

    class _FakeLocator:
        first = None

        def __init__(self):
            self.first = self

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def click(self, timeout=None):
            clicked.append("click")

    class _FakePage:
        def is_closed(self):
            return False

        def get_by_role(self, role, name=None):
            return _FakeLocator()

    mb = browser_mod.MeetBrowser(lambda b: None, lambda n: None, endpoint_fn=lambda: "http://127.0.0.1:1")
    mb.page = _FakePage()
    assert mb._x11_eligible is False
    assert asyncio.run(mb._click(browser_mod.SELECTORS["join"])) is True
    assert clicked == ["click"]


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
