"""A shared video keeps playing after its machine's MCP server restarts.

Seen 2026-09-29: "how long is the sharing for? story_60s_FINAL.mp4 - That
file is not shared any more ... i think it expired": the links were only in
the server's memory, and reinstalling the PC's server had restarted it.
"""
import asyncio
import importlib.util
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fresh_server(shared_file):
    """Load the Linux desktop MCP as a new process would, reading shared_file."""
    sys.path.insert(0, os.path.join(ROOT, "tools", "mcp"))
    spec = importlib.util.spec_from_file_location(
        f"linux_mcp_{time.time_ns()}", os.path.join(ROOT, "tools", "mcp", "linux_desktop_mcp_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._SHARED.clear()
    mod._SHARED_FILE = str(shared_file)
    mod._load_shared()
    return mod


class _Req:
    def __init__(self, token):
        self.path_params = {"token": token}
        self.headers = {}
        self.method = "HEAD"


def _status(mod, token):
    r = mod._media_response(_Req(token))
    r = asyncio.run(r) if asyncio.iscoroutine(r) else r
    return r.status_code


def test_a_link_survives_a_restart(tmp_path):
    video = tmp_path / "story_60s_FINAL.mp4"
    video.write_bytes(b"\0" * 1024)
    store = tmp_path / "shared_media.json"
    first = _fresh_server(store)
    link = first.share_media(str(video))["link"]
    token = link.split("/")[3]
    assert _status(first, token) in (200, 206)
    again = _fresh_server(store)                     # the server restarted
    assert token in again._SHARED and _status(again, token) in (200, 206)
    assert again.share_media(str(video))["link"] == link   # sharing it again: same link


def test_old_links_end_and_playing_extends_them(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"\0" * 10)
    mod = _fresh_server(tmp_path / "s.json")
    token = mod.share_media(str(video))["link"].split("/")[3]
    mod._SHARED[token]["ts"] = time.time() - mod._SHARE_TTL_S + 60     # a minute left
    assert _status(mod, token) in (200, 206)
    assert time.time() - mod._SHARED[token]["ts"] < 5                 # played: 7 more days
    mod._SHARED[token]["ts"] = time.time() - mod._SHARE_TTL_S - 1
    assert _status(mod, token) == 404                                  # expired


def test_windows_has_the_same_code():
    win = open(os.path.join(ROOT, "tools", "mcp", "desktop_mcp_server.py"), encoding="utf-8").read()
    lin = open(os.path.join(ROOT, "tools", "mcp", "linux_desktop_mcp_server.py"), encoding="utf-8").read()
    for piece in ('_SHARED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_media.json")',
                  "def _load_shared() -> None:", "def _save_shared() -> None:",
                  "    _save_shared()\n    name = os.path.basename(p)",
                  'rec["ts"] = now                               # played: another 7 days'):
        assert piece in win and piece in lin, piece
