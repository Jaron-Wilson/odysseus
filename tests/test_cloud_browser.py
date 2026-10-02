"""The cloud browser (src/cloud_browser.py): the agent's browser, watched live.

Asked for on 2026-09-29: "can we add a cloud browser like chatgpt does and
manus ai?"
"""
import asyncio
import os
import random
import shutil
import signal
import socket
import subprocess

import pytest

from src import builtin_mcp, cloud_browser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_agents_browser_mcp_uses_the_cloud_browser(monkeypatch):
    base = builtin_mcp._BUILTIN_NPX_SERVERS["builtin_browser"]["args"]
    monkeypatch.setattr(cloud_browser, "ensure", lambda: "http://127.0.0.1:9333")
    args = asyncio.run(builtin_mcp._browser_args(base))
    assert args[-2:] == ["--cdp-endpoint", "http://127.0.0.1:9333"]
    assert "--headless" not in args and "--browser" not in args and "--caps" in args
    # No cloud browser: the MCP's own headless one, as before.
    monkeypatch.setattr(cloud_browser, "ensure", lambda: "")
    assert asyncio.run(builtin_mcp._browser_args(base)) == base


def test_a_click_on_the_picture_lands_on_the_page():
    v = cloud_browser.Viewer()
    v._size = (1280, 800)
    assert v._xy({"fx": 0.5, "fy": 0.25}) == (640.0, 200.0)
    assert v._xy({"fx": 2, "fy": -1}) == (1280.0, 0.0)        # clamped to the page


def test_x11_display_for_input_respects_headful_flag_and_xvfb_state(monkeypatch):
    monkeypatch.setattr(cloud_browser.shutil, "which", lambda name: "/usr/bin/xdotool")
    monkeypatch.setattr(cloud_browser, "_xvfb_display", lambda: ":321")
    monkeypatch.setattr(cloud_browser, "_xvfb_running", lambda d: d == ":321")
    monkeypatch.setattr(cloud_browser, "headful_enabled", lambda: True)
    assert cloud_browser.x11_display_for_input() == ":321"
    monkeypatch.setattr(cloud_browser, "headful_enabled", lambda: False)
    assert cloud_browser.x11_display_for_input() == ""           # headless mode: use CDP input
    monkeypatch.setattr(cloud_browser, "headful_enabled", lambda: True)
    monkeypatch.setattr(cloud_browser, "_xvfb_running", lambda d: False)
    assert cloud_browser.x11_display_for_input() == ""           # Xvfb never came up: use CDP input
    monkeypatch.setattr(cloud_browser, "_xvfb_running", lambda d: True)
    monkeypatch.setattr(cloud_browser.shutil, "which", lambda name: None)
    assert cloud_browser.x11_display_for_input() == ""           # no xdotool: use CDP input, not a silent no-op


def test_act_replays_input_through_x11_not_cdp_when_headful_display_is_up(monkeypatch):
    """Google blocks sign-in on CDP Input.* traffic even in a real Chrome
    (2026-10-02); the take-over path must use real X11 input (xdotool)
    instead whenever the headful display is actually up, not Playwright's
    page.mouse/page.keyboard."""
    calls = []

    async def fake_xdotool(display, *args):
        calls.append((display, args))
        return True

    monkeypatch.setattr(cloud_browser, "_xdotool", fake_xdotool)
    monkeypatch.setattr(cloud_browser, "x11_display_for_input", lambda: ":321")

    class _FakeMouse:
        async def click(self, *a, **k): raise AssertionError("CDP mouse.click used instead of X11")
        async def move(self, *a, **k): raise AssertionError("CDP mouse.move used instead of X11")

    class _FakeKeyboard:
        async def press(self, *a, **k): raise AssertionError("CDP keyboard.press used instead of X11")
        async def insert_text(self, *a, **k): raise AssertionError("CDP keyboard.insert_text used instead of X11")

    class _FakePage:
        is_closed = lambda self: False
        mouse = _FakeMouse()
        keyboard = _FakeKeyboard()
        url = "https://accounts.google.com/"

    async def go():
        v = cloud_browser.Viewer()
        v._page = _FakePage()
        v._size = (1280, 720)

        async def _noop_connect():
            return None
        v._connect = _noop_connect
        await v.act({"type": "click", "fx": 0.5, "fy": 0.5})
        await v.act({"type": "key", "key": "Enter"})
        await v.act({"type": "text", "text": "someone@example.com"})
        return calls

    recorded = asyncio.run(go())
    kinds = [c[1][0] for c in recorded]
    assert "mousemove" in kinds and "click" in kinds
    assert ("key", "--clearmodifiers", "Return") in [c[1] for c in recorded]
    assert ("type", "--clearmodifiers", "--", "someone@example.com") in [c[1] for c in recorded]
    assert all(c[0] == ":321" for c in recorded)      # targeted the real Xvfb display


