"""The music bar works on the phone, and music moves between devices.

Asked for on 2026-09-28: "I want the mobile version to work with the music
too, I have it open on phone not pc and it's playing on the phone but I don't
see it", and "start it up on my PC or my phone and listen on a different
device: my headphones are connected to PC not phone".
"""
import importlib.util
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import media_routes
from src import device_routing, devices

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHONE = {"name": "pixel-8a", "endpoint": "http://pixel-8a.example:8778", "token": "t", "kind": "phone",
         "commands": ["notify", "now_playing", "media_control", "get_volume", "set_volume", "set_mute", "open_url"]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(devices, "list_devices", lambda: [PHONE, {"name": "old-phone", "endpoint": "", "commands": ["notify"]}])
    monkeypatch.setattr(devices, "get", lambda name: PHONE if name == "pixel-8a" else None)
    monkeypatch.setattr(device_routing, "_resolve", lambda host: ["100.96.131.64"] if "pixel" in host else [])
    device_routing._phone_addr_cache.clear()
    pc = {"server_id": "19d772b0", "name": "windows-desktop", "host": "100.102.86.125",
          "tools": ["now_playing", "get_volume", "media_control", "open_media_url"]}
    monkeypatch.setattr(device_routing, "device_map", lambda mgr: {"100.102.86.125": pc})
    phone_state = {"playing": True, "title": "Save My Love", "artist": "Marshmello", "volume": 60}
    sent = []

    async def send(dev, cmd, params=None):
        sent.append((cmd, params))
        if cmd == "now_playing":
            return {"ok": True, "result": {"ok": True, "playing": phone_state["playing"],
                                           "title": phone_state["title"], "artist": phone_state["artist"]}}
        if cmd == "get_volume":
            return {"ok": True, "result": {"ok": True, "volume": phone_state["volume"], "muted": False}}
        if cmd == "set_volume":
            phone_state["volume"] = params["percent"]
        if cmd == "media_control" and params["action"] == "pause":
            phone_state["playing"] = False
        return {"ok": True, "result": {"ok": True}}
    monkeypatch.setattr(devices, "send_command", send)
    mcp_calls = []

    class _MCP:
        async def call_tool(self, name, args):
            mcp_calls.append((name, args))
            if name.endswith("__now_playing"):
                return {"stdout": json.dumps({"playing": True, "title": "Never Going Home Tonight", "artist": "David Guetta"})}
            return {"stdout": json.dumps({"ok": True})}
    monkeypatch.setattr(media_routes, "_youtube_id", lambda t, a: "q6gO89g1hJc" if "Save" in t else "hmXHtoW7WvU")
    app = FastAPI()
    app.include_router(media_routes.setup_media_routes(_MCP()))
    return TestClient(app), sent, mcp_calls, phone_state


def test_a_phone_is_a_music_device_and_its_browser_gets_it(env):
    c, sent, mcp, st = env
    phones = device_routing.phone_devices()
    assert [p["server_id"] for p in phones] == ["device:pixel-8a"]           # old-phone has no media
    assert device_routing.for_client(None, "100.96.131.64")["server_id"] == "device:pixel-8a"
    r = c.get("/api/media/state", headers={"x-forwarded-for": "100.96.131.64"}).json()
    assert r["ok"] and r["now_playing"]["title"] == "Save My Love" and r["get_volume"]["volume"] == 60
    assert {"device:pixel-8a", "19d772b0"} <= {d["server_id"] for d in r["available"]}


def test_phone_controls(env):
    c, sent, mcp, st = env
    body = {"server_id": "device:pixel-8a"}
    c.post("/api/media/control", json=dict(body, action="play_pause"))
    assert ("media_control", {"action": "play_pause"}) in sent
    c.post("/api/media/control", json=dict(body, action="volume_up"))
    assert st["volume"] == 70                                               # +10 from 60
    c.post("/api/media/control", json=dict(body, action="volume", value=25))
    assert ("set_volume", {"percent": 25}) in sent


def test_handoff_phone_to_pc_and_back(env):
    c, sent, mcp, st = env
    r = c.post("/api/media/handoff", json={"from": "device:pixel-8a", "to": "19d772b0"}).json()
    assert r["ok"] and r["to"] == "windows-desktop" and r["url"] == "https://music.youtube.com/watch?v=q6gO89g1hJc"
    assert ("media_control", {"action": "pause"}) in sent and st["playing"] is False    # phone paused first
    assert ("mcp__19d772b0__open_media_url", {"url": r["url"]}) in mcp
    r2 = c.post("/api/media/handoff", json={"from": "19d772b0", "to": "device:pixel-8a"}).json()
    assert ("mcp__19d772b0__media_control", {"action": "play_pause"}) in mcp
    assert ("open_url", {"url": "https://music.youtube.com/watch?v=hmXHtoW7WvU"}) in sent
    assert c.post("/api/media/handoff", json={"from": "19d772b0", "to": "19d772b0"}).status_code == 400


def test_open_media_url_only_opens_youtube():
    spec = importlib.util.spec_from_file_location("dms_media_url", os.path.join(HERE, "tools", "mcp", "desktop_mcp_server.py"))
    dms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dms)
    for bad in ("https://evil.example/x", "http://music.youtube.com/watch?v=x", "file:///etc/passwd",
                "https://music.youtube.com.evil.example/"):
        assert not dms.open_media_url(bad)["ok"], bad
    assert dms._MEDIA_URL_RE.match("https://music.youtube.com/watch?v=q6gO89g1hJc")


