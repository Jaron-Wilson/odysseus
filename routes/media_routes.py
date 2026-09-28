"""Media state and controls for whichever machine you are browsing from.

One request gets the whole picture — what is playing, how loud, which
output — because three round trips to render one panel is three chances to
show a half-populated widget.

Nothing here is behind the screen-control approval gate. These read and set
audio state; they do not see the screen or move the pointer, and putting a
permission click in front of a volume slider would make the panel useless.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)


def setup_media_routes(mcp_manager) -> APIRouter:
    router = APIRouter(prefix="/api/media", tags=["media"])

    def _client_ip(request: Request) -> str:
        # Tailscale Serve forwards the real client address; without this the
        # answer would always be 127.0.0.1 and every device would look like
        # the server itself.
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""

    def _require_user(request: Request) -> str:
        import os
        if os.getenv("AUTH_ENABLED", "true").lower() == "false":
            return ""
        user = getattr(request.state, "current_user", None)
        if not user:
            raise HTTPException(401, "Sign in first.")
        return user

    async def _call(server_id: str, tool: str, args: Optional[Dict] = None) -> Dict[str, Any]:
        if not mcp_manager:
            return {"ok": False, "error": "MCP manager unavailable"}
        return await mcp_manager.call_tool(f"mcp__{server_id}__{tool}", args or {})

    def _payload(res: Dict[str, Any]) -> Dict[str, Any]:
        """Tool results arrive as {stdout: "<json>"}; unwrap to the object."""
        import json
        if not isinstance(res, dict):
            return {}
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        raw = res.get("stdout")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                return json.loads(raw)
            except Exception:
                pass
        return res

    @router.get("/whoami")
    async def whoami(request: Request):
        """Which machine this browser is sitting on, if it is one we drive."""
        _require_user(request)
        from src import device_routing
        ip = _client_ip(request)
        dev = device_routing.for_client(mcp_manager, ip)
        return {
            "client_ip": ip,
            "device": dev,
            # Listed so the UI can offer a picker when the browser is
            # somewhere we cannot control, instead of showing nothing.
            "available": device_routing.all_devices(mcp_manager),
        }

    @router.get("/state")
    async def state(request: Request, server_id: str = ""):
        """Now playing, volume and outputs, in one round trip."""
        _require_user(request)
        from src import device_routing
        ip = _client_ip(request)
        dev = device_routing.for_client(mcp_manager, ip)
        sid = (server_id or "").strip() or (dev or {}).get("server_id", "")
        if not sid:
            return {
                "ok": False,
                "client_ip": ip,
                "reason": ("This browser is not on a machine Odysseus can control. "
                           "Open the site from the Windows desktop or the laptop, or "
                           "pick a machine explicitly."),
                "available": device_routing.all_devices(mcp_manager),
            }

        tools = set((dev or {}).get("tools") or [])
        wanted = [t for t in ("now_playing", "get_volume", "list_audio_devices")
                  if not tools or t in tools]
        # Fetched together: rendering a panel from three sequential calls is
        # three chances to show it half-filled.
        results = await asyncio.gather(
            *[_call(sid, t) for t in wanted], return_exceptions=True)

        out: Dict[str, Any] = {"ok": True, "client_ip": ip,
                               "device": dev or {"server_id": sid}}
        for name, res in zip(wanted, results):
            if isinstance(res, Exception):
                out[name] = {"ok": False, "error": str(res)}
            else:
                out[name] = _payload(res)
        return out

    @router.post("/control")
    async def control(request: Request):
        """play_pause, next, previous, stop, volume, mute, output."""
        _require_user(request)
        from src import device_routing
        body = await request.json() if request.headers.get("content-type", "").startswith(
            "application/json") else {}
        action = str(body.get("action") or "").strip().lower()
        ip = _client_ip(request)
        dev = device_routing.for_client(mcp_manager, ip)
        sid = str(body.get("server_id") or "").strip() or (dev or {}).get("server_id", "")
        if not sid:
            raise HTTPException(
                400, "No controllable machine for this browser. Pass server_id to choose one.")

        if action in ("play_pause", "play", "pause", "next", "previous", "stop",
                      "volume_up", "volume_down"):
            # volume_up/down are the Windows volume keys: they always do
            # something, and Windows shows its own volume display.
            res = await _call(sid, "media_control", {"action": action})
        elif action == "volume":
            try:
                pct = int(body.get("value"))
            except (TypeError, ValueError):
                raise HTTPException(400, "volume needs an integer value 0-100")
            res = await _call(sid, "set_volume", {"percent": pct})
        elif action == "mute":
            res = await _call(sid, "set_mute", {"muted": bool(body.get("value", True))})
        elif action == "output":
            name = str(body.get("value") or "").strip()
            if not name:
                raise HTTPException(400, "output needs the device name")
            res = await _call(sid, "set_audio_device", {"name": name})
        else:
            raise HTTPException(
                400, "action must be play_pause, play, pause, next, previous, stop, "
                     "volume_up, volume_down, volume, mute or output")
        return {"requested": action, "result": _payload(res)}

    @router.post("/overlay")
    async def overlay(request: Request):
        """Open the frameless desktop music overlay (tools/music_overlay) on
        the machine this browser controls, through its MusicOverlay task."""
        _require_user(request)
        import getpass
        from src import device_routing, machines
        body = await request.json() if request.headers.get("content-type", "").startswith(
            "application/json") else {}
        ip = _client_ip(request)
        dev = device_routing.for_client(mcp_manager, ip)
        sid = str(body.get("server_id") or "").strip() or (dev or {}).get("server_id", "")
        target = next((d for d in device_routing.all_devices(mcp_manager) if d.get("server_id") == sid), None)
        if not target:
            raise HTTPException(400, "No machine to open the overlay on")
        all_peers = await asyncio.to_thread(machines.peers)
        peer = machines.find_peer(all_peers, target.get("host", ""))
        if not peer:
            raise HTTPException(404, "That machine is not on the tailnet")
        r = await machines.run_user_task(peer, getpass.getuser(), "MusicOverlay")
        if not r.get("ok"):
            raise HTTPException(502, f"Could not open the overlay: {r.get('error')}")
        return {"ok": True, "machine": target.get("name") or sid}

    # ------------------------------------------------------------------ #
    # Album art and lyrics for the music bar (static/js/musicBar.js). The
    # Windows media session gives title and artist but no art or lyrics, so
    # they are looked up by name: art from the iTunes Search API, lyrics
    # from lrclib.net. Both are free and need no key. Art is fetched here
    # and served from this origin, so the page needs no new image hosts.
    # ------------------------------------------------------------------ #
    @router.get("/art")
    async def art(request: Request, title: str = "", artist: str = ""):
        _require_user(request)
        from fastapi.responses import Response
        data = await asyncio.to_thread(_art_bytes, title, artist)
        if not data:
            raise HTTPException(404, "No art found")
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})

    @router.get("/lyrics")
    async def lyrics(request: Request, title: str = "", artist: str = "", album: str = ""):
        _require_user(request)
        return await asyncio.to_thread(_lyrics, title, artist, album)

    return router


# ── art and lyrics lookup ────────────────────────────────────────────────
import hashlib as _hashlib
import os as _os
import re as _re

_NOISE_RE = _re.compile(
    r"\s*[\(\[](?:official|lyric|lyrics|audio|video|visualizer|hd|4k|remaster(?:ed)?|"
    r"live|explicit|clean|music video|mv)[^\)\]]*[\)\]]", _re.I)
_LYRICS_CACHE: Dict[str, Dict[str, Any]] = {}


def _clean(title: str, artist: str):
    t = _NOISE_RE.sub("", title or "").strip()
    a = _re.sub(r"\s*-\s*Topic$", "", (artist or "").strip(), flags=_re.I)
    a = a.split(",")[0].split("&")[0].strip()
    # "Artist - Song" titles from video uploads, when the artist is the channel.
    if " - " in t and (not a or t.lower().startswith(a.lower() + " - ")):
        head, _, rest = t.partition(" - ")
        a, t = (a or head.strip()), rest.strip()
    return t, a


def _art_bytes(title: str, artist: str) -> Optional[bytes]:
    import httpx
    t, a = _clean(title, artist)
    if not t:
        return None
    from src.constants import DATA_DIR
    cache_dir = _os.path.join(DATA_DIR, "cache", "music_art")
    _os.makedirs(cache_dir, exist_ok=True)
    key = _hashlib.sha1(f"{t}|{a}".lower().encode()).hexdigest()[:20]
    path = _os.path.join(cache_dir, key + ".jpg")
    if _os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        return data or None
    try:
        r = httpx.get("https://itunes.apple.com/search",
                      params={"term": f"{t} {a}".strip(), "entity": "song", "limit": 1},
                      timeout=8)
        results = r.json().get("results") or []
        url = (results[0].get("artworkUrl100") or "") if results else ""
        data = b""
        if url:
            img = httpx.get(url.replace("100x100bb", "600x600bb"), timeout=8)
            if img.status_code == 200 and img.headers.get("content-type", "").startswith("image/"):
                data = img.content
    except Exception as e:
        logger.debug("art lookup failed for %r: %s", t, e)
        return None
    with open(path, "wb") as f:
        f.write(data)                        # empty file = looked up, none found
    return data or None


def _lyrics(title: str, artist: str, album: str = "") -> Dict[str, Any]:
    import httpx
    t, a = _clean(title, artist)
    if not t:
        return {"ok": False, "error": "no song"}
    key = f"{t}|{a}".lower()
    if key in _LYRICS_CACHE:
        return _LYRICS_CACHE[key]
    out: Dict[str, Any] = {"ok": False, "title": t, "artist": a}
    try:
        r = httpx.get("https://lrclib.net/api/get",
                      params={"track_name": t, "artist_name": a}, timeout=8,
                      headers={"User-Agent": "Odysseus (self-hosted)"})
        if r.status_code != 200:
            r = httpx.get("https://lrclib.net/api/search",
                          params={"track_name": t, "artist_name": a}, timeout=8,
                          headers={"User-Agent": "Odysseus (self-hosted)"})
            hits = r.json() if r.status_code == 200 else []
            d = hits[0] if isinstance(hits, list) and hits else {}
        else:
            d = r.json()
        if d:
            out = {"ok": bool(d.get("plainLyrics") or d.get("syncedLyrics")),
                   "title": d.get("trackName") or t, "artist": d.get("artistName") or a,
                   "plain": d.get("plainLyrics") or "", "synced": d.get("syncedLyrics") or "",
                   "instrumental": bool(d.get("instrumental"))}
    except Exception as e:
        out["error"] = str(e)[:200]
    _LYRICS_CACHE[key] = out
    return out
