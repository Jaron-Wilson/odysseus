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


# The newest "Listen here" (or handoff): a browser playing an older one stops,
# so the song plays on one device at a time. Reported: "I can press open here
# on the website, then I have it running on both my devices".
_LISTEN_NOW: Dict[str, str] = {"id": ""}


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

    # Phones answer through their Modes listener (server_id "device:<name>"),
    # machines through their desktop MCP server.
    _PHONE_TOOLS = {"now_playing", "media_control", "get_volume", "set_volume", "set_mute", "open_url"}

    async def _call(server_id: str, tool: str, args: Optional[Dict] = None) -> Dict[str, Any]:
        if server_id.startswith("device:"):
            from src import devices as _devices
            dev = _devices.get(server_id[len("device:"):])
            if not dev:
                return {"ok": False, "error": "no such phone"}
            if tool not in _PHONE_TOOLS:
                return {"ok": False, "error": f"{tool} is not available on a phone"}
            if tool == "media_control" and (args or {}).get("action") in ("volume_up", "volume_down"):
                vol = (await _devices.send_command(dev, "get_volume", {})).get("result") or {}
                step = 10 if args["action"] == "volume_up" else -10
                tool, args = "set_volume", {"percent": max(0, min(100, int(vol.get("volume") or 0) + step))}
            r = await _devices.send_command(dev, tool, args or {})
            if not r.get("ok"):
                return {"ok": False, "error": r.get("error") or "the phone did not answer"}
            return r.get("result") or {}
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

        # The tools of the machine being asked, not of the one browsing (a PC
        # browser can control the phone, which has no audio-device tools).
        target = next((d for d in device_routing.all_devices(mcp_manager) if d.get("server_id") == sid), None)
        tools = set((target or dev or {}).get("tools") or [])
        wanted = [t for t in ("now_playing", "get_volume", "get_app_volume", "list_audio_devices")
                  if not tools or t in tools]
        # Fetched together: rendering a panel from three sequential calls is
        # three chances to show it half-filled.
        results = await asyncio.gather(
            *[_call(sid, t) for t in wanted], return_exceptions=True)

        out: Dict[str, Any] = {"ok": True, "client_ip": ip,
                               # The device being controlled: a pick wins over the
                               # machine this browser is on. It was the browser's
                               # machine, so the bar said "windows-desktop" while
                               # it drove the phone.
                               "device": target or dev or {"server_id": sid},
                               "here": dev,
                               "listen_id": _LISTEN_NOW["id"],
                               # For the machine picker and "Play on" (handoff).
                               "available": [dict({k: d.get(k) for k in ("server_id", "name", "kind")},
                                                  # "Hear it on" only where it can work
                                                  can_receive="bluetooth_audio_receive" in (d.get("tools") or []),
                                                  can_pair="bluetooth_pair" in (d.get("tools") or []))
                                             for d in device_routing.all_devices(mcp_manager)]}
        for name, res in zip(wanted, results):
            if isinstance(res, Exception):
                out[name] = {"ok": False, "error": str(res)}
            else:
                out[name] = _payload(res)
        # Nothing playing here: say where it is playing. Reported: "the music
        # didn't work from my phone, I was playing yet my PC saw nothing".
        if not (out.get("now_playing") or {}).get("playing"):
            out["elsewhere"] = await _playing_elsewhere(
                [d for d in device_routing.all_devices(mcp_manager) if d.get("server_id") != sid])
        return out

    _elsewhere_cache: Dict[str, tuple] = {}

    async def _playing_elsewhere(devs) -> list:
        """What the other devices are playing, for a "Playing on" row. Each
        is asked at most every 8 s (the bar polls far more often) and given
        3 s: a phone that is asleep must not hold up this machine's bar."""
        import time

        async def one(d):
            sid = d.get("server_id") or ""
            if "now_playing" not in set(d.get("tools") or ["now_playing"]):
                return None
            hit = _elsewhere_cache.get(sid)
            if hit and time.time() - hit[0] < 8:
                np = hit[1]
            else:
                try:
                    np = _payload(await asyncio.wait_for(_call(sid, "now_playing"), 3))
                except Exception:
                    np = {}
                _elsewhere_cache[sid] = (time.time(), np)
            if not np.get("playing") or not np.get("title"):
                return None
            return {"server_id": sid, "name": d.get("name") or sid, "kind": d.get("kind"),
                    "title": np.get("title"), "artist": np.get("artist") or ""}

        found = await asyncio.gather(*[one(d) for d in devs])
        return [f for f in found if f]

    @router.post("/control")
    async def control(request: Request):
        """play_pause, next, previous, stop, volume (system), app_volume, mute, output."""
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
        elif action == "app_volume":
            # The playing app's level in the Windows mixer; "volume" is the
            # whole computer.
            try:
                pct = int(body.get("value"))
            except (TypeError, ValueError):
                raise HTTPException(400, "app_volume needs an integer value 0-100")
            args = {"percent": pct}
            if body.get("app"):
                args["app"] = str(body["app"])
            res = await _call(sid, "set_app_volume", args)
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
                     "volume_up, volume_down, volume, app_volume, mute or output")
        return {"requested": action, "result": _payload(res)}

    @router.post("/handoff")
    async def handoff(request: Request):
        """Play what is playing on one device on another instead: pause it
        there, find the song on YouTube Music, open it on the other. Asked
        for: "start it on my PC or my phone and listen on a different device:
        my headphones are connected to the PC, not the phone"."""
        _require_user(request)
        from src import device_routing
        body = await request.json() if request.headers.get("content-type", "").startswith(
            "application/json") else {}
        src_id = str(body.get("from") or "").strip()
        dst_id = str(body.get("to") or "").strip()
        known = {d.get("server_id"): d for d in device_routing.all_devices(mcp_manager)}
        if src_id not in known or dst_id not in known or src_id == dst_id:
            raise HTTPException(400, "Pick two different connected devices")
        np = _payload(await _call(src_id, "now_playing"))
        title, artist = (np.get("title") or "").strip(), (np.get("artist") or "").strip()
        if not title:
            raise HTTPException(409, f"Nothing is playing on {known[src_id].get('name')}")
        vid = await asyncio.to_thread(_youtube_id, title, artist)
        if not vid:
            raise HTTPException(404, f"Could not find {title!r} on YouTube Music")
        url = f"https://music.youtube.com/watch?v={vid}"
        # Pause first, so the two never play at once.
        if np.get("playing"):
            await _call(src_id, "media_control", {"action": "pause" if src_id.startswith("device:") else "play_pause"})
        opened = _payload(await _call(dst_id, "open_url" if dst_id.startswith("device:") else "open_media_url",
                                      {"url": url}))
        if opened.get("ok") is False:
            raise HTTPException(502, f"Could not open it on {known[dst_id].get('name')}: {opened.get('error')}")
        import uuid
        _LISTEN_NOW["id"] = "handoff-" + uuid.uuid4().hex[:8]        # a browser player elsewhere stops
        return {"ok": True, "title": title, "artist": artist, "url": url,
                "from": known[src_id].get("name"), "to": known[dst_id].get("name")}

    @router.post("/pair")
    async def pair(request: Request):
        """Pair the phone with a computer over Bluetooth from here, for "Hear
        it on". Body {from: "device:<phone>", to: <computer>, manual: bool}:
        manual opens the computer's Bluetooth settings instead."""
        _require_user(request)
        from src import device_routing
        body = await request.json() if request.headers.get("content-type", "").startswith(
            "application/json") else {}
        known = {d.get("server_id"): d for d in device_routing.all_devices(mcp_manager)}
        dst_id = str(body.get("to") or "").strip()
        dst = known.get(dst_id)
        if not dst or dst_id.startswith("device:"):
            raise HTTPException(400, "Pick a computer to pair with")
        tools = set(dst.get("tools") or [])
        if body.get("manual"):
            if "open_bluetooth_settings" not in tools:
                raise HTTPException(409, f"{dst.get('name')} needs the updated desktop MCP for this")
            _payload(await _call(dst_id, "open_bluetooth_settings", {}))
            return {"ok": True, "opened": "bluetooth settings", "to": dst.get("name")}
        if "bluetooth_pair" not in tools:
            raise HTTPException(409, f"{dst.get('name')} needs the updated desktop MCP for this")
        src_id = str(body.get("from") or "")
        src = known.get(src_id)
        if not src:
            raise HTTPException(400, "Pick the phone to pair")
        opened = False
        if src_id.startswith("device:"):
            # Make the phone visible first: Modes opens "Pair new device". Asked
            # for: pairing without going to the phone's settings by hand.
            from src import devices as _devices
            dev = _devices.get(src_id[len("device:"):])
            if dev and "bt_pairing" in (dev.get("commands") or []):
                r = await _devices.send_command(dev, "bt_pairing", {})
                opened = bool((r or {}).get("ok"))
                if opened:
                    await asyncio.sleep(2)                # the screen starts advertising
        res = _payload(await _call(dst_id, "bluetooth_pair", {"device": src.get("name") or "", "seconds": 25}))
        res = dict(res, phone_screen_opened=opened)
        if res.get("ok") is False:
            # Logged: the page shows the reason, but the server log only had
            # "409 Conflict", which left a failed pairing unexplained.
            logger.warning("[media] pair %s with %s failed: %s", src.get("name"), dst.get("name"), res.get("error"))
            raise HTTPException(409, res.get("error") or "It did not pair")
        return dict(res, ok=True, to=dst.get("name"))

    @router.post("/receive")
    async def receive(request: Request):
        """Hear a phone's music on a computer: the phone keeps playing (it is
        the music engine) and the computer plays its sound over Bluetooth,
        through whatever is connected to it. Body {from: "device:<phone>",
        to: <computer server_id>, on: true|false}. Asked for: "if I have
        earbuds connected to the PC it should play through there, but the
        music engine is my phone"."""
        _require_user(request)
        from src import device_routing
        body = await request.json() if request.headers.get("content-type", "").startswith(
            "application/json") else {}
        src_id = str(body.get("from") or "").strip()
        dst_id = str(body.get("to") or "").strip()
        on = bool(body.get("on", True))
        known = {d.get("server_id"): d for d in device_routing.all_devices(mcp_manager)}
        dst = known.get(dst_id)
        if not dst or dst_id.startswith("device:"):
            raise HTTPException(400, "Pick a computer to hear it on")
        if "bluetooth_audio_receive" not in set(dst.get("tools") or []):
            raise HTTPException(409, f"{dst.get('name') or dst_id} needs the updated desktop MCP "
                                     "(tools/mcp/desktop_mcp_server.py) for this")
        name = ""
        if on:
            src = known.get(src_id)
            if not src or not src_id.startswith("device:"):
                raise HTTPException(400, "Pick the phone that is playing")
            name = src.get("name") or src_id[len("device:"):]
        res = _payload(await _call(dst_id, "bluetooth_audio_receive", {"device": name, "on": on}))
        if res.get("ok") is False:
            logger.warning("[media] hear %s on %s failed: %s", name or "(stop)", dst.get("name"), res.get("error"))
            raise HTTPException(409, res.get("error") or "It did not connect")
        return dict(res, ok=True, to=dst.get("name") or dst_id, **({"from": name} if name else {}))

    @router.post("/listen")
    async def listen_here(request: Request):
        """Play what another device is playing in this browser instead: pause
        it there and hand back the song's YouTube video, to play in an
        embedded player. Nothing needs installing where you listen. Asked
        for: "stream it over to my PC or my laptop so that I don't need
        YouTube Music installed"."""
        _require_user(request)
        from src import device_routing
        body = await request.json() if request.headers.get("content-type", "").startswith(
            "application/json") else {}
        src_id = str(body.get("from") or "").strip()
        known = {d.get("server_id"): d for d in device_routing.all_devices(mcp_manager)}
        if src_id not in known:
            raise HTTPException(400, "Pick a connected device")
        np = _payload(await _call(src_id, "now_playing"))
        title, artist = (np.get("title") or "").strip(), (np.get("artist") or "").strip()
        if not title:
            raise HTTPException(409, f"Nothing is playing on {known[src_id].get('name')}")
        vid = await asyncio.to_thread(_youtube_id, title, artist)
        if not vid:
            raise HTTPException(404, f"Could not find {title!r} on YouTube")
        if np.get("playing"):
            await _call(src_id, "media_control", {"action": "pause" if src_id.startswith("device:") else "play_pause"})
        try:
            start_s = max(0, int((np.get("position_ms") or 0) / 1000))
        except (TypeError, ValueError):
            start_s = 0
        import uuid
        _LISTEN_NOW["id"] = uuid.uuid4().hex[:12]
        return {"ok": True, "title": title, "artist": artist, "video_id": vid, "start_s": start_s,
                "listen_id": _LISTEN_NOW["id"],
                "url": f"https://music.youtube.com/watch?v={vid}",
                "from": src_id, "from_name": known[src_id].get("name") or src_id}

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
        # Only computers have the overlay. The bar may be controlling the
        # phone (server_id "device:..."); the overlay still goes on the
        # computer this browser is on. Seen live: with the bar on the phone,
        # every Pop out asked the phone for a Windows task, failed with 502,
        # and fell back to Chrome's pop-out window.
        desktops = [d for d in device_routing.all_devices(mcp_manager)
                    if not str(d.get("server_id") or "").startswith("device:")]
        sid = str(body.get("server_id") or "").strip()
        target = None
        if dev and not str(dev.get("server_id") or "").startswith("device:"):
            target = next((d for d in desktops if d.get("server_id") == dev.get("server_id")), None)
        if not target and sid:
            target = next((d for d in desktops if d.get("server_id") == sid), None)
        if not target:
            raise HTTPException(400, "The overlay opens on a computer: use Pop out from the "
                                     "browser on your PC")
        all_peers = await asyncio.to_thread(machines.peers)
        peer = machines.find_peer(all_peers, target.get("host", ""))
        if not peer:
            raise HTTPException(404, "That machine is not on the tailnet")
        r = await machines.run_user_task(peer, getpass.getuser(), "MusicOverlay")
        if not r.get("ok"):
            logger.warning("[overlay] could not start MusicOverlay on %s: %s",
                           target.get("name"), r.get("error"))
            raise HTTPException(502, f"Could not open the overlay: {r.get('error')}")
        return {"ok": True, "machine": target.get("name") or target.get("server_id")}

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


_YT_CACHE: Dict[str, str] = {}


def _youtube_id(title: str, artist: str) -> Optional[str]:
    """The first YouTube result for a song, from YouTube's own search page."""
    import re
    import urllib.parse
    import httpx
    t, a = _clean(title, artist)
    q = f"{t} {a}".strip()
    if q in _YT_CACHE:
        return _YT_CACHE[q]
    try:
        r = httpx.get("https://www.youtube.com/results", params={"search_query": q + " audio"},
                      headers={"Accept-Language": "en", "User-Agent": "Mozilla/5.0"}, timeout=12,
                      follow_redirects=True)
        m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', r.text)
    except Exception:
        return None
    if not m:
        return None
    _YT_CACHE[q] = m.group(1)
    return m.group(1)


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
