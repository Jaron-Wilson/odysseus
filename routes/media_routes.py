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

        if action in ("play_pause", "play", "pause", "next", "previous", "stop"):
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
                     "volume, mute or output")
        return {"requested": action, "result": _payload(res)}

    return router
