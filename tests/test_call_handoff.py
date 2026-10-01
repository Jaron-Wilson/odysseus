"""Moving a voice call to another device, and picking it up there.

Asked for 2026-10-01: "can we make it so the call i can minimise? and just
like the music player allow to go between the different devices, ie: change
over to my phone cause im heading out and leaving the computer".

The server half (routes/call_routes.py, src/call_handoff.py) is checked
directly: presence, targets, offers and their expiry, all per owner. The
browser half runs the real voiceCall.js and callHandoff.js in two Chromium
contexts, a desktop and a phone, against the real routes (in process,
through request interception), with Chromium's fake microphone.
"""
import io
import json
import math
import re
import struct
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parent.parent

DESK_IP = "100.102.86.125"
PHONE_IP = "100.64.0.9"
NAMES = {DESK_IP: ("DESKTOP-JARON", "desktop"), PHONE_IP: ("pixel-8a", "phone")}


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def api(monkeypatch):
    import routes.call_routes as cr
    from src import call_handoff as ch
    from src import webpush
    ch.reset()
    clock = _Clock()
    monkeypatch.setattr(ch, "_now", clock)
    monkeypatch.setattr(cr, "require_user", lambda request: request.headers.get("X-User", "jaron"))

    def verify(request, sid):
        if sid.startswith("theirs"):
            raise HTTPException(404, "Session not found")
    monkeypatch.setattr(cr, "_verify_session", verify)
    monkeypatch.setattr(cr, "_tailnet_name", lambda ip: NAMES.get(ip, (None, None)))
    subs = [
        {"endpoint": "https://push.example/phone", "p256dh": "k", "auth": "a", "device": "android-phone", "owner": "jaron"},
        {"endpoint": "https://push.example/laptop", "p256dh": "k", "auth": "a", "device": "laptop", "owner": "jaron"},
        {"endpoint": "https://push.example/hers", "p256dh": "k", "auth": "a", "device": "her-phone", "owner": "alex"},
    ]
    monkeypatch.setattr(webpush, "load_subscriptions", lambda: list(subs))
    pushed = []

    async def fake_send(title, body, **kw):
        pushed.append({"title": title, "body": body, **kw})
        return {"sent": 1, "failed": 0}
    monkeypatch.setattr(webpush, "send", fake_send)
    app = FastAPI()
    app.include_router(cr.setup_call_routes())
    with TestClient(app) as c:
        yield c, ch, clock, pushed


def _beat(c, cid, ip, user="jaron", **kw):
    body = {"client_id": cid, "browser_id": kw.pop("browser_id", "b-" + cid), "label": "Chrome on Linux",
            "kind": "desktop", "visible": True, **kw}
    r = c.post("/api/call/presence", json=body, headers={"X-Forwarded-For": ip, "X-User": user})
    assert r.status_code == 200, r.text
    return r.json()


CALL = {"session_id": "s1", "chat_name": "Trip planning", "model": "qwen3", "prefs": {"echo": "auto"}}


def test_targets_are_the_owners_other_pages_phone_first(api):
    c, ch, clock, _ = api
    assert _beat(c, "desk1", DESK_IP, call=CALL)["name"] == "DESKTOP-JARON"
    _beat(c, "desk2", DESK_IP, browser_id="b-desk1")              # another tab of the same browser
    _beat(c, "phone1", PHONE_IP, kind="phone", push_endpoint="https://push.example/phone")
    _beat(c, "other", "100.70.0.1", label="Firefox on Windows")
    _beat(c, "hers", "100.80.0.1", user="alex")
    t = c.get("/api/call/targets", params={"client_id": "desk1"}, headers={"X-Forwarded-For": DESK_IP}).json()["targets"]
    names = [x["name"] for x in t]
    assert names[0] == "pixel-8a" and t[0]["kind"] == "phone" and t[0]["live"] and t[0]["push"]
    assert "Firefox on Windows" in names                          # no tailnet name: the browser and OS
    assert "DESKTOP-JARON" not in names                           # not its own browser's tabs
    assert all(x["id"] != "hers" for x in t)                      # not someone else's page
    # Push only: the laptop (not open), but not the phone twice, nor alex's.
    push_only = [x for x in t if not x["live"]]
    assert [x["name"] for x in push_only] == ["laptop"]
    # A page silent too long is not offered.
    clock.t += ch.LIVE_S + 1
    _beat(c, "desk1", DESK_IP, call=CALL)
    t = c.get("/api/call/targets", params={"client_id": "desk1"}).json()["targets"]
    assert [x["name"] for x in t] == ["laptop"] or all(not x["live"] for x in t)


