"""Play a video from the machine it is on, without copying it.

Asked for on 2026-09-28: "I don't want to copy anything, I want to be able to
host a video from any device: if it's on my device host it on mine, if not
then host it from where it's from".
"""
import importlib.util
import os
import socket
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "desktop_mcp_media", os.path.join(HERE, "tools", "mcp", "desktop_mcp_server.py"))
dms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dms)


@pytest.fixture(autouse=True)
def _shares_in_tmp(tmp_path, monkeypatch):
    # Shared links are saved next to the server file; keep the test's own.
    monkeypatch.setattr(dms, "_SHARED_FILE", str(tmp_path / "shared_media.json"))


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "Great Commission - teaser_30s.mp4"
    p.write_bytes(bytes(range(256)) * 4000)              # 1,024,000 bytes
    return p


@pytest.fixture
def device(video):
    """The machine's media route, served for real on a local port."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route
    app = Starlette(routes=[Route("/media/{token}", dms._media_route, methods=["GET", "HEAD"])])
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def test_share_and_find(video, tmp_path):
    out = dms.share_media(str(video))
    assert out["ok"] and out["link"].startswith("/api/device-media/")
    assert out["link"].endswith("/Great%20Commission%20-%20teaser_30s.mp4")
    assert dms.share_media(str(video))["link"] == out["link"]           # same file, same link
    (tmp_path / "notes.txt").write_text("x")
    assert not dms.share_media(str(tmp_path / "notes.txt"))["ok"]
    assert not dms.share_media(str(tmp_path / "missing.mp4"))["ok"]
    found = dms.find_media("teaser", folder=str(tmp_path))
    assert [f["path"] for f in found["files"]] == [str(video)]


def test_streamed_through_odysseus_with_seeking(video, device, monkeypatch):
    import routes.device_media_routes as dmr
    token = dms.share_media(str(video))["link"].split("/")[3]
    monkeypatch.setattr(dmr, "_bases", lambda mgr: ["http://127.0.0.1:9", device])   # first one is dead
    import src.auth_helpers as ah
    monkeypatch.setattr(ah, "require_authenticated_request", lambda r: None)
    dmr._WHERE.clear()
    app = FastAPI()
    app.include_router(dmr.setup_device_media_routes(None))
    c = TestClient(app)
    whole = c.get(f"/api/device-media/{token}/teaser.mp4")
    assert whole.status_code == 200 and whole.content == video.read_bytes()
    assert whole.headers["content-type"] == "video/mp4" and whole.headers["accept-ranges"] == "bytes"
    part = c.get(f"/api/device-media/{token}/teaser.mp4", headers={"Range": "bytes=1000-1999"})
    assert part.status_code == 206 and part.content == video.read_bytes()[1000:2000]
    assert part.headers["content-range"] == "bytes 1000-1999/1024000"
    tail = c.get(f"/api/device-media/{token}/teaser.mp4", headers={"Range": "bytes=-10"})
    assert tail.content == video.read_bytes()[-10:]
    assert dmr._WHERE[token] == device                                   # found once, remembered
    assert c.get("/api/device-media/nottoken123/x.mp4").status_code == 404
    assert c.get("/api/device-media/bad$token/x.mp4").status_code in (400, 404)


def test_wiring():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    loop = read("src", "agent_loop.py")
    assert "NEVER copy it: call that machine's share_media" in loop
    assert "give DIRECT links to the audio file itself" in loop
    assert "setup_device_media_routes(mcp_manager)" in read("app.py")
    assert 'custom_route("/media/{token}"' in read("tools", "mcp", "linux_desktop_mcp_server.py")
