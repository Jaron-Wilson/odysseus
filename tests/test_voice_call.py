"""The voice call: talk to the agent in the open chat, hear it answer.

Asked for 2026-09-30: calling an agent from the phone "like a live chat".
A phone-network call is not possible (Google Voice has no call API, and
Android keeps call audio from apps), so it is an in-app call: a Call button
(and /call) opens an overlay that listens, sends what you said into the SAME
chat through the normal send path, and reads the streamed reply out loud
sentence by sentence, interruptible.

The browser checks load the real static/js/voiceCall.js (and the real
voiceRecorder.js it takes the STT upload from) in Chromium. The microphone is
a WebAudio oscillator that the test switches on ("speech") and off
("silence"); /api/stt/* and /api/tts/* are answered by route interception;
the chat is a fake window.chatModule whose sendText replays a streamed reply
as the 'odysseus:reply' events chat.js dispatches.
"""
import io
import json
import re
import struct
import wave
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_STATIC = _REPO / "static"
_JS = _STATIC / "js" / "voiceCall.js"
_CSS = _STATIC / "css" / "voiceCall.css"


# --- Wiring (static) ---------------------------------------------------------

def test_the_call_button_script_and_stylesheet_are_shipped_and_precached():
    html = (_STATIC / "index.html").read_text()
    sw = (_STATIC / "sw.js").read_text()
    assert 'id="voice-call-btn"' in html
    assert 'id="voice-call-settings"' in html
    assert '<script type="module" src="/static/js/voiceCall.js"></script>' in html
    # Linked after the other stylesheets so it is not overridden by them.
    links = re.findall(r'<link rel="stylesheet" href="(/static/[^"]+)"', html)
    assert links[-1] == "/static/css/voiceCall.css"
    assert "'/static/css/voiceCall.css'" in sw and "'/static/js/voiceCall.js'" in sw


def test_slash_call_opens_the_call():
    js = (_STATIC / "js" / "slashCommands.js").read_text()
    assert re.search(r"\n  call: \{[^}]*handler: _cmdCall", js)
    assert "window.voiceCall.open()" in js


def test_chat_js_announces_each_reply_for_the_call():
    js = (_STATIC / "js" / "chat.js").read_text()
    for phase in ("start", "delta", "done"):
        assert f"_emitReply('{phase}'" in js
    assert "sendText: _submitText" in js


def test_the_overlay_css_is_neutral_and_respects_reduced_motion():
    css = _CSS.read_text()
    assert "--st-" not in css                         # Studio tokens exist only under html.ui-studio
    assert "prefers-reduced-motion" in css
    assert "\u2014" not in css and "\u2014" not in _JS.read_text()   # no em dashes


# --- Server: the upload is labelled by what it is ---------------------------

def _wav(ms=200, rate=16000, freq=0.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(rate * ms / 1000)
        import math
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / rate)) if freq else 0)
                               for i in range(n)))
    return buf.getvalue()


def test_stt_labels_uploads_by_their_bytes():
    from services.stt.stt_service import audio_container
    assert audio_container(_wav()) == (".wav", "audio/wav")
    assert audio_container(b"\x1aE\xdf\xa3" + b"\0" * 20) == (".webm", "audio/webm")
    assert audio_container(b"OggS" + b"\0" * 20) == (".ogg", "audio/ogg")
    assert audio_container(b"\0\0\0\x20ftypM4A " + b"\0" * 8) == (".mp4", "audio/mp4")
    assert audio_container(b"") == (".webm", "audio/webm")


def test_api_stt_sends_a_wav_as_a_wav(monkeypatch):
    from services.stt import stt_service as mod
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"text": "hello"}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        seen["name"], _, seen["mime"] = files["file"]
        return _Resp()

    class _Ep:
        base_url = "https://api.example.test/v1"
        api_key = "k"

    class _Q:
        def filter(self, *a): return self
        def first(self): return _Ep()

    class _Db:
        def query(self, *a): return _Q()
        def close(self): pass

    import src.database as db
    monkeypatch.setattr(db, "SessionLocal", lambda: _Db(), raising=False)
    monkeypatch.setattr(db, "ModelEndpoint", type("ME", (), {"id": 1}), raising=False)
    monkeypatch.setattr(mod.httpx, "post", fake_post)
    assert mod.STTService()._transcribe_api(_wav(), "ep1", "whisper-1") == "hello"
    assert seen == {"name": "audio.wav", "mime": "audio/wav"}