def test_it_is_wired_and_admin_only():
    routes = open(os.path.join(ROOT, "routes", "cloud_browser_routes.py"), encoding="utf-8").read()
    assert routes.count("_user(request)") >= 4 and "require_admin(request)" in routes
    app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "setup_cloud_browser_routes()" in app
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert 'id="tool-browser-btn"' in html and "/static/js/cloudBrowser.js" in html
    src = open(cloud_browser.__file__, encoding="utf-8").read()
    assert "--remote-debugging-address=127.0.0.1" in src      # never reachable off this machine


def test_the_address_bar():
    a = cloud_browser.address
    assert a("example.com") == "https://example.com"
    assert a("https://x.org/a") == "https://x.org/a"
    assert a("data:text/html,<b>hi</b>") == "data:text/html,<b>hi</b>"
    assert a("localhost:7000/x") == "http://localhost:7000/x"
    assert a("192.168.100.101:8114") == "http://192.168.100.101:8114"
    assert a("odysseus homer").startswith("https://duckduckgo.com/?q=odysseus%20homer")
    assert a("  ") == ""


def test_find_browser_executable(monkeypatch, tmp_path):
    """$ODYSSEUS_BROWSER_EXECUTABLE first, then PATH, then the common
    install paths; "" when none of them pan out. No subprocess involved."""
    monkeypatch.delenv("ODYSSEUS_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(cloud_browser, "_REAL_BROWSER_NAMES", ())
    monkeypatch.setattr(cloud_browser, "_REAL_BROWSER_PATHS", ())
    assert cloud_browser._find_browser_executable() == ""

    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/custom/chrome")
    assert cloud_browser._find_browser_executable() == "/custom/chrome"   # wins outright
    monkeypatch.delenv("ODYSSEUS_BROWSER_EXECUTABLE")

    fake = tmp_path / "google-chrome-stable"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(cloud_browser, "_REAL_BROWSER_NAMES", ("google-chrome-stable",))
    assert cloud_browser._find_browser_executable() == str(fake)

    monkeypatch.setattr(cloud_browser, "_REAL_BROWSER_NAMES", ())     # nothing on PATH now
    common = tmp_path / "common-chrome"
    common.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cloud_browser, "_REAL_BROWSER_PATHS", (str(common),))
    assert cloud_browser._find_browser_executable() == str(common)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _free_display() -> str:
    for _ in range(20):
        d = f":{random.randint(150, 999)}"
        if not cloud_browser._xvfb_running(d):
            return d
    raise RuntimeError("no free X display found")


def _kill_xvfb(display: str) -> None:
    out = subprocess.run(["pgrep", "-f", f"Xvfb {display} "], capture_output=True, text=True)
    for pid in out.stdout.split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        os.remove(cloud_browser._xvfb_socket(display))
    except OSError:
        pass


@pytest.mark.skipif(not cloud_browser.chromium_path(), reason="Playwright's Chromium is not installed")
def test_it_starts_streams_and_takes_input(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(cloud_browser, "PORT", port)
    monkeypatch.setattr(cloud_browser, "ENDPOINT", f"http://127.0.0.1:{port}")
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    page = ("data:text/html,<input id=q autofocus style='position:fixed;left:0;top:0;width:100%;height:100%'>")

    async def go():
        v = cloud_browser.Viewer()
        q = await v.subscribe()
        await v.act({"type": "navigate", "url": page})
        await v.act({"type": "click", "fx": 0.5, "fy": 0.5})
        await v.act({"type": "text", "text": "hello"})
        typed = await v._page.input_value("#q")
        frames = 0
        for _ in range(40):
            try:
                m = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            frames += m.get("type") == "frame"
        status = await v.status()
        v.unsubscribe(q)
        await v._stop_cast()
        await v._browser.close()
        await v._pw.stop()
        return typed, frames, status

    try:
        typed, frames, status = asyncio.run(go())
        assert typed == "hello"
        assert frames >= 1 and status["running"]
        assert os.path.isdir(tmp_path / "cloud_browser" / "profile")   # logins are kept
    finally:
        _kill(port)


def _kill(port):
    out = subprocess.run(["pgrep", "-f", f"remote-debugging-port={port}"], capture_output=True, text=True)
    for pid in out.stdout.split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


@pytest.mark.skipif(not (cloud_browser._find_browser_executable() and shutil.which("Xvfb")),
                    reason="no real Chrome or Xvfb on this machine")
def test_real_chrome_launches_headful_under_xvfb(tmp_path, monkeypatch):
    """ensure() launches the real browser on a virtual display, not
    --headless=new, and it is still a controllable, screencastable tab."""
    port = _free_port()
    display = _free_display()
    monkeypatch.setattr(cloud_browser, "PORT", port)
    monkeypatch.setattr(cloud_browser, "ENDPOINT", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ODYSSEUS_CLOUD_BROWSER_DISPLAY", display)
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))

    async def go():
        v = cloud_browser.Viewer()
        q = await v.subscribe()
        await v.act({"type": "navigate", "url": "data:text/html,<h1>hi</h1>"})
        frame = None
        for _ in range(40):
            try:
                m = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            if m.get("type") == "frame":
                frame = m
                break
        status = await v.status()
        v.unsubscribe(q)
        await v._stop_cast()
        await v._browser.close()
        await v._pw.stop()
        return frame, status

    try:
        frame, status = asyncio.run(go())
        assert frame is not None                 # the screencast works the same over this browser
        assert status["running"] and status["headful"]
        assert cloud_browser._xvfb_running(display)      # it ran on the virtual display, not --headless
    finally:
        _kill(port)
        _kill_xvfb(display)