def test_the_pc_bar_says_what_the_phone_is_playing(env):
    # Reported: "the music didn't work from my phone, I was playing yet my PC
    # saw nothing".
    c, sent, mcp, st = env

    class _IdlePC:
        async def call_tool(self, name, args):
            if name.endswith("__now_playing"):
                return {"stdout": json.dumps({"playing": False, "title": ""})}
            return {"stdout": json.dumps({"ok": True})}

    def fresh():
        app = FastAPI()
        app.include_router(media_routes.setup_media_routes(_IdlePC()))
        return TestClient(app)

    pc = fresh()
    r = pc.get("/api/media/state", headers={"x-forwarded-for": "100.102.86.125"}).json()
    assert r["device"]["server_id"] == "19d772b0" and not r["now_playing"]["playing"]
    assert r["elsewhere"] == [{"server_id": "device:pixel-8a", "name": "pixel-8a", "kind": "phone",
                               "title": "Save My Love", "artist": "Marshmello"}]
    asks = sum(1 for cmd, _ in sent if cmd == "now_playing")
    pc.get("/api/media/state", headers={"x-forwarded-for": "100.102.86.125"})
    assert sum(1 for cmd, _ in sent if cmd == "now_playing") == asks      # cached: the bar polls often

    st["playing"] = False
    r = fresh().get("/api/media/state", headers={"x-forwarded-for": "100.102.86.125"}).json()
    assert r["elsewhere"] == []                                              # paused is not "playing on"


def test_the_bar_offers_control_and_listen_here():
    js = open(os.path.join(HERE, "static", "js", "musicBar.js"), encoding="utf-8").read()
    assert "(_state.elsewhere || []).length ? _elsewhereHtml(_state.elsewhere[0])" in js
    assert 'data-mb-control="' in js and 'data-mb-listen="${_esc(e.server_id)}"' in js