# --- Browser checks ----------------------------------------------------------

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_PAGE = "<!doctype html><html><head><meta charset='utf-8'></head><body></body></html>"

_SETUP = r"""
(() => {
  window.__ev = [];
  window.__sent = [];
  const ctx = new AudioContext();
  const osc = ctx.createOscillator();
  osc.frequency.value = 220;
  const gain = ctx.createGain();
  gain.gain.value = 0;
  const dest = ctx.createMediaStreamDestination();
  osc.connect(gain); gain.connect(dest); osc.start();
  window.__mic = {
    ctx, stream: dest.stream,
    talk(on) { gain.gain.setValueAtTime(on ? 0.3 : 0, ctx.currentTime); window.__ev.push({type: on ? 'MIC_ON' : 'MIC_OFF', t: Date.now()}); },
  };
  window.__gum = async (c) => { window.__constraints = c; await ctx.resume(); return dest.stream; };
  // The chat: sendText replays a streamed reply as chat.js announces it.
  window.__reply = ['Sure thing. ', 'The weather is sunny today. ', 'Bring a hat!'];
  window.chatModule = {
    currentSessionId: () => 's1',
    currentSessionName: () => 'Trip planning',
    hasActiveStream: () => false,
    sendText(text) {
      window.__sent.push(text);
      const fire = (phase, t) => window.dispatchEvent(new CustomEvent('odysseus:reply', {detail: {phase, sessionId: 's1', text: t}}));
      let acc = '';
      setTimeout(() => fire('start', ''), 30);
      window.__reply.forEach((piece, i) => setTimeout(() => { acc += piece; fire('delta', acc); }, 80 + i * 400));
      setTimeout(() => { window.__doneAt = Date.now(); fire('done', acc); }, 80 + window.__reply.length * 400);
    },
  };
})();
"""


class _Fakes:
    def __init__(self):
        self.stt = []            # request bodies
        self.stt_text = ["what's the weather like", "and tomorrow"]
        self.stt_fail = 0        # this many requests answer 500 first
        self.tts = []            # texts, in request order
        self.tts_ms = 250


@pytest.fixture
def call_page():
    fakes = _Fakes()
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page()
        files = {
            "/static/js/voiceCall.js": _JS.read_text(),
            "/static/js/voiceRecorder.js": (_STATIC / "js" / "voiceRecorder.js").read_text(),
        }

        def route(r):
            url = r.request.url
            path = re.sub(r"^https://example\.test", "", url).split("?")[0]
            if path in files:
                r.fulfill(body=files[path], content_type="text/javascript")
            elif path == "/api/stt/stats":
                r.fulfill(body=json.dumps({"available": True, "provider": "local", "model": "base"}),
                          content_type="application/json")
            elif path == "/api/stt/transcribe":
                fakes.stt.append(r.request.post_data_buffer or b"")
                if fakes.stt_fail > 0:
                    fakes.stt_fail -= 1
                    r.fulfill(status=500, body=json.dumps({"detail": {"message": "Whisper crashed"}}),
                              content_type="application/json")
                    return
                i = min(len(fakes.stt) - 1, len(fakes.stt_text) - 1)
                r.fulfill(body=json.dumps({"text": fakes.stt_text[i]}), content_type="application/json")
            elif path == "/api/tts/stats":
                r.fulfill(body=json.dumps({"available": True, "ready": True, "provider": "local", "speed": 1}),
                          content_type="application/json")
            elif path == "/api/tts/synthesize":
                fakes.tts.append(json.loads(r.request.post_data or "{}").get("text"))
                r.fulfill(body=_wav(fakes.tts_ms, freq=330), content_type="audio/wav")
            elif path in ("/", ""):
                r.fulfill(body=_PAGE, content_type="text/html")
            elif path.startswith("/static/") and (_REPO / path.lstrip("/")).is_file():
                # The rest of the app's modules, for the ones that import them.
                r.fulfill(body=(_REPO / path.lstrip("/")).read_bytes(),
                          content_type="text/javascript" if path.endswith(".js") else "text/plain")
            else:
                r.fulfill(status=404, body="")

        pg.route("**/*", route)
        pg.goto("https://example.test/")
        pg.evaluate(_SETUP)
        pg.evaluate("() => import('/static/js/voiceCall.js').then(m => { window.__vc = m; })")
        pg.wait_for_function("() => window.__vc")
        yield pg, fakes
        browser.close()


