"""The music bar: controls for YouTube Music on the PC, with album art and
lyrics looked up by name (the Windows media session has neither)."""
import os

import pytest

from routes import media_routes as mr

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("title,artist,want", [
    ("Oceans (Where Feet May Fail) (Official Lyric Video)", "Hillsong UNITED - Topic",
     ("Oceans (Where Feet May Fail)", "Hillsong UNITED")),
    ("Elevation Worship - Graves Into Gardens [Official Video]", "",
     ("Graves Into Gardens", "Elevation Worship")),
    ("Way Maker", "Leeland, Sinach", ("Way Maker", "Leeland")),
])
def test_youtube_titles_are_cleaned_for_lookup(title, artist, want):
    assert mr._clean(title, artist) == want


class _Resp:
    def __init__(self, status=200, js=None, content=b"", ctype="application/json"):
        self.status_code, self._js, self.content = status, js, content
        self.headers = {"content-type": ctype}

    def json(self):
        return self._js


def test_art_is_looked_up_once_and_cached(tmp_path, monkeypatch):
    import httpx
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        if "itunes" in url:
            return _Resp(js={"results": [{"artworkUrl100": "https://x/a/100x100bb.jpg"}]})
        assert "600x600bb" in url
        return _Resp(content=b"\xff\xd8\xffJPEG", ctype="image/jpeg")

    monkeypatch.setattr(httpx, "get", fake_get)
    assert mr._art_bytes("Oceans", "Hillsong UNITED").startswith(b"\xff\xd8")
    assert mr._art_bytes("Oceans", "Hillsong UNITED").startswith(b"\xff\xd8")
    assert len(seen) == 2                               # the second call came from the cache


def test_lyrics_from_lrclib(monkeypatch):
    import httpx
    mr._LYRICS_CACHE.clear()
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(js={
        "trackName": "Oceans", "artistName": "Hillsong UNITED",
        "plainLyrics": "You call me out upon the waters", "syncedLyrics": "[00:10.00] You call me"}))
    d = mr._lyrics("Oceans (Official Video)", "Hillsong UNITED - Topic")
    assert d["ok"] and d["plain"].startswith("You call me") and d["synced"]


def test_wiring():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    routes = read("routes", "media_routes.py")
    assert '@router.get("/art")' in routes and '@router.get("/lyrics")' in routes
    html = read("static", "index.html")
    assert 'id="music-btn"' in html and 'id="music-bar"' in html
    js = read("static", "js", "musicBar.js")
    assert "/api/media/control" in js and "/api/media/lyrics" in js and 'onerror="' not in js
    assert "import './musicBar.js';" in read("static", "js", "chat.js")
