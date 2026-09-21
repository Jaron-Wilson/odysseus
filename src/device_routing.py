"""Work out which machine the browser is sitting on.

"Show me what I'm listening to" means a different machine depending on
where the page is open, and the answer is already in the request: on the
tailnet every device has its own stable address, and Odysseus sees the
client IP. So the page does not need to ask which machine you are on, and
you do not need to pick one from a list.

The mapping is derived from the registered MCP servers rather than
configured separately. A server registered at http://100.102.86.125:8931
is, by construction, the server for the machine at 100.102.86.125 — keeping
a second list in step with the first would only be a way to get them out of
step.
"""

import logging
import re
import socket
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Tools a machine must expose to be worth routing media requests to. A
# server that cannot answer any of them is not a media device, and offering
# it would produce an empty panel rather than an honest absence.
MEDIA_TOOLS = ("now_playing", "get_volume", "media_control")


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""


def _resolve(host: str) -> List[str]:
    """Every address a hostname answers to, so MagicDNS names match too."""
    if not host:
        return []
    if re.match(r"^[\d.]+$", host) or ":" in host:
        return [host]
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except Exception:
        return []


def device_map(mcp_mgr) -> Dict[str, dict]:
    """Client IP -> the MCP server that controls that machine."""
    out: Dict[str, dict] = {}
    if not mcp_mgr:
        return out
    try:
        from src.database import McpServer, SessionLocal
    except ImportError:
        return out

    tools_by_server = getattr(mcp_mgr, "_tools", {}) or {}
    db = SessionLocal()
    try:
        for srv in db.query(McpServer).filter(McpServer.is_enabled == True).all():  # noqa: E712
            if not srv.url:
                continue  # stdio servers are local, not a separate machine
            names = {t.get("name") for t in tools_by_server.get(srv.id, [])}
            if not any(t in names for t in MEDIA_TOOLS):
                continue
            host = _host_of(srv.url)
            for addr in _resolve(host):
                out[addr] = {"server_id": srv.id, "name": srv.name,
                             "host": host, "tools": sorted(n for n in names if n)}
    finally:
        db.close()
    return out


def for_client(mcp_mgr, client_ip: str) -> Optional[dict]:
    """The device the request came from, or None if it is not one we drive.

    Returning None matters: a browser on a phone, or off the tailnet
    entirely, has no controllable machine behind it, and guessing one would
    mean the volume slider silently moved a different computer.
    """
    client_ip = (client_ip or "").strip()
    if not client_ip:
        return None
    devices = device_map(mcp_mgr)
    hit = devices.get(client_ip)
    if hit:
        return dict(hit, client_ip=client_ip)
    return None


def all_devices(mcp_mgr) -> List[dict]:
    """Every machine that could be controlled, for an explicit picker."""
    seen: Dict[str, dict] = {}
    for addr, info in device_map(mcp_mgr).items():
        seen.setdefault(info["server_id"], dict(info, addresses=[]))
        seen[info["server_id"]]["addresses"].append(addr)
    return list(seen.values())