def _open(pg, prefs=None, denied=False):
    pg.evaluate("""([prefs, denied]) => {
      const gum = denied
        ? async () => { throw new DOMException('Permission denied', 'NotAllowedError'); }
        : window.__gum;
      window.__call = window.__vc.open({
        getUserMedia: gum, prefs: prefs || {},
        onEvent: (e) => window.__ev.push(e),
      });
    }""", [prefs or {}, denied])


def _events(pg, kind=None):
    ev = pg.evaluate("window.__ev.map(e => ({type: e.type, text: e.text, state: e.state, t: e.t}))")
    return [e for e in ev if kind is None or e["type"] == kind]


def _wait_event(pg, kind, n=1, timeout=8000):
    pg.wait_for_function("([k, n]) => window.__ev.filter(e => e.type === k).length >= n", arg=[kind, n],
                         timeout=timeout)


def _say(pg, ms):
    pg.evaluate("window.__mic.talk(true)")
    pg.wait_for_timeout(ms)
    pg.evaluate("window.__mic.talk(false)")


def _state(pg):
    return pg.evaluate("document.querySelector('.vc-overlay') && document.querySelector('.vc-overlay').dataset.state")


def test_speech_then_silence_sends_one_message_into_the_chat(call_page):
    pg, fakes = call_page
    _open(pg)
    _wait_event(pg, "calibrated")                  # a second of room noise first
    assert _state(pg) == "listening"
    c = pg.evaluate("window.__constraints.audio")
    assert c["echoCancellation"] and c["noiseSuppression"] and c["autoGainControl"]
    _say(pg, 900)
    pg.wait_for_function("() => window.__sent.length >= 1", timeout=6000)
    pg.wait_for_timeout(1200)                      # nothing else goes out afterwards
    assert pg.evaluate("window.__sent") == ["what's the weather like"]
    assert len(fakes.stt) == 1
    body = fakes.stt[0]                            # multipart: the same upload the composer mic uses
    assert b'name="file"; filename="utterance.wav"' in body
    assert body.find(b"RIFF") < body.find(b"WAVEfmt ")
    # The turn ended after the pause, not while talking.
    on_off = _events(pg, "MIC_OFF")[0]["t"]
    end = _events(pg, "speech-end")[0]["t"]
    assert 500 <= end - on_off <= 1800
    assert "Trip planning" in pg.inner_text(".vc-title")
    assert "what's the weather like" in pg.inner_text(".vc-transcript")


def test_the_reply_is_spoken_sentence_by_sentence_as_it_streams(call_page):
    pg, fakes = call_page
    _open(pg)
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-end", 3, timeout=10000)
    assert fakes.tts == ["Sure thing.", "The weather is sunny today.", "Bring a hat!"]
    starts = _events(pg, "speak-start")
    assert [e["text"] for e in starts] == fakes.tts
    # It started talking before the reply finished streaming.
    assert starts[0]["t"] < pg.evaluate("window.__doneAt")
    # Played one after another, never overlapping.
    ends = _events(pg, "speak-end")
    for a, b in zip(ends, starts[1:]):
        assert b["t"] >= a["t"]
    pg.wait_for_function("() => document.querySelector('.vc-overlay').dataset.state === 'listening'")
    states = [e["state"] for e in _events(pg, "state")]
    assert states[:4] == ["connecting", "listening", "thinking", "speaking"] and states[-1] == "listening"
    assert "Bring a hat!" in pg.inner_text(".vc-transcript")


