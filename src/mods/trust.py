"""Trusted publishers: GitHub owners whose mods install and update without a
manual permission review (Settings > Mods > "Trusted publishers", docs/mods.md,
"Trusted publishers").

A mod is *from a trusted publisher* only when all of this holds:

  1. its source is a git URL on https://github.com/<owner>/<repo> (a folder
     install, an ssh URL or any other host never counts);
  2. <owner> is in the admin's list of trusted publishers; and
  3. GitHub's API says the repository really belongs to a listed owner
     (`owner.login` of GET /repos/<owner>/<repo>, redirects followed), so a
     lookalike URL, or a fork that someone else owns, does not count. When
     GitHub cannot be asked (rate limit, outage, private without a token), the
     mod is not trusted: it fails closed to the normal review.

For a trusted mod, install and update approve what it asks for themselves (its
permissions, the legacy settings it imports and the capabilities it uses) and
write one line to mods/trust-log.jsonl, shown in the mod's detail page under
"History". Some things are never approved this way and always need a click:

    ALWAYS_ASK = ui.style + every permission in HIGH_RISK_PERMISSIONS

A per-mod override ("Always ask me") turns auto-approval off for one mod.

The list is stored in the mods registry settings (`settings.trust.publishers`).
When the admin never saved one, the default is the GitHub login of the
configured catalog token (GET /user), or nothing without a token.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from fastapi import HTTPException, Request

from src.mods import catalog, paths

logger = logging.getLogger(__name__)

# `ui.style` changes the look of the whole UI (a mod's CSS reaches every page),
# so it is always a human decision, even from a publisher you trust.
UI_STYLE = "ui.style"

# Permissions that act on the world, run with the user's rights, or open the
# server to people who are not signed in. A trusted publisher's mod that asks
# for one of these still waits for a manual click:
#   devices.control  sends commands to your linked devices
#   public.http      receives requests from anyone, without a sign-in
#   agent.run        runs the agent (shell, files, web) with your account's rights
#   chats            sends messages to the agent as if you typed them
#   browser          drives the server's cloud browser and the accounts signed in there
#   embed            shows a server page outside the mod sandbox, with your sign-in
#   stats.read       server-wide activity: the admin powers an admin_only mod asks for
#   google.calendar  creates events and emails invites from your Google account
HIGH_RISK_PERMISSIONS = frozenset({
    "devices.control", "public.http", "agent.run", "chats", "browser", "embed", "stats.read", "google.calendar",
})
ALWAYS_ASK = frozenset(HIGH_RISK_PERMISSIONS | {UI_STYLE})

MAX_PUBLISHERS = 50
VERIFY_TTL_S = 3600          # a verified owner is remembered this long
VERIFY_FAIL_TTL_S = 60       # a failed check (rate limit, outage) is retried soon
RECORD_MAX_AGE_S = 7 * 24 * 3600   # how long the saved verification of an installed mod counts without network
MAX_LOG_BYTES = 2 * 1024 * 1024

_GH_HTTPS_RE = re.compile(r"^https://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$",
                          re.I)
_lock = threading.RLock()
_verified: Dict[str, Tuple[float, Optional[str]]] = {}     # "owner/repo" -> (when, real owner login or None)
_viewer_cache: Dict[str, Tuple[float, str]] = {}           # token fingerprint -> (when, login)


def always_ask_list() -> List[str]:
    return sorted(ALWAYS_ASK)


def clear_cache() -> None:
    with _lock:
        _verified.clear()
        _viewer_cache.clear()


# ── The list ─────────────────────────────────────────────────────────────

def _stored() -> Dict[str, Any]:
    from src.mods import registry
    return dict(registry.get_settings().get("trust") or {})


def clean_owner(v: Any) -> str:
    s = str(v or "").strip()
    m = re.match(r"^(?:https?://github\.com/)?@?([^/\s]+)/?$", s, re.I)
    s = m.group(1) if m else s
    if not catalog.OWNER_RE.match(s):
        raise ValueError(f"{str(v)[:60]!r} is not a GitHub user or organization name")
    return s


def viewer_login() -> str:
    """The GitHub login of the catalog token (GET /user), "" without a token or
    when GitHub does not answer. Kept for an hour."""
    tok, _src = catalog.token()
    if not tok:
        return ""
    key = str(hash(tok))
    with _lock:
        hit = _viewer_cache.get(key)
        if hit and time.time() - hit[0] < (VERIFY_TTL_S if hit[1] else VERIFY_FAIL_TTL_S * 5):
            return hit[1]
    try:
        login = catalog.viewer()
    except catalog.CatalogError as e:
        logger.info("[mods] trusted publishers: could not read the token's login: %s", getattr(e, "message", e))
        login = ""
    with _lock:
        _viewer_cache[key] = (time.time(), login)
    return login


def publishers() -> List[str]:
    """The trusted owners as the admin wrote them (default: the token's login)."""
    s = _stored()
    raw = s.get("publishers")
    if raw is None:
        v = viewer_login()
        return [v] if v else []
    return [o for o in raw if isinstance(o, str)]


def is_listed(owner: str, listed: Optional[Iterable[str]] = None) -> bool:
    lo = (owner or "").lower()
    return bool(lo) and lo in {p.lower() for p in (publishers() if listed is None else listed)}


def save_publishers(values: Any) -> List[str]:
    from src.mods import registry
    if isinstance(values, str):
        values = re.split(r"[\n,]", values)
    if not isinstance(values, list):
        raise ValueError("publishers must be a list of GitHub owners")
    out: List[str] = []
    for v in values:
        if not str(v or "").strip():
            continue
        o = clean_owner(v)
        if o.lower() not in {x.lower() for x in out}:
            out.append(o)
    if len(out) > MAX_PUBLISHERS:
        raise ValueError(f"at most {MAX_PUBLISHERS} trusted publishers")
    with registry._lock:
        allset = registry.get_settings()
        cur = dict(allset.get("trust") or {})
        cur["publishers"] = out
        allset["trust"] = cur
        registry.save_settings(allset)
    return out


def settings_view() -> Dict[str, Any]:
    s = _stored()
    default = s.get("publishers") is None
    return {"publishers": publishers(), "is_default": default, "viewer": viewer_login(),
            "always_ask": always_ask_list(), "high_risk": sorted(HIGH_RISK_PERMISSIONS), "ui_style": UI_STYLE}


# ── Whose mod is it ──────────────────────────────────────────────────────

def url_owner(source: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """(owner, repo) of a git source on https://github.com, else None."""
    if not isinstance(source, dict) or source.get("type") != "git":
        return None
    m = _GH_HTTPS_RE.match(str(source.get("url") or "").strip())
    return (m.group(1), m.group(2)) if m else None


def verified_owner(owner: str, repo: str) -> Optional[str]:
    """The real owner login of github.com/<owner>/<repo> according to the API,
    or None when it cannot be told (not found, private without a token, rate
    limited, offline)."""
    key = f"{owner}/{repo}".lower()
    with _lock:
        hit = _verified.get(key)
        if hit and time.time() - hit[0] < (VERIFY_TTL_S if hit[1] else VERIFY_FAIL_TTL_S):
            return hit[1]
    real: Optional[str] = None
    try:
        r = catalog.get(f"{catalog.GITHUB_API}/repos/{quote(owner)}/{quote(repo)}")
        if r.status == 200:
            d = r.json()
            login = ((d.get("owner") or {}).get("login")) if isinstance(d, dict) else None
            real = str(login) if isinstance(login, str) and login else None
    except (catalog.CatalogError, ValueError) as e:
        logger.info("[mods] trusted publishers: could not verify %s/%s: %s", owner, repo, getattr(e, "message", e))
    with _lock:
        _verified[key] = (time.time(), real)
    return real


def publisher_of(source: Optional[Dict[str, Any]], listed: Optional[List[str]] = None) -> Optional[str]:
    """The trusted owner this source really belongs to, or None. Asks GitHub
    (cached) only when the URL's owner is listed."""
    uo = url_owner(source)
    if not uo:
        return None
    lst = publishers() if listed is None else listed
    if not is_listed(uo[0], lst):
        return None
    real = verified_owner(*uo)
    return real if real and is_listed(real, lst) else None


def explain_untrusted(source: Optional[Dict[str, Any]], listed: Optional[List[str]] = None) -> str:
    uo = url_owner(source)
    if not isinstance(source, dict) or source.get("type") != "git":
        return "Installed from a folder, which is never trusted."
    if not uo:
        return "Not a https://github.com URL, so no publisher can be verified."
    lst = publishers() if listed is None else listed
    if not is_listed(uo[0], lst):
        return f"{uo[0]} is not in your trusted publishers."
    real = verified_owner(*uo)
    if not real:
        return "GitHub did not confirm who owns the repository (private, rate limited or offline)."
    if not is_listed(real, lst):
        return f"The URL says {uo[0]}, but GitHub says the repository belongs to {real}, who is not trusted."
    return ""


# ── Deciding for one install or update ───────────────────────────────────

def assess_parts(source: Optional[Dict[str, Any]], requested: List[str], new_permissions: List[str],
                 existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The trust decision for installing (or updating `existing` to) a mod
    that asks for `requested`, of which `new_permissions` are not granted yet.

    {publisher, trusted, override, always_ask, auto_approve, reason}
      publisher     the verified trusted owner, or None
      override      the admin set "Always ask me" for this mod
      always_ask    the permissions that still need a manual click
      auto_approve  install or update may go ahead without a review click
    """
    pub = publisher_of(source)
    override = bool((existing or {}).get("trust_always_ask"))
    # An update only asks again for what is new; a first install for all of it.
    judged = new_permissions if existing else requested
    must = sorted(set(judged) & ALWAYS_ASK)
    out: Dict[str, Any] = {"publisher": pub, "trusted": bool(pub), "override": override, "always_ask": must,
                           "auto_approve": False, "reason": ""}
    if not pub:
        out["reason"] = explain_untrusted(source)
    elif override:
        out["reason"] = f"You set this mod to always ask you, so {pub}'s trust is not used."
    elif must:
        out["reason"] = (f"It comes from {pub}, a trusted publisher, but it asks for {', '.join(must)}, "
                         "which always needs your click.")
    else:
        out["auto_approve"] = True
        out["reason"] = f"Auto-approved because it's from {pub}, a trusted publisher."
    return out


def assess_staged(staged: Dict[str, Any]) -> Dict[str, Any]:
    """From a registry.prepare() answer."""
    from src.mods import registry
    existing = registry.get(staged["id"]) if staged.get("previous_version") is not None else None
    requested = [p["id"] for p in staged.get("permissions") or []]
    return assess_parts(staged.get("source"), requested, list(staged.get("new_permissions") or []), existing)


def assess_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """From a staged install's meta.json (what confirm has)."""
    from src.mods import registry
    man = meta.get("manifest") or {}
    existing = registry.get(man.get("id")) if meta.get("update_of") else None
    requested = list(man.get("permissions") or [])
    granted = set((existing or {}).get("granted_permissions") or [])
    new = sorted(set(requested) - granted) if existing else requested
    return assess_parts(meta.get("source"), requested, new, existing)


# ── Installed mods ───────────────────────────────────────────────────────

def remember(mod_id: str, owner: Optional[str]) -> None:
    """Save that `mod_id` was verified as belonging to `owner` (or forget it),
    so lists that do not touch the network (Update all) can use it."""
    from src.mods import registry
    try:
        registry._update(mod_id, trust_verified={"owner": owner, "at": time.time()} if owner else None)
    except registry.RegistryError:
        pass


def refresh(entry: Dict[str, Any]) -> Optional[str]:
    """Verify an installed mod now (cached for an hour) and save the result."""
    pub = publisher_of(entry.get("source"))
    rec = entry.get("trust_verified") or {}
    if (pub or None) != (rec.get("owner") or None) or (pub and time.time() - float(rec.get("at") or 0) > 600):
        remember(entry["id"], pub)
    return pub


def saved_publisher(entry: Dict[str, Any]) -> Optional[str]:
    """The trusted owner of an installed mod from what was last verified, no
    network: the URL's owner and the verified one must both be listed now, and
    the check must be recent."""
    rec = entry.get("trust_verified") or {}
    owner = rec.get("owner")
    uo = url_owner(entry.get("source"))
    if not owner or not uo or time.time() - float(rec.get("at") or 0) > RECORD_MAX_AGE_S:
        return None
    lst = publishers()
    return owner if is_listed(uo[0], lst) and is_listed(owner, lst) else None


def would_auto_update(entry: Dict[str, Any], new_permissions: List[str]) -> bool:
    """Network free guess used for the "Review needed" marker: would a trusted
    update asking for `new_permissions` go through without a click? (apply_one
    decides for real, with GitHub.)"""
    if entry.get("trust_always_ask") or not saved_publisher(entry):
        return False
    return not (set(new_permissions) & ALWAYS_ASK)


def mod_view(entry: Dict[str, Any], verify: bool = False) -> Dict[str, Any]:
    pub = refresh(entry) if verify else saved_publisher(entry)
    login = None
    try:
        login = viewer_login() if pub else ""
    except Exception:
        login = ""
    return {"publisher": pub, "trusted": bool(pub), "always_ask": bool(entry.get("trust_always_ask")),
            "yours": bool(pub and login and pub.lower() == login.lower())}


def set_always_ask(mod_id: str, on: bool) -> Dict[str, Any]:
    from src.mods import registry
    return registry._update(mod_id, trust_always_ask=bool(on))


# ── The audit log ────────────────────────────────────────────────────────

def log_file() -> str:
    return os.path.join(paths.mods_root(), "trust-log.jsonl")


def audit(mod_id: str, event: str, *, version: Any = None, commit: Any = None, permissions: Iterable[str] = (),
          imports: Iterable[str] = (), capabilities: Iterable[str] = (), publisher: Optional[str] = None,
          by: str = "", note: str = "") -> Dict[str, Any]:
    """One line of history. `event`: auto_installed, auto_updated (approved by
    the trusted publisher rule) or approved (an admin clicked)."""
    perms = sorted(set(permissions))
    rec = {"ts": time.time(), "mod": mod_id, "event": event, "version": version, "commit": (str(commit)[:12] if commit else None),
           "permissions": perms, "imports": sorted(set(imports)), "capabilities": sorted(set(capabilities)),
           "publisher": publisher, "by": by or None, "note": note or None}
    rec["text"] = _text(rec)
    try:
        os.makedirs(paths.mods_root(), exist_ok=True)
        p = log_file()
        try:
            if os.path.getsize(p) > MAX_LOG_BYTES:
                with open(p, "rb") as f:
                    tail = f.read()[-MAX_LOG_BYTES // 2:]
                with open(p, "wb") as f:
                    f.write(tail[tail.find(b"\n") + 1:])
        except OSError:
            pass
        with _lock, open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        logger.warning("[mods] trust log: %s", e)
    return rec


def _text(r: Dict[str, Any]) -> str:
    what = {"auto_installed": "Installed", "auto_updated": "Updated", "approved": "Approved"}.get(r["event"], r["event"])
    ver = f" {r['version']}" if r.get("version") else ""
    com = f" ({r['commit']})" if r.get("commit") else ""
    granted = ", ".join(r["permissions"]) if r["permissions"] else "no permissions"
    extra = []
    if r.get("imports"):
        extra.append("imports " + ", ".join(r["imports"]))
    if r.get("capabilities"):
        extra.append("capabilities " + ", ".join(r["capabilities"]))
    tail = f"; {'; '.join(extra)}" if extra else ""
    if r["event"] in ("auto_installed", "auto_updated"):
        who = f"trusted publisher {r['publisher']}"
    else:
        who = f"approved by {r['by'] or 'an admin'}"
    return f"{what}{ver}{com}: granted {granted}{tail}; {who}"


def history(mod_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """The mod's lines, newest first."""
    out: List[Dict[str, Any]] = []
    try:
        with open(log_file(), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("mod") == mod_id:
                    out.append(r)
    except OSError:
        return []
    return list(reversed(out))[:limit]


def audit_confirmed(entry: Dict[str, Any], meta: Dict[str, Any], decision: Dict[str, Any], *, auto: bool,
                    by: str = "") -> None:
    """After a confirm: the line for what was granted."""
    from src.mods import capabilities
    man = entry.get("manifest") or meta.get("manifest") or {}
    update = bool(meta.get("update_of"))
    if auto:
        event = "auto_updated" if update else "auto_installed"
    else:
        event = "approved"
    audit(entry["id"], event, version=entry.get("version"), commit=entry.get("commit"),
          permissions=entry.get("granted_permissions") or [], imports=entry.get("granted_imports") or [],
          capabilities=capabilities.declared(man), publisher=decision.get("publisher"), by=by)


# ── Routes ───────────────────────────────────────────────────────────────

def setup_routes(router) -> None:
    """GET/PUT /api/mods/trust (admin only), GET /api/mods/{id}/history. Set up
    before the /api/mods/{mod_id} routes so "trust" is not read as a mod id."""
    import asyncio

    @router.get("/api/mods/trust")
    async def trust_get(request: Request):
        from src.mods import routes
        routes._require_admin(request)
        return await asyncio.to_thread(settings_view)

    @router.put("/api/mods/trust")
    async def trust_put(request: Request):
        from src.mods import routes
        routes._require_admin(request)
        body = await routes._json(request)
        try:
            await asyncio.to_thread(save_publishers, body.get("publishers"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        clear_cache()
        return await asyncio.to_thread(settings_view)

    @router.get("/api/mods/{mod_id}/history")
    async def trust_history(request: Request, mod_id: str):
        from src.mods import registry, routes
        routes._require_admin(request)
        if not registry.get(mod_id):
            raise HTTPException(404, "No such mod")
        return {"history": history(mod_id)}
