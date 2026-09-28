"""Play a file that lives on one of the user's machines, streamed from there:
/api/device-media/<token>/<name>.

Asked for: "I don't want to copy anything, I want to be able to host a video
from any device". The machine's desktop MCP server shares one file under an
unguessable token (share_media) and serves it at /media/<token>. The page is
on HTTPS and cannot load a machine's plain-HTTP port itself, so the bytes
pass through here, behind the normal login, with Range forwarded so the
player can seek. Nothing is stored on this server.
"""

import logging
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

logger = logging.getLogger(__name__)

_WHERE: Dict[str, str] = {}             # token -> the machine's base URL, once found
_PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "cache-control")


def _bases(mcp_manager) -> list:
    """Base URLs of the connected machines that can share media."""
    out = []
    try:
        from src.database import McpServer, SessionLocal
    except ImportError:
        return out
    tools = getattr(mcp_manager, "_tools", {}) or {}
    db = SessionLocal()
    try:
        for srv in db.query(McpServer).filter(McpServer.is_enabled == True).all():  # noqa: E712
            if not srv.url:
                continue
            names = {t.get("name") for t in tools.get(srv.id, [])}
            if names and "share_media" not in names:
                continue
            u = urlparse(srv.url)
            if u.scheme in ("http", "https") and u.netloc:
                out.append(f"{u.scheme}://{u.netloc}")
    finally:
        db.close()
    return out


async def _find(mcp_manager, token: str) -> Optional[str]:
    if token in _WHERE:
        return _WHERE[token]
    async with httpx.AsyncClient(timeout=4) as c:
        for base in _bases(mcp_manager):
            try:
                r = await c.head(f"{base}/media/{token}")
            except Exception:
                continue
            if r.status_code in (200, 206):
                _WHERE[token] = base
                return base
    return None


def setup_device_media_routes(mcp_manager) -> APIRouter:
    router = APIRouter(tags=["device-media"])

    @router.api_route("/api/device-media/{token}/{name}", methods=["GET", "HEAD"])
    async def device_media(request: Request, token: str, name: str):
        from src.auth_helpers import require_authenticated_request
        require_authenticated_request(request)
        if not token.replace("-", "").replace("_", "").isalnum() or len(token) > 64:
            raise HTTPException(400, "Invalid link")
        base = await _find(mcp_manager, token)
        if not base:
            raise HTTPException(404, "That file is not shared any more, or its machine is off")
        headers = {}
        if request.headers.get("range"):
            headers["Range"] = request.headers["range"]
        client = httpx.AsyncClient(timeout=httpx.Timeout(20, read=None))
        try:
            upstream = await client.send(
                client.build_request(request.method, f"{base}/media/{token}", headers=headers),
                stream=True)
        except Exception as e:
            await client.aclose()
            _WHERE.pop(token, None)
            raise HTTPException(502, f"Could not reach the machine: {e}")
        if upstream.status_code == 404:
            await upstream.aclose()
            await client.aclose()
            _WHERE.pop(token, None)
            raise HTTPException(404, "That file is not shared any more")
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in _PASS_HEADERS}

        async def _close():
            await upstream.aclose()
            await client.aclose()
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 headers=out_headers, background=BackgroundTask(_close))

    return router
