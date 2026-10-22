"""HTTP routes of the mod system.

  /mods/<id>/...                 the mod's own server, proxied (src/mods/proxy.py)
  /api/mods                      admin: list, install, update, remove, logs...
  /api/mods/ui                   any user: the tabs and home cards to show
  /api/mods/v1/...               mods: the mod API, bearer ODYSSEUS_MOD_TOKEN

The mod API is exempt from AuthMiddleware (app.py) because mods do not hold a
user session; every route here checks the mod's token itself and then the
permission the route needs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse

from src.mods import access, events, manifest as mf, notify, placement, proxy, registry, runtime, tools

logger = logging.getLogger(__name__)

_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
MAX_SETTINGS_BYTES = 64 * 1024
MAX_KV_KEYS = 256
MAX_KV_VALUE_BYTES = 64 * 1024
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Per-mod, per-permission rate limits: (calls, seconds).
RATE_LIMITS = {"notify": (30, 60), "llm.call": (20, 60), "events.emit": (240, 60), "devices.control": (30, 60)}
_calls: Dict[tuple, Deque[float]] = defaultdict(deque)


def _err(e: registry.RegistryError):
    return JSONResponse({"detail": e.message, "errors": e.details}, status_code=e.status)


def _require_admin(request: Request) -> str:
    from core.middleware import require_admin
    from src.auth_helpers import require_user
    if getattr(request.state, "api_token", False):
        raise HTTPException(403, "Managing mods needs a signed-in admin")
    user = require_user(request)
    require_admin(request)
    return user


async def _json(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Expected a JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a JSON object")
    return data


def _view(entry: Dict[str, Any]) -> Dict[str, Any]:
    out = registry.public(entry)
    out["state"] = runtime.state(entry["id"]).view()
    out["tool_names"] = [t["name"] for t in tools.cached(entry["id"])]
    out["notify_muted"] = bool(entry.get("notify_muted"))
    out["subscriptions"] = events.subscriptions(entry["id"])
    return out


async def _after_enable(mod_id: str) -> None:
    """Start a mod that starts at boot or has tools (its tool list is read
    when it starts); lazy ones without tools wait for first use."""
    entry = registry.get(mod_id) or {}
    man = entry.get("manifest") or {}
    if man.get("runtime", {}).get("start") == "boot" or man.get("tools"):
        view = await runtime.start(mod_id)
        if view["status"] == "running":
            await tools.refresh(mod_id)


# ── The mod API (/api/mods/v1) ──────────────────────────────────────────

def _mod(request: Request, permission: Optional[str] = None) -> Dict[str, Any]:
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    mod_id = registry.mod_for_token(token)
    if not mod_id:
        raise HTTPException(401, "Invalid or missing mod token")
    entry = registry.get(mod_id)
    if not entry or not entry.get("enabled"):
        raise HTTPException(403, "This mod is disabled")
    if permission and permission not in (entry.get("granted_permissions") or []):
        raise HTTPException(403, f"This mod was not granted the {permission} permission")
    if permission in RATE_LIMITS:
        n, per = RATE_LIMITS[permission]
        q = _calls[(mod_id, permission)]
        now = time.time()
        while q and now - q[0] > per:
            q.popleft()
        if len(q) >= n:
            raise HTTPException(429, f"Too many {permission} calls; at most {n} per {per} seconds")
        q.append(now)
    runtime.touch(mod_id)
    return entry


def _owner_for(entry: Dict[str, Any], body: Dict[str, Any]) -> str:
    """Whose notification/model call this is: a user the mod names (from the
    identity headers it was given) if they may use the mod, else the primary
    admin."""
    want = str(body.get("user") or "").strip()
    if want and access.known_user(want) and access.may_use(entry, want):
        return want
    from src.event_bus import _resolve_event_owner
    return _resolve_event_owner(None) or ""


def _setup_v1(router: APIRouter) -> None:
    @router.get("/api/mods/v1/me")
    async def me(request: Request):
        e = _mod(request)
        return {"id": e["id"], "name": e.get("name"), "version": e.get("version"),
                "permissions": e.get("granted_permissions") or [], "core_api": mf.CORE_API_VERSION,
                "base_path": f"/mods/{e['id']}"}

    @router.post("/api/mods/v1/notify")
    async def v1_notify(request: Request):
        e = _mod(request, "notify")
        body = await _json(request)
        try:
            msg = notify.clean(body)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        result = await notify.send(e["id"], _owner_for(e, body), msg)
        runtime.log_line(e["id"], f"[odysseus] notify ({msg['level']}): {msg['title'][:80]}")
        return {"ok": True, "channels": result}

    @router.post("/api/mods/v1/events")
    async def v1_emit(request: Request):
        e = _mod(request, "events.emit")
        body = await _json(request)
        etype = str(body.get("type") or "")
        declared = ((e.get("manifest") or {}).get("events") or {}).get("emits") or []
        if etype not in declared:
            raise HTTPException(400, f"{etype!r} is not in this mod's events.emits")
        try:
            ev = events.publish(e["id"], etype, body.get("data"), owner=_owner_for(e, body) or None)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        return {"ok": True, "id": ev["id"], "delivered_to": ev["delivered_to"]}

    @router.get("/api/mods/v1/events/subscriptions")
    async def v1_subs(request: Request):
        e = _mod(request, "events.subscribe")
        return {"subscriptions": events.subscriptions(e["id"])}

    @router.post("/api/mods/v1/events/subscriptions")
    async def v1_sub_add(request: Request):
        e = _mod(request, "events.subscribe")
        body = await _json(request)
        try:
            return events.add_subscription(e["id"], str(body.get("pattern") or ""), body.get("path"))
        except ValueError as ex:
            raise HTTPException(400, str(ex))

    @router.delete("/api/mods/v1/events/subscriptions")
    async def v1_sub_del(request: Request, pattern: str = ""):
        e = _mod(request, "events.subscribe")
        return {"removed": events.remove_subscription(e["id"], pattern)}

    @router.get("/api/mods/v1/devices")
    async def v1_devices(request: Request):
        _mod(request, "devices.read")
        from src import devices
        return {"devices": [devices.public(d) for d in devices.list_devices()]}

    @router.post("/api/mods/v1/devices/{name}/command")
    async def v1_device_command(request: Request, name: str):
        e = _mod(request, "devices.control")
        body = await _json(request)
        from src import devices
        dev = devices.resolve(name)
        if not dev:
            raise HTTPException(404, f"No device {name!r}")
        command = str(body.get("command") or "")
        if command not in devices.KNOWN_COMMANDS:
            raise HTTPException(400, f"Unknown command {command!r}")
        if not devices.supports(dev, command):
            raise HTTPException(400, f"{dev.get('name')} does not support {command}")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        runtime.log_line(e["id"], f"[odysseus] device command {command} -> {dev.get('name')}")
        return await devices.send_command(dev, command, params)

    def _json_file(path: str) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = json.load(f)
            return v if isinstance(v, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_json(path: str, data: Dict[str, Any]) -> None:
        from core.atomic_io import atomic_write_json
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write_json(path, data)

    @router.get("/api/mods/v1/settings")
    async def v1_settings(request: Request):
        e = _mod(request)
        return {"settings": _json_file(os.path.join(registry.paths.mod_dir(e["id"]), "settings.json"))}

    @router.put("/api/mods/v1/settings")
    async def v1_settings_put(request: Request):
        e = _mod(request)
        body = await _json(request)
        values = body.get("settings", body)
        if not isinstance(values, dict) or len(json.dumps(values)) > MAX_SETTINGS_BYTES:
            raise HTTPException(400, "settings must be an object under 64 KB")
        _write_json(os.path.join(registry.paths.mod_dir(e["id"]), "settings.json"), values)
        return {"ok": True}

    def _kv_path(mod_id: str) -> str:
        return os.path.join(registry.paths.mod_dir(mod_id), "kv.json")

    @router.get("/api/mods/v1/kv")
    async def v1_kv_list(request: Request):
        e = _mod(request, "storage")
        return {"keys": sorted(_json_file(_kv_path(e["id"])).keys())}

    @router.get("/api/mods/v1/kv/{key}")
    async def v1_kv_get(request: Request, key: str):
        e = _mod(request, "storage")
        data = _json_file(_kv_path(e["id"]))
        if key not in data:
            raise HTTPException(404, "No such key")
        return {"key": key, "value": data[key]}

    @router.put("/api/mods/v1/kv/{key}")
    async def v1_kv_put(request: Request, key: str):
        e = _mod(request, "storage")
        if not _KEY_RE.match(key):
            raise HTTPException(400, "keys may use letters, digits and . _ : -")
        body = await _json(request)
        if "value" not in body or len(json.dumps(body["value"])) > MAX_KV_VALUE_BYTES:
            raise HTTPException(400, "send {\"value\": ...} under 64 KB")
        data = _json_file(_kv_path(e["id"]))
        if key not in data and len(data) >= MAX_KV_KEYS:
            raise HTTPException(400, f"at most {MAX_KV_KEYS} keys")
        data[key] = body["value"]
        _write_json(_kv_path(e["id"]), data)
        return {"ok": True}

    @router.delete("/api/mods/v1/kv/{key}")
    async def v1_kv_del(request: Request, key: str):
        e = _mod(request, "storage")
        data = _json_file(_kv_path(e["id"]))
        existed = data.pop(key, None) is not None
        _write_json(_kv_path(e["id"]), data)
        return {"ok": True, "existed": existed}

    @router.post("/api/mods/v1/llm/chat")
    async def v1_llm(request: Request):
        e = _mod(request, "llm.call")
        body = await _json(request)
        messages = body.get("messages")
        if (not isinstance(messages, list) or not messages or len(json.dumps(messages)) > 100_000
                or not all(isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
                           and isinstance(m.get("content"), str) for m in messages)):
            raise HTTPException(400, "messages must be a list of {role, content} under 100 KB")
        role = body.get("model_role") or "utility"
        if role not in ("utility", "default"):
            raise HTTPException(400, "model_role must be utility or default")
        try:
            max_tokens = max(16, min(int(body.get("max_tokens") or 1024), 8192))
            temperature = max(0.0, min(float(body.get("temperature") if body.get("temperature") is not None else 0.3), 2.0))
        except (TypeError, ValueError):
            raise HTTPException(400, "max_tokens and temperature must be numbers")
        owner = _owner_for(e, body)
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async
        url, model, headers = resolve_endpoint(role, owner=owner or None)
        if not url or not model:
            raise HTTPException(503, "No model is configured (Settings > AI Defaults)")
        try:
            text = await llm_call_async(url, model, messages, temperature=temperature, max_tokens=max_tokens,
                                        headers=headers, owner=owner or None, timeout=180)
        except Exception as ex:
            raise HTTPException(502, f"The model call failed: {type(ex).__name__}")
        runtime.log_line(e["id"], f"[odysseus] llm/chat via {model}")
        return {"content": text, "model": model}


# ── Admin and UI ─────────────────────────────────────────────────────────

def setup_mods_routes(auth_manager=None) -> APIRouter:
    access.set_auth_manager(auth_manager)
    router = APIRouter(tags=["mods"])
    _setup_v1(router)

    @router.get("/api/mods")
    async def list_mods(request: Request):
        _require_admin(request)
        return {"mods": [_view(m) for m in sorted(registry.list_mods(), key=lambda m: m["id"])],
                "core_api": mf.CORE_API_VERSION, "permissions": mf.PERMISSIONS,
                "notify": notify.channel_settings(), "nav_groups": placement.groups()}

    @router.post("/api/mods/install/prepare")
    async def install_prepare(request: Request):
        _require_admin(request)
        body = await _json(request)
        try:
            return await asyncio.to_thread(registry.prepare, str(body.get("source") or ""), body.get("ref"),
                                           body.get("subdir"))
        except registry.RegistryError as e:
            return _err(e)

    @router.post("/api/mods/install/confirm")
    async def install_confirm(request: Request):
        _require_admin(request)
        body = await _json(request)
        iid = str(body.get("install_id") or "")
        try:
            meta = registry._staged(iid)
            update_of = meta.get("update_of")
            if update_of:
                await runtime.stop(update_of, "stopped for an update")
            entry = await asyncio.to_thread(registry.confirm, iid, body.get("permissions") or [])
        except registry.RegistryError as e:
            return _err(e)
        mod_id = entry["id"]
        if update_of:
            if entry.get("enabled"):
                await _after_enable(mod_id)
        elif body.get("enable", True):
            registry.set_enabled(mod_id, True)
            await _after_enable(mod_id)
        return {"mod": _view(registry.get(mod_id))}

    @router.post("/api/mods/install/cancel")
    async def install_cancel(request: Request):
        _require_admin(request)
        body = await _json(request)
        try:
            registry.discard(str(body.get("install_id") or ""))
        except registry.RegistryError as e:
            return _err(e)
        return {"ok": True}

    @router.get("/api/mods/settings")
    async def get_settings(request: Request):
        _require_admin(request)
        return {"notify": notify.channel_settings()}

    @router.put("/api/mods/settings")
    async def put_settings(request: Request):
        _require_admin(request)
        body = await _json(request)
        return {"notify": notify.save_channel_settings(body.get("notify") or {})}

    @router.get("/api/mods/events")
    async def recent_events(request: Request, source: str = ""):
        _require_admin(request)
        return {"events": events.recent(100, source or None)}

    @router.post("/api/mods/scaffold")
    async def scaffold(request: Request):
        _require_admin(request)
        body = await _json(request)
        from src.mods import scaffold as sc
        try:
            out = await asyncio.to_thread(sc.create, str(body.get("folder") or ""), str(body.get("id") or ""),
                                          str(body.get("name") or ""), str(body.get("description") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return out

    @router.get("/api/mods/ui")
    async def ui_list(request: Request):
        user = proxy.cookie_user(request)
        if user is None:
            raise HTTPException(401, "Not signed in")
        out, shown = [], []
        for entry in sorted(registry.list_mods(), key=lambda m: m["id"]):
            if not entry.get("enabled") or not access.may_use(entry, user):
                continue
            ui = (entry.get("manifest") or {}).get("ui") or {}
            if not (ui.get("tabs") or ui.get("home_cards") or ui.get("settings_path")):
                continue
            shown.append(entry)
            base = proxy.frame_base(entry["id"], user)
            override = entry.get("placement") or {}
            out.append({
                "id": entry["id"], "name": entry.get("name"),
                "status": runtime.state(entry["id"]).status,
                "tabs": [dict(t, url=base + t["path"], **placement.resolve(t, override)) for t in ui.get("tabs") or []],
                "home_cards": [dict(c, url=base + c["path"]) for c in ui.get("home_cards") or []],
                "settings_url": base + ui["settings_path"] if ui.get("settings_path") else None,
            })
        # `nav`: the tabs in sidebar order, in their groups, hidden ones left out.
        return {"mods": out, "nav": placement.nav(shown), "groups": placement.groups()}

    @router.get("/api/mods/{mod_id}")
    async def get_mod(request: Request, mod_id: str):
        _require_admin(request)
        e = registry.get(mod_id)
        if not e:
            raise HTTPException(404, "No such mod")
        return {"mod": _view(e)}

    @router.post("/api/mods/{mod_id}/{action}")
    async def mod_action(request: Request, mod_id: str, action: str):
        _require_admin(request)
        e = registry.get(mod_id)
        if not e:
            raise HTTPException(404, "No such mod")
        try:
            if action == "enable":
                registry.set_enabled(mod_id, True)
                await _after_enable(mod_id)
            elif action == "disable":
                registry.set_enabled(mod_id, False)
                await runtime.stop(mod_id, "disabled")
            elif action == "start":
                await runtime.start(mod_id)
                if runtime.state(mod_id).status == "running":
                    await tools.refresh(mod_id)
            elif action == "stop":
                await runtime.stop(mod_id)
            elif action == "restart":
                await runtime.restart(mod_id)
                if runtime.state(mod_id).status == "running":
                    await tools.refresh(mod_id)
            elif action == "rotate-token":
                registry.rotate_token(mod_id)
                runtime.log_line(mod_id, "[odysseus] token rotated")
                if runtime.state(mod_id).status not in ("stopped",):
                    await runtime.restart(mod_id)
            elif action == "update":
                body = await _json(request) if (request.headers.get("content-length") or "0") != "0" else {}
                src = e.get("source") or {}
                source = src.get("url") or src.get("path") or ""
                ref = body.get("ref") if body.get("ref") is not None else src.get("ref")
                staged = await asyncio.to_thread(registry.prepare, source, ref, src.get("subdir"), mod_id)
                return staged
            elif action == "access":
                body = await _json(request)
                registry.set_access(mod_id, str(body.get("access") or ""))
            elif action == "placement":
                body = await _json(request)
                registry.set_placement(mod_id, body.get("section"), body.get("hidden"))
            elif action == "mute":
                body = await _json(request)
                registry._update(mod_id, notify_muted=bool(body.get("muted")))
            else:
                raise HTTPException(404, "Unknown action")
        except registry.RegistryError as ex:
            return _err(ex)
        return {"mod": _view(registry.get(mod_id))}

    @router.delete("/api/mods/{mod_id}")
    async def delete_mod(request: Request, mod_id: str, keep_data: bool = False):
        _require_admin(request)
        if not registry.get(mod_id):
            raise HTTPException(404, "No such mod")
        await runtime.stop(mod_id, "removed")
        try:
            await asyncio.to_thread(registry.remove, mod_id, keep_data)
        except registry.RegistryError as e:
            return _err(e)
        return {"ok": True}

    @router.get("/api/mods/{mod_id}/logs")
    async def logs(request: Request, mod_id: str, lines: int = 300):
        _require_admin(request)
        if not registry.get(mod_id):
            raise HTTPException(404, "No such mod")
        return {"log": runtime.read_log(mod_id, lines)}

    # ── The proxy ────────────────────────────────────────────────────────
    @router.api_route("/mods/{mod_id}", methods=_METHODS, include_in_schema=False)
    async def mod_root(request: Request, mod_id: str):
        q = request.url.query
        return RedirectResponse(f"/mods/{mod_id}/" + (f"?{q}" if q else ""), status_code=307)

    @router.api_route("/mods/{mod_id}/{rest:path}", methods=_METHODS, include_in_schema=False)
    async def mod_http(request: Request, mod_id: str, rest: str):
        return await proxy.handle_http(request, mod_id, rest)

    @router.websocket("/mods/{mod_id}/{rest:path}")
    async def mod_ws(websocket: WebSocket, mod_id: str, rest: str):
        await proxy.handle_ws(websocket, mod_id, rest)

    return router
