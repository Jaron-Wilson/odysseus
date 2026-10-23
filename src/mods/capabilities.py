"""Capabilities: what a mod provides to other mods, and what it needs from them.

A manifest may say

    "provides": ["messaging.sms"]          this mod can do it for others
    "requires": ["messaging.sms"]          hard: it does not work without it
    "optional": ["messaging.sms"]          soft: it works without it, better with it

A capability id is a namespaced string, `<namespace>.<name>[.<more>]`
(`messaging.sms`, `storage.s3`, `home.lights`). The core knows some of them
(KNOWN, with their actions, events and rules); any other well-formed id is
allowed, and then the actions are whatever the provider answers.

Mods never talk to each other directly. A mod that declared a capability in
`requires` or `optional` calls

    POST /api/mods/v1/cap/<capability>/<action>     {"user": ..., ...arguments}

and the core:

1. checks the calling mod declared it (that is what the user approved at
   install), and that the user the call is for may use the caller and the
   provider;
2. applies the capability's own rules (KNOWN[...]["policy"]): for
   messaging.sms, a rate limit and "only the user's own allowed numbers,
   unless they granted this mod more";
3. routes it to the provider: the first enabled mod (by id) that lists it in
   `provides`, else the core's built-in provider (BUILTIN), as
   `POST /mod/cap/<capability>/<action>` on the provider mod with the signed
   identity of that user and `X-Odysseus-Caller: <caller id>`;
4. logs it (`mods/capability-log.jsonl`, and a line in the caller's log).

Inbound things (a text arrived) are events, on the existing events API. A
capability's events (KNOWN[...]["events"]) can only be emitted by a provider
of it, and are delivered only to mods that declare the capability (and that
the user it is for may use); see src/mods/events.py.

A provider can also reach the mods that use it, through the same broker:

    POST /api/mods/v1/cap/<capability>/<action>?to=<mod id>      (a "callback")

allowed only when the caller lists <capability> in `provides` and the mod
`to` lists it in `requires` or `optional` (so the user approved that link at
install time). The core routes it to `POST /mod/cap/<capability>/<action>` on
the mod `to`, with `X-Odysseus-Caller: <provider id>` and
`X-Odysseus-Callback: 1`. A manifest's `scopes` ({"discord.commands":
["print"]}) are names a mod claims inside a provider's namespace; the core
only validates and shows them, and a provider reads them with
`GET /api/mods/v1/cap/<capability>/consumers`.

The built-in provider of messaging.sms is the Odysseus SMS gateway
(routes/sms_routes.py: SMS Forwarder inbound, SMS Gate reply URL). When a
"Phone & Messages" mod lists messaging.sms in `provides`, calls go to it
instead, with the same rules, and nothing changes for the callers.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from src.mods import access, paths, registry, signing

logger = logging.getLogger(__name__)

CAP_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}(?:\.[a-z][a-z0-9-]{0,31}){1,4}$")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_SCOPES = 16
MAX_PER_LIST = 16
MAX_CAP_LEN = 64
KINDS = ("provides", "requires", "optional")
PROVIDER_PATH = "/mod/cap"
CALL_TIMEOUT_S = 30
MAX_BODY_BYTES = 64 * 1024
LOG_MAX_BYTES = 1024 * 1024
BUILTIN = "core"          # the provider id shown for the core's own providers (and the source of its events)

SMS_MAX_TEXT = 1600
SMS_MAX_GRANTED = 10


class CapError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# ── What the core knows ──────────────────────────────────────────────────

KNOWN: Dict[str, Dict[str, Any]] = {
    "messaging.sms": {
        "title": "Text messages (SMS)",
        "description": "Send texts to your own phone number, and get the texts you send to Odysseus",
        "actions": {
            "send": "Send a text: {to (optional, default your own number), text}. Only your own allowed numbers, "
                    "or numbers you granted the mod",
            "status": "Whether texting is set up: {available, can_send, can_receive, targets}",
        },
        "events": ["messaging.sms.received"],
        "builtin": "The Odysseus SMS gateway (Settings > Calls & Meetings > Phone SMS)",
        # (calls, seconds) per calling mod and action; every window must have room.
        "rate_limits": {"send": [(10, 60), (100, 24 * 3600)], "status": [(60, 60)]},
    },
}


def describe(cap: str) -> Dict[str, Any]:
    k = KNOWN.get(cap) or {}
    return {"id": cap, "title": k.get("title") or cap, "description": k.get("description") or "",
            "known": bool(k), "actions": dict(k.get("actions") or {}), "events": list(k.get("events") or []),
            "builtin": k.get("builtin") or ""}


def event_capability(etype: str) -> Optional[str]:
    """The known capability an event type belongs to (messaging.sms.received
    is messaging.sms's), or None for an ordinary mod event."""
    for cap, k in KNOWN.items():
        if etype in (k.get("events") or []):
            return cap
    return None


# ── The manifest fields ──────────────────────────────────────────────────

def _check_list(raw: Any, key: str, errors: List[str]) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        errors.append(f"`{key}` must be a list of capability ids like messaging.sms")
        return []
    bad = [c for c in raw if len(c) > MAX_CAP_LEN or not CAP_RE.match(c)]
    if bad:
        errors.append(f"`{key}`: capability ids are namespaced lowercase names like messaging.sms "
                      f"(not {', '.join(repr(c) for c in bad[:3])})")
    out = list(dict.fromkeys(c for c in raw if c not in bad))
    if len(out) > MAX_PER_LIST:
        errors.append(f"`{key}` can have at most {MAX_PER_LIST} entries")
        out = out[:MAX_PER_LIST]
    return out


def _check_scopes(raw: Any, lists: Dict[str, List[str]], errors: List[str]) -> Dict[str, List[str]]:
    """`scopes`: {capability: [name, ...]}, names a mod claims inside a
    capability it requires or may use (the groups of slash commands it owns,
    for discord.commands). The core does not interpret them: the provider does,
    and reads them with `consumers`. Shown in the install review."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append("`scopes` must be an object like {\"discord.commands\": [\"print\"]}")
        return {}
    out: Dict[str, List[str]] = {}
    used = set(lists["requires"]) | set(lists["optional"])
    for cap, names in raw.items():
        if cap not in used:
            errors.append(f"`scopes`: {cap!r} is not in `requires` or `optional`")
            continue
        if not isinstance(names, list) or not all(isinstance(n, str) and SCOPE_RE.match(n) for n in names):
            errors.append(f"`scopes.{cap}` must be a list of names (lowercase letters, digits, - and _)")
            continue
        if len(names) > MAX_SCOPES:
            errors.append(f"`scopes.{cap}` can have at most {MAX_SCOPES} names")
            names = names[:MAX_SCOPES]
        out[cap] = list(dict.fromkeys(names))
    return out


def check(raw: Dict[str, Any], events_out: Dict[str, Any], errors: List[str],
          warnings: List[str]) -> Dict[str, List[str]]:
    """`provides`, `requires` and `optional` of a manifest (src/mods/manifest.py)."""
    out = {k: _check_list(raw.get(k), k, errors) for k in KINDS}
    both = sorted(set(out["requires"]) & set(out["optional"]))
    if both:
        errors.append(f"{', '.join(both)} cannot be both in `requires` and `optional`")
    own = sorted((set(out["requires"]) | set(out["optional"])) & set(out["provides"]))
    if own:
        warnings.append(f"it needs {', '.join(own)}, which it provides itself")
    out["scopes"] = _check_scopes(raw.get("scopes"), out, errors)
    emits = set((events_out or {}).get("emits") or [])
    for cap in out["provides"]:
        for ev in (KNOWN.get(cap) or {}).get("events") or []:
            if ev not in emits:
                warnings.append(f"it provides {cap} but does not list {ev} in `events.emits`, so it cannot "
                                "report it")
    # A capability's events belong to its providers: another mod cannot claim them.
    for ev in sorted(emits):
        cap = event_capability(ev)
        if cap and cap not in out["provides"]:
            errors.append(f"`events.emits`: {ev} is the {cap} capability's event; only a mod that provides "
                          f"{cap} may emit it")
    return out


def declared(manifest: Dict[str, Any], kinds: Tuple[str, ...] = ("requires", "optional")) -> List[str]:
    out: List[str] = []
    for k in kinds:
        out += [c for c in (manifest or {}).get(k) or [] if c not in out]
    return out


def declares(entry: Dict[str, Any], cap: str) -> bool:
    return cap in declared((entry or {}).get("manifest") or {}, KINDS)


# ── Providers ────────────────────────────────────────────────────────────

def _builtin_available(cap: str) -> bool:
    return cap in BUILTIN_HANDLERS


def owner_of(cap: str, mods: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """A mod's own named API: a capability whose namespace is an installed
    mod's id (`print-hub.jobs`) is served by that mod only, so no other mod
    can answer in its name."""
    if cap in KNOWN:      # the core's own capabilities belong to no mod
        return None
    ns = cap.split(".", 1)[0]
    for e in mods if mods is not None else registry.list_mods():
        if e["id"] == ns:
            return ns
    return None


def providers(cap: str, exclude: Optional[str] = None) -> List[Dict[str, Any]]:
    """Who can serve `cap`, in the order calls would pick them: enabled mods
    that provide it (by id), then the core's built-in provider."""
    out = []
    mods = sorted(registry.list_mods(), key=lambda m: m["id"])
    owner = owner_of(cap, mods)
    for e in mods:
        if e["id"] == exclude or not e.get("enabled"):
            continue
        if owner and e["id"] != owner:
            continue
        if cap in ((e.get("manifest") or {}).get("provides") or []):
            out.append({"kind": "mod", "id": e["id"], "name": e.get("name") or e["id"]})
    if _builtin_available(cap):
        out.append({"kind": "core", "id": BUILTIN, "name": (KNOWN.get(cap) or {}).get("builtin") or "Odysseus"})
    return out


def provider(cap: str, exclude: Optional[str] = None) -> Optional[Dict[str, Any]]:
    p = providers(cap, exclude)
    return p[0] if p else None


def availability(manifest: Dict[str, Any], exclude: Optional[str] = None) -> Dict[str, Any]:
    """For the install review and the Settings > Mods card: each capability
    the mod provides, requires and may use, with who provides it now.
    `missing` are the hard requirements nobody provides."""
    def row(cap):
        p = provider(cap, exclude)
        return dict(describe(cap), available=bool(p), provider=p)
    req = [row(c) for c in (manifest or {}).get("requires") or []]
    opt = [row(c) for c in (manifest or {}).get("optional") or []]
    return {"provides": [describe(c) for c in (manifest or {}).get("provides") or []],
            "requires": req, "optional": opt, "scopes": dict((manifest or {}).get("scopes") or {}),
            "missing": [r["id"] for r in req if not r["available"]],
            "missing_optional": [r["id"] for r in opt if not r["available"]]}


def dependents(mod_id: str) -> List[Dict[str, Any]]:
    """Enabled mods that would lose a capability if `mod_id` were disabled or
    removed: they declare one it provides, and nobody else provides it."""
    entry = registry.get(mod_id) or {}
    mine = (entry.get("manifest") or {}).get("provides") or []
    if not mine:
        return []
    out = []
    for e in sorted(registry.list_mods(), key=lambda m: m["id"]):
        if e["id"] == mod_id or not e.get("enabled"):
            continue
        man = e.get("manifest") or {}
        for kind in ("requires", "optional"):
            for cap in man.get(kind) or []:
                if cap in mine and not provider(cap, exclude=mod_id):
                    out.append({"id": e["id"], "name": e.get("name") or e["id"], "capability": cap, "kind": kind})
    return out


# ── Grants: what a user allowed a mod beyond the defaults ─────────────────

def _user_key(user: Optional[str]) -> str:
    """Grants are per user; with sign-in off there is one user, whatever name a call carries."""
    return "" if access.auth_disabled() else (user or "")


def grants(entry: Dict[str, Any], user: Optional[str]) -> Dict[str, Any]:
    g = (entry.get("capability_grants") or {}).get(_user_key(user)) or {}
    return g if isinstance(g, dict) else {}


def set_grant(mod_id: str, user: Optional[str], cap: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Save one user's grant for one capability of one mod. For messaging.sms:
    {"numbers": [...]}, extra numbers the mod may text for this user."""
    if cap != "messaging.sms":
        raise CapError(400, f"{cap} has nothing to grant")
    from routes.sms_routes import normalize_number
    nums: List[str] = []
    for n in values.get("numbers") or []:
        norm = normalize_number(n)
        if len(norm.lstrip("+")) < 5:
            raise CapError(400, f"Not a phone number: {str(n)[:30]}")
        if norm not in nums:
            nums.append(norm)
    if len(nums) > SMS_MAX_GRANTED:
        raise CapError(400, f"At most {SMS_MAX_GRANTED} numbers")
    with registry._lock:
        data = registry._load()
        m = data["mods"].get(mod_id)
        if not m:
            raise CapError(404, f"no mod {mod_id!r}")
        all_grants = dict(m.get("capability_grants") or {})
        mine = dict(all_grants.get(_user_key(user)) or {})
        if nums:
            mine[cap] = {"numbers": nums}
        else:
            mine.pop(cap, None)
        if mine:
            all_grants[_user_key(user)] = mine
        else:
            all_grants.pop(_user_key(user), None)
        m["capability_grants"] = all_grants
        registry._save(data)
    log({"event": "grant", "mod": mod_id, "user": user or "", "capability": cap, "numbers": nums})
    return {cap: {"numbers": nums}} if nums else {}


# ── The log ──────────────────────────────────────────────────────────────

def log_file() -> str:
    return os.path.join(paths.mods_root(), "capability-log.jsonl")


def log(record: Dict[str, Any]) -> None:
    """Append one line to mods/capability-log.jsonl (rotated at 1 MB)."""
    p = log_file()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.exists(p) and os.path.getsize(p) > LOG_MAX_BYTES:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict({"ts": round(time.time(), 3)}, **record), default=str) + "\n")
        os.chmod(p, 0o600)
    except OSError as e:
        logger.warning("[mods] capability log write failed: %s", e)


def read_log(limit: int = 200, mod_id: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in (log_file(), log_file() + ".1"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            continue
        for line in reversed(lines):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if mod_id and mod_id not in (r.get("caller"), r.get("provider"), r.get("mod")):
                continue
            out.append(r)
            if len(out) >= limit:
                return out
    return out


# ── Rate limits ──────────────────────────────────────────────────────────

_calls: Dict[tuple, Deque[float]] = defaultdict(deque)


def _rate(caller: str, cap: str, action: str) -> None:
    windows = ((KNOWN.get(cap) or {}).get("rate_limits") or {}).get(action) or [(120, 60)]
    now = time.time()
    for n, per in windows:
        q = _calls[(caller, cap, action, per)]
        while q and now - q[0] > per:
            q.popleft()
        if len(q) >= n:
            raise CapError(429, f"Too many {cap} {action} calls; at most {n} per {per} seconds")
    for n, per in windows:
        _calls[(caller, cap, action, per)].append(now)


# ── messaging.sms rules and the built-in provider ────────────────────────

def _sms_config(user: Optional[str]) -> Dict[str, Any]:
    from routes import sms_routes
    return sms_routes.get_config(None if access.auth_disabled() else (user or None))


def _sms_own_numbers(user: Optional[str]) -> List[str]:
    from routes.sms_routes import normalize_number
    out = []
    for n in _sms_config(user).get("numbers") or []:
        norm = normalize_number(n)
        if norm and norm not in out:
            out.append(norm)
    return out


def _mask(number: str) -> str:
    return ("*" * max(0, len(number) - 4)) + number[-4:] if len(number) > 4 else number


def _sms_policy(caller: Dict[str, Any], user: Optional[str], action: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """messaging.sms: check and normalize the arguments before any provider
    sees them. `send` may only text the user's own allowed numbers (Settings
    > Calls & Meetings > Phone SMS) or numbers the user granted this mod."""
    if action == "status":
        return {}
    if action != "send":
        raise CapError(404, f"messaging.sms has no action {action!r} (send, status)")
    from routes.sms_routes import normalize_number
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise CapError(400, "send needs a `text`")
    if len(text) > SMS_MAX_TEXT:
        raise CapError(400, f"`text` is longer than {SMS_MAX_TEXT} characters")
    own = _sms_own_numbers(user)
    granted = [normalize_number(n) for n in (grants(caller, user).get("messaging.sms") or {}).get("numbers") or []]
    raw_to = args.get("to")
    if raw_to in (None, ""):
        if not own:
            raise CapError(409, "The user has no phone number set up for texts "
                                "(Settings > Calls & Meetings > Phone SMS)")
        to = own[0]
    else:
        to = normalize_number(raw_to)
        if not to or (to not in own and to not in granted):
            raise CapError(403, "That number is not the user's own allowed number, and the user has not granted "
                                "this mod more (Settings > Mods, the mod's card, Text messages)")
    return {"to": to, "text": text.strip()}


async def _sms_builtin(action: str, args: Dict[str, Any], user: Optional[str]) -> Dict[str, Any]:
    from routes import sms_routes
    cfg = _sms_config(user)
    can_send = bool(cfg.get("numbers")) and bool(str(cfg.get("reply_url") or "").strip())
    if action == "status":
        # `available`: a text sent now would go out (the gateway has a number and a reply URL).
        return {"available": can_send, "provider": BUILTIN, "can_send": can_send,
                "can_receive": bool(cfg.get("secret_hash")) and bool(cfg.get("numbers")),
                "targets": [_mask(n) for n in _sms_own_numbers(user)]}
    if not can_send:
        raise CapError(409, "The SMS gateway has no reply URL or number (Settings > Calls & Meetings > Phone SMS)")
    parts = sms_routes.split_reply(args["text"])
    vias = []
    for part in parts:
        vias.append(await sms_routes.deliver(user or None, cfg, args["to"], part))
    ok = all(v != "failed" for v in vias)
    if not ok:
        raise CapError(502, "The phone did not take the text (check the SMS Gate reply URL)", parts=len(parts))
    return {"ok": True, "to": _mask(args["to"]), "parts": len(parts), "via": vias[0] if vias else ""}


Policy = Callable[[Dict[str, Any], Optional[str], str, Dict[str, Any]], Dict[str, Any]]
POLICIES: Dict[str, Policy] = {"messaging.sms": _sms_policy}
BUILTIN_HANDLERS: Dict[str, Callable[[str, Dict[str, Any], Optional[str]], Awaitable[Dict[str, Any]]]] = {
    "messaging.sms": _sms_builtin,
}


# ── A call ───────────────────────────────────────────────────────────────

async def _call_mod(provider_id: str, cap: str, action: str, args: Dict[str, Any], user: Optional[str],
                    caller_id: str, callback: bool = False) -> Dict[str, Any]:
    from src.mods import runtime
    port = await runtime.ensure_running(provider_id)
    if not port:
        st = runtime.state(provider_id)
        raise CapError(503, f"The {provider_id} mod, which provides {cap}, is not running "
                            f"({st.last_error or st.status})")
    headers = signing.sign_identity(registry.token_of(provider_id), provider_id,
                                    signing.caller(user, access.auth_disabled()))
    headers["Content-Type"] = "application/json"
    headers["X-Odysseus-Caller"] = caller_id
    headers["X-Odysseus-Capability"] = cap
    if callback:
        headers["X-Odysseus-Callback"] = "1"
    url = f"http://127.0.0.1:{port}{PROVIDER_PATH}/{cap}/{action}"
    try:
        r = await runtime._client().post(url, content=json.dumps(args), headers=headers, timeout=CALL_TIMEOUT_S)
    except Exception as e:
        raise CapError(502, f"The {provider_id} mod did not answer: {type(e).__name__}")
    try:
        data = r.json()
    except ValueError:
        data = {"output": r.text[:2000]}
    if not isinstance(data, dict):
        data = {"result": data}
    if r.status_code >= 400 or data.get("error"):
        raise CapError(r.status_code if 400 <= r.status_code < 600 else 502,
                       str(data.get("error") or data.get("detail") or f"HTTP {r.status_code}")[:2000])
    runtime.touch(provider_id)
    return data


def _log_args(cap: str, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """What the log keeps of the arguments: never message text, only its size."""
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if k == "text" and isinstance(v, str):
            out["chars"] = len(v)
        elif k == "to" and isinstance(v, str):
            out["to"] = v
    return out


async def call(caller: Dict[str, Any], user: Optional[str], cap: str, action: str,
               args: Dict[str, Any], provider_id: Optional[str] = None) -> Dict[str, Any]:
    """One capability call from mod `caller` on behalf of `user`, to the
    first provider, or to `provider_id` (a mod id, or "core") when the caller
    names one. Raises CapError. Answers the provider's own answer (a JSON
    object) with `ok`, `capability`, `action` and `provider` set by the core."""
    from src.mods import runtime
    caller_id = caller["id"]
    if not CAP_RE.match(cap or "") or len(cap) > MAX_CAP_LEN:
        raise CapError(400, "not a capability id (like messaging.sms)")
    if not ACTION_RE.match(action or ""):
        raise CapError(400, "not an action name (like send)")
    if cap not in declared(caller.get("manifest") or {}):
        raise CapError(403, f"This mod did not declare {cap} in `requires` or `optional`")
    known = KNOWN.get(cap)
    if known and action not in (known.get("actions") or {}):
        raise CapError(404, f"{cap} has no action {action!r} ({', '.join(known.get('actions') or {})})")
    if not access.may_use(caller, user):
        raise CapError(403, "That user may not use this mod")
    try:
        _rate(caller_id, cap, action)
    except CapError as e:
        log({"caller": caller_id, "user": user or "", "capability": cap, "action": action, "ok": False,
             "error": e.message})
        raise
    record: Dict[str, Any] = {"caller": caller_id, "user": user or "", "capability": cap, "action": action}
    try:
        policy = POLICIES.get(cap)
        clean = policy(caller, user, action, args) if policy else dict(args)
        record.update(_log_args(cap, action, clean))
        candidates = providers(cap)
        prov = next((p for p in candidates if p["id"] == provider_id), None) if provider_id else \
            (candidates[0] if candidates else None)
        if not prov:
            where = f"{provider_id} does not provide {cap} (or is turned off)" if provider_id else \
                f"Nothing provides {cap}"
            raise CapError(424, f"{where}. Install a mod that provides it (Settings > Mods > Browse).", missing=cap)
        record["provider"] = prov["id"]
        if prov["kind"] == "mod":
            pentry = registry.get(prov["id"]) or {}
            if not access.may_use(pentry, user):
                raise CapError(403, f"That user may not use the {prov['id']} mod, which provides {cap}")
            result = await _call_mod(prov["id"], cap, action, clean, user, caller_id)
        else:
            result = await BUILTIN_HANDLERS[cap](action, clean, user)
    except CapError as e:
        record.update(ok=False, error=e.message, status=e.status)
        log(record)
        runtime.log_line(caller_id, f"[odysseus] {cap} {action} refused: {e.message}")
        raise
    except Exception as e:   # a provider bug is the caller's 502, never a crash
        logger.exception("[mods] capability %s %s from %s failed", cap, action, caller_id)
        record.update(ok=False, error=type(e).__name__, status=502)
        log(record)
        raise CapError(502, f"{cap} {action} failed: {type(e).__name__}")
    record["ok"] = True
    if isinstance(result, dict) and result.get("via"):
        record["via"] = result["via"]
    log(record)
    runtime.log_line(caller_id, f"[odysseus] {cap} {action} via {record['provider']}"
                                + (f" to {_mask(record['to'])}" if record.get("to") else ""))
    if action == "send":
        logger.info("[mods] %s: %s send via %s for %s", caller_id, cap, record["provider"], user or "-")
    out = dict(result) if isinstance(result, dict) else {"result": result}
    out.setdefault("ok", True)
    out.update(capability=cap, action=action, provider=record["provider"])
    return out


# ── Providers reaching their consumers ────────────────────────────────────

def _provides(entry: Dict[str, Any], cap: str) -> bool:
    return cap in ((entry or {}).get("manifest") or {}).get("provides", [])


def consumers(provider: Dict[str, Any], cap: str) -> List[Dict[str, Any]]:
    """The enabled mods that declare `cap` (in `requires` or `optional`), with
    the `scopes` each claimed for it. Only a mod that provides `cap` may ask:
    a provider needs to know who its consumers are (which groups of slash
    commands each may register), and nobody else does."""
    if not _provides(provider, cap):
        raise CapError(403, f"This mod does not provide {cap}")
    out = []
    for e in sorted(registry.list_mods(), key=lambda m: m["id"]):
        if e["id"] == provider["id"] or not e.get("enabled") or not declares(e, cap):
            continue
        man = e.get("manifest") or {}
        out.append({"id": e["id"], "name": e.get("name") or e["id"], "version": e.get("version") or "",
                    "scopes": list((man.get("scopes") or {}).get(cap) or []),
                    "requires": cap in (man.get("requires") or []), "admin_only": access.admin_only(e),
                    "access": "admins" if access.admin_only(e) else e.get("access", "admins")})
    return out


async def callback(provider: Dict[str, Any], user: Optional[str], cap: str, action: str, args: Dict[str, Any],
                   target_id: str) -> Dict[str, Any]:
    """A provider calls one of its consumers back: `provider` (a mod that
    lists `cap` in `provides`) -> mod `target_id` (which declared `cap`), on
    behalf of `user`. Routed to the target's POST /mod/cap/<cap>/<action>
    with X-Odysseus-Caller and X-Odysseus-Callback. Raises CapError."""
    from src.mods import runtime
    pid = provider["id"]
    if not CAP_RE.match(cap or "") or len(cap) > MAX_CAP_LEN:
        raise CapError(400, "not a capability id (like discord.commands)")
    if not ACTION_RE.match(action or ""):
        raise CapError(400, "not an action name (like interaction)")
    if not _provides(provider, cap):
        raise CapError(403, f"This mod does not provide {cap}, so it cannot call its consumers")
    if target_id == pid:
        raise CapError(400, "a mod cannot call itself")
    target = registry.get(target_id)
    if not target or not target.get("enabled"):
        raise CapError(404, f"No enabled mod {target_id!r}")
    if not declares(target, cap):
        raise CapError(403, f"{target_id} did not declare {cap} in `requires` or `optional`")
    if not access.may_use(provider, user) or not access.may_use(target, user):
        raise CapError(403, "That user may not use this mod")
    record: Dict[str, Any] = {"caller": pid, "user": user or "", "capability": cap, "action": action,
                              "provider": target_id, "callback": True}
    try:
        _rate(pid, cap, "callback:" + action)
        result = await _call_mod(target_id, cap, action, dict(args), user, pid, callback=True)
    except CapError as e:
        record.update(ok=False, error=e.message, status=e.status)
        log(record)
        runtime.log_line(pid, f"[odysseus] {cap} {action} callback to {target_id} refused: {e.message}")
        raise
    record["ok"] = True
    log(record)
    out = dict(result) if isinstance(result, dict) else {"result": result}
    out.setdefault("ok", True)
    out.update(capability=cap, action=action, provider=pid, to=target_id)
    return out


# ── Inbound: the built-in SMS gateway got a text ──────────────────────────

def sms_received(owner: Optional[str], sender: str, text: str) -> None:
    """Called by the SMS gateway for each accepted inbound text: publish
    messaging.sms.received to the mods that declare messaging.sms (events.py
    keeps it to them). Never raises into the gateway."""
    if not any(declares(e, "messaging.sms") and e.get("enabled") for e in registry.list_mods()):
        return
    try:
        from src.mods import events
        events.publish(BUILTIN, "messaging.sms.received",
                       {"from": sender, "text": text, "user": owner or "", "provider": BUILTIN}, owner=owner)
    except Exception as e:
        logger.warning("[mods] messaging.sms.received not published: %s", type(e).__name__)


def known_view() -> List[Dict[str, Any]]:
    """Every known capability with its current providers (Settings > Mods)."""
    return [dict(describe(c), providers=providers(c)) for c in KNOWN]
