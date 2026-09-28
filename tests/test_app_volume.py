"""Two volumes in the music bar: the computer's and the playing app's.

Asked for: "not just youtube music's volume, let me change computer volume
too". The bar's volume was the Windows master volume all along; this adds the
playing app's own level (its entry in the Windows volume mixer) beside it,
and labels which is which.
"""
import importlib.util
import json
import os
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import media_routes

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "desktop_mcp_server_av", os.path.join(_HERE, "tools", "mcp", "desktop_mcp_server.py"))
dms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dms)


class _Vol:
    def __init__(self, level, muted=False):
        self.level, self.muted = level, muted

    def GetMasterVolume(self):
        return self.level

    def SetMasterVolume(self, v, _ctx):
        self.level = v

    def GetMute(self):
        return self.muted

    def SetMute(self, m, _ctx):
        self.muted = bool(m)


class _Proc:
    def __init__(self, name):
        self._n = name

    def name(self):
        return self._n


class _Session:
    def __init__(self, name, level, state=1, muted=False):
        self.Process = _Proc(name) if name else None
        self.SimpleAudioVolume = _Vol(level, muted)
        self.State = state


def _fake_pycaw(monkeypatch, sessions):
    pkg = types.ModuleType("pycaw")
    mod = types.ModuleType("pycaw.pycaw")
    mod.AudioUtilities = types.SimpleNamespace(GetAllSessions=lambda: sessions)
    pkg.pycaw = mod
    monkeypatch.setitem(sys.modules, "pycaw", pkg)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", mod)
    monkeypatch.setattr(dms, "_input_guard", lambda: None)


def test_app_volume_follows_the_media_session(monkeypatch):
    # Chrome has two sessions (tabs in different processes); both move.
    chrome_a, chrome_b = _Session("chrome.exe", 0.8), _Session("chrome.exe", 0.8, muted=True)
    sessions = [_Session(None, 1.0), _Session("Discord.exe", 0.5), chrome_a, chrome_b]
    _fake_pycaw(monkeypatch, sessions)
    monkeypatch.setattr(dms, "_last_media_source", "Chrome")
    got = dms.get_app_volume()
    assert got == {"ok": True, "app": "chrome.exe", "label": "Chrome", "volume": 80, "muted": False}
    out = dms.set_app_volume(35)
    assert out["ok"] and out["volume"] == 35 and out["was"] == 80
    assert chrome_a.SimpleAudioVolume.level == chrome_b.SimpleAudioVolume.level == 0.35
    assert chrome_b.SimpleAudioVolume.muted is False      # a level on a muted app is silent
    assert sessions[1].SimpleAudioVolume.level == 0.5     # other apps untouched


def test_app_volume_by_name_and_fallbacks(monkeypatch):
    yt = _Session("YouTube Music.exe", 0.6)
    _fake_pycaw(monkeypatch, [_Session("Spotify.exe", 0.4, state=0), yt])
    monkeypatch.setattr(dms, "_last_media_source", "com.github.th-ch.youtube-music")
    assert dms.get_app_volume()["app"] == "YouTube Music.exe"
    assert dms.get_app_volume(app="spotify.exe")["volume"] == 40
    assert not dms.get_app_volume(app="vlc.exe")["ok"]
    # No media id yet: the one app actually making sound.
    monkeypatch.setattr(dms, "_last_media_source", "")
    assert dms.get_app_volume()["app"] == "YouTube Music.exe"
    assert not dms.set_app_volume(101)["ok"]


def test_route_sends_app_volume_and_state_reads_it(monkeypatch):
    calls = []

    class _MCP:
        async def call_tool(self, name, args):
            calls.append((name, args))
            if name.endswith("__get_app_volume"):
                return {"stdout": json.dumps({"ok": True, "app": "chrome.exe", "label": "Chrome", "volume": 40})}
            return {"stdout": json.dumps({"ok": True})}

    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(media_routes.setup_media_routes(_MCP()))
    c = TestClient(app)
    r = c.post("/api/media/control", json={"action": "app_volume", "value": 25, "server_id": "pc1"})
    assert r.status_code == 200
    assert calls[-1] == ("mcp__pc1__set_app_volume", {"percent": 25})
    assert c.post("/api/media/control", json={"action": "app_volume", "value": "x",
                                              "server_id": "pc1"}).status_code == 400
    st = c.get("/api/media/state?server_id=pc1").json()
    assert st["get_app_volume"]["label"] == "Chrome"


def test_ui_labels_both_volumes():
    read = lambda *p: open(os.path.join(_HERE, *p), encoding="utf-8").read()
    js = read("static", "js", "musicBar.js")
    assert "data-mb-appvol" in js and "_control('app_volume', nv)" in js
    assert 'data-mb="voltarget"' in js and '<span class="mp-vollabel">${_esc(_devName())}</span>' in js
    assert "_control(app ? 'app_volume' : 'volume', nv)" in js
    ov = read("tools", "music_overlay", "music_overlay.py")
    assert "Volume buttons: computer" in ov and '"<MouseWheel>"' in ov
    assert "source_app_user_model_id" in ov
