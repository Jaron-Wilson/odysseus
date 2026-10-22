"""Installed mods: install (from a git URL or a local folder), approve, enable,
disable, update, remove, and each mod's token.

Installing and updating are two steps, so the user sees what a mod asks for
before any of its code runs:

  1. prepare(source)  fetches the mod into mods/.staging/<n>/, reads and
     checks its manifest, and returns the permissions it wants (and, for an
     update, which of them are new). Nothing from the mod runs here.
  2. confirm(n, approved_permissions)  must be given exactly the requested
     permissions. Only then is the code moved into place, its setup commands
     run, and (for a new mod) a token made.

The registry file holds each mod's token encrypted (src/secret_storage.py)
and is written 0600.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from src.mods import manifest as mf
from src.mods import paths
from src.mods import signing

logger = logging.getLogger(__name__)

_lock = threading.RLock()
STAGING_TTL_S = 3600
MAX_MOD_BYTES = 200 * 1024 * 1024
GIT_TIMEOUT_S = 300
SETUP_TIMEOUT_S = 900
_COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", "*.pyc",
                                      ".pytest_cache", ".mypy_cache", ".ruff_cache")
_GIT_URL_RE = re.compile(r"^(?:https?://|ssh://|git://|file://|git@[A-Za-z0-9.-]+:)[^\s]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SUBDIR_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")

# token -> mod id, rebuilt when the registry changes
_token_index: Optional[Dict[str, str]] = None


class RegistryError(Exception):
    def __init__(self, message: str, status: int = 400, details: Optional[List[str]] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or []


# ── Storage ──────────────────────────────────────────────────────────────

def _load() -> Dict[str, Any]:
    try:
        with open(paths.registry_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("mods", {})
            data.setdefault("settings", {})
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"mods": {}, "settings": {}}


def _save(data: Dict[str, Any]) -> None:
    global _token_index
    os.makedirs(paths.mods_root(), exist_ok=True)
    p = paths.registry_file()
    tmp = p + f".tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)
    _token_index = None


def _encrypt(token: str) -> str:
    try:
        from src.secret_storage import encrypt
        return encrypt(token)
    except Exception:
        return token


def _decrypt(value: str) -> str:
    try:
        from src.secret_storage import decrypt
        return decrypt(value)
    except Exception:
        return value


def list_mods() -> List[Dict[str, Any]]:
    with _lock:
        return [dict(m) for m in _load()["mods"].values()]


def get(mod_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        m = _load()["mods"].get(mod_id)
        return dict(m) if m else None


def token_of(mod_id: str) -> str:
    m = get(mod_id)
    return _decrypt(m.get("token", "")) if m else ""


def mod_for_token(token: str) -> Optional[str]:
    """The id of the mod a bearer token belongs to, or None."""
    global _token_index
    if not token or not token.startswith("odm_"):
        return None
    with _lock:
        if _token_index is None:
            _token_index = {}
            for mid, m in _load()["mods"].items():
                t = _decrypt(m.get("token", ""))
                if t:
                    _token_index[t] = mid
        import hmac
        for t, mid in _token_index.items():
            if hmac.compare_digest(t, token):
                return mid
    return None


def _update(mod_id: str, **fields) -> Dict[str, Any]:
    with _lock:
        data = _load()
        m = data["mods"].get(mod_id)
        if not m:
            raise RegistryError(f"no mod {mod_id!r}", 404)
        m.update(fields)
        _save(data)
        return dict(m)


def get_settings() -> Dict[str, Any]:
    """Core-wide mod settings (notification channels)."""
    with _lock:
        return dict(_load().get("settings") or {})


def save_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        data = _load()
        data["settings"] = values
        _save(data)
        return dict(values)


# ── Fetching ─────────────────────────────────────────────────────────────

def _git(args: List[str], cwd: Optional[str] = None, timeout: int = GIT_TIMEOUT_S) -> str:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "SSH_AUTH_SOCK", "TMPDIR")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise RegistryError("git is not installed on the server")
    except subprocess.TimeoutExpired:
        raise RegistryError("git timed out")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise RegistryError("git failed: " + (msg[-1] if msg else f"exit {r.returncode}"))
    return r.stdout.strip()


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.lstat(os.path.join(root, fn)).st_size
            except OSError:
                pass
        if total > MAX_MOD_BYTES:
            break
    return total


def parse_source(source: str, ref: Optional[str], subdir: Optional[str]) -> Dict[str, Any]:
    source = (source or "").strip()
    ref = (ref or "").strip() or None
    subdir = (subdir or "").strip().strip("/") or None
    if ref and not _REF_RE.match(ref):
        raise RegistryError("the branch or tag has characters that are not allowed")
    if subdir and (not _SUBDIR_RE.match(subdir) or ".." in subdir.split("/")):
        raise RegistryError("the folder inside the repository is not a plain relative path")
    if not source:
        raise RegistryError("give a git URL or a folder on the server")
    if _GIT_URL_RE.match(source):
        if source.startswith("-"):
            raise RegistryError("not a git URL")
        return {"type": "git", "url": source, "ref": ref, "subdir": subdir}
    path = os.path.abspath(os.path.expanduser(source))
    if os.path.isdir(path):
        if ref:
            raise RegistryError("a branch or tag only applies to a git URL")
        return {"type": "path", "path": path, "subdir": subdir}
    raise RegistryError("that is neither a git URL (https://, ssh://, git@, file://) nor a folder on the server")


def _fetch(src: Dict[str, Any], dest: str) -> Dict[str, Any]:
    """Put the mod's files at `dest`. Returns {commit} for git sources."""
    info: Dict[str, Any] = {}
    if src["type"] == "path":
        base = os.path.join(src["path"], src["subdir"]) if src.get("subdir") else src["path"]
        if not os.path.isdir(base):
            raise RegistryError(f"no folder {src.get('subdir')!r} in {src['path']}")
        real, root = os.path.realpath(base), os.path.realpath(paths.mods_root())
        if real == root or real.startswith(root + os.sep) or root.startswith(real + os.sep):
            raise RegistryError("that folder is, or holds, the installed mods folder")
        if _dir_size(base) > MAX_MOD_BYTES:
            raise RegistryError("the mod folder is larger than 200 MB")
        shutil.copytree(base, dest, ignore=_COPY_IGNORE, symlinks=True)
        return info
    clone = dest + ".clone"
    args = ["clone", "--depth", "1", "--quiet"]
    if src.get("ref"):
        args += ["--branch", src["ref"]]
    if src.get("subdir"):
        args += ["--filter=blob:none", "--sparse"]
    _git(args + ["--", src["url"], clone])
    try:
        if src.get("subdir"):
            _git(["sparse-checkout", "set", src["subdir"]], cwd=clone)
        info["commit"] = _git(["rev-parse", "HEAD"], cwd=clone)[:12]
        base = os.path.join(clone, src["subdir"]) if src.get("subdir") else clone
        if not os.path.isdir(base):
            raise RegistryError(f"no folder {src['subdir']!r} in the repository")
        if _dir_size(base) > MAX_MOD_BYTES:
            raise RegistryError("the mod is larger than 200 MB")
        shutil.copytree(base, dest, ignore=_COPY_IGNORE, symlinks=True)
    finally:
        shutil.rmtree(clone, ignore_errors=True)
    return info