def test_offer_accept_connect_and_the_source_follows(api):
    c, ch, clock, pushed = api
    _beat(c, "desk1", DESK_IP, call=CALL)
    _beat(c, "phone1", PHONE_IP, kind="phone")
    o = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": CALL}).json()
    assert o["status"] == "pending" and o["to_name"] == "pixel-8a" and o["from_name"] == "DESKTOP-JARON"
    assert o["expires_in"] == int(ch.OFFER_TTL_S) and not o["pushed"] and pushed == []
    # The phone sees it on its next beat; alex's pages never do.
    offers = _beat(c, "phone1", PHONE_IP, kind="phone")["offers"]
    assert [x["id"] for x in offers] == [o["id"]] and offers[0]["model"] == "qwen3"
    assert offers[0]["prefs"] == {"echo": "auto"} and offers[0]["session_id"] == "s1"
    assert _beat(c, "x", PHONE_IP, user="alex")["offers"] == []
    assert c.get(f"/api/call/offer/{o['id']}", headers={"X-User": "alex"}).status_code == 404
    assert c.post(f"/api/call/offer/{o['id']}/accept", json={"client_id": "x"}, headers={"X-User": "alex"}).status_code == 404
    # Accepted: the source goes quiet. Connected: it hangs up.
    assert c.post(f"/api/call/offer/{o['id']}/accept", json={"client_id": "phone1"}).json()["status"] == "accepted"
    assert _beat(c, "desk1", DESK_IP, call=CALL)["outgoing"][0]["status"] == "accepted"
    assert c.post(f"/api/call/offer/{o['id']}/accept", json={"client_id": "desk2"}).status_code == 409
    assert c.post(f"/api/call/offer/{o['id']}/connected", json={"client_id": "desk2"}).status_code == 409
    assert c.post(f"/api/call/offer/{o['id']}/connected", json={"client_id": "phone1"}).json()["status"] == "connected"
    assert _beat(c, "desk1", DESK_IP)["outgoing"][0]["status"] == "connected"
    assert _beat(c, "phone1", PHONE_IP, kind="phone")["offers"] == []