def test_talking_over_the_agent_stops_it_and_listens(call_page):
    pg, fakes = call_page
    fakes.tts_ms = 4000                             # long sentences, so there is time to cut in
    _open(pg, {"bargeIn": True, "echo": "headphones"})
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-start", timeout=10000)
    pg.wait_for_timeout(300)
    assert pg.evaluate("!window.__call.audio.paused")
    pg.evaluate("window.__mic.talk(true)")
    t_on = _events(pg, "MIC_ON")[-1]["t"]
    _wait_event(pg, "barge-in", timeout=3000)
    t_barge = _events(pg, "barge-in")[0]["t"]
    assert 200 <= t_barge - t_on <= 1200              # about 250ms of speech, not a click
    assert pg.evaluate("window.__call.audio.paused")
    assert _state(pg) == "listening"
    assert len(_events(pg, "speak-end")) == 0          # the first sentence never finished
    pg.wait_for_timeout(600)
    pg.evaluate("window.__mic.talk(false)")
    # What was said over the agent is the next turn.
    pg.wait_for_function("() => window.__sent.length >= 2", timeout=6000)
    assert pg.evaluate("window.__sent") == ["what's the weather like", "and tomorrow"]
    assert len(fakes.stt) == 2


def test_interrupt_button_stops_the_voice(call_page):
    pg, fakes = call_page
    fakes.tts_ms = 4000
    _open(pg)
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-start", timeout=10000)
    pg.click(".vc-interrupt")
    _wait_event(pg, "interrupt")
    assert pg.evaluate("window.__call.audio.paused")
    assert _state(pg) == "listening"
    pg.wait_for_timeout(1500)                          # the rest of the reply is not spoken
    assert len(_events(pg, "speak-start")) == 1


def test_end_releases_the_microphone(call_page):
    pg, _ = call_page
    _open(pg)
    _wait_event(pg, "calibrated")
    assert pg.evaluate("window.__mic.stream.getAudioTracks().every(t => t.readyState === 'live')")
    pg.click(".vc-end")
    _wait_event(pg, "ended")
    assert pg.evaluate("window.__mic.stream.getAudioTracks().every(t => t.readyState === 'ended')")
    assert pg.evaluate("document.querySelector('.vc-overlay')") is None
    assert pg.evaluate("window.__vc.isActive()") is False


def test_escape_minimizes_mid_reply_and_never_hangs_up(call_page):
    pg, fakes = call_page
    fakes.tts_ms = 4000
    _open(pg)
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-start", timeout=10000)
    pg.keyboard.press("Escape")
    _wait_event(pg, "minimized")
    assert pg.is_hidden(".vc-overlay") and pg.is_visible(".vc-pill")
    assert not pg.evaluate("document.documentElement.classList.contains('vc-open')")
    # Still talking, still listening.
    assert not pg.evaluate("window.__call.audio.paused")
    assert pg.evaluate("window.__mic.stream.getAudioTracks().every(t => t.readyState === 'live')")
    pg.keyboard.press("Escape")                         # minimized, Escape is the app's again
    pg.wait_for_timeout(300)
    assert pg.evaluate("window.__vc.isActive()") and _events(pg, "ended") == []
    pg.click(".vc-pill-expand")
    _wait_event(pg, "expanded")
    assert pg.is_visible(".vc-overlay") and pg.is_hidden(".vc-pill")


def test_denied_permission_shows_the_error(call_page):
    pg, _ = call_page
    _open(pg, denied=True)
    pg.wait_for_selector(".vc-error:not([hidden])")
    assert "denied" in pg.inner_text(".vc-error").lower()
    assert _state(pg) == "error"
    pg.click(".vc-end")
    assert pg.evaluate("document.querySelector('.vc-overlay')") is None