def _sweep_staging() -> None:
    root = paths.staging_root()
    if not os.path.isdir(root):
        return
    now = time.time()
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if now - os.path.getmtime(p) > STAGING_TTL_S:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


# ── Install / update, step 1 ─────────────────────────────────────────────

def prepare(source: str, ref: Optional[str] = None, subdir: Optional[str] = None,
            update_of: Optional[str] = None) -> Dict[str, Any]:
    """Fetch and check a mod without running any of it. See the module doc."""
    _sweep_staging()
    existing = get(update_of) if update_of else None
    if update_of and not existing:
        raise RegistryError(f"no mod {update_of!r}", 404)
    src = parse_source(source, ref, subdir)
    sid = uuid.uuid4().hex[:16]
    stage = os.path.join(paths.staging_root(), sid)
    os.makedirs(stage, exist_ok=True)
    try:
        info = _fetch(src, os.path.join(stage, "src"))
        try:
            manifest = mf.load(os.path.join(stage, "src"))
        except mf.ManifestError as e:
            raise RegistryError("the mod's manifest has problems", details=e.errors)
        if update_of and manifest["id"] != update_of:
            raise RegistryError(f"that is mod {manifest['id']!r}, not {update_of!r}")
        if not update_of and get(manifest["id"]):
            raise RegistryError(f"mod {manifest['id']!r} is already installed; update it instead", 409)
        granted = set(existing.get("granted_permissions") or []) if existing else set()
        meta = {
            "install_id": sid,
            "created": time.time(),
            "source": src,
            "commit": info.get("commit"),
            "manifest": manifest,
            "update_of": update_of,
        }
        with open(os.path.join(stage, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {
        "install_id": sid,
        "id": manifest["id"],
        "name": manifest["name"],
        "version": manifest["version"],
        "previous_version": existing.get("version") if existing else None,
        "description": manifest["description"],
        "author": manifest["author"],
        "runtime": manifest["runtime"]["type"],
        "setup": manifest["runtime"]["setup"],
        "command": manifest["runtime"].get("command") or [],
        "source": src,
        "commit": info.get("commit"),
        "permissions": mf.describe_permissions(manifest["permissions"]),
        "new_permissions": sorted(set(manifest["permissions"]) - granted) if existing else manifest["permissions"],
        "ui": manifest["ui"],
        "tools": bool(manifest["tools"]),
        "tutorial": _tutorial_summary(manifest.get("tutorial")),
        "warnings": manifest.get("warnings") or [],
        # What it provides, needs (`missing`: nobody provides it yet) and may
        # use (src/mods/capabilities.py); for an update, what it newly uses.
        "capabilities": _capabilities_review(manifest, existing),
    }


def _capabilities_review(manifest: Dict[str, Any], existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    from src.mods import capabilities
    out = capabilities.availability(manifest)
    before = set(capabilities.declared((existing or {}).get("manifest") or {})) if existing else set()
    out["new"] = [c for c in capabilities.declared(manifest) if c not in before] if existing else []
    return out


def _staged(install_id: str) -> Dict[str, Any]:
    if not re.match(r"^[0-9a-f]{16}$", install_id or ""):
        raise RegistryError("unknown install", 404)
    p = os.path.join(paths.staging_root(), install_id, "meta.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RegistryError("that install expired or was already used; start again", 404)


def discard(install_id: str) -> None:
    _staged(install_id)
    shutil.rmtree(os.path.join(paths.staging_root(), install_id), ignore_errors=True)


def _run_setup(mod_id: str, manifest: Dict[str, Any]) -> None:
    """The manifest's setup commands, in the mod folder, with the mod's env."""
    from src.mods import runtime
    cmds = manifest["runtime"].get("setup") or []
    if not cmds:
        return
    log = paths.log_file(mod_id)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    env = runtime.base_env(mod_id, port=0)
    with open(log, "a", encoding="utf-8") as lf:
        for cmd in cmds:
            argv = runtime.resolve_command(mod_id, list(cmd), setup=True)
            lf.write(f"[odysseus] setup: {' '.join(argv)}\n")
            lf.flush()
            try:
                r = subprocess.run(argv, cwd=paths.src_dir(mod_id), env=env, stdout=lf, stderr=subprocess.STDOUT,
                                   timeout=SETUP_TIMEOUT_S, stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                raise RegistryError(f"setup command not found: {argv[0]}")
            except subprocess.TimeoutExpired:
                raise RegistryError(f"setup command timed out: {' '.join(argv)}")
            if r.returncode != 0:
                raise RegistryError(f"setup command failed (exit {r.returncode}): {' '.join(argv)}; see the mod's log")


# ── Install / update, step 2 ─────────────────────────────────────────────

def approved_list(approved: Any) -> List[str]:
    """The `permissions` of a confirm: a list of permission id strings."""
    if approved is None:
        return []
    if not isinstance(approved, list) or not all(isinstance(p, str) for p in approved):
        raise RegistryError('permissions must be a list of permission ids (strings), like ["notify", "storage"]')
    return approved


def confirm(install_id: str, approved: List[str]) -> Dict[str, Any]:
    """Approve a prepared install or update. `approved` must equal the
    requested permissions (the user saw and accepted all of them)."""
    from src.mods import runtime
    approved = approved_list(approved)
    meta = _staged(install_id)
    manifest = meta["manifest"]
    if sorted(set(approved)) != sorted(manifest["permissions"]):
        raise RegistryError("approve every permission the mod asks for, or cancel the install", 400)
    mod_id = manifest["id"]
    stage_src = os.path.join(paths.staging_root(), install_id, "src")
    now = time.time()
    with _lock:
        data = _load()
        existing = data["mods"].get(mod_id)
        if meta.get("update_of") is None and existing:
            raise RegistryError(f"mod {mod_id!r} is already installed", 409)
        if meta.get("update_of") and not existing:
            raise RegistryError(f"mod {mod_id!r} was removed", 404)
        os.makedirs(paths.mod_dir(mod_id), exist_ok=True)
        os.makedirs(paths.data_dir(mod_id), exist_ok=True)
        dest = paths.src_dir(mod_id)
        old = dest + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(dest):
            os.replace(dest, old)
        os.replace(stage_src, dest)
        shutil.rmtree(os.path.join(paths.staging_root(), install_id), ignore_errors=True)
        entry = dict(existing or {})
        entry.update({
            "id": mod_id,
            "name": manifest["name"],
            "version": manifest["version"],
            "manifest": manifest,
            "source": meta["source"],
            "commit": meta.get("commit"),
            "granted_permissions": list(manifest["permissions"]),
            "updated_at": now,
        })
        if not existing:
            entry.update({"installed_at": now, "enabled": False, "access": "admins",
                          "token": _encrypt(signing.new_token()),
                          "compose_project": runtime.new_compose_project(mod_id)})
        elif not existing.get("compose_project"):
            # Installed before compose projects had an instance id: a docker mod
            # ran as odysseus-mod-<id> and keeps that (its volumes are there).
            was_docker = ((existing.get("manifest") or {}).get("runtime") or {}).get("type") == "docker"
            entry["compose_project"] = (runtime.LEGACY_PROJECT_PREFIX + mod_id if was_docker
                                        else runtime.new_compose_project(mod_id))
        # The next start of a docker mod rebuilds its images from this code.
        entry["build_pending"] = manifest["runtime"]["type"] == "docker"
        data["mods"][mod_id] = entry
        _save(data)
    try:
        _run_setup(mod_id, manifest)
    except RegistryError as e:
        _update(mod_id, setup_error=e.message, enabled=False)
        raise
    shutil.rmtree(paths.src_dir(mod_id) + ".old", ignore_errors=True)
    out = _update(mod_id, setup_error=None)
    logger.info("[mods] %s %s %s", "updated" if existing else "installed", mod_id, manifest["version"])
    return out


def set_enabled(mod_id: str, enabled: bool) -> Dict[str, Any]:
    return _update(mod_id, enabled=bool(enabled))


def set_access(mod_id: str, access: str) -> Dict[str, Any]:
    if access not in ("admins", "users"):
        raise RegistryError("access must be admins or users")
    return _update(mod_id, access=access)


def set_placement(mod_id: str, section: Any = None, hidden: Any = False) -> Dict[str, Any]:
    """The user's choice of where the mod's tabs go (src/mods/placement.py).
    An empty section means the manifest's own."""
    from src.mods import placement
    try:
        value = placement.clean_override(section, hidden)
    except ValueError as e:
        raise RegistryError(str(e))
    return _update(mod_id, placement=value)


def mark_tutorial_seen(mod_id: str, user: str, version: int) -> Dict[str, Any]:
    """Remember that `user` saw (or skipped) version `version` of the mod's
    tutorial (src/mods/tutorial.py). Kept across updates."""
    with _lock:
        data = _load()
        m = data["mods"].get(mod_id)
        if not m:
            raise RegistryError(f"no mod {mod_id!r}", 404)
        seen = dict(m.get("tutorial_seen") or {})
        key = str(user or "")[:120]
        seen[key] = max(int(version or 1), int(seen.get(key) or 0))
        if len(seen) > 5000:   # a bound, not a real limit
            seen = dict(list(seen.items())[-5000:])
        m["tutorial_seen"] = seen
        _save(data)
        return dict(m)


def rotate_token(mod_id: str) -> Dict[str, Any]:
    return _update(mod_id, token=_encrypt(signing.new_token()), token_rotated_at=time.time())


def remove(mod_id: str, keep_data: bool = False) -> None:
    with _lock:
        data = _load()
        if mod_id not in data["mods"]:
            raise RegistryError(f"no mod {mod_id!r}", 404)
        data["mods"].pop(mod_id)
        _save(data)
    d = paths.mod_dir(mod_id)
    if keep_data:
        shutil.rmtree(paths.src_dir(mod_id), ignore_errors=True)
    else:
        shutil.rmtree(d, ignore_errors=True)
    logger.info("[mods] removed %s", mod_id)


def public(entry: Dict[str, Any]) -> Dict[str, Any]:
    """What the UI may see of a registry entry (never the token)."""
    man = entry.get("manifest") or {}
    return {
        "id": entry["id"],
        "name": entry.get("name"),
        "version": entry.get("version"),
        "description": man.get("description", ""),
        "author": man.get("author", ""),
        "homepage": man.get("homepage", ""),
        "enabled": bool(entry.get("enabled")),
        "access": entry.get("access", "admins"),
        "source": entry.get("source"),
        "commit": entry.get("commit"),
        "installed_at": entry.get("installed_at"),
        "updated_at": entry.get("updated_at"),
        "token_rotated_at": entry.get("token_rotated_at"),
        "setup_error": entry.get("setup_error"),
        "runtime": {k: man.get("runtime", {}).get(k) for k in ("type", "start", "health", "idle_stop_minutes")},
        "permissions": mf.describe_permissions(entry.get("granted_permissions") or []),
        "ui": man.get("ui") or {},
        "placement": {"section": None, "hidden": False, **(entry.get("placement") or {})},
        "warnings": man.get("warnings") or [],
        "tools": man.get("tools"),
        "events": man.get("events"),
        "tutorial": _tutorial_summary(man.get("tutorial")),
    }


def _tutorial_summary(tut: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not tut:
        return None
    return {"version": tut.get("version") or 1, "steps": len(tut.get("steps") or []), "has_page": bool(tut.get("path"))}


def python_for(mod_id: str) -> str:
    """The mod's own virtualenv python if it has one, else the core's."""
    src = paths.src_dir(mod_id)
    for cand in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe", "venv/Scripts/python.exe"):
        p = os.path.join(src, cand)
        if os.path.exists(p):
            return p
    return sys.executable
