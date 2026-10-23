"""The legacy settings handoff: how a mod gets the settings its feature had
while it was still built into the core, so nobody retypes them.

A manifest may say

    "imports": ["sms", "telephony", "meet"]

Each name is a slice from SLICES below: a fixed list of the exact settings
files and keys the core had for that feature. Anything else is a manifest
error, so a mod cannot ask for "everything" or for another mod's settings.
Installing or updating a mod shows its slices (the install review) and the
admin who confirms grants them (`granted_imports` in the registry).

The mod then calls, with its own bearer token (src/mods/routes.py `_mod`):

    GET  /api/mods/v1/legacy            its slices and whether each can still be read
    POST /api/mods/v1/legacy/export     {"slice": "sms"}  the slice's settings, secrets decrypted
    POST /api/mods/v1/legacy/imported   {"slice": "sms"}  the mod has stored them: close the slice

Rules:

* Only a slice the mod declared and was granted. Another slice is a 403.
* Secrets (SLICES[...]["secrets"]) are decrypted here, because the core owns
  the key (src/secret_storage.py); the mod re-encrypts them with its own. They
  travel only on this answer, which is `Cache-Control: no-store`, and only over
  the local channel: a request that did not come straight from this machine
  (a proxy header, a non-loopback peer) is refused.
* One time: once the mod marks a slice imported, `export` answers 410. An
  admin can open it again (`POST /api/mods/<id>/legacy/<slice>/reopen`), for a
  mod that was reinstalled; reinstalling without keeping the data does that
  by itself.
* Every call is one line in mods/legacy-import-log.jsonl: the mod, the slice,
  how many users, which settings and which secret fields were present. Never a
  value. Nothing here writes a secret to disk, and nothing logs one.
* Reading only: the core's own files are never changed.

The core's secret store works without importing the rest of the app (the
fallback below reads the key file directly), so a broken import elsewhere
cannot skip this.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.mods import paths

logger = logging.getLogger(__name__)

_lock = threading.RLock()
LOG_MAX_BYTES = 1024 * 1024

# slice -> what it is. `source`: "prefs" (the per-user store, user_prefs.json,
# the keys in `keys`) or "file" (one JSON file in the data folder).
# `secrets`: paths inside a key (prefs) or the file, decrypted on the way out.
SLICES: Dict[str, Dict[str, Any]] = {
    "sms": {
        "title": "Texting settings",
        "description": "Your allowed numbers, the reply URL (it can hold the SMS gateway's password), the forward "
                       "secret's hash and each number's conversation",
        "source": "prefs",
        "keys": ["sms_gateway"],
        "secrets": [],
    },
    "telephony": {
        "title": "Phone call settings",
        "description": "The Twilio account, the free SIP line and the call PIN, with the Twilio auth token and "
                       "the SIP password (decrypted here, re-encrypted by the mod)",
        "source": "prefs",
        "keys": ["phone_calls"],
        "secrets": [("phone_calls", "auth_token"), ("phone_calls", "sip", "password")],
    },
    "meet": {
        "title": "Google Meet settings",
        "description": "How the agent joins a meeting, and the chat of each meeting",
        "source": "prefs",
        "keys": ["google_meet", "google_meet_chats"],
        "secrets": [],
    },
    "games": {
        "title": "Games page settings",
        "description": "Sunshine's addresses and login (the password decrypted here, re-encrypted by the mod), "
                       "Moonlight Web's address, the pinned certificates and the Steam sync settings",
        "source": "file",
        "file": "games.json",
        "keys": ["sunshine_urls", "username", "password", "credentials_file", "moonlight_url",
                 "moonlight_health_url", "play_in", "covers_dir", "pc_host", "pc_covers_dir",
                 "steam_libraries", "nightly_sync", "tls", "pins", "last_sync"],
        "secrets": [("password",)],
    },
}

MAX_SLICES = 8


# ── The manifest ─────────────────────────────────────────────────────────

def check(raw: Dict[str, Any], errors: List[str]) -> List[str]:
    """`imports` of a manifest: slice names the core knows."""
    val = raw.get("imports")
    if val is None:
        return []
    if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
        errors.append("`imports` must be a list of legacy settings slices like \"sms\"")
        return []
    bad = sorted({s for s in val if s not in SLICES})
    if bad:
        errors.append(f"unknown `imports` slice(s): {', '.join(bad)} (known: {', '.join(sorted(SLICES))})")
    if len(val) > MAX_SLICES:
        errors.append(f"`imports` lists at most {MAX_SLICES} slices")
    return sorted({s for s in val if s in SLICES})[:MAX_SLICES]


def review(manifest: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """What the install review shows: each slice the mod will import, and for
    an update which are new (not granted before)."""
    granted = set((existing or {}).get("granted_imports") or [])
    out = []
    for s in manifest.get("imports") or []:
        spec = SLICES.get(s)
        if not spec:
            continue
        out.append({"slice": s, "title": spec["title"], "description": spec["description"],
                    "secrets": ["/".join(p) for p in spec["secrets"]],
                    "new": bool(existing) and s not in granted})
    return out


# ── State: which slices a mod has closed ─────────────────────────────────

def _state_file() -> str:
    return os.path.join(paths.mods_root(), "legacy-imports.json")


def _load() -> Dict[str, Any]:
    try:
        with open(_state_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    path = _state_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def imported_at(entry: Dict[str, Any], slice_: str) -> Optional[float]:
    """When the mod closed the slice, unless it was reinstalled since."""
    rec = ((_load().get(entry["id"]) or {}).get(slice_)) or None
    if not rec or rec.get("installed_at") != entry.get("installed_at"):
        return None
    return rec.get("at")


def _mark(entry: Dict[str, Any], slice_: str) -> float:
    now = time.time()
    with _lock:
        data = _load()
        data.setdefault(entry["id"], {})[slice_] = {"at": now, "installed_at": entry.get("installed_at")}
        _save(data)
    return now


def _reopen(mod_id: str, slice_: str) -> bool:
    with _lock:
        data = _load()
        if slice_ not in (data.get(mod_id) or {}):
            return False
        del data[mod_id][slice_]
        _save(data)
    return True


# ── The audit log (never a value) ────────────────────────────────────────

def _log_file() -> str:
    return os.path.join(paths.mods_root(), "legacy-import-log.jsonl")


def audit(mod_id: str, slice_: str, event: str, **info: Any) -> None:
    line = {"t": round(time.time(), 3), "mod": mod_id, "slice": slice_, "event": event}
    line.update(info)
    try:
        path = _log_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError as e:
        logger.warning("[mods] legacy import log: %s", e)
    try:
        from src.mods import runtime
        runtime.log_line(mod_id, f"[odysseus] legacy settings: {event} {slice_}")
    except Exception:
        pass


# ── Reading the core's settings ──────────────────────────────────────────

def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _decrypt(value: str) -> str:
    """The core's own decryption (src/secret_storage.py). Where that module
    cannot be imported (a circular import when the app is half loaded), the
    same Fernet key file is read directly. "" when it cannot be read."""
    if not value or not value.startswith("enc:"):
        return value or ""
    try:
        from src import secret_storage
        return secret_storage.decrypt(value)
    except Exception:
        pass
    try:
        from cryptography.fernet import Fernet
        with open(os.path.join(_data_dir(), ".app_key"), "rb") as f:
            key = f.read().strip()
        return Fernet(key).decrypt(value[4:].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _walk(d: Any, path: Tuple[str, ...]) -> Tuple[Optional[Dict], Optional[str]]:
    for k in path[:-1]:
        d = d.get(k) if isinstance(d, dict) else None
        if d is None:
            return None, None
    return (d, path[-1]) if isinstance(d, dict) else (None, None)


def _decrypt_secrets(container: Dict[str, Any], paths_: List[Tuple[str, ...]], prefix: str,
                     present: List[str], unreadable: List[str]) -> None:
    for p in paths_:
        parent, last = _walk(container, p)
        if not parent or not parent.get(last):
            continue
        name = (prefix + "/" if prefix else "") + "/".join(p)
        plain = _decrypt(str(parent[last]))
        if plain:
            parent[last] = plain
            present.append(name)
        else:
            parent[last] = ""
            unreadable.append(name)


def read_slice(slice_: str) -> Dict[str, Any]:
    """The slice's settings as the core had them, secrets decrypted.
    {"data": ..., "present": [keys], "secrets": [fields set], "unreadable": [fields],
    "users": n}. Nothing is written."""
    spec = SLICES[slice_]
    present: List[str] = []
    secret_names: List[str] = []
    unreadable: List[str] = []
    users = 0
    if spec["source"] == "prefs":
        raw = _read_json(os.path.join(_data_dir(), "user_prefs.json"))
        raw = raw if isinstance(raw, dict) else {}
        if isinstance(raw.get("_users"), dict):
            owners = {str(u): p for u, p in raw["_users"].items() if isinstance(p, dict)}
        else:
            owners = {"local": raw}
        out_users: Dict[str, Any] = {}
        for user, prefs in owners.items():
            mine = {k: copy.deepcopy(prefs[k]) for k in spec["keys"] if k in prefs and prefs[k] not in (None, "", {}, [])}
            if not mine:
                continue
            _decrypt_secrets(mine, [p for p in spec["secrets"]], "", secret_names, unreadable)
            out_users[user] = mine
            present += [k for k in mine if k not in present]
        users = len(out_users)
        data: Any = {"_users": out_users} if out_users else {}
    else:
        raw = _read_json(os.path.join(_data_dir(), spec["file"]))
        raw = raw if isinstance(raw, dict) else {}
        data = {k: copy.deepcopy(raw[k]) for k in spec["keys"] if k in raw and raw[k] is not None}
        _decrypt_secrets(data, [p for p in spec["secrets"]], "", secret_names, unreadable)
        present = list(data)
        users = 1 if data else 0
    return {"data": data, "present": sorted(present), "secrets": sorted(set(secret_names)),
            "unreadable": sorted(set(unreadable)), "users": users}


def available(slice_: str) -> bool:
    """Whether the core has anything for the slice, without decrypting it."""
    spec = SLICES[slice_]
    if spec["source"] == "prefs":
        raw = _read_json(os.path.join(_data_dir(), "user_prefs.json"))
        if not isinstance(raw, dict):
            return False
        owners = [p for p in (raw.get("_users") or {}).values()] if isinstance(raw.get("_users"), dict) else [raw]
        return any(isinstance(p, dict) and any(p.get(k) for k in spec["keys"]) for p in owners)
    raw = _read_json(os.path.join(_data_dir(), spec["file"]))
    return isinstance(raw, dict) and any(raw.get(k) for k in spec["keys"])


# ── The local channel ────────────────────────────────────────────────────

_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded", "tailscale-user-login",
                  "tailscale-funnel-request", "cf-connecting-ip")


def is_local(request: Request) -> bool:
    """The request came straight from this machine, not through a proxy,
    Tailscale Serve or Funnel."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


