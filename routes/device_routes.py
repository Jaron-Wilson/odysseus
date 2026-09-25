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
