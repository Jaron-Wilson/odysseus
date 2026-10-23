"""The guided setup checklist (Settings > Setup), computed from real state.

GET /api/setup/status (routes/setup_routes.py) calls build(). Nothing here is
a static list of "things to do": every step asks the server what is true now
(is there an admin, does a model answer, which linked computer lacks which
helper feature, which mod has an update) and says so in plain words.

A step is a dict:

    id           stable, e.g. "essentials.model" or "devices.computer.laptop"
    group        "essentials" | "devices" | "accounts" | "mods"
    title        what to do, a short sentence
    gives        what it gives you, in plain words
    status       "done" | "needs_you" | "optional" | "not_applicable"
    detail       the live fact behind the status ("2 models ready")
    action       the main button: {kind, label, ...} (see below) or None
    dismissible  the person may hide it ("Not for me")
    dismissed    they did

Action kinds (the page, static/js/setupGuide.js, knows each):
    settings            {"target": a Settings place, "then": optional follow-up like "enroll:computer"}
    mod                 {"link": "#mod/<id>/<tab>?path=..."}
    browse-mods         {"query": ...} opens Settings > Mods > Browse
    enable-notifications  turns notifications on in this browser
    install-tools       {"server_id": ...} installs/updates a linked computer's helper

"left" counts steps that need the person (status needs_you, not dismissed).
Optional steps never hold the checklist open; they only decorate it.

Non-admins get only the steps they can do themselves (their own account,
notifications, their Google and phone number, mod steps for mods they may
use). Dismissals are per user, in the user's own preferences.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PREF_KEY = "setup_guide"
MAX_DISMISSED = 300
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,120}$")
DEVICE_TIMEOUT_S = 14.0

GROUPS = [
    ("essentials", "Essentials",
     "The few things Odysseus needs before it is useful: an account, an AI model, and a way to reach you."),
    ("devices", "Your devices",
     "Odysseus can open apps, play music and use the screen on your own computers and phone. Each computer needs "
     "the small Odysseus helper installed on it once, and each phone needs to be paired once."),
    ("accounts", "Connect accounts",
     "Optional. Connect the accounts you want Odysseus to read and act on."),
    ("mods", "Mods",
     "Mods are add-ons that give Odysseus new pages, tools and abilities. Optional."),
]

DONE, NEEDS, OPTIONAL, NA = "done", "needs_you", "optional", "not_applicable"


def step(id: str, group: str, title: str, gives: str, status: str, detail: str = "",
         action: Optional[Dict[str, Any]] = None, dismissible: bool = False, **extra) -> Dict[str, Any]:
    out = {"id": id, "group": group, "title": title, "gives": gives, "status": status, "detail": detail,
           "action": action, "dismissible": bool(dismissible) and status != DONE, "dismissed": False}
    out.update(extra)
    return out


def safe_part(text: str) -> str:
    """Make a host name or id usable inside a step id."""
    return re.sub(r"[^a-z0-9._-]+", "-", str(text or "").lower()).strip("-")[:60] or "x"


def goto(label: str, target: str, then: str = "") -> Dict[str, Any]:
    return {"kind": "settings", "label": label, "target": target, "then": then}


def helper_missing(computer: str, feature: str) -> str:
    """What to tell someone when a linked computer lacks a helper feature, and
    where to fix it: the one place that installs it."""
    return (f"{computer} cannot {feature} yet: the Odysseus helper on it is missing that feature or is out of date. "
            f"Open Settings > Devices and press Install (or Update) next to {computer}, or Settings > Setup, "
            "which lists it.")


# ── Remembered choices (per user) ───────────────────────────────────────

def load_state(user: Optional[str]) -> Dict[str, Any]:
    from routes import prefs_routes
    raw = prefs_routes._load_for_user(user or None).get(PREF_KEY)
    raw = raw if isinstance(raw, dict) else {}
    dismissed = [d for d in raw.get("dismissed", []) if isinstance(d, str) and ID_RE.match(d)]
    hidden = raw.get("card_hidden")
    return {"dismissed": dismissed[:MAX_DISMISSED], "seen": bool(raw.get("seen")),
            "card_hidden": [h for h in hidden if isinstance(h, str)] if isinstance(hidden, list) else None}


def save_state(user: Optional[str], state: Dict[str, Any]) -> None:
    from routes import prefs_routes
    prefs = prefs_routes._load_for_user(user or None)
    prefs[PREF_KEY] = {"dismissed": list(state.get("dismissed", []))[-MAX_DISMISSED:],
                       "seen": bool(state.get("seen")), "card_hidden": state.get("card_hidden")}
    prefs_routes._save_for_user(user or None, prefs)


# ── Essentials ──────────────────────────────────────────────────────────

def account_steps(user: str, is_admin: bool, auth_enabled: bool, totp: bool) -> List[Dict[str, Any]]:
    if not auth_enabled:
        return [step("essentials.account", "essentials", "Sign-in is turned off",
                     "Anyone who can reach this server has full access, so keep it on a private network.",
                     NA, "AUTH_ENABLED is false on this server.")]
    who = "Your admin account" if is_admin else "Your account"
    out = [step("essentials.account", "essentials", who,
                "The sign-in that owns this Odysseus" + (" and can change every setting." if is_admin
                                                         else ", your chats and your settings."),
                DONE, f"Signed in as {user}.")]
    out.append(step("essentials.2fa", "essentials", "Add a second sign-in step",
                    "A code from an authenticator app on your phone, so a stolen password alone is not enough.",
                    DONE if totp else OPTIONAL,
                    "Two-step sign-in is on." if totp else "Not on yet.",
                    None if totp else goto("Turn on two-step sign-in", "tab:account"), dismissible=True))
    return out


def model_steps(is_admin: bool, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chat = [i for i in items if (i.get("model_type") or "llm") in ("llm", "chat")]
    ready = [i for i in chat if i.get("models")]
    down = [i for i in chat if not i.get("models")]
    if ready:
        n = sum(len(i["models"]) for i in ready)
        detail = f"{n} model{'s' if n != 1 else ''} ready from {len(ready)} connection{'s' if len(ready) != 1 else ''}."
        if down:
            detail += f" {len(down)} other connection{'s are' if len(down) != 1 else ' is'} not answering."
        return [step("essentials.model", "essentials", "An AI model that answers",
                     "The brain behind every chat. Without one, Odysseus has nothing to answer with.",
                     DONE, detail)]
    if down:
        detail = (f"{len(down)} connection{'s are' if len(down) != 1 else ' is'} added but not answering. "
                  "Check that the server is running and the key is right.")
    else:
        detail = "No AI model is connected yet."
    if is_admin:
        action = goto("Add an AI model", "tab:services")
        detail += " Pick a provider, paste its key (or point at a model running on your own computer)."
    else:
        action = None
        detail += " Ask an admin to add one."
    return [step("essentials.model", "essentials", "An AI model that answers",
                 "The brain behind every chat. Without one, Odysseus has nothing to answer with.",
                 NEEDS, detail, action)]


def push_steps(subscriptions: int, here: Optional[str]) -> List[Dict[str, Any]]:
    """`here`: what this browser says: "on", "off", "unsupported", or None (not asked)."""
    gives = "Reminders, finished jobs and urgent mail reach you even when this page is closed."
    if here == "on":
        return [step("essentials.notifications", "essentials", "Notifications on this device", gives, DONE,
                     "This browser will get them.")]
    if here == "unsupported":
        return [step("essentials.notifications", "essentials", "Notifications on this device", gives, OPTIONAL,
                     "This browser cannot show them on this address. They need https: open Odysseus at its "
                     "https address on this device.", None, dismissible=True)]
    if here is None and subscriptions:
        return [step("essentials.notifications", "essentials", "Notifications on this device", gives, DONE,
                     f"{subscriptions} device{'s' if subscriptions != 1 else ''} will get them.")]
    detail = ("Another device of yours has them, but not this one." if subscriptions
              else "No device is turned on yet.")
    return [step("essentials.notifications", "essentials", "Notifications on this device", gives, NEEDS, detail,
                 {"kind": "enable-notifications", "label": "Turn on notifications",
                  "target": "set-push-toggle"}, dismissible=True)]


# ── Your devices (admins) ───────────────────────────────────────────────

_PHONE_OS = ("android", "ios", "ipados")
_HELPER = ("Each computer needs the small Odysseus helper installed once. Open Add a computer, then paste the "
           "one command it shows into a terminal on that computer.")


def device_steps(ov: Dict[str, Any], tools: List[Dict[str, Any]], registered: int) -> List[Dict[str, Any]]:
    """From the Devices overview (routes/device_routes.py), what machine_tools
    (routes/enroll_routes.py) found, and how many devices are registered."""
    out: List[Dict[str, Any]] = []
    net = ov.get("network") or {}
    peers_ok = bool(ov.get("tailscale"))
    if not peers_ok:
        if net.get("installed_off"):
            detail, action = ("The Tailscale add-on is installed but turned off. Turn it on in Settings > Mods.",
                              goto("Open Mods", "tab:mods"))
        elif net.get("provider"):
            detail, action = ((f"{(net['provider'] or {}).get('name', 'Tailscale')} found no machines"
                               + (f": {net['error']}" if net.get("error") else "")
                               + ". Is Tailscale running on this server and signed in?"),
                              goto("Open Devices", "tab:devices"))
        else:
            detail, action = ("Install Tailscale on this server and on each device (tailscale.com/download), then "
                              "install the Tailscale add-on here.",
                              {"kind": "browse-mods", "label": "Get the Tailscale add-on", "query": "tailscale"})
        out.append(step("devices.network", "devices", "Connect your devices with Tailscale",
                        "Tailscale is how Odysseus finds your computers and phone, from anywhere, without "
                        "opening your network to the internet.", NEEDS, detail, action, dismissible=True))
        out.append(step("devices.computers", "devices", "Link your computers",
                        "Lets Odysseus open apps, play music and use the screen on them. " + _HELPER,
                        NA, "Needs Tailscale first, so Odysseus can see them."))
        return out
    n = len(ov.get("machines") or []) + len(ov.get("others") or [])
    out.append(step("devices.network", "devices", "Connect your devices with Tailscale",
                    "Tailscale is how Odysseus finds your computers and phone.", DONE,
                    f"{n} device{'s' if n != 1 else ''} on your tailnet."))

    by_host = {str(t.get("host") or ""): t for t in tools if t.get("host")}
    computers = [m for m in ov.get("machines") or []
                 if not m.get("is_self") and (m.get("os") or "").lower() not in _PHONE_OS]
    phones = [m for m in ov.get("machines") or [] if (m.get("os") or "").lower() in _PHONE_OS]
    for m in computers:
        label = m.get("label") or m.get("name") or m.get("host")
        sid = f"devices.computer.{safe_part(m.get('host'))}"
        gives = (f"Lets Odysseus open apps, play music and use the screen on {label}. " + _HELPER)
        t = by_host.get(str(m.get("host") or ""))
        linked = bool(m.get("servers"))
        if not linked:
            out.append(step(sid, "devices", f"Link {label}", gives, NEEDS,
                            (f"{label} is {'online' if m.get('online') else 'offline: turn it on first'}. "
                             "The helper is not installed on it yet."),
                            goto("Add this computer", "devices-add-card", "enroll:computer"), dismissible=True,
                            subject=label))
        elif t and t.get("connected") and t.get("up_to_date"):
            out.append(step(sid, "devices", f"{label}: helper installed", gives, DONE,
                            "Every Odysseus feature is installed and current."))
        elif t and t.get("connected"):
            bits = []
            if t.get("missing"):
                bits.append("can add " + ", ".join(t["missing"]))
            if t.get("outdated"):
                bits.append("an update is available")
            action = ({"kind": "install-tools", "label": ("Install and update" if t.get("missing") and t.get("outdated")
                                                          else "Install" if t.get("missing") else "Update"),
                       "server_id": t["server_id"]} if t.get("online") else goto("Open Devices", "tab:devices"))
            out.append(step(sid, "devices", f"Update the helper on {label}", gives, NEEDS,
                            f"{label} " + "; ".join(bits) + ("." if t.get("online") else ". It is offline right now."),
                            action, dismissible=True, subject=label))
        else:
            out.append(step(sid, "devices", f"Reconnect {label}", gives, NEEDS,
                            f"{label} has the helper but Odysseus cannot reach it. "
                            + ("Turn it on, then press Ping." if not m.get("online") else "Press Ping on its row."),
                            goto("Open Devices", "tab:devices"), dismissible=True, subject=label))
    if not computers:
        out.append(step("devices.computers", "devices", "Link a computer",
                        "Lets Odysseus open apps, play music and use the screen on it. " + _HELPER,
                        OPTIONAL, "Odysseus does not see another computer on your tailnet yet. Install Tailscale "
                                  "on it first, then link it here.",
                        goto("Add a computer", "devices-add-card", "enroll:computer"), dismissible=True))
    for m in phones:
        label = m.get("label") or m.get("name") or m.get("host")
        sid = f"devices.phone.{safe_part(m.get('host'))}"
        gives = f"Lets Odysseus send {label} notifications and run commands on it."
        if m.get("phones"):
            out.append(step(sid, "devices", f"{label}: paired", gives, DONE, "Paired."))
        else:
            out.append(step(sid, "devices", f"Pair {label}", gives, NEEDS,
                            "Open Pair a phone, then scan the code with the phone's camera.",
                            goto("Pair this phone", "devices-add-card", "enroll:phone"), dismissible=True,
                            subject=label))
    if not phones:
        done = registered > 0
        out.append(step("devices.phone", "devices", "Pair your phone",
                        "Lets Odysseus send your phone notifications and run commands on it.",
                        DONE if done else OPTIONAL,
                        f"{registered} device{'s' if registered != 1 else ''} paired." if done
                        else "No phone is paired yet.",
                        None if done else goto("Pair a phone", "devices-add-card", "enroll:phone"),
                        dismissible=True))
    return out


# ── Connect accounts ────────────────────────────────────────────────────

def account_connect_steps(user: str, is_admin: bool, email_accounts: int, google: Dict[str, Any],
                          sms_numbers: int) -> List[Dict[str, Any]]:
    out = [step("accounts.email", "accounts", "Connect your email",
                "Odysseus can read, summarize and draft replies to your mail.",
                DONE if email_accounts else OPTIONAL,
                f"{email_accounts} mailbox{'es' if email_accounts != 1 else ''} connected." if email_accounts
                else "No mailbox is connected yet.",
                None if email_accounts else goto("Add a mailbox", "tab:integrations"), dismissible=True)]
    gives = "Odysseus can use your Google Calendar, Drive, Docs, Sheets, Tasks and Contacts."
    if google.get("connected"):
        lost = [p for p in google.get("products") or [] if p.get("needs_reconnect")]
        if lost:
            out.append(step("accounts.google", "accounts", "Reconnect Google", gives, NEEDS,
                            "Google no longer allows some of what you turned on. Connect it again.",
                            goto("Open Google", "tab:google")))
        else:
            out.append(step("accounts.google", "accounts", "Connect your Google account", gives, DONE,
                            f"Connected as {google.get('email') or 'your Google account'}."))
    elif google.get("configured"):
        out.append(step("accounts.google", "accounts", "Connect your Google account", gives, OPTIONAL,
                        "Not connected yet.", goto("Connect Google", "tab:google"), dismissible=True))
    elif is_admin:
        out.append(step("accounts.google", "accounts", "Set up Google", gives, OPTIONAL,
                        "Google needs a one-time setup first: you paste an OAuth client from Google Cloud "
                        "(steps are in docs/google.md).", goto("Open Google", "tab:google"), dismissible=True))
    out.append(step("accounts.phone", "accounts", "Get texts from Odysseus",
                    "Odysseus can text your phone when something needs you, and can be asked things by text.",
                    DONE if sms_numbers else OPTIONAL,
                    f"{sms_numbers} number{'s' if sms_numbers != 1 else ''} added." if sms_numbers
                    else "No phone number is added yet.",
                    None if sms_numbers else goto("Add your number", "sms-numbers"), dismissible=True))
    return out


# ── Mods ────────────────────────────────────────────────────────────────

def mod_steps(installed: List[Dict[str, Any]], recommended: List[Dict[str, Any]], updates: List[Dict[str, Any]],
              imports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = [step("mods.browse", "mods", "Browse mods",
                "Add-ons for music, 3D printing, your phone, games and more. Nothing is installed until you "
                "approve it.", DONE if installed else OPTIONAL,
                f"{len(installed)} installed." if installed else "None installed yet.",
                {"kind": "browse-mods", "label": "Browse mods", "query": ""}, dismissible=True)]
    for r in recommended[:3]:
        out.append(step(f"mods.recommended.{safe_part(r.get('id') or r.get('repo'))}", "mods",
                        f"Try {r.get('name') or r.get('repo')}", r.get("summary") or "A recommended mod.",
                        OPTIONAL, "Recommended and verified.",
                        {"kind": "browse-mods", "label": "See it in Browse", "query": r.get("name") or r.get("repo")},
                        dismissible=True))
    if updates:
        names = ", ".join(u.get("name") or u.get("id") for u in updates[:3])
        out.append(step("mods.updates", "mods", f"Update {len(updates)} mod{'s' if len(updates) != 1 else ''}",
                        "Newer versions have fixes and new features. You see what changed before applying.",
                        NEEDS, names + (" and more" if len(updates) > 3 else "") + " can be updated.",
                        goto("Review updates", "tab:mods"), dismissible=True))
    for i in imports:
        out.append(step(f"mods.import.{safe_part(i['mod_id'])}.{safe_part(i['slice'])}", "mods",
                        f"Bring your old {i['title'].lower()} into {i['mod_name']}",
                        "This used to be built into Odysseus. Importing it once means you do not type it again.",
                        NEEDS, "Your old settings are still here, waiting to be imported.",
                        goto("Open Mods", "tab:mods"), dismissible=True))
    return out


def from_mod_steps(found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for f in found:
        s = f["step"]
        ans = f.get("answer")
        needs = NEEDS if not s.get("optional", True) else OPTIONAL
        if ans is not None:
            status, detail = (DONE if ans["done"] else needs), ans.get("detail") or ""
        else:
            status = needs
            detail = ("" if not s.get("status_path") else
                      f"Not checked yet: open {f['mod_name']} so it can say how this is going.")
        out.append(step(f"mods.step.{safe_part(f['mod_id'])}.{safe_part(s['id'])}", "mods",
                        s["title"], s.get("description") or "", status, detail, f.get("action"),
                        dismissible=True, mod=f["mod_name"]))
    return out


# ── Gathering the facts ─────────────────────────────────────────────────

def _models(user: str, is_admin: bool) -> List[Dict[str, Any]]:
    from routes.model_routes import fetch_models
    return fetch_models(owner=user or "", is_admin=is_admin).get("items") or []


def _auth(request) -> Dict[str, Any]:
    from src.auth_helpers import _auth_disabled
    mgr = getattr(request.app.state, "auth_manager", None)
    return {"mgr": mgr, "enabled": not _auth_disabled() and bool(mgr and getattr(mgr, "is_configured", False))}


def _email_accounts(user: str) -> int:
    from core.database import EmailAccount, SessionLocal
    db = SessionLocal()
    try:
        q = db.query(EmailAccount)
        if user:
            q = q.filter(EmailAccount.owner == user)
        return q.count()
    finally:
        db.close()


def _google(user: str) -> Dict[str, Any]:
    from src.google_workspace import connection
    return connection.status(user or None)


def _sms_numbers(user: str) -> int:
    from routes import sms_routes
    return len(sms_routes.get_config(user or None).get("numbers") or [])


def _push_count(user: str) -> int:
    from src import webpush
    return sum(1 for s in webpush.load_subscriptions() if (s.get("owner") or "") == (user or ""))


_device_cache: Dict[str, Any] = {"at": 0.0, "facts": None}
DEVICE_CACHE_S = 20.0


def forget_devices() -> None:
    _device_cache.update(at=0.0, facts=None)


async def _devices(mgr) -> Dict[str, Any]:
    """The Devices overview and each computer's helper state. Checking a
    helper asks the computer itself, so the answer is kept for a few seconds."""
    if _device_cache["facts"] and time.time() - _device_cache["at"] < DEVICE_CACHE_S:
        return _device_cache["facts"]
    facts = await _devices_now(mgr)
    _device_cache.update(at=time.time(), facts=facts)
    return facts


async def _devices_now(mgr) -> Dict[str, Any]:
    from routes import device_routes, enroll_routes
    from src import devices
    ov = await asyncio.to_thread(device_routes._build_overview, False)
    try:
        tools = [{k: v for k, v in m.items() if k != "_peer"} for m in await enroll_routes.machine_tools(mgr)]
    except Exception as e:
        logger.debug("setup: machine tools unavailable (%s)", e)
        tools = []
    return {"ov": ov, "tools": tools, "registered": len(devices.list_devices())}


def _mods_facts(user: str) -> Dict[str, Any]:
    from src.mods import catalog, legacy, registry, updates
    mods = registry.list_mods()
    ups: Dict[str, Dict[str, Any]] = {}
    try:
        for u in updates.view().get("needs_update") or []:
            ups[u["id"]] = {"id": u["id"], "name": u.get("name")}
        for mid in catalog.updates():
            ups.setdefault(mid, {"id": mid, "name": (registry.get(mid) or {}).get("name") or mid})
    except Exception as e:
        logger.debug("setup: update info unavailable (%s)", e)
    imports = []
    for m in mods:
        if not m.get("enabled"):
            continue
        for sl in (m.get("manifest") or {}).get("imports") or []:
            spec = legacy.SLICES.get(sl)
            if spec and not legacy.imported_at(m, sl) and legacy.available(sl):
                imports.append({"mod_id": m["id"], "mod_name": m.get("name") or m["id"], "slice": sl,
                                "title": spec["title"]})
    recommended = []
    try:
        cached = catalog.cached() or {}
        have = {(m.get("source") or {}).get("url", "").lower() for m in mods}
        ids = {m["id"] for m in mods}
        for e in (cached.get("entries") or {}).values():
            man = e.get("manifest") or {}
            ix = e.get("index") or {}
            if not (ix.get("verified") or e.get("verified")) or not man.get("valid") or not man.get("compatible"):
                continue
            if man.get("id") in ids or (e.get("html_url") or "").lower() in have:
                continue
            recommended.append({"id": man.get("id"), "repo": e.get("repo"), "name": man.get("name") or e.get("repo"),
                                "summary": (man.get("description") or e.get("description") or "")[:200]})
        recommended.sort(key=lambda r: str(r.get("name") or "").lower())
    except Exception as e:
        logger.debug("setup: catalog unavailable (%s)", e)
    return {"installed": [{"id": m["id"]} for m in mods], "recommended": recommended,
            "updates": list(ups.values()), "imports": imports}


def _safe(name: str, fn, default):
    try:
        return fn()
    except Exception as e:
        logger.warning("setup guide: %s unavailable (%s: %s)", name, type(e).__name__, e)
        return default


async def build(request, user: str, is_admin: bool, here: Optional[str] = None, mcp_manager=None) -> Dict[str, Any]:
    """The whole checklist for this user."""
    auth = _auth(request)
    totp = bool(auth["enabled"] and user and _safe("2fa", lambda: auth["mgr"].totp_enabled(user), False))
    steps: List[Dict[str, Any]] = []
    steps += account_steps(user, is_admin, auth["enabled"], totp)
    steps += _safe("models", lambda: model_steps(is_admin, _models(user, is_admin)),
                   model_steps(is_admin, []))
    steps += push_steps(_safe("push", lambda: _push_count(user), 0), here)

    if is_admin:
        try:
            facts = await asyncio.wait_for(_devices(mcp_manager), DEVICE_TIMEOUT_S)
            steps += device_steps(facts["ov"], facts["tools"], facts["registered"])
        except Exception as e:
            logger.warning("setup guide: devices unavailable (%s: %s)", type(e).__name__, e)

    google = _safe("google", lambda: _google(user), {})
    steps += account_connect_steps(user, is_admin, _safe("email", lambda: _email_accounts(user), 0), google,
                                   _safe("sms", lambda: _sms_numbers(user), 0))

    if is_admin:
        mf = _safe("mods", lambda: _mods_facts(user), {"installed": [], "recommended": [], "updates": [], "imports": []})
        steps += mod_steps(mf["installed"], mf["recommended"], mf["updates"], mf["imports"])
    try:
        from src.mods import setup_steps
        steps += from_mod_steps(await setup_steps.collect(user or None))
    except Exception as e:
        logger.warning("setup guide: mod steps unavailable (%s: %s)", type(e).__name__, e)

    out = assemble(steps, load_state(user))
    out["is_admin"] = bool(is_admin)
    return out


def assemble(steps: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Group the steps, mark what was dismissed, count what is left."""
    dismissed = set(state.get("dismissed") or [])
    for s in steps:
        s["dismissed"] = s["dismissible"] and s["id"] in dismissed
    groups = []
    for gid, title, intro in GROUPS:
        items = [s for s in steps if s["group"] == gid]
        if items:
            groups.append({"id": gid, "title": title, "intro": intro, "steps": items})
    live = [s for s in steps if not s["dismissed"] and s["status"] != NA]
    needs = [s["id"] for s in live if s["status"] == NEEDS]
    optional = [s["id"] for s in live if s["status"] == OPTIONAL]
    hidden = state.get("card_hidden")
    card_hidden = hidden is not None and all(i in set(hidden) for i in needs)
    return {"groups": groups,
            "summary": {"left": len(needs), "optional_left": len(optional),
                        "done": sum(1 for s in steps if s["status"] == DONE), "total": len(steps),
                        "needs": needs, "complete": not needs},
            "seen": bool(state.get("seen")), "card_hidden": card_hidden, "built_at": time.time()}