def test_decline_timeout_and_a_failed_start_leave_the_source_live(api):
    c, ch, clock, _ = api
    _beat(c, "desk1", DESK_IP, call=CALL)
    _beat(c, "phone1", PHONE_IP, kind="phone")
    o = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": CALL}).json()
    assert c.post(f"/api/call/offer/{o['id']}/decline", json={}).json()["status"] == "declined"
    assert c.post(f"/api/call/offer/{o['id']}/accept", json={"client_id": "phone1"}).status_code == 409
    # Nobody answers.
    o = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": CALL}).json()
    clock.t += ch.OFFER_TTL_S + 1
    assert c.get(f"/api/call/offer/{o['id']}").json()["status"] == "expired"
    assert c.post(f"/api/call/offer/{o['id']}/accept", json={"client_id": "phone1"}).status_code == 409
    # Accepted but the phone never got its mic going.
    _beat(c, "desk1", DESK_IP, call=CALL)
    _beat(c, "phone1", PHONE_IP, kind="phone")
    o = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": CALL}).json()
    c.post(f"/api/call/offer/{o['id']}/accept", json={"client_id": "phone1"})
    clock.t += ch.CONNECT_TTL_S + 1
    d = c.get(f"/api/call/offer/{o['id']}").json()
    assert d["status"] == "failed" and "in time" in d["reason"]
    # Or said why it could not.
    _beat(c, "desk1", DESK_IP, call=CALL)
    _beat(c, "phone1", PHONE_IP, kind="phone")
    o = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": CALL}).json()
    c.post(f"/api/call/offer/{o['id']}/accept", json={"client_id": "phone1"})
    d = c.post(f"/api/call/offer/{o['id']}/failed", json={"client_id": "phone1", "reason": "No speech recognition here"}).json()
    assert d["status"] == "failed" and d["reason"] == "No speech recognition here"
    # A new offer from the same page replaces a pending one.
    a = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": CALL}).json()
    b = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": CALL}).json()
    assert c.get(f"/api/call/offer/{a['id']}").json()["status"] == "cancelled"
    assert c.post(f"/api/call/offer/{b['id']}/cancel", json={}).json()["status"] == "cancelled"
    # Finished offers are forgotten after a while.
    clock.t += ch.KEEP_DONE_S + ch.FORGET_S
    _beat(c, "desk1", DESK_IP)
    assert c.get(f"/api/call/offer/{a['id']}").status_code == 404


def test_offers_need_the_owners_chat_and_an_open_or_reachable_target(api):
    c, ch, clock, _ = api
    _beat(c, "desk1", DESK_IP, call=CALL)
    _beat(c, "phone1", PHONE_IP, kind="phone")
    bad = {**CALL, "session_id": "theirs-1"}
    assert c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": bad}).status_code == 404
    assert c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": {}}).status_code == 400
    assert c.post("/api/call/offer", json={"client_id": "desk1", "to": "gone", "call": CALL}).status_code == 404
    assert c.post("/api/call/offer", json={"client_id": "desk1", "to": "push:nope", "call": CALL}).status_code == 404
    hers = "push:" + ch.push_key("https://push.example/hers")
    assert c.post("/api/call/offer", json={"client_id": "desk1", "to": hers, "call": CALL}).status_code == 404
    assert c.post("/api/call/presence", json={"client_id": "bad id!"}).status_code == 400
    assert c.post("/api/call/offer/x/explode", json={}).status_code == 400


def test_a_phone_not_on_screen_gets_a_notification(api):
    c, ch, clock, pushed = api
    _beat(c, "desk1", DESK_IP, call=CALL)
    # Not open at all: through its push subscription only.
    to = "push:" + ch.push_key("https://push.example/laptop")
    o = c.post("/api/call/offer", json={"client_id": "desk1", "to": to, "call": CALL}).json()
    assert o["pushed"] and o["to_name"] == "laptop"
    # Open in the background with its own subscription.
    _beat(c, "phone1", PHONE_IP, kind="phone", visible=False, push_endpoint="https://push.example/phone")
    o2 = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone1", "call": CALL}).json()
    assert o2["pushed"]
    # The tab the notification opens is a new page of the same browser: it gets the offer.
    offers = _beat(c, "phone-new-tab", PHONE_IP, kind="phone", browser_id="b-phone1")["offers"]
    assert o2["id"] in [x["id"] for x in offers]
    # A page reporting a subscription that is not the owner's is not pushed to.
    _beat(c, "phone2", PHONE_IP, kind="phone", visible=False, push_endpoint="https://push.example/hers")
    o3 = c.post("/api/call/offer", json={"client_id": "desk1", "to": "phone2", "call": CALL}).json()
    assert not o3["pushed"]
    c.get("/api/call/offer/" + o["id"])                         # let the tasks run
    eps = [p["endpoint"] for p in pushed]
    assert eps == ["https://push.example/laptop", "https://push.example/phone"]
    assert pushed[1]["title"] == "Continue your call with qwen3"
    assert pushed[1]["url"] == f"/?call_offer={o2['id']}" and pushed[1]["tag"] == "odysseus-call"
    assert "DESKTOP-JARON" in pushed[1]["body"]