def test_stt_failure_is_retried_once_then_shown(call_page):
    pg, fakes = call_page
    fakes.stt_fail = 2
    _open(pg)
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    pg.wait_for_selector(".vc-error:not([hidden])", timeout=6000)
    assert len(fakes.stt) == 2                           # one retry, then give up
    assert "Whisper crashed" in pg.inner_text(".vc-error")
    assert pg.evaluate("window.__sent") == []
    assert _state(pg) == "listening"                     # and the call goes on


def test_push_to_talk_sends_on_release(call_page):
    pg, fakes = call_page
    _open(pg, {"mode": "ptt"})
    _wait_event(pg, "calibrated")
    assert pg.is_visible(".vc-talk")
    _say(pg, 400)                                        # not holding: ignored
    pg.wait_for_timeout(1200)
    assert fakes.stt == []
    pg.keyboard.down("Space")
    _say(pg, 500)
    pg.wait_for_timeout(1500)                            # a pause while held does not end the turn
    _say(pg, 300)
    assert fakes.stt == []
    pg.keyboard.up("Space")
    pg.wait_for_function("() => window.__sent.length >= 1", timeout=6000)
    assert len(fakes.stt) == 1


def test_markdown_is_read_as_words_and_split_into_sentences(call_page):
    pg, _ = call_page
    out = pg.evaluate(r"""() => {
      const {speakableText, takeSentences} = window.__vc;
      const raw = "<think>plan it</think>## Plan\n\nSure, **Dr. Lee** can help. See [the docs](https://x.test).\n\n```py\nprint(1)\n```\n- First item\n- Second one? Yes";
      const plain = speakableText(raw);
      const partial = takeSentences(plain, 0, false);
      const all = takeSentences(plain, 0, true);
      // Streamed a few characters at a time, the same sentences come out.
      const streamed = [];
      for (const step of [1, 3, 7]) {
        let idx = 0; const got = [];
        for (let n = step; n < raw.length + step; n += step) {
          const r = takeSentences(speakableText(raw.slice(0, n)), idx, n >= raw.length);
          idx = r.next; got.push(...r.sentences);
        }
        streamed.push(got);
      }
      return {plain, partial: partial.sentences, all: all.sentences, streamed};
    }""")
    assert "plan it" not in out["plain"] and "print" not in out["plain"] and "*" not in out["plain"]
    assert out["all"] == ["Plan", "Sure, Dr. Lee can help.", "See the docs.", "First item", "Second one?", "Yes"]
    assert out["partial"] == out["all"][:-1]            # "Yes" may still grow
    assert out["streamed"] == [out["all"]] * 3


# --- Minimized ---------------------------------------------------------------

def _pill_state(pg):
    return pg.evaluate("document.querySelector('.vc-pill').dataset.state")


def test_minimized_the_call_goes_on_in_the_pill(call_page):
    pg, fakes = call_page
    fakes.tts_ms = 600
    _open(pg)
    _wait_event(pg, "calibrated")
    pg.click(".vc-minimize")
    _wait_event(pg, "minimized")
    assert pg.is_visible(".vc-pill") and pg.is_hidden(".vc-overlay")
    assert "Trip planning" in pg.inner_text(".vc-pill")
    assert _pill_state(pg) == "listening" and "Listening" in pg.inner_text(".vc-pill-state")
    # A whole turn while minimized: heard, sent, spoken, back to listening.
    pg.evaluate("window.__mic.talk(true)")
    pg.wait_for_timeout(250)
    lvl = pg.evaluate("Number(getComputedStyle(document.querySelector('.vc-pill')).getPropertyValue('--vc-level'))")
    assert lvl > 0.05                                    # the level bars move with the voice
    pg.wait_for_timeout(450)
    pg.evaluate("window.__mic.talk(false)")
    pg.wait_for_function("() => document.querySelector('.vc-pill').dataset.state === 'thinking'", timeout=6000)
    pg.wait_for_function("() => document.querySelector('.vc-pill').dataset.state === 'speaking'", timeout=8000)
    assert pg.inner_text(".vc-pill-state") == "Speaking"
    _wait_event(pg, "speak-end", 3, timeout=10000)
    pg.wait_for_function("() => document.querySelector('.vc-pill').dataset.state === 'listening'", timeout=6000)
    assert pg.evaluate("window.__sent") == ["what's the weather like"]
    assert pg.is_hidden(".vc-overlay")                   # it stayed minimized the whole time
    # Mute from the pill.
    pg.click(".vc-pill-mute")
    assert pg.evaluate("window.__call.muted")
    assert pg.get_attribute(".vc-pill-mute", "aria-pressed") == "true"
    assert pg.inner_text(".vc-pill-state") == "Muted"
    pg.click(".vc-pill-mute")
    assert not pg.evaluate("window.__call.muted")
    pg.click(".vc-pill-main")
    assert pg.is_visible(".vc-overlay")
    pg.click(".vc-minimize")
    pg.click(".vc-pill-end")
    _wait_event(pg, "ended")
    assert pg.evaluate("document.querySelector('.vc-pill')") is None
    assert pg.evaluate("window.__mic.stream.getAudioTracks().every(t => t.readyState === 'ended')")


