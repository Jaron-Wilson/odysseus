"""The ADS-B receiver page (routes/adsb_routes.py, static/js/adsbPanel.js).

Asked for: put the Raspberry Pi ADS-B receiver (an adsb.im feeder running
tar1090/readsb, reached over Tailscale) into Odysseus.

The page shows two things: a status summary the server builds from the
receiver's own JSON (data/stats.json and data/aircraft.json, which tar1090
serves next to its map), and the receiver's live map in a frame. The status
is fetched here rather than in the browser because tar1090 sends no CORS
header on stats.json and the app's CSP only allows same-origin fetches.

The receiver address is a setting (DATA_DIR/adsb.json, or ODYSSEUS_ADSB_URL),
not a constant: it is a private tailnet name. The page's CSP allows framing
exactly that origin and nothing else (core/middleware.py).
"""
import json
import os
import threading
import time
from typing import Dict, Optional
from urllib.parse import urlsplit

import httpx

FETCH_TIMEOUT_S = 4.0
CACHE_S = 5.0
MAX_AIRCRAFT = 60
M_PER_NM = 1852.0

_lock = threading.Lock()
_cache: Dict = {"at": 0.0, "url": None, "status": None}
_origin_cache: Dict = {"key": None, "origin": None}


# ── Settings ────────────────────────────────────────────────────────────────

def _config_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "adsb.json")


def load_config() -> Dict:
    try:
        with open(_config_path(), encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    url = cfg.get("url") or os.environ.get("ODYSSEUS_ADSB_URL") or ""
    return {"url": normalize_url(url) if url else ""}


def normalize_url(url: str) -> str:
    """The receiver's base address, always ending in "/" (tar1090's map)."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The receiver address has to be an http:// or https:// URL")
    if parts.query or parts.fragment:
        raise ValueError("The receiver address can't have a query or a fragment")
    if parts.username or parts.password:
        raise ValueError("The receiver address can't carry a username or password")
    path = parts.path or "/"
    # Accept a pasted .../data/aircraft.json and keep the map's base.
    if path.endswith(".json"):
        path = path.rsplit("/data/", 1)[0] + "/" if "/data/" in path else "/"
    if not path.endswith("/"):
        path += "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def save_config(data: Dict) -> Dict:
    url = normalize_url(data.get("url", ""))
    from core.atomic_io import atomic_write_json
    with _lock:
        os.makedirs(os.path.dirname(_config_path()), exist_ok=True)
        atomic_write_json(_config_path(), {"url": url}, indent=2)
        _cache.update(at=0.0, url=None, status=None)
    return load_config()


def frame_origin() -> Optional[str]:
    """The receiver's origin, for the CSP's frame-src, or None when unset.

    Read on every page response, so it is cached on the settings file's
    mtime (and the env var) rather than parsed each time.
    """
    path = _config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    key = (path, mtime, os.environ.get("ODYSSEUS_ADSB_URL"))
    if _origin_cache["key"] == key:
        return _origin_cache["origin"] or None
    try:
        url = load_config()["url"]
    except ValueError:
        url = ""
    origin = ""
    if url:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
    _origin_cache.update(key=key, origin=origin)
    return origin or None


# ── Status ──────────────────────────────────────────────────────────────────

def _num(v) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _period(stats: Dict, key: str) -> Dict:
    p = stats.get(key) or {}
    local = p.get("local") or {}
    start, end = _num(p.get("start")), _num(p.get("end"))
    accepted = local.get("accepted") or []
    return {
        "messages": _num(p.get("messages")),
        "positions": _num(p.get("position_count_total")),
        "seconds": (end - start) if start is not None and end is not None else None,
        "decoded": sum(a for a in accepted if _num(a) is not None) if isinstance(accepted, list) else None,
        "peak_dbfs": _num(local.get("peak_signal")),
        "signal_dbfs": _num(local.get("signal")),
        "noise_dbfs": _num(local.get("noise")),
        "max_range_nm": round(p["max_distance"] / M_PER_NM, 1) if _num(p.get("max_distance")) else None,
        "start": start,
    }


def summarize(stats: Dict, aircraft_json: Dict, now: Optional[float] = None) -> Dict:
    """The receiver's two files -> what the page shows. Pure, for the tests.

    Only aircraft with a position heard in the last minute count as "with a
    position": readsb keeps a contact in aircraft.json for a while after its
    last report.
    """
    now = now if now is not None else time.time()
    rows = aircraft_json.get("aircraft") or []
    planes = []
    for a in rows:
        if not isinstance(a, dict) or not a.get("hex"):
            continue
        seen_pos = _num(a.get("seen_pos"))
        alt = a.get("alt_baro")
        planes.append({
            "hex": a["hex"],
            "flight": (a.get("flight") or "").strip() or None,
            "registration": a.get("r") or None,
            "type": a.get("t") or None,
            "alt": "ground" if alt == "ground" else _num(alt),
            "gs": _num(a.get("gs")),
            "track": _num(a.get("track")),
            "rssi": _num(a.get("rssi")),
            "seen": _num(a.get("seen")),
            "seen_pos": seen_pos,
            "has_position": "lat" in a and seen_pos is not None and seen_pos <= 60,
            "messages": _num(a.get("messages")),
        })
    planes.sort(key=lambda p: (not p["has_position"], p["seen"] if p["seen"] is not None else 1e9))
    last1 = _period(stats, "last1min")
    return {
        "now": {
            "aircraft": len(planes),
            "with_position": sum(1 for p in planes if p["has_position"]),
            "messages_per_s": round(last1["messages"] / 60, 1) if last1["messages"] is not None else None,
        },
        "last1min": last1,
        "last15min": _period(stats, "last15min"),
        "total": _period(stats, "total"),
        "gain_db": _num(stats.get("gain_db")),
        "aircraft": planes[:MAX_AIRCRAFT],
        "receiver_time": _num(aircraft_json.get("now")),
        "fetched_at": now,
    }


async def _get_json(client: httpx.AsyncClient, url: str) -> Dict:
    r = await client.get(url, headers={"accept": "application/json"})
    r.raise_for_status()
    return r.json()


async def fetch_status(transport: Optional[httpx.AsyncBaseTransport] = None) -> Dict:
    """The receiver's status, cached for a few seconds so an open page and a
    second tab don't each poll the Pi. Never raises: an unreachable receiver
    is a status the page shows, not an error."""
    url = load_config()["url"]
    if not url:
        return {"configured": False}
    if _cache["url"] == url and _cache["status"] is not None and time.time() - _cache["at"] < CACHE_S:
        return _cache["status"]

    out: Dict = {"configured": True, "url": url}
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, trust_env=False, transport=transport,
                                     follow_redirects=False) as c:
            stats = await _get_json(c, url + "data/stats.json")
            planes = await _get_json(c, url + "data/aircraft.json")
        out.update(ok=True, **summarize(stats, planes))
    except httpx.TimeoutException:
        out.update(ok=False, error=f"The receiver did not answer within {FETCH_TIMEOUT_S:g} s")
    except httpx.HTTPStatusError as e:
        out.update(ok=False, error=f"The receiver answered HTTP {e.response.status_code}")
    except (httpx.HTTPError, ValueError) as e:
        out.update(ok=False, error=f"Could not reach the receiver ({type(e).__name__})")
    _cache.update(at=time.time(), url=url, status=out)
    return out