def test_falls_back_to_headless_chromium_without_a_real_browser(tmp_path, monkeypatch):
    """Point the executable lookup at a path that does not exist (as a
    server with no real Chrome would): ensure() still comes up, on
    Playwright's own Chromium, same as before this feature."""
    if not cloud_browser.chromium_path():
        pytest.skip("Playwright's Chromium is not installed")
    port = _free_port()
    monkeypatch.setattr(cloud_browser, "PORT", port)
    monkeypatch.setattr(cloud_browser, "ENDPOINT", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/nonexistent/chrome-that-is-not-there")
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    try:
        endpoint = cloud_browser.ensure()
        assert endpoint == f"http://127.0.0.1:{port}"
        assert cloud_browser.running()
    finally:
        _kill(port)


@pytest.mark.skipif(not cloud_browser.chromium_path(), reason="Playwright's Chromium is not installed")
def test_tabs_close_and_the_last_one_leaves_a_blank_tab(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(cloud_browser, "PORT", port)
    monkeypatch.setattr(cloud_browser, "ENDPOINT", f"http://127.0.0.1:{port}")
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))

    async def go():
        v = cloud_browser.Viewer()
        q = await v.subscribe()
        await v.act({"type": "navigate", "url": "data:text/html,one"})
        await v.act({"type": "new_tab", "url": "data:text/html,two"})
        before = len(v.tabs())
        await v.act({"type": "close_tab"})
        await asyncio.sleep(0.3)
        after = v.tabs()
        for _ in range(len(after)):
            await v.act({"type": "close_tab"})
            await asyncio.sleep(0.3)
        last = v.tabs()
        v.unsubscribe(q)
        await v._stop_cast()
        await v._browser.close()
        await v._pw.stop()
        return before, after, last

    try:
        before, after, last = asyncio.run(go())
        assert len(after) == before - 1
        assert all("two" not in t["url"] for t in after)      # the active one went
        assert any(t["active"] for t in after)                # and a neighbor took over
        assert len(last) == 1 and last[0]["url"] == "about:blank" and last[0]["active"]
    finally:
        _kill(port)


def test_cookies_txt_and_json_exports_parse():
    txt = ("# Netscape HTTP Cookie File\n"
           ".google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc\n"
           "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tHSID\tdef\n"
           "garbage line\n")
    got = cloud_browser.parse_cookies(txt)
    assert [c["name"] for c in got] == ["SID", "HSID"]
    assert got[0]["expires"] == 1893456000 and got[0]["secure"] and not got[0]["httpOnly"]
    assert got[1]["httpOnly"] and "expires" not in got[1]           # 0 = a session cookie
    js = ('[{"domain": ".google.com", "name": "NID", "value": "x", "path": "/", "secure": false,'
          ' "httpOnly": true, "sameSite": "no_restriction", "expirationDate": 1893456000.5}]')
    (c,) = cloud_browser.parse_cookies(js)
    assert c["sameSite"] == "None" and c["secure"]                  # Chrome wants Secure with None
    assert c["httpOnly"] and c["expires"] == 1893456000.5
    for bad in ("", "hello", "[]", "{not json"):
        with pytest.raises(ValueError):
            cloud_browser.parse_cookies(bad)


def test_the_cookie_import_route_is_admin_only():
    import routes.cloud_browser_routes as r
    src = open(r.__file__, encoding="utf-8").read()
    block = src.split('@router.post("/cookies")', 1)[1].split("@router.", 1)[0]
    assert "_user(request)" in block


@pytest.mark.skipif(not cloud_browser.chromium_path(), reason="Playwright's Chromium is not installed")
def test_an_imported_login_lands_in_the_profile(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(cloud_browser, "PORT", port)
    monkeypatch.setattr(cloud_browser, "ENDPOINT", f"http://127.0.0.1:{port}")
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))

    async def go():
        v = cloud_browser.Viewer()
        r = await v.import_cookies("example.com\tFALSE\t/\tFALSE\t1893456000\tlogin\tyes\n")
        names = [c["name"] for c in await v._browser.contexts[0].cookies("http://example.com/")]
        await v._browser.close()
        await v._pw.stop()
        return r, names

    try:
        r, names = asyncio.run(go())
        assert r == {"ok": True, "count": 1, "sites": ["example.com"]}
        assert names == ["login"]
    finally:
        _kill(port)
