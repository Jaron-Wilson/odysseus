"""Settings > Devices — /api/devices/*.

The device registry (src/devices.py) used to be reachable only through the
agent's manage_devices tool, so there was no way to see, fix or remove a
device by hand, and no way to tell that a phone's push subscription was
filed under a different name than the phone itself.

Admin only: a device record carries the token that lets its holder launch
apps on the phone, and every device is shared by the whole install.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import devices, webpush

logger = logging.getLogger(__name__)


# ── Computers: machines reached over MCP rather than push ─────────────────
#
# A PC or laptop is not in the device registry at all: Odysseus reaches it
# through an MCP server running on it (tools/mcp/desktop_mcp_server.py on
# Windows, linux_desktop_mcp_server.py on Linux). Any configured server that
# offers list_apps is a computer here; the rest are shown as plain servers so
# an offline machine is still listed, not silently missing.

def _mcp():
    from src.tool_utils import get_mcp_manager
    return get_mcp_manager()


def _configured_servers():
    from core.database import McpServer, SessionLocal
    db = SessionLocal()
    try:
        return [{"id": s.id, "name": s.name, "url": s.url or "", "transport": s.transport,
                 "enabled": bool(s.is_enabled)} for s in db.query(McpServer).all()]
    finally:
        db.close()


def _parse_tool_json(stdout: str):
    """An MCP tool's text output as JSON. FastMCP sends a returned list as one
    text block per item, which arrive joined, so decode them one by one."""
    import json
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    dec, items, i = json.JSONDecoder(), [], 0
    while i < len(text):
        while i < len(text) and text[i] in " \r\n\t,":
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        items.append(obj)
    return items


def _apps_from(result) -> dict:
    """Normalize list_apps output from the Windows and Linux servers alike to
    {"apps": [{"name", "launch", "running"?}], "total"}. `launch` is what that
    machine's launch_app wants: a curated key or Start menu name on Windows,
    a .desktop id on Linux."""
    if isinstance(result, list):                       # older Windows server
        apps = [{"name": a.get("description") or a.get("app"), "launch": a.get("app"),
                 "running": a.get("running"), "curated": True}
                for a in result if isinstance(a, dict) and a.get("app")]
        return {"apps": apps, "total": len(apps), "note": ""}
    if isinstance(result, dict):
        apps = []
        for a in result.get("curated") or []:         # Windows, special handling
            apps.append({"name": a.get("description") or a.get("app"), "launch": a.get("app"),
                         "running": a.get("running"), "curated": True})
        for n in result.get("installed") or []:        # Windows Start menu
            apps.append({"name": n, "launch": n})
        for a in result.get("apps") or []:             # Linux .desktop entries
            if isinstance(a, dict) and (a.get("id") or a.get("name")):
                apps.append({"name": a.get("name") or a.get("id"), "launch": a.get("id") or a.get("name")})
        curated = len(result.get("curated") or [])
        total = (result.get("installed_total") or 0) + curated if "installed_total" in result \
            else result.get("count") or len(apps)
        return {"apps": apps, "total": total, "note": result.get("note", "")}
    return {"apps": [], "total": 0, "note": ""}


def _subscriptions_view():
    """Push subscriptions, each with the device it reaches (if linked)."""
    out = []
    for s in webpush.load_subscriptions():
        label = s.get("device") or ""
        out.append({
            "device": label,
            "owner": s.get("owner") or "",
            "endpoint": (s.get("endpoint") or "")[:48] + "…",
            "linked_to": devices.owner_of_alias(label) if label else None,
        })
    return out


def setup_device_routes() -> APIRouter:
    router = APIRouter(tags=["devices"])

    async def _body(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    def _get_or_404(name: str) -> dict:
        d = devices.get(name)
        if d is None:
            raise HTTPException(404, f"no device named {name!r}")
        return d

    @router.get("/api/devices")
    async def list_all(request: Request):
        require_admin(request)
        return {
            "devices": [devices.public(d) for d in devices.list_devices()],
            "subscriptions": _subscriptions_view(),
            "commands": devices.KNOWN_COMMANDS,
        }

    @router.post("/api/devices")
    async def add(request: Request):
        require_admin(request)
        body = await _body(request)
        name = (body.get("name") or "").strip()
        if devices.get(name) is not None:
            raise HTTPException(409, f"a device named {name!r} already exists")
        try:
            rec = devices.register(
                name, kind=(body.get("kind") or "phone").strip(),
                commands=body.get("commands") or ["notify"],
                endpoint=body.get("endpoint") or "")
        except ValueError as e:
            raise HTTPException(400, str(e))
        logger.info("[devices] added %s", rec.get("name"))
        return {"ok": True, "device": devices.public(rec)}

    @router.patch("/api/devices/{name}")
    async def edit(name: str, request: Request):
        require_admin(request)
        body = await _body(request)
        try:
            rec = devices.update(
                name,
                new_name=body.get("new_name"),
                kind=body.get("kind"),
                endpoint=body.get("endpoint"),
                commands=body.get("commands"),
            )
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "device": devices.public(rec)}

    @router.delete("/api/devices/{name}")
    async def delete(name: str, request: Request):
        require_admin(request)
        if not devices.remove(name):
            raise HTTPException(404, f"no device named {name!r}")
        logger.info("[devices] removed %s", name)
        return {"ok": True}

    @router.post("/api/devices/{name}/aliases")
    async def link(name: str, request: Request):
        require_admin(request)
        body = await _body(request)
        try:
            rec = devices.add_alias(name, body.get("alias") or "")
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "device": devices.public(rec)}

    @router.delete("/api/devices/{name}/aliases/{alias}")
    async def unlink(name: str, alias: str, request: Request):
        require_admin(request)
        try:
            rec = devices.remove_alias(name, alias)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"ok": True, "device": devices.public(rec)}

    @router.post("/api/devices/{name}/token")
    async def reveal_token(name: str, request: Request):
        """The full token, on request, to paste into the device's listener.
        POST so it is never fetched by a prefetch or left in a history list."""
        require_admin(request)
        d = _get_or_404(name)
        if not d.get("token"):
            raise HTTPException(409, f"{name} has no token")
        return {"token": d["token"]}

    @router.get("/api/devices/computers")
    async def computers(request: Request):
        """Every configured MCP server, with whether it is up and what it can
        do. The PC and laptop show up here with app control; an offline one
        is listed with its error rather than left out."""
        require_admin(request)
        mgr = _mcp()
        tools_by_server = {}
        if mgr:
            for t in mgr.get_all_tools():
                tools_by_server.setdefault(t["server_id"], set()).add(t["name"])
        out = []
        for s in _configured_servers():
            status = mgr.get_server_status(s["id"]) if mgr else {"status": "disconnected"}
            tools = tools_by_server.get(s["id"], set())
            err = status.get("error")
            # The MCP client reports a machine that is asleep or off as
            # "unhandled errors in a TaskGroup", which says nothing useful.
            if err and any(x in str(err) for x in ("TaskGroup", "ConnectError", "Connection refused",
                                                   "timed out", "Name or service not known")):
                err = "Can't reach it: is it on, awake, and running its MCP server?"
            out.append({
                **s,
                "status": status.get("status", "disconnected"),
                "error": err,
                "tool_count": status.get("tool_count", len(tools)),
                "apps": "list_apps" in tools,
                "launch": "launch_app" in tools,
                "screen": "screenshot" in tools,
            })
        return {"computers": out}

    @router.get("/api/devices/computers/{server_id}/apps")
    async def computer_apps(server_id: str, request: Request, match: str = ""):
        require_admin(request)
        mgr = _mcp()
        if not mgr:
            raise HTTPException(503, "MCP is not running")
        res = await mgr.call_tool(f"mcp__{server_id}__list_apps", {"match": match})
        if res.get("exit_code"):
            raise HTTPException(502, (res.get("stderr") or res.get("error") or "list_apps failed")[:300])
        try:
            parsed = _parse_tool_json(res.get("stdout", ""))
        except ValueError:
            raise HTTPException(502, "list_apps returned something that is not JSON")
        return _apps_from(parsed)

    @router.post("/api/devices/computers/{server_id}/launch")
    async def computer_launch(server_id: str, request: Request):
        """Open one app from that machine's own list. The machine resolves the
        name; nothing here can pass it a path."""
        require_admin(request)
        mgr = _mcp()
        if not mgr:
            raise HTTPException(503, "MCP is not running")
        body = await _body(request)
        app = (body.get("app") or "").strip()
        if not app:
            raise HTTPException(400, "app is required")
        res = await mgr.call_tool(f"mcp__{server_id}__launch_app", {"app": app})
        if res.get("exit_code"):
            return {"ok": False, "error": (res.get("stderr") or res.get("error") or "launch failed")[:400]}
        try:
            return _parse_tool_json(res.get("stdout", "")) or {"ok": True}
        except ValueError:
            return {"ok": True, "raw": (res.get("stdout") or "")[:400]}

    @router.post("/api/devices/{name}/test")
    async def test(name: str, request: Request):
        """Try both routes to the device and say which one works."""
        require_admin(request)
        d = _get_or_404(name)
        push = await webpush.send("Odysseus", f"Test notification for {d['name']}.",
                                  device=d["name"], url="/")
        listener = None
        if d.get("endpoint"):
            listener = await devices.send_command(
                d, "notify", {"title": "Odysseus", "message": f"Listener test for {d['name']}."})
        return {"push": push, "listener": listener}

    return router