def test_pick_up_a_call_running_elsewhere(api):
    c, ch, clock, _ = api
    _beat(c, "desk1", DESK_IP, call=CALL)
    d = _beat(c, "phone1", PHONE_IP, kind="phone")
    assert d["calls"] == [{"client_id": "desk1", "name": "DESKTOP-JARON", "kind": "desktop", "session_id": "s1",
                           "chat_name": "Trip planning", "model": "qwen3", "prefs": {"echo": "auto"}}]
    assert _beat(c, "desk1", DESK_IP, call=CALL)["calls"] == []          # not its own
    assert _beat(c, "x", PHONE_IP, user="alex")["calls"] == []
    o = c.post("/api/call/pickup", json={"client_id": "phone1", "from": "desk1"}).json()
    assert o["status"] == "accepted" and o["session_id"] == "s1"
    out = _beat(c, "desk1", DESK_IP, call=CALL)["outgoing"]
    assert out[0]["id"] == o["id"] and out[0]["status"] == "accepted" and out[0]["to_name"] == "pixel-8a"
    assert c.post(f"/api/call/offer/{o['id']}/connected", json={"client_id": "phone1"}).json()["status"] == "connected"
    _beat(c, "desk1", DESK_IP)
    assert c.post("/api/call/pickup", json={"client_id": "phone1", "from": "desk1"}).status_code == 404


def test_every_call_route_is_authenticated():
    src = (_REPO / "routes" / "call_routes.py").read_text()
    routes = re.findall(r"@router\.(?:get|post)\(", src)
    assert len(routes) == src.count("owner = require_user(request)") == 7
    assert "from routes.call_routes import setup_call_routes" in (_REPO / "app.py").read_text()


# --- The voice call's system note --------------------------------------------

def test_call_turns_tell_the_model_it_is_in_a_voice_call():
    from routes.chat_routes import VOICE_CALL_NOTE, apply_voice_call_note
    msgs = [{"role": "system", "content": "You are Odysseus."}, {"role": "user", "content": "hi"}]
    apply_voice_call_note(msgs)
    assert msgs[0]["content"].startswith("You are Odysseus.") and VOICE_CALL_NOTE in msgs[0]["content"]
    assert len(msgs) == 2
    bare = [{"role": "user", "content": "hi"}]
    apply_voice_call_note(bare)
    assert bare[0] == {"role": "system", "content": VOICE_CALL_NOTE}
    for words in ("live voice call", "read aloud", "short", "markdown"):
        assert words in VOICE_CALL_NOTE
    route = (_REPO / "routes" / "chat_routes.py").read_text()
    assert 'form_data.get("voice_call")' in route
    chat = (_REPO / "static" / "js" / "chat.js").read_text()
    assert "if (_voiceTurn) fd.append('voice_call', '1');" in chat
    vc = (_REPO / "static" / "js" / "voiceCall.js").read_text()
    assert "cm.sendText(text, { voiceCall: true })" in vc and "fd.append('voice_call', '1');" in vc


def test_wiring_script_worker_and_service_worker():
    html = (_REPO / "static" / "index.html").read_text()
    sw = (_REPO / "static" / "sw.js").read_text()
    assert '<script type="module" src="/static/js/callHandoff.js"></script>' in html
    assert 'id="set-vcEcho"' in html
    assert "'/static/js/callHandoff.js'" in sw and "'/static/js/callPresenceWorker.js'" in sw
    assert "options.tag === 'odysseus-call'" in sw and "odysseus-call-offer" in sw
    assert "const CACHE_NAME = 'odysseus-v346';" in sw
    for f in ("callHandoff.js", "callPresenceWorker.js"):
        assert "\u2014" not in (_REPO / "static" / "js" / f).read_text()