def test_a_mobile_pill_sits_in_the_safe_area_clear_of_the_composer():
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        ctx = browser.new_context(viewport={"width": 412, "height": 915}, is_mobile=True, has_touch=True)
        pg = ctx.new_page()
        css = _CSS.read_text()
        pg.set_content("<meta name='viewport' content='width=device-width, initial-scale=1'>"
                       f"<style>:root{{--bg:#111;--fg:#eee;--panel:#222;--border:#333;--red:#e33}}{css}</style>"
                       "<form id='chat-form' style='position:fixed;left:0;right:0;bottom:0;height:120px'></form>"
                       "<div class='vc-pill'><button class='vc-pill-main'><span class='vc-pill-lvl'><i></i></span>"
                       "<span class='vc-pill-text'><span class='vc-pill-state'>Listening</span>"
                       "<span class='vc-pill-sub'>A long chat name that goes on and on 1:23</span></span></button>"
                       "<button class='vc-pill-btn'></button><button class='vc-pill-btn'></button>"
                       "<button class='vc-pill-btn vc-pill-end'></button></div>")
        box = pg.locator(".vc-pill").bounding_box()
        form = pg.locator("#chat-form").bounding_box()
        assert box["y"] >= 8 and box["y"] + box["height"] < form["y"]
        assert box["x"] >= 0 and box["x"] + box["width"] <= 412
        assert "env(safe-area-inset-top)" in css.split(".vc-pill {", 1)[1].split("}", 1)[0]
        browser.close()


# --- The call stays in its own chat ----------------------------------------

_SSE_REPLY = "".join(f"data: {json.dumps({'delta': d})}\n\n" for d in ["It is ", "raining in Lisbon. ", "Take a coat."]) + "data: [DONE]\n\n"


def test_minimized_over_another_chat_the_call_still_talks_in_its_own(call_page):
    pg, fakes = call_page
    seen = []

    def chat_stream(route):
        seen.append(route.request.post_data_buffer or b"")
        route.fulfill(body=_SSE_REPLY, content_type="text/event-stream")
    pg.route("**/api/chat_stream", chat_stream)
    pg.evaluate("""() => {
      window.__cur = 's1';
      window.chatModule.currentSessionId = () => window.__cur;
      const orig = window.chatModule.sendText;
      window.chatModule.sendText = (t, o) => { window.__sendOpts = o; orig(t); };
    }""")
    _open(pg)
    _wait_event(pg, "calibrated")
    # First turn with the call's chat on screen: through the composer, flagged.
    _say(pg, 700)
    _wait_event(pg, "speak-end", 3, timeout=10000)
    assert pg.evaluate("window.__sendOpts") == {"voiceCall": True}
    pg.wait_for_function("() => document.querySelector('.vc-overlay').dataset.state === 'listening'")
    # Minimize and open another chat. Its replies are not the call's.
    pg.click(".vc-minimize")
    pg.evaluate("window.__cur = 's2'")
    _say(pg, 700)
    pg.wait_for_function("() => window.__ev.some(e => e.type === 'send' && e.text === 'and tomorrow')", timeout=6000)
    pg.evaluate("""() => {
      const fire = (phase, t) => window.dispatchEvent(new CustomEvent('odysseus:reply', {detail: {phase, sessionId: 's2', text: t}}));
      fire('start', ''); fire('delta', 'Something for the other chat. '); fire('done', 'Something for the other chat. ');
    }""")
    _wait_event(pg, "speak-end", 5, timeout=10000)
    assert pg.evaluate("window.__sent") == ["what's the weather like"]      # not into s2's composer
    assert len(seen) == 1
    body = seen[0].decode("utf-8", "replace")
    assert 'name="session"\r\n\r\ns1' in body and 'name="voice_call"\r\n\r\n1' in body
    assert 'name="message"\r\n\r\nand tomorrow' in body
    assert fakes.tts[3:] == ["It is raining in Lisbon.", "Take a coat."]
    assert "Something for the other chat." not in fakes.tts
    assert pg.evaluate("window.__call.sid") == "s1"