# ── The routes ───────────────────────────────────────────────────────────

def _entry_slice(request: Request, body: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    from src.mods import routes as mod_routes
    entry = mod_routes._mod(request)
    if not is_local(request):
        raise HTTPException(403, "Legacy settings are handed over on the local channel only")
    name = str(body.get("slice") or "")
    if name not in SLICES:
        raise HTTPException(404, "No such settings slice")
    declared = set((entry.get("manifest") or {}).get("imports") or [])
    granted = set(entry.get("granted_imports") or [])
    if name not in declared or name not in granted:
        audit(entry["id"], name, "refused")
        raise HTTPException(403, "This mod was not granted that settings slice")
    return entry, name


def _no_store(payload: Dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def setup_legacy_routes(router: APIRouter) -> None:
    P = "/api/mods/v1/legacy"

    @router.get(P)
    async def v1_legacy(request: Request):
        from src.mods import routes as mod_routes
        entry = mod_routes._mod(request)
        granted = set(entry.get("granted_imports") or [])
        out = []
        for s in (entry.get("manifest") or {}).get("imports") or []:
            when = imported_at(entry, s)
            out.append({"slice": s, "title": SLICES[s]["title"], "granted": s in granted,
                        "imported": bool(when), "imported_at": when,
                        "available": (s in granted and not when and available(s))})
        return {"slices": out}

    @router.post(P + "/export")
    async def v1_legacy_export(request: Request):
        from src.mods import routes as mod_routes
        body = await mod_routes._json(request)
        entry, name = _entry_slice(request, body)
        if imported_at(entry, name):
            raise HTTPException(410, "This slice was already imported; an admin can open it again")
        got = await asyncio.to_thread(read_slice, name)
        audit(entry["id"], name, "export", users=got["users"], settings=got["present"],
              secrets=got["secrets"], unreadable=got["unreadable"])
        return _no_store({"slice": name, "title": SLICES[name]["title"], "data": got["data"],
                          "present": got["present"], "secrets": got["secrets"],
                          "unreadable": got["unreadable"], "users": got["users"]})

    @router.post(P + "/imported")
    async def v1_legacy_imported(request: Request):
        from src.mods import routes as mod_routes
        body = await mod_routes._json(request)
        entry, name = _entry_slice(request, body)
        when = _mark(entry, name)
        audit(entry["id"], name, "imported")
        return _no_store({"ok": True, "slice": name, "imported_at": when})

    @router.get("/api/mods/{mod_id}/legacy")
    async def legacy_status(request: Request, mod_id: str):
        from src.mods import registry, routes as mod_routes
        mod_routes._require_admin(request)
        entry = registry.get(mod_id)
        if not entry:
            raise HTTPException(404, "No such mod")
        return {"slices": [{"slice": s, "title": SLICES[s]["title"],
                            "granted": s in (entry.get("granted_imports") or []),
                            "imported_at": imported_at(entry, s), "available": available(s)}
                           for s in (entry.get("manifest") or {}).get("imports") or []]}

    @router.post("/api/mods/{mod_id}/legacy/{slice_name}/reopen")
    async def legacy_reopen(request: Request, mod_id: str, slice_name: str):
        from src.mods import registry, routes as mod_routes
        mod_routes._require_admin(request)
        if not registry.get(mod_id) or slice_name not in SLICES:
            raise HTTPException(404, "No such mod or slice")
        done = _reopen(mod_id, slice_name)
        audit(mod_id, slice_name, "reopened" if done else "reopen-noop")
        return {"ok": True, "reopened": done}
