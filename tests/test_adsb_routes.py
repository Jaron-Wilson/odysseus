"""The ADS-B receiver page (routes/adsb_routes.py, src/adsb.py)."""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware
from routes import adsb_routes
from src import adsb

STATS = {
    "gain_db": 43.9,
    "last1min": {"start": 1000.0, "end": 1060.0, "messages": 1500, "position_count_total": 120,
                 "max_distance": 92600.0,
                 "local": {"accepted": [1400, 90], "signal": -18.2, "noise": -33.1, "peak_signal": -2.9}},
    "last15min": {"start": 160.0, "end": 1060.0, "messages": 21000, "position_count_total": 1800,
                  "max_distance": 94877.0, "local": {}},
    "total": {"start": 0.0, "end": 367200.0, "messages": 2758242, "position_count_total": 308185,
              "max_distance": 94877.0, "local": {}},
}
AIRCRAFT = {"now": 1060.0, "aircraft": [
    {"hex": "a1b2c3", "seen": 30.0},                                          # heard, no position
    {"hex": "abc123", "flight": "PDT5995 ", "t": "E145", "r": "N123", "alt_baro": 21000,
     "gs": 380.2, "lat": 1, "lon": 2, "seen_pos": 0.4, "seen": 0.1, "rssi": -9.5, "messages": 410},
    {"hex": "def456", "alt_baro": "ground", "lat": 1, "lon": 2, "seen_pos": 200, "seen": 2.0},  # stale position
    {"flight": "NOHEX"},                                                      # dropped
]}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ODYSSEUS_ADSB_URL", raising=False)
    adsb._cache.update(at=0.0, url=None, status=None)
    return tmp_path


def _receiver(calls=None, status=200):
    def handler(request):
        if calls is not None:
            calls.append(str(request.url))
        body = {"/data/stats.json": STATS, "/data/aircraft.json": AIRCRAFT}.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


# ── Settings ────────────────────────────────────────────────────────────

def test_the_address_is_normalized_to_the_maps_base():
    assert adsb.normalize_url(" https://pi.ts.net ") == "https://pi.ts.net/"
    assert adsb.normalize_url("http://100.1.2.3:8080/tar1090") == "http://100.1.2.3:8080/tar1090/"
    assert adsb.normalize_url("https://pi.ts.net/data/aircraft.json") == "https://pi.ts.net/"
    assert adsb.normalize_url("") == ""


@pytest.mark.parametrize("bad", ["ftp://pi/", "javascript:alert(1)", "pi.ts.net", "https://pi/?x=1",
                                 "https://pi/#map", "https://u:p@pi/", "https:///"])
def test_odd_addresses_are_refused(bad):
    with pytest.raises(ValueError):
        adsb.normalize_url(bad)


def test_the_setting_is_saved_and_falls_back_to_the_env(data_dir, monkeypatch):
    assert adsb.load_config() == {"url": ""}
    monkeypatch.setenv("ODYSSEUS_ADSB_URL", "https://env.ts.net")
    assert adsb.load_config() == {"url": "https://env.ts.net/"}
    assert adsb.save_config({"url": "https://pi.ts.net"}) == {"url": "https://pi.ts.net/"}
    assert json.loads((data_dir / "adsb.json").read_text()) == {"url": "https://pi.ts.net/"}


# ── Status ──────────────────────────────────────────────────────────────

def test_the_summary_counts_what_the_receiver_hears():
    s = adsb.summarize(STATS, AIRCRAFT, now=5.0)
    assert s["now"] == {"aircraft": 3, "with_position": 1, "messages_per_s": 25.0}
    assert s["total"]["messages"] == 2758242 and s["total"]["positions"] == 308185
    assert s["total"]["max_range_nm"] == 51.2 and s["last1min"]["max_range_nm"] == 50.0
    assert s["last1min"]["decoded"] == 1490 and s["last1min"]["peak_dbfs"] == -2.9
    assert s["gain_db"] == 43.9
    first = s["aircraft"][0]
    assert first["flight"] == "PDT5995" and first["type"] == "E145" and first["has_position"]
    assert [p["hex"] for p in s["aircraft"]] == ["abc123", "def456", "a1b2c3"]
    assert s["aircraft"][1]["alt"] == "ground" and not s["aircraft"][1]["has_position"]


