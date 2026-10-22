"""HTTP routes of the mod system.

  /mods/<id>/...                 the mod's own server, proxied (src/mods/proxy.py)
  /api/mods                      admin: list, install, update, remove, logs...
  /api/mods/ui                   any user: the tabs and home cards to show
  /api/mods/<id>/tutorial...     any user who may use the mod: its tutorial (src/mods/tutorial.py)
  /api/mods/v1/...               mods: the mod API, bearer ODYSSEUS_MOD_TOKEN
  /api/mods/v1/cap/<cap>/<act>   mods: a capability call, brokered by the core (src/mods/capabilities.py)

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

from src.mods import (access, capabilities, catalog, events, manifest as mf, notify, placement, proxy, registry,
                      runtime, tools, tutorial)

logger = logging.getLogger(__name__)

_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
MAX_SETTINGS_BYTES = 64 * 1024
MAX_KV_KEYS = 256
MAX_KV_VALUE_BYTES = 64 * 1024
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Per-mod, per-permission rate limits: (calls, seconds).
RATE_LIMITS = {"notify": (30, 60), "llm.call": (20, 60), "events.emit": (240, 60), "devices.control": (30, 60),
               "tools.refresh": (6, 60)}
_calls: Dict[tuple, Deque[float]] = defaultdict(deque)
_background: set = set()   # running background tasks (kept so they are not garbage collected)


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


def _view(entry: Dict[str, Any], user: Optional[str] = None) -> Dict[str, Any]:
    out = registry.public(entry)
    # What it provides, needs and may use, with who provides each now; the
    # mods that would lose a capability if it went; and what this admin granted it.
    out["capabilities"] = capabilities.availability(entry.get("manifest") or {})
    out["dependents"] = capabilities.dependents(entry["id"])
    out["grants"] = capabilities.grants(entry, user) if user is not None else {}
    out["state"] = runtime.state(entry["id"]).view()
    out["tool_names"] = [t["name"] for t in tools.cached(entry["id"])]
    out["notify_muted"] = bool(entry.get("notify_muted"))
    out["subscriptions"] = events.subscriptions(entry["id"])
    # From the cached catalog only (src/mods/catalog.py); never fetches.
    out["update"] = catalog.updates().get(entry["id"])
    return out


def _check_blocked(staged: Dict[str, Any], allow_blocked: bool) -> Optional[JSONResponse]:
    """Refuse a version a curated index blocks, unless the admin explicitly
    overrode it. A refused install is thrown away."""
    src = staged.get("source") or {}
    why = catalog.blocked(src.get("url") or "", staged.get("version") or "")
    if not why:
        return None
    if allow_blocked:
        staged.setdefault("warnings", []).insert(0, f"Installing anyway: {why}")
        staged["blocked"] = why
        return None
    try:
        registry.discard(staged["install_id"])
    except registry.RegistryError:
        pass
    return JSONResponse({"detail": f"Blocked: {why}. Install it anyway only if you know why it was blocked.",
                         "blocked": True, "errors": []}, status_code=409)


def _dependents_refusal(mod_id: str, doing: str, force: bool) -> Optional[JSONResponse]:
    """A 409 listing the mods that would lose a capability only `mod_id`
    provides, unless the admin already confirmed (`force`)."""
    deps = [] if force else capabilities.dependents(mod_id)
    if not deps:
        return None
    hard = sorted({d["name"] for d in deps if d["kind"] == "requires"})
    soft = sorted({d["name"] for d in deps if d["kind"] == "optional"} - set(hard))
    parts = []
    if hard:
        parts.append(f"{', '.join(hard)} {'stops' if len(hard) == 1 else 'stop'} working")
    if soft:
        parts.append(f"{', '.join(soft)} {'loses' if len(soft) == 1 else 'lose'} features")
    caps = ", ".join(sorted({d["capability"] for d in deps}))
    return JSONResponse({"detail": f"{doing} it means {' and '.join(parts)}: nothing else provides {caps}.",
                         "dependents": deps, "errors": []}, status_code=409)


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
        _rate(mod_id, permission)
    runtime.touch(mod_id)
    return entry


def _rate(mod_id: str, key: str) -> None:
    n, per = RATE_LIMITS[key]
    q = _calls[(mod_id, key)]
    now = time.time()
    while q and now - q[0] > per:
        q.popleft()
    if len(q) >= n:
        raise HTTPException(429, f"Too many {key} calls; at most {n} per {per} seconds")
    q.append(now)


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

    @router.post("/api/mods/v1/tools/refresh")
    async def v1_tools_refresh(request: Request):
        """The mod's tools changed (it was configured, a device appeared...):
        read GET <tools.endpoint> again. By default this happens in the
        background, so a mod that serves one request at a time cannot
        deadlock; `{"wait": true}` waits and answers the new tool names."""
        e = _mod(request)
        if not (e.get("manifest") or {}).get("tools"):
            raise HTTPException(400, "This mod has no `tools` in its manifest")
        _rate(e["id"], "tools.refresh")
        body = await _json(request) if (request.headers.get("content-length") or "0") != "0" else {}
        if body.get("wait") is True:
            found = await tools.refresh(e["id"])
            runtime.log_line(e["id"], f"[odysseus] tool list refreshed: {len(found)} tool(s)")
            return {"ok": True, "tools": [t["name"] for t in found]}

        async def later(mod_id: str) -> None:
            await asyncio.sleep(0.2)
            try:
                found = await tools.refresh(mod_id)
                runtime.log_line(mod_id, f"[odysseus] tool list refreshed: {len(found)} tool(s)")
            except Exception as ex:   # logged, never raised into the event loop
                logger.warning("[mods] tool refresh for %s failed: %s", mod_id, ex)

        task = asyncio.get_running_loop().create_task(later(e["id"]))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return {"ok": True, "queued": True}

    # ── Capabilities (src/mods/capabilities.py) ──
    @router.get("/api/mods/v1/cap")
    async def v1_caps(request: Request):
        """The capabilities this mod declared, and whether each is available now."""
        e = _mod(request)
        return capabilities.availability(e.get("manifest") or {})

    @router.post("/api/mods/v1/cap/{capability}/{action}")
    async def v1_cap_call(request: Request, capability: str, action: str, provider: str = ""):
        """`?provider=<mod id>` calls that mod by name (it must provide the
        capability); otherwise the first provider answers."""
        e = _mod(request)
        raw = await request.body()
        if len(raw) > capabilities.MAX_BODY_BYTES:
            raise HTTPException(413, "the arguments are larger than 64 KB")
        body = await _json(request) if raw.strip() else {}
        user = _owner_for(e, body)
        args = {k: v for k, v in body.items() if k != "user"}
        try:
            return await capabilities.call(e, user or None, capability, action, args, provider[:32] or None)
        except capabilities.CapError as ex:
            return JSONResponse({"detail": ex.message, **ex.extra}, status_code=ex.status)


# ── Admin and UI ─────────────────────────────────────────────────────────

def setup_mods_routes(auth_manager=None) -> APIRouter:
    access.set_auth_manager(auth_manager)
    router = APIRouter(tags=["mods"])
    _setup_v1(router)
    from src.mods.host_api import setup_host_routes
    setup_host_routes(router)

    @router.get("/api/mods")
    async def list_mods(request: Request):
        user = _require_admin(request)
        return {"mods": [_view(m, user or "") for m in sorted(registry.list_mods(), key=lambda m: m["id"])],
                "core_api": mf.CORE_API_VERSION, "permissions": mf.PERMISSIONS,
                "notify": notify.channel_settings(), "nav_groups": placement.groups(),
                "capabilities": capabilities.known_view()}

    @router.post("/api/mods/install/prepare")
    async def install_prepare(request: Request):
        _require_admin(request)
        body = await _json(request)
        try:
            staged = await asyncio.to_thread(registry.prepare, str(body.get("source") or ""), body.get("ref"),
                                             body.get("subdir"))
        except registry.RegistryError as e:
            return _err(e)
        refused = await asyncio.to_thread(_check_blocked, staged, body.get("allow_blocked") is True)
        return refused or staged

    @router.post("/api/mods/install/confirm")
    async def install_confirm(request: Request):
        _require_admin(request)
        body = await _json(request)
        iid = str(body.get("install_id") or "")
        try:
            approved = registry.approved_list(body.get("permissions"))
            meta = registry._staged(iid)
            update_of = meta.get("update_of")
            # A hard requirement nobody provides: say so, and install only when told to.
            missing = capabilities.availability(meta.get("manifest") or {})["missing"]
            if missing and body.get("allow_missing") is not True:
                return JSONResponse({"detail": f"Nothing installed provides {', '.join(missing)}, which this mod "
                                               "needs. Install a provider first, or install it anyway.",
                                     "missing_requires": missing, "errors": []}, status_code=409)
            if update_of:
                await runtime.stop(update_of, "stopped for an update")
            entry = await asyncio.to_thread(registry.confirm, iid, approved)
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
                # The frame URL prefix, for deep links to a page under a tab (#mod/<id>/<tab>?path=...).
                "base": base,
                "tabs": [dict(t, url=base + t["path"], **placement.resolve(t, override)) for t in ui.get("tabs") or []],
                "home_cards": [dict(c, url=base + c["path"]) for c in ui.get("home_cards") or []],
                "settings_url": base + ui["settings_path"] if ui.get("settings_path") else None,
                "tutorial": tutorial.summary(entry, user),
            })
        # `nav`: the tabs in sidebar order, in their groups, hidden ones left out.
        # `replaces`: which mod tab takes over an old built-in page key (#adsb...).
        return {"mods": out, "nav": placement.nav(shown), "groups": placement.groups(),
                "replaces": placement.replacements(shown), "legacy_pages": placement.LEGACY_PAGES}

    # ── Tutorials (src/mods/tutorial.py): any user who may use the mod ──
    def _usable(request: Request, mod_id: str):
        user = proxy.cookie_user(request)
        if user is None:
            raise HTTPException(401, "Not signed in")
        entry = registry.get(mod_id)
        if not entry or not entry.get("enabled") or not access.may_use(entry, user):
            raise HTTPException(404, "No such mod")
        return entry, user

    @router.get("/api/mods/{mod_id}/tutorial")
    async def tutorial_get(request: Request, mod_id: str):
        entry, user = _usable(request, mod_id)
        tut = tutorial.public(entry, user, proxy.frame_base(mod_id, user))
        if not tut:
            raise HTTPException(404, "This mod has no tutorial")
        return {"tutorial": tut}

    @router.post("/api/mods/{mod_id}/tutorial/seen")
    async def tutorial_seen(request: Request, mod_id: str):
        entry, user = _usable(request, mod_id)
        tut = tutorial.of(entry)
        if not tut:
            raise HTTPException(404, "This mod has no tutorial")
        registry.mark_tutorial_seen(mod_id, user, int(tut.get("version") or 1))
        return {"ok": True, "version": tut.get("version") or 1}

    @router.get("/api/mods/{mod_id}/tutorial/image/{step}")
    async def tutorial_image(request: Request, mod_id: str, step: int):
        entry, _user = _usable(request, mod_id)
        hit = tutorial.image_file(entry, step)
        if not hit:
            raise HTTPException(404, "No such image")
        from fastapi.responses import FileResponse
        return FileResponse(hit[0], media_type=hit[1], headers={
            "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox"})

    # ── Finding mods (src/mods/catalog.py). Before /api/mods/{mod_id}. ──
    def _cat_err(e: catalog.CatalogError):
        return JSONResponse({"detail": e.message, "errors": []}, status_code=e.status)

    @router.get("/api/mods/catalog")
    async def catalog_list(request: Request, q: str = "", tag: str = "", verified: str = "",
                           compatible: str = "", refresh: bool = False, capability: str = ""):
        _require_admin(request)
        try:
            return await asyncio.to_thread(catalog.query, q[:200], tag[:40], verified or None, compatible or None,
                                           refresh, capability[:capabilities.MAX_CAP_LEN])
        except catalog.CatalogError as e:
            return _cat_err(e)

    @router.get("/api/mods/catalog/settings")
    async def catalog_get_settings(request: Request):
        _require_admin(request)
        return {"settings": catalog.settings()}

    @router.put("/api/mods/catalog/settings")
    async def catalog_put_settings(request: Request):
        _require_admin(request)
        body = await _json(request)
        try:
            return {"settings": await asyncio.to_thread(catalog.save_settings, body)}
        except catalog.CatalogError as e:
            return _cat_err(e)

    @router.get("/api/mods/catalog/image")
    async def catalog_image(request: Request, u: str = ""):
        _require_admin(request)
        from fastapi.responses import Response
        try:
            body, ctype = await asyncio.to_thread(catalog.fetch_image, u)
        except catalog.CatalogError as e:
            return Response(status_code=e.status if e.status < 500 else 502)
        return Response(body, media_type=ctype, headers={
            "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox"})

    @router.get("/api/mods/catalog/{owner}/{repo}")
    async def catalog_details(request: Request, owner: str, repo: str, ref: str = ""):
        _require_admin(request)
        try:
            return await asyncio.to_thread(catalog.details, owner, repo, ref or None)
        except catalog.CatalogError as e:
            return _cat_err(e)

    # ── Capabilities (src/mods/capabilities.py). Before /api/mods/{mod_id}. ──
    @router.get("/api/mods/capabilities")
    async def capabilities_list(request: Request):
        _require_admin(request)
        return {"capabilities": capabilities.known_view()}

    @router.get("/api/mods/capabilities/log")
    async def capabilities_log(request: Request, mod: str = "", limit: int = 200):
        _require_admin(request)
        return {"log": await asyncio.to_thread(capabilities.read_log, max(1, min(limit, 1000)), mod or None)}

    @router.get("/api/mods/{mod_id}")
    async def get_mod(request: Request, mod_id: str):
        user = _require_admin(request)
        e = registry.get(mod_id)
        if not e:
            raise HTTPException(404, "No such mod")
        return {"mod": _view(e, user or "")}

    @router.get("/api/mods/{mod_id}/dependents")
    async def get_dependents(request: Request, mod_id: str):
        _require_admin(request)
        if not registry.get(mod_id):
            raise HTTPException(404, "No such mod")
        return {"dependents": capabilities.dependents(mod_id)}

    @router.post("/api/mods/{mod_id}/{action}")
    async def mod_action(request: Request, mod_id: str, action: str, force: bool = False):
        user = _require_admin(request)
        e = registry.get(mod_id)
        if not e:
            raise HTTPException(404, "No such mod")
        try:
            if action == "enable":
                registry.set_enabled(mod_id, True)
                await _after_enable(mod_id)
            elif action == "disable":
                refused = _dependents_refusal(mod_id, "Disabling", force)
                if refused:
                    return refused
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
                refused = await asyncio.to_thread(_check_blocked, staged, body.get("allow_blocked") is True)
                return refused or staged
            elif action == "access":
                body = await _json(request)
                registry.set_access(mod_id, str(body.get("access") or ""))
            elif action == "placement":
                body = await _json(request)
                registry.set_placement(mod_id, body.get("section"), body.get("hidden"))
            elif action == "mute":
                body = await _json(request)
                registry._update(mod_id, notify_muted=bool(body.get("muted")))
            elif action == "grants":
                # This admin's grant to the mod for one capability, like extra
                # numbers it may text: {"capability": "messaging.sms", "numbers": [...]}.
                body = await _json(request)
                cap = str(body.get("capability") or "")
                if cap not in capabilities.declared(e.get("manifest") or {}):
                    raise HTTPException(400, f"This mod does not use {cap or 'that capability'}")
                try:
                    capabilities.set_grant(mod_id, user or "", cap, body)
                except capabilities.CapError as ex:
                    raise HTTPException(ex.status, ex.message)
            else:
                raise HTTPException(404, "Unknown action")
        except registry.RegistryError as ex:
            return _err(ex)
        return {"mod": _view(registry.get(mod_id), user or "")}

    @router.delete("/api/mods/{mod_id}")
    async def delete_mod(request: Request, mod_id: str, keep_data: bool = True, remove_images: bool = False,
                         force: bool = False):
        """Remove a mod. keep_data (the default) keeps its data folder and, for
        a docker mod, its volumes; remove_images also deletes the images built
        for a docker mod (`compose down --rmi local`). Other mods that need a
        capability only it provides make this a 409 unless `force`."""
        _require_admin(request)
        if not registry.get(mod_id):
            raise HTTPException(404, "No such mod")
        refused = _dependents_refusal(mod_id, "Removing", force)
        if refused:
            return refused
        await runtime.stop(mod_id, "removed")
        await runtime.remove_docker(mod_id, images=remove_images, volumes=not keep_data)
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
