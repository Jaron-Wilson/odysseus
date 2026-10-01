"""Phone calls: Twilio rings Odysseus, the call's audio streams in over a
WebSocket, and the agent answers out loud (routes/telephony_routes.py,
src/telephony/).

The webhook and the media stream are open to the internet by design, so a
good part of this is about who gets nothing: a forged or unsigned webhook,
a stranger's number, a wrong PIN, a reused stream token, and a Funnel
request for anything but the phone paths. The rest runs whole calls through
src/telephony/simulator.py (Twilio's side of the protocol) against a real
server on a local port: the greeting, a turn into the call's chat through
the same detached agent run a queued message uses, barge-in, and hanging up.
"""

import asyncio
import io
import json
import logging
import shutil
import socket
import subprocess
import threading
import time
import wave

import numpy as np
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.models import ChatMessage, Session
from routes import prefs_routes
from src.telephony import call as call_mod, codec, config, simulator, speech, twilio

ALICE = "+15550102000"
BOB = "+15550103000"
STRANGER = "+15559998888"
AGENT_NUM = "+15550109999"
SID = "AC" + "a1" * 16
TOKEN = "f00dfeedcafe" * 2 + "abcd1234"          # 32 characters, like Twilio's
PUBLIC = "https://ody.example.ts.net:8443"


# ── Codec ────────────────────────────────────────────────────────────────

def test_mulaw_decode_matches_g711():
    # Reference points from ITU-T G.711 (mu-law): 0xFF and 0x7F are zero,
    # 0x80 / 0x00 the positive / negative peaks.
    d = codec.ulaw_to_pcm16(bytes([0xFF, 0x7F, 0x80, 0x00, 0xF0, 0x70]))
    assert d.tolist() == [0, 0, 32124, -32124, 120, -120]


def test_mulaw_encode_is_bit_exact_with_audioop_where_it_exists():
    x = np.arange(-32768, 32768, 7, dtype=np.int16)
    mine = codec.pcm16_to_ulaw(x)
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop
    except ImportError:
        # Python 3.13 dropped audioop: check the round trip instead.
        back = codec.ulaw_to_pcm16(mine).astype(int)
        assert np.max(np.abs(back - x.astype(int)) / (np.abs(x.astype(int)) + 64)) < 0.07
        return
    assert mine == audioop.lin2ulaw(x.tobytes(), 2)


def test_mulaw_round_trip_keeps_speech_level():
    s = simulator.tone(0.5)
    back = codec.ulaw_to_pcm16(codec.pcm16_to_ulaw(s))
    assert abs(codec.rms(back) - codec.rms(s)) < 0.01


def test_tts_wav_becomes_phone_audio():
    # 24 kHz (Kokoro) and 22.05 kHz stereo (some API servers) both come out
    # as 8 kHz mu-law of the same length.
    t = np.arange(24000) / 24000
    pcm = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    ulaw = codec.to_phone(codec.wav_bytes(pcm, 24000))
    assert len(ulaw) == 8000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        st = np.repeat(pcm[:22050], 2)
        wf.writeframes(st.tobytes())
    assert abs(len(codec.to_phone(buf.getvalue())) - 8000) <= 1
    # The 440 Hz tone survives the low-pass and the resample.
    back = codec.ulaw_to_pcm16(ulaw).astype(float)
    spec = np.abs(np.fft.rfft(back))
    assert abs(np.argmax(spec) * 8000 / len(back) - 440) < 5


def test_downsampling_filters_out_what_8k_cannot_carry():
    t = np.arange(24000) / 24000
    hiss = (np.sin(2 * np.pi * 7000 * t) * 8000).astype(np.int16)    # above the 4 kHz phone band
    out = codec.resample(hiss, 24000, 8000)
    assert codec.rms(out) < 0.1 * codec.rms(hiss)