def test_listen_here_plays_it_in_the_browser(env):
    # Asked for: "stream it over to my PC or my laptop so that I don't need
    # YouTube Music installed".
    c, sent, mcp, st = env
    real_send = devices.send_command

    async def with_position(dev, cmd, params=None):
        r = await real_send(dev, cmd, params)
        if cmd == "now_playing":
            r["result"]["position_ms"] = 83500
        return r
    devices.send_command = with_position
    try:
        r = c.post("/api/media/listen", json={"from": "device:pixel-8a"}).json()
    finally:
        devices.send_command = real_send
    assert r["video_id"] == "q6gO89g1hJc" and r["start_s"] == 83 and r["from_name"] == "pixel-8a"
    assert ("media_control", {"action": "pause"}) in sent and st["playing"] is False   # paused there
    assert not any(n.endswith("open_media_url") for n, _ in mcp)                     # nothing opened on a PC
    assert c.post("/api/media/listen", json={"from": "nope"}).status_code == 400
    assert c.post("/api/media/listen", json={"from": "device:pixel-8a"}).status_code == 200  # paused: still has a title


def test_the_browser_player_and_device_names():
    js = open(os.path.join(HERE, "static", "js", "musicBar.js"), encoding="utf-8").read()
    assert "const YT_ORIGIN = 'https://www.youtube-nocookie.com';" in js      # the one frame-src the CSP allows
    assert "/embed/${encodeURIComponent(d.video_id)}?autoplay=1&start=" in js
    assert "m.event === 'onError'" in js                                       # embedding blocked: link instead
    # The app sends Referrer-Policy: no-referrer; without this the player
    # refuses to play at all (error 153).
    assert 'referrerpolicy="strict-origin-when-cross-origin"' in js
    # Reported: "when I change volume it says PC instead of what device".
    assert "function _targetName() { return _volTarget() === 'app' ? _appLabel() : _devName(); }" in js
    assert "'PC'; }" not in js.split("function _targetName")[1][:120]
    css = open(os.path.join(HERE, "core", "middleware.py"), encoding="utf-8").read()
    assert "frame-src 'self' https://www.youtube-nocookie.com" in css


def test_state_names_the_device_being_controlled(env):
    c, sent, mcp, st = env
    r = c.get("/api/media/state?server_id=device:pixel-8a", headers={"x-forwarded-for": "100.102.86.125"}).json()
    assert r["device"]["server_id"] == "device:pixel-8a" and r["device"]["kind"] == "phone"
    assert r["here"]["server_id"] == "19d772b0"                        # the PC this browser is on


def test_hear_the_phone_on_a_computer(env, monkeypatch):
    # Asked for: "if I have earbuds connected to the PC it should play through
    # there, but the music engine is my phone".
    c, sent, mcp, st = env
    pc = device_routing.device_map(None)["100.102.86.125"]
    body = {"from": "device:pixel-8a", "to": "19d772b0", "on": True}
    r = c.post("/api/media/receive", json=body)
    assert r.status_code == 409 and "updated desktop MCP" in r.json()["detail"]    # old MCP: no tool
    pc["tools"] = pc["tools"] + ["bluetooth_audio_receive"]
    r = c.post("/api/media/receive", json=body).json()
    assert r["ok"] and r["to"] == "windows-desktop" and r["from"] == "pixel-8a"
    assert ("mcp__19d772b0__bluetooth_audio_receive", {"device": "pixel-8a", "on": True}) in mcp
    assert not any(cmd == "media_control" for cmd, _ in sent)                     # the phone keeps playing
    c.post("/api/media/receive", json={"to": "19d772b0", "on": False})
    assert ("mcp__19d772b0__bluetooth_audio_receive", {"device": "", "on": False}) in mcp
    assert c.post("/api/media/receive", json={"from": "device:pixel-8a", "to": "device:pixel-8a"}).status_code == 400


def test_the_desktop_side_matches_the_phone_by_name_and_opens_the_app():
    src = open(os.path.join(HERE, "tools", "mcp", "desktop_mcp_server.py"), encoding="utf-8").read()
    assert "AudioPlaybackConnection.try_create_from_id(d.id)" in src and "await conn.open_async()" in src
    assert "--app-launch-url-for-shortcuts-menu-item=" in src and '"YouTube Music*.lnk"' in src
    ns = {}
    exec(src[src.index("def _norm_name"):src.index("async def _audio_sources")], {"re": __import__("re")}, ns)
    assert ns["_norm_name"]("pixel-8a") in ns["_norm_name"]("Pixel 8a")