async def test_status_fetches_both_files_and_caches(data_dir):
    adsb.save_config({"url": "https://pi.ts.net"})
    calls = []
    s = await adsb.fetch_status(transport=_receiver(calls))
    assert s["ok"] and s["configured"] and s["url"] == "https://pi.ts.net/"
    assert calls == ["https://pi.ts.net/data/stats.json", "https://pi.ts.net/data/aircraft.json"]
    await adsb.fetch_status(transport=_receiver(calls))
    assert len(calls) == 2  # served from the cache


async def test_an_unreachable_receiver_is_a_status_not_an_error(data_dir):
    assert await adsb.fetch_status() == {"configured": False}
    adsb.save_config({"url": "https://pi.ts.net"})
    s = await adsb.fetch_status(transport=_receiver(status=502))
    assert s == {"configured": True, "url": "https://pi.ts.net/", "ok": False,
                 "error": "The receiver answered HTTP 502"}

    def down(request):
        raise httpx.ConnectError("no route", request=request)
    adsb._cache.update(at=0.0, url=None, status=None)
    s = await adsb.fetch_status(transport=httpx.MockTransport(down))
    assert not s["ok"] and s["error"] == "Could not reach the receiver (ConnectError)"


# ── The routes ──────────────────────────────────────────────────────────

class _Auth:
    is_configured = True

    def __init__(self, admins):
        self.admins = admins

    def is_admin(self, user):
        return user in self.admins


def _app(user=None, admins=()):
    app = FastAPI()
    app.state.auth_manager = _Auth(set(admins))

    @app.middleware("http")
    async def _who(request, call_next):
        request.state.current_user = user
        return await call_next(request)

    app.include_router(adsb_routes.setup_adsb_routes())
    return app


def test_only_an_admin_gets_anything(data_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    for user in (None, "guest"):
        c = TestClient(_app(user=user, admins={"jaron"}))
        assert c.get("/api/adsb/status").status_code == 403
        assert c.get("/api/adsb/config").status_code == 403
        assert c.put("/api/adsb/config", json={"url": "https://evil/"}).status_code == 403
    assert not (data_dir / "adsb.json").exists()
    c = TestClient(_app(user="jaron", admins={"jaron"}))
    assert c.get("/api/adsb/status").json() == {"configured": False}


def test_the_config_route_validates(data_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    c = TestClient(_app(user="jaron", admins={"jaron"}))
    assert c.put("/api/adsb/config", json={"url": "ftp://pi/"}).status_code == 400
    assert c.put("/api/adsb/config", json={"url": 5}).status_code == 400
    assert c.put("/api/adsb/config", content=b"nope").status_code == 400
    assert c.put("/api/adsb/config", json={"url": "https://pi.ts.net"}).json() == {"url": "https://pi.ts.net/"}
    assert c.get("/api/adsb/config").json() == {"url": "https://pi.ts.net/"}
    assert c.put("/api/adsb/config", json={"url": ""}).json() == {"url": ""}


# ── The page's CSP ──────────────────────────────────────────────────────

def _csp():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    return TestClient(app).get("/").headers["content-security-policy"]


def _frame_src():
    return next(d for d in _csp().split("; ") if d.startswith("frame-src"))


def test_the_csp_frames_only_the_set_receiver(data_dir):
    assert _frame_src() == "frame-src 'self' https://www.youtube-nocookie.com"
    adsb.save_config({"url": "https://pi.ts.net:8443/tar1090/"})
    assert _frame_src() == "frame-src 'self' https://www.youtube-nocookie.com https://pi.ts.net:8443"
    adsb.save_config({"url": ""})
    assert _frame_src() == "frame-src 'self' https://www.youtube-nocookie.com"


def test_a_hand_edited_setting_cannot_inject_into_the_csp(data_dir):
    (data_dir / "adsb.json").write_text(json.dumps({"url": "https://pi.ts.net; script-src *"}))
    assert _frame_src() == "frame-src 'self' https://www.youtube-nocookie.com"
    assert "script-src *" not in _csp()