def test_the_agent_opening_a_page_minimizes_the_call(call_page):
    pg, _ = call_page
    _open(pg)
    _wait_event(pg, "calibrated")
    assert pg.is_visible(".vc-overlay")
    pg.evaluate("""() => import('/static/js/chatStream.js').then(m => { window.__cs = m; })""")
    pg.wait_for_function("() => window.__cs", timeout=15000)
    pg.evaluate("() => window.__cs.handleUIControl({ui_event: 'open_panel', panel: 'settings', settings_target: 'voice call'})")
    _wait_event(pg, "minimized")
    assert pg.is_hidden(".vc-overlay") and pg.is_visible(".vc-pill")
    assert pg.evaluate("window.__vc.isActive()")
    # A theme change is not something to look at: the call stays as it is.
    pg.click(".vc-pill-expand")
    pg.evaluate("() => window.__cs.handleUIControl({ui_event: 'clear_highlight'})")
    assert pg.is_visible(".vc-overlay")


# --- Echo --------------------------------------------------------------------

def test_echo_check_drops_the_agents_own_words(call_page):
    pg, _ = call_page
    r = pg.evaluate("""() => {
      const {isEcho} = window.__vc;
      const spoken = ['The weather in Lisbon is sunny today.', 'Bring a hat!'];
      return {
        exact: isEcho('the weather in Lisbon is sunny today', spoken),
        sloppy: isEcho('weather in lisbon is sunny to day bring', spoken),
        tail: isEcho('bring a hat', spoken),
        user: isEcho('wait, what about tomorrow', spoken),
        mixed: isEcho('no stop I meant Porto', spoken),
        short_user: isEcho('stop', spoken),
        short_echo: isEcho('Bring a', spoken),
        nothing: isEcho('', spoken),
        no_ref: isEcho('the weather', []),
      };
    }""")
    assert r == {"exact": True, "sloppy": True, "tail": True, "user": False, "mixed": False,
                 "short_user": False, "short_echo": True, "nothing": False, "no_ref": False}


def test_barge_in_needs_more_over_speakers_than_over_headphones(call_page):
    pg, _ = call_page
    r = pg.evaluate("""() => {
      const {bargeParams} = window.__vc;
      return {
        hp: bargeParams({noise: 0.005, echo: 0.04, mode: 'headphones'}),
        auto_quiet: bargeParams({noise: 0.005, echo: 0, mode: 'auto'}),
        auto_loud: bargeParams({noise: 0.005, echo: 0.04, mode: 'auto'}),
        strict: bargeParams({noise: 0.005, echo: 0.04, mode: 'strict'}),
      };
    }""")
    assert r["strict"] is None
    assert r["hp"]["onsetMs"] == 250 and abs(r["hp"]["threshold"] - 0.02) < 1e-9
    assert r["auto_quiet"]["onsetMs"] == 400 and r["auto_quiet"]["threshold"] >= r["hp"]["threshold"]
    assert abs(r["auto_loud"]["threshold"] - 0.1) < 1e-9                # 2.5x what the speakers put in the mic