# --- Two browsers: the desktop hands the call to the phone -------------------

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_PAGE = """<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<style>:root{--bg:#101114;--fg:#e8e8ea;--panel:#1b1c20;--border:#2c2d33;--red:#e5484d}
body{background:var(--bg);color:var(--fg);font-family:system-ui;margin:0}</style>
<link rel='stylesheet' href='/static/css/voiceCall.css'></head><body>
<header style='padding:14px 16px;border-bottom:1px solid var(--border)'>Odysseus</header>
<main style='padding:16px'>Trip planning</main>
<form id='chat-form' style='position:fixed;left:0;right:0;bottom:0;padding:12px;border-top:1px solid var(--border)'>
<textarea id='message' style='width:100%;height:48px'></textarea></form>
<script>
window.__ev = [];
window.chatModule = { currentSessionId: () => 's1', currentSessionName: () => 'Trip planning',
  hasActiveStream: () => false, sendText() {} };
window.sessionModule = { getSessions: () => [{id: 's1', name: 'Trip planning', model: 'qwen3'}],
  selectSession: (id) => { window.__selected = id; } };
</script>
<script type='module'>
import * as vc from '/static/js/voiceCall.js';
window.__vc = vc;
import('/static/js/callHandoff.js').then((m) => { window.__ch = m; });
</script></body></html>"""


def _wav(ms=200, rate=16000, freq=330.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(rate * ms / 1000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)))
    return buf.getvalue()


def _wire(ctx, client, ip):
    def route(r):
        req = r.request
        path = re.sub(r"^https://odysseus\.test", "", req.url)
        bare = path.split("?")[0]
        if bare.startswith("/api/call/"):
            res = client.request(req.method, path, content=req.post_data_buffer or b"",
                                 headers={"Content-Type": req.headers.get("content-type", "application/json"),
                                          "X-Forwarded-For": ip})
            r.fulfill(status=res.status_code, body=res.content, content_type="application/json")
        elif bare == "/api/stt/stats":
            r.fulfill(body=json.dumps({"available": True, "provider": "local"}), content_type="application/json")
        elif bare == "/api/stt/transcribe":
            r.fulfill(body=json.dumps({"text": ""}), content_type="application/json")
        elif bare == "/api/tts/stats":
            r.fulfill(body=json.dumps({"available": True, "provider": "local", "speed": 1}), content_type="application/json")
        elif bare == "/api/tts/synthesize":
            r.fulfill(body=_wav(), content_type="audio/wav")
        elif bare == "/":
            r.fulfill(body=_PAGE, content_type="text/html")
        elif bare.startswith("/static/") and (_REPO / bare.lstrip("/")).is_file():
            ct = "text/javascript" if bare.endswith(".js") else "text/css" if bare.endswith(".css") else "application/octet-stream"
            r.fulfill(body=(_REPO / bare.lstrip("/")).read_bytes(), content_type=ct)
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)


@pytest.fixture
def two(api, monkeypatch, tmp_path):
    c, ch, clock, pushed = api
    monkeypatch.setattr(ch, "_now", __import__("time").time)      # real time for the browsers
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                              "--autoplay-policy=no-user-gesture-required"])
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        desk_ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
        phone_ctx = browser.new_context(
            viewport={"width": 412, "height": 915}, is_mobile=True, has_touch=True, device_scale_factor=2.6,
            permissions=["microphone"],
            user_agent="Mozilla/5.0 (Linux; Android 15; Pixel 8a) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36")
        _wire(desk_ctx, c, DESK_IP)
        _wire(phone_ctx, c, PHONE_IP)
        desk, phone = desk_ctx.new_page(), phone_ctx.new_page()
        for pg in (desk, phone):
            pg.goto("https://odysseus.test/")
            pg.wait_for_function("() => window.__ch && window.__vc")
        # Both have beaten at least once.
        desk.wait_for_function("() => true")
        yield desk, phone, ch, tmp_path
        browser.close()