def test_read_wav_float_and_streaming_size():
    f = (np.sin(np.arange(1600) / 10) * 0.5).astype("<f4")
    hdr = (b"RIFF" + (36 + f.nbytes).to_bytes(4, "little") + b"WAVE" + b"fmt " + (16).to_bytes(4, "little")
           + (3).to_bytes(2, "little") + (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
           + (64000).to_bytes(4, "little") + (4).to_bytes(2, "little") + (32).to_bytes(2, "little")
           + b"data" + (0xFFFFFFFF).to_bytes(4, "little"))
    s, rate = codec.read_wav(hdr + f.tobytes())
    assert rate == 16000 and len(s) == 1600 and 15000 < np.max(s) < 17000
    with pytest.raises(ValueError):
        codec.read_wav(b"ID3 not a wav")


def test_stt_gets_16k_wav():
    wav = codec.for_stt(simulator.tone(1.0))
    s, rate = codec.read_wav(wav)
    assert rate == 16000 and len(s) == 16000


# ── Endpointing and speakable text, same as the in-app call ──────────────

def _vad_events(levels, **kw):
    v = speech.Vad(**kw)
    return [(i, e) for i, lv in enumerate(levels) if (e := v.push(lv, 20))]


def test_vad_calibrates_then_finds_a_turn():
    levels = [0.002] * 50 + [0.2] * 40 + [0.002] * 40
    ev = _vad_events(levels)
    assert [e for _, e in ev] == ["calibrated", "start", "end"]
    # Speech starts after 120 ms above, ends after 700 ms below.
    assert ev[1][0] == 50 + 5 and ev[2][0] == 90 + 34


_JS_PROBE = r"""
globalThis.window = globalThis;
globalThis.document = { readyState: 'complete', getElementById: () => null, querySelector: () => null,
  addEventListener() {} };
globalThis.localStorage = { getItem: () => null, setItem() {} };
const m = await import(process.argv[2]);
const cases = JSON.parse(process.argv[3]);
const out = { text: [], vad: [] };
for (const c of cases.text) {
  const plain = m.speakableText(c);
  const half = m.takeSentences(plain, 0, false);
  out.text.push([plain, half.sentences, half.next, m.takeSentences(plain, 0, true).sentences]);
}
for (const levels of cases.vad) {
  const v = new m.Vad({});
  const ev = [];
  levels.forEach((lv, i) => { const e = v.push(lv, 20); if (e) ev.push([i, e]); });
  out.vad.push(ev);
}
console.log(JSON.stringify(out));
"""

_TEXT_CASES = [
    "# Plan\n**Bold** move. Dr. Who is here. Then *more*",
    "Here is code:\n```py\nprint(1)\n```\nDone! See [the docs](https://x.y/z) or https://a.b/c now.",
    "<think>hmm, let me think</think>The answer is 3.5. Really? Yes.",
    "1. First item\n2. Second item\n| a | b |\n|---|---|\n| 1 | 2 |",
    "Wait for it",
]
_VAD_CASES = [
    [0.002] * 50 + [0.2] * 40 + [0.002] * 40,
    [0.01] * 50 + [0.05] * 3 + [0.01] * 5 + [0.2] * 80 + [0.02] * 20 + [0.2] * 10 + [0.0] * 60,
]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_python_turn_taking_matches_voicecall_js(tmp_path):
    from pathlib import Path
    js = Path(__file__).resolve().parents[1] / "static" / "js" / "voiceCall.js"
    probe = tmp_path / "probe.mjs"
    probe.write_text(_JS_PROBE)
    p = subprocess.run(["node", str(probe), str(js), json.dumps({"text": _TEXT_CASES, "vad": _VAD_CASES})],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    for case, (plain, sentences, nxt, final) in zip(_TEXT_CASES, out["text"]):
        mine = speech.speakable_text(case)
        assert mine == plain
        assert list(speech.take_sentences(mine, 0, False)) == [sentences, nxt]
        assert speech.take_sentences(mine, 0, True)[0] == final
    for levels, ev in zip(_VAD_CASES, out["vad"]):
        assert [list(x) for x in _vad_events(levels)] == ev


# ── Signatures and TwiML ─────────────────────────────────────────────────

def test_signature_matches_twilios_documented_example():
    # docs: twilio.com/docs/usage/security, "Validating Signatures from Twilio".
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert twilio.signature(url, params.items(), "12345") == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert twilio.valid_signature(url, params.items(), "12345", "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")
    assert not twilio.valid_signature(url, params.items(), "54321", "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")
    tampered = dict(params, Digits="9999")
    assert not twilio.valid_signature(url, tampered.items(), "12345", "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")
    assert not twilio.valid_signature(url, params.items(), "12345", "")


def test_signature_accepts_the_url_with_or_without_443():
    params = {"A": "1"}
    sig = twilio.signature("https://h.example/x", params.items(), TOKEN)
    assert twilio.valid_signature("https://h.example:443/x", params.items(), TOKEN, sig)


def test_twiml_escapes_what_it_is_given():
    xml = twilio.say_and_hang_up('Tom & "Jerry" <hi>')
    assert "Tom &amp; \"Jerry\" &lt;hi&gt;" in xml
    tm = simulator.parse_twiml(twilio.connect_stream("wss://h/x", {"token": 'a"b'}))
    assert tm.stream_url == "wss://h/x" and tm.params == {"token": 'a"b'}


# ── The app ──────────────────────────────────────────────────────────────

class _Mgr:
    def __init__(self):
        self.sessions = {}

    def get_sessions_for_user(self, username=None):
        if username is None:
            return self.sessions
        return {k: s for k, s in self.sessions.items() if s.owner == username}

    def get_session(self, sid):
        return self.sessions.get(sid)

    def add_message(self, sid, msg):
        self.sessions[sid].history.append(msg)

    def save_sessions(self):
        pass

    def _persist_message(self, sid, msg):
        pass


def _make_app():
    from core.middleware import FunnelGuardMiddleware
    from routes import telephony_routes
    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(telephony_routes.setup_telephony_routes())
    app.include_router(prefs_routes.setup_prefs_routes())

    @app.get("/api/chat/secret-stuff")
    async def private():
        return {"private": True}

    app.add_middleware(FunnelGuardMiddleware)
    return app


class _Stubs:
    """What the call talks to, recorded: speech to text, text to speech, the agent."""

    def __init__(self):
        self.heard = ["what time is it"]
        self.stt_calls = []
        self.tts_calls = []
        self.replies = []
        self.reply_text = "It is noon. Anything else?"
        self.reply_delay = 0.0

    def stt(self, wav):
        self.stt_calls.append(wav)
        return self.heard.pop(0) if self.heard else "and another thing"

    def tts(self, text):
        self.tts_calls.append(text)
        n = int(24000 * min(1.5, 0.04 * len(text)))
        return codec.wav_bytes((np.sin(np.arange(n) / 3) * 6000).astype(np.int16), 24000)

    async def reply(self, sid, text):
        self.replies.append((sid, text))
        for word in self.reply_text.split(" "):
            if self.reply_delay:
                await asyncio.sleep(self.reply_delay)
            yield ("delta", word + " ")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    from routes import telephony_routes
    telephony_routes._PENDING.clear()
    telephony_routes._MESSAGES.clear()

    mgr = _Mgr()
    from src import ai_interaction
    import core.models as cm
    monkeypatch.setattr(ai_interaction, "_session_manager", mgr)
    monkeypatch.setattr(cm, "_SESSION_MANAGER_INSTANCE", mgr)

    made = []

    def fake_create(sm, owner, model, endpoint_id, name=""):
        sid = f"call{len(made) + 1}"
        s = Session(id=sid, name=name, endpoint_url="http://llm/v1", model=model, owner=owner)
        mgr.sessions[sid] = s
        made.append((sid, owner, model, endpoint_id, name))
        return sid, s

    import routes.session_routes as sr
    monkeypatch.setattr(sr, "create_direct_chat", fake_create)
    # The card's model list reads the endpoints table. Served from the test
    # client's thread, a real query would leave a SQLite connection in that
    # thread's pool slot, which later tests sharing the engine trip over.
    import routes.sms_routes as sms
    monkeypatch.setattr(sms, "available_models", lambda owner, is_admin: [
        {"model": "qwen", "name": "qwen", "endpoint_id": "ep-a", "url": "http://llm/v1", "endpoint_name": "Box"}])

    stubs = _Stubs()
    monkeypatch.setattr(call_mod, "default_stt", stubs.stt)
    monkeypatch.setattr(call_mod, "default_tts", stubs.tts)
    monkeypatch.setattr(call_mod, "engines_ready", lambda: [])

    app = _make_app()
    with TestClient(app) as client:
        def configure(user="alice", **over):
            body = {"enabled": True, "account_sid": SID, "auth_token": TOKEN, "phone_number": AGENT_NUM,
                    "numbers": [ALICE], "public_url": PUBLIC, "model": "qwen", "endpoint_id": "ep-a"}
            body.update(over)
            r = client.put("/api/telephony/config", headers={"x-test-user": user}, json=body)
            assert r.status_code == 200, r.text
            return r.json()

        tw = simulator.FakeTwilio("http://testserver", SID, TOKEN, AGENT_NUM, PUBLIC)
        yield {"client": client, "configure": configure, "tw": tw, "mgr": mgr, "made": made,
               "stubs": stubs, "app": app, "prefs": tmp_path / "user_prefs.json"}


def _call(env, caller=ALICE, **kw):
    call_sid, r = env["tw"].incoming(env["client"], caller, **kw)
    return call_sid, r


def test_allowed_caller_is_connected_to_a_media_stream(env):
    env["configure"]()
    call_sid, r = _call(env)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/xml")
    tm = simulator.parse_twiml(r.text)
    assert tm.verbs[:2] == ["Connect", "Stream"]
    assert tm.stream_url == "wss://ody.example.ts.net:8443/api/telephony/twilio/stream"
    assert len(tm.params["token"]) >= 24
    sid, owner, model, ep, name = env["made"][0]
    assert owner == "alice" and model == "qwen" and ep == "ep-a" and name.startswith("Phone call ")
    assert ALICE in name


def test_forged_or_unsigned_webhooks_are_a_plain_404(env):
    env["configure"]()
    _, r = _call(env, bad_signature=True)
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    _, r = _call(env, sign_with="someone-elses-token-0123456789ab")
    assert r.status_code == 404
    # No signature at all.
    r = env["client"].post("/api/telephony/twilio/voice",
                           data={"AccountSid": SID, "From": ALICE, "To": AGENT_NUM, "CallSid": "CA1"})
    assert r.status_code == 404
    # Signed for another URL (a replay to a different path).
    params = {"AccountSid": SID, "From": ALICE, "To": AGENT_NUM, "CallSid": "CA1", "Direction": "inbound"}
    sig = twilio.signature(PUBLIC + "/api/telephony/twilio/done", params.items(), TOKEN)
    r = env["client"].post("/api/telephony/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 404
    assert env["made"] == []


def test_signature_is_checked_with_the_called_users_token(env):
    env["configure"]("alice")
    env["configure"]("bob", phone_number="+15550107777", numbers=[BOB], auth_token="b0b" * 10 + "xy")
    tw_bob = simulator.FakeTwilio("http://testserver", SID, "b0b" * 10 + "xy", "+15550107777", PUBLIC)
    # Alice's number with Bob's token: no.
    _, r = _call(env, sign_with="b0b" * 10 + "xy")
    assert r.status_code == 404
    _, r = tw_bob.incoming(env["client"], BOB)
    assert r.status_code == 200 and env["made"][-1][1] == "bob"
    # Bob's number does not let Alice's phone in.
    _, r = tw_bob.incoming(env["client"], ALICE)
    assert "Stream" not in r.text


def test_stranger_is_turned_away_politely(env):
    env["configure"]()
    _, r = _call(env, STRANGER)
    tm = simulator.parse_twiml(r.text)
    assert tm.verbs == ["Say", "Hangup"] and "only takes calls from its owner" in tm.say[0]
    assert env["made"] == []


def test_stranger_can_leave_a_message_that_lands_in_a_chat(env, monkeypatch):
    env["configure"](unknown="message")
    call_sid, r = _call(env, STRANGER)
    tm = simulator.parse_twiml(r.text)
    assert "Record" in tm.verbs
    from src.telephony import twilio as tw_mod
    got = {}

    async def fake_fetch(sid, tok, url):
        got["fetch"] = (sid, url)
        return codec.wav_bytes(simulator.tone(1.0), 8000)

    async def fake_delete(sid, tok, rec_sid):
        got["deleted"] = rec_sid

    monkeypatch.setattr(tw_mod, "fetch_recording", fake_fetch)
    monkeypatch.setattr(tw_mod, "delete_recording", fake_delete)
    pushed = []

    async def fake_push(owner, text):
        pushed.append((owner, text))
        return {"sent": 1}

    import routes.sms_routes as sms
    monkeypatch.setattr(sms, "_push_owner", fake_push)
    env["stubs"].heard = ["hi it's the dentist, call me back"]
    r = env["tw"].webhook(env["client"], "/api/telephony/twilio/recording", {
        "CallSid": call_sid, "RecordingSid": "RE" + "1" * 32, "RecordingStatus": "completed",
        "RecordingUrl": "https://api.twilio.com/2010-04-01/Accounts/x/Recordings/RE1", "RecordingDuration": "6"})
    assert r.status_code == 200
    from routes import telephony_routes
    env["client"].portal.call(lambda: asyncio.wait(list(telephony_routes._TASKS), timeout=5) if telephony_routes._TASKS else asyncio.sleep(0))
    sid = env["made"][-1][0]
    assert env["made"][-1][4] == "Phone messages"
    notes = [m.content for m in env["mgr"].sessions[sid].history]
    assert notes == [f"Voice message from {STRANGER} (6s): hi it's the dentist, call me back"]
    assert got["deleted"] == "RE" + "1" * 32 and pushed and pushed[0][0] == "alice"
    # The "after recording" step hangs up, and only for a signed request.
    r = env["tw"].webhook(env["client"], "/api/telephony/twilio/done", {"CallSid": call_sid, "From": STRANGER,
                                                                        "To": AGENT_NUM, "Direction": "inbound"})
    assert simulator.parse_twiml(r.text).verbs[-1] == "Hangup"


def test_recording_url_must_be_twilios(env):
    with pytest.raises(RuntimeError):
        asyncio.run(twilio.fetch_recording(SID, TOKEN, "https://evil.example/steal"))


def test_turned_off_says_so(env):
    env["configure"](enabled=False)
    _, r = _call(env)
    assert "turned off" in r.text and env["made"] == []


def test_allowlist_defaults_to_the_sms_numbers(env):
    from routes import sms_routes
    sms_routes.save_config("alice", {"numbers": [ALICE, "+15550104444"]})
    out = env["configure"](numbers=[])
    assert out["allowed"] == [ALICE, "+15550104444"] and out["numbers_from_sms"] is True
    _, r = _call(env, "+15550104444")
    assert "Stream" in r.text
    # And nothing saved anywhere means no one, not everyone.
    sms_routes.save_config("alice", {})
    _, r = _call(env, ALICE)
    assert "Stream" not in r.text


def test_pin_gate(env):
    env["configure"](pin="2468")
    call_sid, r = _call(env)
    tm = simulator.parse_twiml(r.text)
    assert tm.verbs[:2] == ["Gather", "Say"] and env["made"] == []
    base = {"CallSid": call_sid, "From": ALICE, "To": AGENT_NUM, "Direction": "inbound"}
    r = env["tw"].webhook(env["client"], tm.gather_action, {**base, "Digits": "1111"})
    tm2 = simulator.parse_twiml(r.text)
    assert "not right" in tm2.say[0] and tm2.gather_action.endswith("try=2")
    r = env["tw"].webhook(env["client"], tm2.gather_action, {**base, "Digits": "2468"})
    assert "Stream" in r.text and len(env["made"]) == 1
    # Three wrong tries end the call.
    r = env["tw"].webhook(env["client"], PUBLIC + "/api/telephony/twilio/pin?try=3", {**base, "Digits": "0000"})
    assert simulator.parse_twiml(r.text).verbs == ["Say", "Hangup"]
    # A right PIN from a stranger's phone is still no.
    r = env["tw"].webhook(env["client"], tm.gather_action, {**base, "From": STRANGER, "Digits": "2468"})
    assert "Stream" not in r.text


def test_answer_delay_rings_first(env):
    env["configure"](answer_delay=15)
    _, r = _call(env)
    tm = simulator.parse_twiml(r.text)
    assert tm.verbs == ["Pause", "Redirect"] and 'length="15"' in r.text
    assert tm.redirect == PUBLIC + "/api/telephony/twilio/voice?rung=1"
    call_sid, r = env["tw"].incoming(env["client"], ALICE, path=tm.redirect)
    assert "Stream" in r.text


def test_outbound_call_me_skips_the_pin_and_only_calls_your_numbers(env, monkeypatch):
    env["configure"](pin="2468")
    placed = []

    async def fake_create(sid, tok, from_, to, url):
        placed.append((sid, from_, to, url))
        return {"call_sid": "CA9", "status": "queued"}

    monkeypatch.setattr(twilio, "create_call", fake_create)
    h = {"x-test-user": "alice"}
    r = env["client"].post("/api/telephony/call-me", headers=h, json={"to": STRANGER})
    assert r.status_code == 400 and placed == []
    r = env["client"].post("/api/telephony/call-me", headers=h, json={})
    assert r.status_code == 200 and placed == [(SID, AGENT_NUM, ALICE, PUBLIC + "/api/telephony/twilio/voice")]
    # Twilio then asks what to do once Alice answers: connect, no PIN.
    r = env["tw"].webhook(env["client"], "/api/telephony/twilio/voice", {
        "CallSid": "CA9", "From": AGENT_NUM, "To": ALICE, "Direction": "outbound-api"})
    assert "Stream" in r.text and env["made"][-1][4].startswith("Call to ")


def test_relay_engine_hands_twilio_the_greeting(env):
    env["configure"](engine="relay", greeting="Hey, Odysseus here.", relay_voice="en-US-Journey-O")
    _, r = _call(env)
    tm = simulator.parse_twiml(r.text)
    assert tm.relay_url == "wss://ody.example.ts.net:8443/api/telephony/twilio/relay"
    assert 'welcomeGreeting="Hey, Odysseus here."' in r.text and 'voice="en-US-Journey-O"' in r.text


def test_engines_not_ready_is_said_not_silence(env, monkeypatch):
    env["configure"]()
    monkeypatch.setattr(call_mod, "engines_ready", lambda: ["Speech to text: pick one."])
    _, r = _call(env)
    tm = simulator.parse_twiml(r.text)
    assert tm.verbs == ["Say", "Hangup"] and "speech engines" in tm.say[0]


# ── Settings secrets ─────────────────────────────────────────────────────

def test_settings_never_echo_the_token_or_pin(env, caplog):
    caplog.set_level(logging.DEBUG)
    out = env["configure"](pin="2468")
    h = {"x-test-user": "alice"}
    got = env["client"].get("/api/telephony/config", headers=h)
    for body in (json.dumps(out), got.text):
        assert TOKEN not in body and "2468" not in body and "enc:" not in body
    assert got.json()["has_auth_token"] is True and got.json()["has_pin"] is True
    # On disk: encrypted, not plaintext.
    raw = env["prefs"].read_text()
    assert TOKEN not in raw and "2468" not in raw and '"auth_token": "enc:' in raw
    assert config.auth_token(config.get_config("alice")) == TOKEN
    # The generic prefs API neither shows nor overwrites it.
    allp = env["client"].get("/api/prefs", headers=h).json()
    assert "phone_calls" not in allp
    assert env["client"].get("/api/prefs/phone_calls", headers=h).json()["value"] is None
    assert env["client"].put("/api/prefs/phone_calls", headers=h, json={"value": {}}).status_code == 403
    # Saving without a token keeps the saved one; clearing drops it.
    env["client"].put("/api/telephony/config", headers=h, json={"greeting": "Yo"})
    assert config.auth_token(config.get_config("alice")) == TOKEN
    env["client"].put("/api/telephony/config", headers=h, json={"clear_auth_token": True})
    assert env["client"].get("/api/telephony/config", headers=h).json()["has_auth_token"] is False
    _call(env)
    assert TOKEN not in caplog.text


def test_settings_validation(env):
    h = {"x-test-user": "alice"}
    put = lambda b: env["client"].put("/api/telephony/config", headers=h, json=b)
    assert put({"account_sid": "nope"}).status_code == 400
    assert put({"pin": "12"}).status_code == 400
    assert put({"public_url": "http://insecure.example"}).status_code == 400
    assert put({"public_url": "https://h.example/with/path"}).status_code == 400
    assert put({"engine": "magic"}).status_code == 400
    assert put({"numbers": ["+1555010200" + str(i) for i in range(6)]}).status_code == 400
    assert put({"answer_delay": 99}).json()["answer_delay"] == 25


def test_test_button_reports_each_check(env, monkeypatch):
    env["configure"]()

    async def fake_find(sid, tok, number):
        return {"phone_number": number, "voice_url": PUBLIC + "/api/telephony/twilio/voice"}

    monkeypatch.setattr(twilio, "find_number", fake_find)
    r = env["client"].post("/api/telephony/test", headers={"x-test-user": "alice"}).json()
    names = {c["name"]: c for c in r["checks"]}
    assert names["Twilio account"]["ok"] and names["Number points here"]["ok"]
    assert names["Speech engines"]["ok"] and names["Model"]["ok"]
    assert names["Public URL"]["ok"] is False      # nothing listens on the made-up host
    assert TOKEN not in json.dumps(r)


# ── Funnel guard ─────────────────────────────────────────────────────────

def test_funnel_reaches_only_the_phone_paths(env):
    c = env["client"]
    funnel = {"Tailscale-Funnel-Request": "?1"}
    assert c.get("/api/chat/secret-stuff").status_code == 200            # on the tailnet: fine
    r = c.get("/api/chat/secret-stuff", headers=funnel)
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert c.get("/api/telephony/config", headers={**funnel, "x-test-user": "alice"}).status_code == 404
    assert c.get("/api/telephony/twilio/health", headers=funnel).json()["ok"] is True
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/other/ws", headers=funnel) as ws:
            ws.receive_text()


# ── Whole calls over a real socket ───────────────────────────────────────

@pytest.fixture
def server(env):
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = uvicorn.Config(env["app"], host="127.0.0.1", port=port, log_level="warning", ws="websockets",
                         lifespan="off")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    assert srv.started
    base = f"http://127.0.0.1:{port}"
    env["tw"] = simulator.FakeTwilio(base, SID, TOKEN, AGENT_NUM, PUBLIC)
    yield base
    srv.should_exit = True
    th.join(5)


def _connect(env, base, caller=ALICE):
    import httpx
    with httpx.Client(timeout=10) as client:
        call_sid, r = env["tw"].incoming(client, caller)
    tm = simulator.parse_twiml(r.text)
    return call_sid, env["tw"]._to_local(tm.stream_url or tm.relay_url), tm.params["token"]


def _greeted():
    # A second of line noise to set the noise floor while the greeting
    # plays, then wait for it to finish (a turn starts once it has).
    return [("audio", simulator.silence(1.2)), ("wait_reply", 5), ("sleep", 0.5)]


def _speak(seconds=1.0):
    return [("audio", simulator.silence(0.3)), ("audio", simulator.tone(seconds)),
            ("audio", simulator.silence(1.0))]


def test_a_call_greets_then_answers_through_the_chat(env, server, monkeypatch):
    """The real agent path: run_headless puts the words in the call's chat
    and starts a detached run; the reply streams back and is spoken."""
    env["configure"](greeting="Hi, it's Odysseus.")

    async def fake_resume(sess, sm, context, agent_loop=None, *, source="", client_device=None,
                          context_length=None):
        assert (context[-1]["role"], context[-1]["content"]) == ("user", "what time is it")
        from routes.chat_routes import VOICE_CALL_NOTE
        assert any(m["role"] == "system" and VOICE_CALL_NOTE in m["content"] for m in context)
        full = ""
        for piece in ["<think>check the clock</think>", "It is noon. ", "Anything ", "else?"]:
            thinking = piece.startswith("<think>")
            yield "data: " + json.dumps({"delta": piece, **({"thinking": True} if thinking else {})}) + "\n\n"
            if not thinking:
                full += piece
            await asyncio.sleep(0.01)
        sm.add_message(sess.id, ChatMessage("assistant", full, metadata={"source": source}))
        yield "data: [DONE]\n\n"

    import src.screen_control_resume as scr
    monkeypatch.setattr(scr, "_resume_stream", fake_resume)
    # The setup a send does (auth headers, model recovery, compaction) reads
    # the real database from the server's thread; it is best-effort and not
    # what this test is about, and it would leave a connection behind in
    # that thread for later tests.
    from src import chat_queue

    async def no_prepare(sess, sid, context):
        return context, 0

    monkeypatch.setattr(chat_queue, "_prepare", no_prepare)
    monkeypatch.setattr(chat_queue, "on_run_finished", lambda sid, status: None)
    call_sid, url, token = _connect(env, server)
    script = _greeted() + _speak() + [("wait_reply", 10)]
    res = asyncio.run(simulator.media_call(url, token, call_sid, script))
    st = env["stubs"]
    assert st.tts_calls == ["Hi, it's Odysseus.", "It is noon.", "Anything else?"]
    assert len(res.clips) == 3 and all(n > 0 for n in res.clips) and res.clears == 0
    assert len(st.stt_calls) == 1 and codec.read_wav(st.stt_calls[0])[1] == 16000
    hist = env["mgr"].sessions["call1"].history
    assert [(m.role, m.content, m.metadata.get("source")) for m in hist] == [
        ("user", "what time is it", "phone_call"), ("assistant", "It is noon. Anything else?", "phone_call")]


def test_talking_over_the_agent_cuts_it_off(env, server, monkeypatch):
    env["configure"]()
    st = env["stubs"]
    st.heard = ["tell me a long story", "actually stop"]
    st.reply_text = "Once upon a time. There was a cat. It sat. It sat more. The end."
    st.reply_delay = 0.03
    from src.telephony import agent
    monkeypatch.setattr(agent, "reply", st.reply)
    call_sid, url, token = _connect(env, server)
    script = (_greeted() + _speak() +
              [("barge_in", simulator.tone(1.0)), ("audio", simulator.silence(1.0)), ("wait_reply", 10)])
    res = asyncio.run(simulator.media_call(url, token, call_sid, script))
    assert res.clears >= 1
    assert [t for _, t in st.replies] == ["tell me a long story", "actually stop"]


def test_saying_bye_hangs_up(env, server, monkeypatch):
    env["configure"]()
    st = env["stubs"]
    st.heard = ["okay bye"]
    from src.telephony import agent
    monkeypatch.setattr(agent, "reply", st.reply)
    call_sid, url, token = _connect(env, server)
    script = _greeted() + _speak() + [("wait_reply", 10), ("sleep", 1.0)]
    res = asyncio.run(simulator.media_call(url, token, call_sid, script))
    assert st.tts_calls == [config.DEFAULT_GREETING, call_mod.GOODBYE] and st.replies == []
    assert res.closed_by_server


def test_a_stream_token_works_once_and_only_for_its_call(env, server, monkeypatch):
    env["configure"](greeting="Hello.")
    call_sid, url, token = _connect(env, server)
    wrong = asyncio.run(simulator.media_call(url, token, "CA" + "0" * 32, [("sleep", 0.5)]))
    assert wrong.closed_by_server and not wrong.audio
    ok = asyncio.run(simulator.media_call(url, token, call_sid, [("wait_reply", 5)]))
    assert ok.audio
    again = asyncio.run(simulator.media_call(url, token, call_sid, [("sleep", 0.5)]))
    assert again.closed_by_server and not again.audio
    made_up = asyncio.run(simulator.media_call(url, "x" * 32, call_sid, [("sleep", 0.5)]))
    assert made_up.closed_by_server and not made_up.audio


def test_relay_call_speaks_the_reply_as_text(env, server, monkeypatch):
    env["configure"](engine="relay")
    st = env["stubs"]
    st.reply_text = "**Sure**, here it is. See https://x.y for more."
    from src.telephony import agent
    monkeypatch.setattr(agent, "reply", st.reply)
    call_sid, url, token = _connect(env, server)
    spoken = asyncio.run(simulator.relay_call(url, token, call_sid, ["read it to me", "bye"]))
    assert spoken == ["Sure, here it is. See a link for more.", call_mod.GOODBYE]
    assert st.replies == [("call1", "read it to me")]


def test_the_settings_card_has_every_field_its_script_uses():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "static"
    js = (root / "js" / "devicesSettings.js").read_text()
    html = (root / "index.html").read_text()
    ids = set(re.findall(r"\$\('(phone-[a-z-]+)'\)", js)) | set(re.findall(r"#(phone-[a-z-]+)", js))
    assert {"phone-enabled", "phone-token", "phone-test", "phone-call-me"} <= ids
    for i in sorted(ids):
        assert f'id="{i}"' in html, i
    # The token field never gets a value from the server, only a placeholder.
    assert "$('phone-token').value = ''" in js and "cfg.auth_token" not in js