def test_auto_echo_resumes_the_voice_when_it_only_heard_itself(call_page):
    pg, fakes = call_page
    fakes.tts_ms = 3000
    fakes.stt_text = ["what's the weather like", "sure thing"]           # the second "turn" is the speakers
    _open(pg)                                                             # echo: auto (the default)
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-start", timeout=10000)
    pg.wait_for_timeout(500)                                              # past the echo calibration
    _say(pg, 900)
    _wait_event(pg, "hold", timeout=4000)
    _wait_event(pg, "echo-dropped", timeout=6000)
    _wait_event(pg, "release")
    assert _events(pg, "barge-in") == []                                  # the reply was never cut off
    assert pg.evaluate("window.__sent") == ["what's the weather like"]
    assert not pg.evaluate("window.__call.audio.paused")
    assert "headphones" in pg.inner_text(".vc-error")                     # said once
    _wait_event(pg, "speak-end", 3, timeout=15000)


def test_auto_echo_a_real_interruption_still_cuts_in(call_page):
    pg, fakes = call_page
    fakes.tts_ms = 3000
    fakes.stt_text = ["what's the weather like", "no wait, what about Porto"]
    _open(pg)
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-start", timeout=10000)
    pg.wait_for_timeout(500)
    _say(pg, 900)
    _wait_event(pg, "barge-in", timeout=6000)
    pg.wait_for_function("() => window.__sent.length >= 2", timeout=6000)
    assert pg.evaluate("window.__sent") == ["what's the weather like", "no wait, what about Porto"]


def test_strict_echo_ignores_the_mic_while_the_agent_talks(call_page):
    pg, fakes = call_page
    fakes.tts_ms = 3000
    _open(pg, {"echo": "strict"})
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-start", timeout=10000)
    _say(pg, 900)
    pg.wait_for_timeout(500)
    assert _events(pg, "hold") == [] and _events(pg, "barge-in") == []
    assert len(fakes.stt) == 1
    pg.click(".vc-interrupt")                                             # the button still cuts in
    _wait_event(pg, "interrupt")
    assert _state(pg) == "listening"


def test_the_voice_plays_through_the_echo_cancelling_loopback(call_page):
    pg, fakes = call_page
    _open(pg)
    _wait_event(pg, "loopback", timeout=10000)
    ok = [e for e in pg.evaluate("window.__ev") if e["type"] == "loopback"][0]
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-end", 3, timeout=10000)                        # played to the end through it
    assert ok.get("ok") is True, ok
    assert pg.evaluate("!!window.__call._loop && window.__call._loop.out.srcObject instanceof MediaStream")
    assert pg.evaluate("window.__call._loop.pc2.connectionState") == "connected"


def test_without_webrtc_the_voice_plays_straight_out(call_page):
    pg, fakes = call_page
    pg.evaluate("() => { window.RTCPeerConnection = undefined; }")
    _open(pg)
    _wait_event(pg, "calibrated")
    _say(pg, 700)
    _wait_event(pg, "speak-end", 3, timeout=10000)
    assert pg.evaluate("window.__call._loop") is None


def test_local_whisper_missing_falls_back_to_the_browser_recognizer(call_page):
    pg, _ = call_page
    pg.route("**/api/stt/stats", lambda r: r.fulfill(
        body=json.dumps({"available": False, "provider": "local"}), content_type="application/json"))
    r = pg.evaluate("""async () => {
      window.webkitSpeechRecognition = function () { this.start = () => {}; this.stop = () => {}; this.abort = () => {}; };
      const a = await window.__vc.resolveStt();
      window.webkitSpeechRecognition = undefined;
      window.SpeechRecognition = undefined;
      const b = await window.__vc.resolveStt();
      return {a: a.kind, notice: a.notice || '', b: b.kind, reason: b.reason || ''};
    }""")
    assert r["a"] == "browser" and "Whisper" in r["notice"]
    assert r["b"] == "none" and "not installed" in r["reason"]