def _start_desk_call(desk):
    desk.evaluate("""() => { window.__call = window.__vc.open({sessionId: 's1', chatName: 'Trip planning',
      onEvent: (e) => window.__ev.push(e)}); }""")
    desk.wait_for_function("() => window.__call.state === 'listening'", timeout=10000)


def _offer_to_phone(desk):
    desk.wait_for_selector(".vc-move-btn:not([hidden]):not([disabled])")
    desk.click(".vc-move-btn")
    desk.wait_for_selector(".vc-move-item", timeout=10000)
    items = desk.locator(".vc-move-item")
    assert "pixel-8a" in items.nth(0).inner_text()                   # the phone first
    items.nth(0).click()


def test_the_desktop_hands_the_call_to_the_phone(two):
    desk, phone, ch, shots = two
    _start_desk_call(desk)
    _offer_to_phone(desk)
    desk.wait_for_function("() => /Waiting for pixel-8a/.test(document.querySelector('.vc-handoff').textContent)")
    phone.wait_for_selector(".vc-offer", timeout=10000)
    assert "Continue your call with qwen3" in phone.inner_text(".vc-offer")
    assert "DESKTOP-JARON" in phone.inner_text(".vc-offer")
    phone.screenshot(path=str(shots / "phone-offer.png"))
    phone.click(".vc-offer-go")                                        # the tap that allows mic and sound
    phone.wait_for_function("() => window.__vc.isActive() && window.__vc.current().state === 'listening'", timeout=10000)
    assert phone.evaluate("window.__vc.current().sid") == "s1"
    assert phone.evaluate("window.__selected") == "s1"                 # the chat comes up there too
    desk.wait_for_function("() => !window.__vc.isActive()", timeout=10000)
    assert desk.evaluate("window.__ev.some(e => e.type === 'ended' && e.reason === 'moved')")
    assert "Call moved to pixel-8a" in desk.inner_text(".vc-toast")
    assert desk.evaluate("document.querySelector('.vc-pill, .vc-overlay')") is None
    phone.screenshot(path=str(shots / "phone-call.png"))


def test_declined_the_desktop_call_carries_on(two):
    desk, phone, ch, _ = two
    _start_desk_call(desk)
    _offer_to_phone(desk)
    phone.wait_for_selector(".vc-offer", timeout=10000)
    phone.click(".vc-offer-no")
    desk.wait_for_function("() => /declined/.test(document.querySelector('.vc-handoff').textContent)", timeout=10000)
    assert desk.evaluate("window.__vc.isActive() && !window.__vc.current().muted")
    assert desk.evaluate("window.__vc.current().state") == "listening"
    assert not phone.evaluate("window.__vc.isActive()")


def test_unanswered_the_offer_times_out_and_the_desktop_call_carries_on(two, monkeypatch):
    desk, phone, ch, _ = two
    monkeypatch.setattr(ch, "OFFER_TTL_S", 7.0)
    _start_desk_call(desk)
    _offer_to_phone(desk)
    phone.wait_for_selector(".vc-offer", timeout=10000)
    desk.wait_for_function("() => /No answer on pixel-8a/.test(document.querySelector('.vc-handoff').textContent)", timeout=15000)
    assert desk.evaluate("window.__vc.isActive() && window.__vc.current().state === 'listening'")
    phone.wait_for_selector(".vc-offer", state="detached", timeout=10000)


def test_the_phone_picks_up_the_call_running_on_the_desktop(two):
    desk, phone, ch, shots = two
    _start_desk_call(desk)
    phone.wait_for_selector(".vc-pickup", timeout=10000)
    assert "DESKTOP-JARON" in phone.inner_text(".vc-pickup")
    phone.screenshot(path=str(shots / "phone-pickup.png"))
    phone.click(".vc-pickup-go")
    phone.wait_for_function("() => window.__vc.isActive() && window.__vc.current().state === 'listening'", timeout=10000)
    desk.wait_for_function("() => !window.__vc.isActive()", timeout=10000)
    assert "Call moved to pixel-8a" in desk.inner_text(".vc-toast")
