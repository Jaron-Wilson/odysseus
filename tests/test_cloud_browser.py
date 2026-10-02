"""The cloud browser (src/cloud_browser.py): the agent's browser, watched live.

Asked for on 2026-09-29: "can we add a cloud browser like chatgpt does and
manus ai?"
"""
import asyncio
import os
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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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
