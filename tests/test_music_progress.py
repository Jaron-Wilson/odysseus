"""The music bar's progress line: position and duration from every machine.

Asked for on 2026-09-29: "its supposed to make a line to have music
duration/duration left" (the chat's own coding agents could not finish it).
"""
import importlib.util
import os
import sys

from routes.media_routes import normalize_now_playing

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _linux_mcp():
    sys.path.insert(0, os.path.join(HERE, "tools", "mcp"))
    spec = importlib.util.spec_from_file_location(
        "linux_mcp_progress", os.path.join(HERE, "tools", "mcp", "linux_desktop_mcp_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_laptops_players_list_becomes_the_one_being_heard():
    np = normalize_now_playing({"playing": True, "players": [
        {"player": "vlc", "status": "Paused", "title": "Old", "position": 10, "duration": 100},
        {"player": "chromium", "status": "Playing", "title": "Now", "artist": "Band",
         "position": 61.5, "duration": 200, "position_at": 1790670000.5},
    ]})
    assert np["title"] == "Now" and np["artist"] == "Band" and np["playing"] is True
    assert (np["position"], np["duration"], np["position_at"]) == (61.5, 200.0, 1790670000.5)
    assert "players" not in np


def test_the_phone_sends_milliseconds_and_no_time():
    np = normalize_now_playing({"playing": True, "title": "Song", "position_ms": 83000, "duration_ms": 225000})
    assert (np["position"], np["duration"], np["position_at"]) == (83.0, 225.0, None)


def test_a_stream_or_a_silent_player_has_no_line():
    for raw in ({"playing": True, "title": "Radio", "position": 30, "duration": None},
                {"playing": True, "title": "Radio", "position": 30, "duration": 0},
                {"playing": True, "title": "Old MCP"}):
        np = normalize_now_playing(raw)
        assert np["position"] is None and np["duration"] is None and np["position_at"] is None
    assert normalize_now_playing({}) == {} and normalize_now_playing(None) == {}


def test_a_position_past_the_end_is_the_end():
    assert normalize_now_playing({"title": "x", "position": 400, "duration": 300})["position"] == 300


def test_mpris_times_are_read_from_gdbus():
    m = _linux_mcp()
    assert m._position_s("(<int64 83000000>,)") == 83.0
    assert m._position_s("(<83500000>,)") == 83.5
    meta = "({'mpris:trackid': <objectpath '/t/1'>, 'mpris:length': <uint64 225000000>, 'xesam:title': <'Song'>},)"
    assert m._length_s(meta) == 225.0
    assert m._length_s("({'xesam:title': <'Live'>},)") is None


def test_windows_reads_the_timeline():
    src = open(os.path.join(HERE, "tools", "mcp", "desktop_mcp_server.py"), encoding="utf-8").read()
    i = src.index("def now_playing()")
    body = src[i:i + 4000]
    assert "GetTimelineProperties()" in body and "LastUpdatedTime.ToUnixTimeMilliseconds()" in body
    assert "position_at = $at" in body


def test_all_three_surfaces_draw_it():
    js = open(os.path.join(HERE, "static", "js", "musicBar.js"), encoding="utf-8").read()
    assert "${has ? PROGRESS_BAR_HTML : ''}" in js                       # mini bar and pop-out
    assert "${np.title ? PROGRESS_PANEL_HTML : ''}" in js                # panel
    assert "pip.setInterval(_paintProgress, PROGRESS_MS)" in js          # keeps moving popped out