def test_pop_out_opens_on_this_computer_even_when_the_bar_drives_the_phone(env, monkeypatch):
    # Seen live: with the bar controlling the phone, every Pop out asked the
    # phone for the Windows overlay task, got 502 and fell back to Chrome's
    # pop-out window ("popup for music is doing the chrome new tab still").
    c, sent, mcp, st = env
    from src import machines
    started = []
    peers = [{"host": "100.102.86.125", "os": "windows", "name": "desktop-jaron"},
             {"host": "pixel-8a.example", "os": "android", "name": "pixel-8a"}]
    monkeypatch.setattr(machines, "peers", lambda: peers)
    monkeypatch.setattr(machines, "find_peer", lambda ps, host: next((p for p in ps if p["host"] == host), None))

    async def run_task(peer, user, task):
        started.append((peer["name"], task))
        return {"ok": True} if peer["os"] == "windows" else {"ok": False, "error": "only Windows machines have this task"}
    monkeypatch.setattr(machines, "run_user_task", run_task)
    r = c.post("/api/media/overlay", json={"server_id": "device:pixel-8a"},
               headers={"x-forwarded-for": "100.102.86.125"})
    assert r.status_code == 200 and started == [("desktop-jaron", "MusicOverlay")]
    # From the phone's own browser there is no computer here: say so, unless one is picked.
    r = c.post("/api/media/overlay", json={"server_id": "device:pixel-8a"},
               headers={"x-forwarded-for": "100.96.131.64"})
    assert r.status_code == 400 and "computer" in r.json()["detail"]
    r = c.post("/api/media/overlay", json={"server_id": "19d772b0"}, headers={"x-forwarded-for": "100.96.131.64"})
    assert r.status_code == 200 and started[-1] == ("desktop-jaron", "MusicOverlay")


def test_one_device_plays_at_a_time(env):
    # Reported: "I can press open here on the website, then I have it running
    # on both my devices".
    c, sent, mcp, st = env
    first = c.post("/api/media/listen", json={"from": "device:pixel-8a"}).json()["listen_id"]
    state = c.get("/api/media/state?server_id=device:pixel-8a", headers={"x-forwarded-for": "100.102.86.125"}).json()
    assert state["listen_id"] == first
    st["playing"] = True
    second = c.post("/api/media/listen", json={"from": "device:pixel-8a"}).json()["listen_id"]
    assert second != first                                             # the first browser's player stops
    c.post("/api/media/handoff", json={"from": "device:pixel-8a", "to": "19d772b0"})
    state = c.get("/api/media/state?server_id=device:pixel-8a", headers={"x-forwarded-for": "100.102.86.125"}).json()
    assert state["listen_id"] not in (first, second)                   # a handoff stops browser players too
    js = open(os.path.join(HERE, "static", "js", "musicBar.js"), encoding="utf-8").read()
    assert "if (_state.listen_id && _listen.id && _state.listen_id !== _listen.id)" in js
    assert "} else if (_listen && ['play_pause', 'next', 'previous'].includes(what)) {" in js
    assert "_listenCommand(_listen.playing ? 'pauseVideo' : 'playVideo')" in js


