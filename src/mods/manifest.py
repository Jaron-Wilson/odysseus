"""The mod manifest, odysseus-mod.json: what it may contain, and checking it.

A manifest is untrusted input (it comes from whatever repository the user
pointed at), so everything here is strict: unknown permissions are refused,
ids and paths are limited to safe characters, and nothing is executed. The
full format is documented in docs/mods.md.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

MANIFEST_FILE = "odysseus-mod.json"

# The mod API version this core speaks. A manifest's `min_core` is compared
# against it, not against the Odysseus release number.
CORE_API_VERSION = "0.1"
MANIFEST_VERSION = 1

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
# Ids that would collide with the core's own routes under /api/mods.
RESERVED_IDS = {"v1", "v2", "install", "settings", "events", "scaffold", "ui", "core", "odysseus", "catalog"}
# Paths the core requests on the mod's server: absolute, no "..", no scheme.
PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
SUB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
EVENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
# A subscription pattern: "<mod id or *>/<event type glob>" or a bare type glob.
EVENT_PATTERN_RE = re.compile(r"^(?:[a-z0-9*][a-z0-9*-]{0,31}/)?[a-z0-9*][a-z0-9_.*-]{0,63}$")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]+)?$")
TAG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,31}$")
MAX_TAGS = 10
MAX_SCREENSHOTS = 8
HTTPS_URL_RE = re.compile(r"^https://[^\s\"'<>]{1,290}$")

# Suggested tags for `tags` (docs/mods.md, "Tags"). Other tags are allowed;
# these are the ones the Browse view always offers as filters.
SUGGESTED_TAGS: Dict[str, str] = {
    "3d-printing": "3D printers, slicing, print monitoring",
    "hardware": "Devices, sensors, microcontrollers",
    "home": "Home automation: lights, doors, climate",
    "notifications": "Alerts and messaging",
    "media": "Music, video, photos, streaming",
    "games": "Games and game servers",
    "dev-tools": "Code, builds, deploys, servers",
    "ai": "Models, agents, local AI",
    "productivity": "Tasks, notes, calendars, email",
    "monitoring": "Status pages, logs, uptime",
    "network": "Network tools, DNS, VPN",
    "radio": "SDR, ADS-B, AIS, ham radio",
    "car": "Vehicles, OBD2, dashboards",
    "finance": "Money, budgets, markets",
}

# Every permission a mod can ask for, with the words shown at install time.
PERMISSIONS: Dict[str, str] = {
    "notify": "Send you notifications (web push, the app, and any channels you turned on for mods)",
    "events.emit": "Publish events that other mods and scheduled tasks can react to",
    "events.subscribe": "Receive events published by other mods",
    "devices.read": "See the list of your linked devices",
    "devices.control": "Send commands to your linked devices (its tools that do this always ask you first)",
    "network.lan": "Talk to machines on your local network (informational for process mods)",
    "storage": "Keep data in a key-value store the core holds for it",
    "llm.call": "Ask your configured AI models questions (uses your model routing and your usage)",
    "agent.run": ("Run the agent on your models in chats it creates for you, with the core tools it picks "
                  "(this can include the shell, files and the web, with your account's rights)"),
    "budgets": "See what its own model calls and agent runs cost, and set monthly budgets for them",
}

RUNTIME_TYPES = ("process", "docker")
START_MODES = ("lazy", "boot")


class ManifestError(ValueError):
    """The manifest is missing, unreadable or invalid. `errors` lists each problem."""

    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "invalid manifest")


def _version_tuple(v: str) -> Tuple[int, ...]:
    core = re.split(r"[-+]", str(v), 1)[0]
    out = []
    for part in core.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def core_satisfies(min_core: str) -> bool:
    return _version_tuple(CORE_API_VERSION) >= _version_tuple(min_core)


def _str(d: Dict, key: str, errors: List[str], *, required: bool = False, max_len: int = 200,
         default: str = "") -> str:
    v = d.get(key, None)
    if v is None:
        if required:
            errors.append(f"`{key}` is required")
        return default
    if not isinstance(v, str):
        errors.append(f"`{key}` must be a string")
        return default
    v = v.strip()
    if required and not v:
        errors.append(f"`{key}` must not be empty")
    if len(v) > max_len:
        errors.append(f"`{key}` is longer than {max_len} characters")
        v = v[:max_len]
    return v


def _path(v: Any, where: str, errors: List[str], default: Optional[str] = None) -> Optional[str]:
    if v is None:
        return default
    if not isinstance(v, str) or not PATH_RE.match(v) or ".." in v.split("/") or "//" in v:
        errors.append(f"{where} must be a path starting with / (got {v!r})")
        return default
    return v


def _discovery(raw: Dict[str, Any], errors: List[str]) -> Dict[str, Any]:
    """`tags`, `icon_url` and `screenshots`: optional, shown by the Browse view."""
    tags = raw.get("tags")
    out_tags: List[str] = []
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            errors.append("`tags` must be a list of strings like 3d-printing")
        else:
            bad = [t for t in tags if not TAG_RE.match(t)]
            if bad:
                errors.append("`tags` must be lowercase slugs (letters, digits and single hyphens, up to 32), "
                              f"not {', '.join(repr(t) for t in bad[:3])}")
            out_tags = list(dict.fromkeys(t for t in tags if TAG_RE.match(t)))
            if len(out_tags) > MAX_TAGS:
                errors.append(f"`tags` can have at most {MAX_TAGS} entries")
                out_tags = out_tags[:MAX_TAGS]
    icon = raw.get("icon_url")
    if icon is not None and (not isinstance(icon, str) or not HTTPS_URL_RE.match(icon)):
        errors.append("`icon_url` must be an https URL")
        icon = None
    shots = raw.get("screenshots")
    out_shots: List[str] = []
    if shots is not None:
        if (not isinstance(shots, list) or len(shots) > MAX_SCREENSHOTS
                or not all(isinstance(u, str) and HTTPS_URL_RE.match(u) for u in shots)):
            errors.append(f"`screenshots` must be a list of at most {MAX_SCREENSHOTS} https URLs")
        else:
            out_shots = list(shots)
    return {"tags": out_tags, "icon_url": icon or "", "screenshots": out_shots}


def validate(raw: Any) -> Dict[str, Any]:
    """Check a parsed manifest and return it normalized (defaults filled in).

    Raises ManifestError listing every problem found. Problems that do not
    stop the mod working (a nav group this core does not have) are listed in
    the result's `warnings` instead."""
    from src.mods import placement
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(raw, dict):
        raise ManifestError(["the manifest must be a JSON object"])

    mv = raw.get("manifest_version", MANIFEST_VERSION)
    if mv != MANIFEST_VERSION:
        errors.append(f"`manifest_version` {mv!r} is not supported (this core reads {MANIFEST_VERSION})")

    mid = _str(raw, "id", errors, required=True, max_len=32)
    if mid and (not ID_RE.match(mid) or "--" in mid or mid.endswith("-")):
        errors.append("`id` must be 2 to 32 characters: lowercase letters, digits and single hyphens, "
                      "starting with a letter")
    if mid in RESERVED_IDS:
        errors.append(f"`id` {mid!r} is reserved")
    out: Dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "id": mid,
        "name": _str(raw, "name", errors, required=True, max_len=60),
        "version": _str(raw, "version", errors, required=True, max_len=40),
        "description": _str(raw, "description", errors, max_len=300),
        "author": _str(raw, "author", errors, max_len=100),
        "license": _str(raw, "license", errors, max_len=60),
        "homepage": _str(raw, "homepage", errors, max_len=300),
        "min_core": _str(raw, "min_core", errors, max_len=20, default="0.1") or "0.1",
    }
    out.update(_discovery(raw, errors))
    if out["version"] and not VERSION_RE.match(out["version"]):
        errors.append("`version` must look like 1.2.3")
    if out["homepage"] and not re.match(r"^https?://", out["homepage"]):
        errors.append("`homepage` must be an http(s) URL")
    if not VERSION_RE.match(out["min_core"]):
        errors.append("`min_core` must look like 0.1")
    elif not core_satisfies(out["min_core"]):
        errors.append(f"this mod needs mod API {out['min_core']}; this Odysseus has {CORE_API_VERSION}")

    # ── runtime ──
    rt = raw.get("runtime")
    if not isinstance(rt, dict):
        errors.append("`runtime` is required and must be an object")
        rt = {}
    rtype = rt.get("type", "process")
    if rtype not in RUNTIME_TYPES:
        errors.append(f"`runtime.type` must be one of {', '.join(RUNTIME_TYPES)}")
    runtime: Dict[str, Any] = {"type": rtype}
    if rtype == "process":
        cmd = rt.get("command")
        if (not isinstance(cmd, list) or not cmd or len(cmd) > 64
                or not all(isinstance(c, str) and c and len(c) < 500 and "\x00" not in c for c in cmd)):
            errors.append("`runtime.command` must be a non-empty list of strings for a process mod")
            cmd = []
        runtime["command"] = cmd
    elif rtype == "docker":
        compose = rt.get("compose", "docker-compose.yml")
        if (not isinstance(compose, str) or not re.match(r"^[A-Za-z0-9._/-]{1,120}$", compose)
                or compose.startswith("/") or ".." in compose.split("/")):
            errors.append("`runtime.compose` must be a relative file name inside the mod")
            compose = "docker-compose.yml"
        runtime["compose"] = compose
        service = rt.get("service")
        if service is not None and (not isinstance(service, str) or not re.match(r"^[A-Za-z0-9._-]{1,64}$", service)):
            errors.append("`runtime.service` must be a compose service name")
            service = None
        runtime["service"] = service
    setup = rt.get("setup") or []
    if (not isinstance(setup, list) or len(setup) > 8 or not all(
            isinstance(c, list) and c and len(c) <= 64
            and all(isinstance(a, str) and a and len(a) < 500 and "\x00" not in a for a in c) for c in setup)):
        errors.append("`runtime.setup` must be a list of commands, each a list of strings")
        setup = []
    runtime["setup"] = setup
    runtime["health"] = _path(rt.get("health"), "`runtime.health`", errors, "/health")
    start = rt.get("start", "lazy")
    if start not in START_MODES:
        errors.append("`runtime.start` must be `lazy` or `boot`")
        start = "lazy"
    runtime["start"] = start
    env = rt.get("env") or {}
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and re.match(r"^[A-Z_][A-Z0-9_]{0,63}$", k) and isinstance(v, str) and len(v) < 2000
            for k, v in env.items()):
        errors.append("`runtime.env` must map UPPER_CASE names to strings")
        env = {}
    if any(k.startswith(("ODYSSEUS_", "MOD_")) for k in env):
        errors.append("`runtime.env` cannot set ODYSSEUS_* or MOD_* variables; the core sets those")
        env = {k: v for k, v in env.items() if not k.startswith(("ODYSSEUS_", "MOD_"))}
    runtime["env"] = env
    idle = rt.get("idle_stop_minutes", 0)
    if not isinstance(idle, int) or idle < 0 or idle > 7 * 24 * 60:
        errors.append("`runtime.idle_stop_minutes` must be a whole number of minutes (0 = never)")
        idle = 0
    runtime["idle_stop_minutes"] = idle
    out["runtime"] = runtime

    # ── permissions ──
    perms = raw.get("permissions", [])
    if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
        errors.append("`permissions` must be a list of strings")
        perms = []
    unknown = [p for p in perms if p not in PERMISSIONS]
    if unknown:
        errors.append(f"unknown permission(s): {', '.join(sorted(set(unknown)))}")
    out["permissions"] = sorted({p for p in perms if p in PERMISSIONS})

    # ── ui ──
    ui = raw.get("ui") or {}
    if not isinstance(ui, dict):
        errors.append("`ui` must be an object")
        ui = {}
    tabs = []
    for i, t in enumerate(ui.get("tabs") or []):
        if not isinstance(t, dict) or not SUB_ID_RE.match(str(t.get("id", ""))):
            errors.append(f"`ui.tabs[{i}]` needs an `id` (lowercase letters, digits, - and _)")
            continue
        title = t.get("title") if isinstance(t.get("title"), str) else ""
        tab = {"id": t["id"], "title": (title.strip() or out["name"])[:40],
               "icon": str(t.get("icon") or "")[:32],
               "path": _path(t.get("path"), f"`ui.tabs[{i}].path`", errors, "/")}
        tab.update(placement.check_tab(t, f"ui.tabs[{i}]", errors, warnings))
        if tab.get("replaces") and any(x.get("replaces") == tab["replaces"] for x in tabs):
            errors.append(f"`ui.tabs[{i}].replaces`: only one tab can replace {tab['replaces']!r}")
        tabs.append(tab)
    cards = []
    for i, c in enumerate(ui.get("home_cards") or []):
        if not isinstance(c, dict) or not SUB_ID_RE.match(str(c.get("id", ""))):
            errors.append(f"`ui.home_cards[{i}]` needs an `id`")
            continue
        h = c.get("height", 180)
        if not isinstance(h, int) or not 60 <= h <= 600:
            errors.append(f"`ui.home_cards[{i}].height` must be 60 to 600 pixels")
            h = 180
        title = c.get("title") if isinstance(c.get("title"), str) else ""
        cards.append({"id": c["id"], "title": (title.strip() or out["name"])[:40], "height": h,
                      "path": _path(c.get("path"), f"`ui.home_cards[{i}].path`", errors, "/")})
    if len(tabs) > 8 or len(cards) > 8:
        errors.append("a mod can have at most 8 tabs and 8 home cards")
    out["ui"] = {"tabs": tabs[:8], "home_cards": cards[:8],
                 "settings_path": _path(ui.get("settings_path"), "`ui.settings_path`", errors, None)}

    # ── tools ──
    tools = raw.get("tools")
    if tools is not None and not isinstance(tools, dict):
        errors.append("`tools` must be an object")
        tools = None
    if tools:
        kws = tools.get("keywords") or []
        if not isinstance(kws, list) or not all(isinstance(k, str) and 0 < len(k.strip()) <= 40 for k in kws):
            errors.append("`tools.keywords` must be a list of short strings")
            kws = []
        out["tools"] = {"endpoint": _path(tools.get("endpoint"), "`tools.endpoint`", errors, "/mod/tools"),
                        "keywords": sorted({k.strip().lower() for k in kws})[:64]}
    else:
        out["tools"] = None

    # ── events ──
    ev = raw.get("events") or {}
    if not isinstance(ev, dict):
        errors.append("`events` must be an object")
        ev = {}
    emits = ev.get("emits") or []
    subs = ev.get("subscribes") or []
    if not isinstance(emits, list) or not all(isinstance(e, str) and EVENT_RE.match(e) for e in emits):
        errors.append("`events.emits` must be a list of event types like print.finished")
        emits = []
    if not isinstance(subs, list) or not all(isinstance(e, str) and EVENT_PATTERN_RE.match(e) for e in subs):
        errors.append("`events.subscribes` must be a list of patterns like print-hub/print.* or *.failed")
        subs = []
    out["events"] = {"emits": sorted(set(emits)), "subscribes": sorted(set(subs)),
                     "webhook": _path(ev.get("webhook"), "`events.webhook`", errors, "/mod/events")}
    if out["events"]["emits"] and "events.emit" not in out["permissions"]:
        errors.append("`events.emits` needs the events.emit permission")
    if out["events"]["subscribes"] and "events.subscribe" not in out["permissions"]:
        errors.append("`events.subscribes` needs the events.subscribe permission")

    # ── tutorial (src/mods/tutorial.py) ──
    from src.mods import tutorial
    out["tutorial"] = tutorial.check(raw.get("tutorial"), bool(out["ui"]["settings_path"]),
                                     lambda v, where, errs: _path(v, where, errs, None), errors, warnings)

    if errors:
        raise ManifestError(errors)
    out["warnings"] = warnings
    return out


def load(mod_dir: str) -> Dict[str, Any]:
    """Read and validate <mod_dir>/odysseus-mod.json."""
    path = os.path.join(mod_dir, MANIFEST_FILE)
    try:
        if os.path.getsize(path) > 256 * 1024:
            raise ManifestError([f"{MANIFEST_FILE} is larger than 256 KB"])
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ManifestError([f"no {MANIFEST_FILE} at the top of the mod"])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ManifestError([f"{MANIFEST_FILE} is not valid JSON: {e}"])
    out = validate(raw)
    from src.mods import tutorial
    out["warnings"] = out["warnings"] + tutorial.missing_images(out.get("tutorial"), mod_dir)
    return out


def describe_permissions(perms: List[str]) -> List[Dict[str, str]]:
    return [{"id": p, "description": PERMISSIONS.get(p, p)} for p in perms]
