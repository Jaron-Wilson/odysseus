"""Music and video play inside the chat: YouTube links get a player, and so do
links to audio or video files, including files copied into Odysseus'
chat_media folder and served from /api/chat-media/<name>."""
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _client(tmp_path, monkeypatch):
    from routes import chat_media_routes as cmr
    monkeypatch.setattr(cmr, "MEDIA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(cmr.setup_chat_media_routes())
    return TestClient(app)


def test_files_are_served_with_seeking(tmp_path, monkeypatch):
    (tmp_path / "tone.wav").write_bytes(b"RIFF" + b"x" * 996)
    c = _client(tmp_path, monkeypatch)
    r = c.get("/api/chat-media/tone.wav", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.headers["content-range"] == "bytes 0-99/1000"
    assert r.headers["content-type"].startswith("audio/")
    assert [f["name"] for f in c.get("/api/chat-media").json()["files"]] == ["tone.wav"]
    assert c.get("/api/chat-media/missing.mp4").status_code == 404
    assert c.get("/api/chat-media/.hidden").status_code == 400


def test_the_page_allows_youtube_and_media():
    mw = open(os.path.join(HERE, "core", "middleware.py"), encoding="utf-8").read()
    assert "frame-src 'self' https://www.youtube-nocookie.com" in mw
    assert "media-src 'self' blob: https:" in mw
    assert "https://i.ytimg.com" in mw


def test_links_become_players():
    md = open(os.path.join(HERE, "static", "js", "markdown.js"), encoding="utf-8").read()
    assert "export function enhanceMedia(" in md and "youtube-nocookie.com/embed/" in md
    assert "enhanceMedia,\n" in md                     # on the default export
    chat = open(os.path.join(HERE, "static", "js", "chat.js"), encoding="utf-8").read()
    render = open(os.path.join(HERE, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert chat.count("markdownModule.enhanceMedia(") >= 2 and render.count("markdownModule.enhanceMedia(") == 2
    rules = open(os.path.join(HERE, "src", "agent_loop.py"), encoding="utf-8").read()
    assert "Music and video play inside this chat" in rules and "/api/chat-media/" in rules