def test_hear_it_only_where_it_works_and_pairing_from_here(env):
    c, sent, mcp, st = env
    pc = device_routing.device_map(None)["100.102.86.125"]
    avail = {d["server_id"]: d for d in c.get("/api/media/state", headers={"x-forwarded-for": "100.102.86.125"}).json()["available"]}
    assert avail["19d772b0"]["can_receive"] is False and avail["19d772b0"]["can_pair"] is False
    assert c.post("/api/media/pair", json={"from": "device:pixel-8a", "to": "19d772b0"}).status_code == 409
    pc["tools"] = pc["tools"] + ["bluetooth_audio_receive", "bluetooth_pair", "open_bluetooth_settings"]
    avail = {d["server_id"]: d for d in c.get("/api/media/state", headers={"x-forwarded-for": "100.102.86.125"}).json()["available"]}
    assert avail["19d772b0"]["can_receive"] and avail["19d772b0"]["can_pair"]
    r = c.post("/api/media/pair", json={"from": "device:pixel-8a", "to": "19d772b0"}).json()
    assert r["ok"] and ("mcp__19d772b0__bluetooth_pair", {"device": "pixel-8a", "seconds": 25}) in mcp
    r = c.post("/api/media/pair", json={"to": "19d772b0", "manual": True}).json()
    assert r["opened"] == "bluetooth settings" and ("mcp__19d772b0__open_bluetooth_settings", {}) in mcp
    js = open(os.path.join(HERE, "static", "js", "musicBar.js"), encoding="utf-8").read()
    assert "d.kind !== 'phone' && d.can_receive && d.server_id !== current" in js
    assert "if (on && /pair/i.test(e.message))" in js and "data-mb-pair-manual" in js
    mcp_src = open(os.path.join(HERE, "tools", "mcp", "desktop_mcp_server.py"), encoding="utf-8").read()
    assert "async def bluetooth_pair(device: str, seconds: int = 20)" in mcp_src and "args.accept()" in mcp_src


def test_a_stale_pairing_is_named_and_redone():
    # Seen live: the Pixel was paired with the PC, but only its plain Bluetooth
    # record existed (no audio side), so Hear it could never connect.
    src = open(os.path.join(HERE, "tools", "mcp", "desktop_mcp_server.py"), encoding="utf-8").read()
    assert "is paired with this PC but not as an audio source" in src and '"stale_pairing": stale.name' in src
    assert "await stale.pairing.unpair_async()" in src and '"removed_old_pairing": removed' in src


def test_pair_opens_the_phones_pairing_screen_first(env, monkeypatch):
    c, sent, mcp, st = env
    pc = device_routing.device_map(None)["100.102.86.125"]
    pc["tools"] = pc["tools"] + ["bluetooth_pair", "open_bluetooth_settings", "bluetooth_audio_receive"]
    PHONE["commands"] = PHONE["commands"] + ["bt_pairing"]
    try:
        import routes.media_routes as mr
        slept = []

        async def no_sleep(s):
            slept.append(s)
        monkeypatch.setattr(mr.asyncio, "sleep", no_sleep)
        r = c.post("/api/media/pair", json={"from": "device:pixel-8a", "to": "19d772b0"}).json()
        assert r["phone_screen_opened"] is True and slept == [2]
        assert [cmd for cmd, _ in sent][-1] == "bt_pairing"                       # the phone first...
        assert ("mcp__19d772b0__bluetooth_pair", {"device": "pixel-8a", "seconds": 25}) == mcp[-1]  # ...then the PC
    finally:
        PHONE["commands"] = [x for x in PHONE["commands"] if x != "bt_pairing"]


def test_the_pc_matches_the_phone_strictly_and_only_unpairs_a_phone():
    # Seen live: a loose match on "pixel" hit "Jaron's Pixel Buds Pro" and
    # removed their pairing from the PC.
    src = open(os.path.join(HERE, "tools", "mcp", "desktop_mcp_server.py"), encoding="utf-8").read()
    ns = {"re": __import__("re")}
    exec(src[src.index("def _norm_name"):src.index("_AEP = [")], ns)
    m, n = ns["_name_matches"], ns["_norm_name"]
    assert m(n("pixel-8a"), "Pixel 8a") and m(n("pixel-8a"), "Jaron's Pixel 8a")
    assert not m(n("pixel-8a"), "Jaron's Pixel Buds Pro") and not m(n("pixel"), "Jaron's Pixel Buds Pro")
    assert not m("", "Pixel 8a") and not m(n("px"), "px")
    assert "if stale is not None and await _is_phone(stale):" in src
    assert "BluetoothMajorClass.PHONE" in src
    assert "DeviceInformationKind.ASSOCIATION_ENDPOINT)" in src
    assert "want in _norm_name" not in src
