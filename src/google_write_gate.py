"""Ask before any write to the user's Google account (and Gmail).

Asked for: "if its trying to use my google account other than reading please
make it ask me".

`intercept` runs in src/tool_execution.py's execute_tool_block for every tool
call, whatever the model, native function calls or fenced blocks. A call that
would change something in the user's Google account does not run: it is held
in src/action_approvals.py and the chat shows what it would do, with Approve
and Deny. What counts:

  Google Workspace   (Drive, Docs, Sheets, Slides, Tasks) is the Google
                     add-on's (mod `google`): its writing tools are
                     `asks_first`, held by src/mods/tools.py the same way
  Google Meet        made by the Phone & Messages mod: its meet_create tool is
                     `asks_first`, so the mod tool flow asks (src/mods/tools.py)
  Google Calendar    manage_calendar create_event / update_event /
                     delete_event (and a batch of events) when the calendar
                     is linked through the connected Google account (source
                     "google", src/google_calendar_sync.py) or is a CalDAV
                     calendar on Google (apidata.googleusercontent.com or
                     google.com/calendar/dav); other CalDAV servers and local
                     calendars are not asked about. The Calendar page's own
                     create/edit/delete buttons are the user acting directly
                     and do not ask
  Gmail              send_email, reply_to_email, archive_email, delete_email,
                     mark_email_read and bulk_email on a mailbox whose IMAP or
                     SMTP server is Google's (imap.gmail.com, smtp.gmail.com,
                     Google Workspace) or whose address is @gmail.com
  app_api            writes (POST/PUT/PATCH/DELETE) to /api/email, /api/calendar,
                     /api/caldav or /api/google are refused while this
                     is on: the named tools above are the way, and they ask

Reads (list_events,
list_calendars, list/read/search email, drafts saved in Odysseus) never ask.

Scheduled tasks and the mail listener hold the call
the same way and send a notification, so nothing runs unseen. Coding agents
(claude_code, /claude direct) run their own tools outside this path and are
not covered.

The switch (DATA_DIR/google_write_approval.json, default on) lives in
Settings > Google. Only an admin can turn it off. It is a file of its own, not
a key in settings.json, so manage_settings cannot reach it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

EMAIL_WRITE_TOOLS = ("send_email", "reply_to_email", "archive_email", "delete_email",
                     "mark_email_read", "bulk_email")
BARE_EMAIL_TOOLS = ("list_email_accounts", "send_email", "list_emails", "read_email", "reply_to_email",
                    "archive_email", "delete_email", "mark_email_read", "bulk_email", "download_attachment",
                    "search_emails", "draft_email", "draft_email_reply", "ai_draft_email_reply")
CAL_WRITE_ACTIONS = ("create_event", "update_event", "delete_event")
APP_API_GUARDED = ("/api/email", "/api/calendar", "/api/caldav", "/api/google")

_GOOGLE_MAIL_HOSTS = ("gmail.com", "googlemail.com", "google.com")
_GOOGLE_ADDRESS = re.compile(r"@(gmail|googlemail)\.com\s*>?\s*$", re.I)


# ── The switch ───────────────────────────────────────────────────────────

def _settings_file() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "google_write_approval.json")


def settings() -> Dict[str, Any]:
    try:
        with open(_settings_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return {"enabled": d.get("enabled") is not False, "changed_by": d.get("changed_by") or "",
                    "changed_at": d.get("changed_at")}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        pass
    return {"enabled": True, "changed_by": "", "changed_at": None}


def enabled() -> bool:
    return settings()["enabled"]


def set_enabled(on: bool, by: str = "") -> Dict[str, Any]:
    from core.atomic_io import atomic_write_json
    d = {"enabled": bool(on), "changed_by": by or "", "changed_at": time.time()}
    atomic_write_json(_settings_file(), d, indent=1)
    logger.warning("[google-approval] ask before Google writes turned %s by %s", "on" if on else "OFF", by or "-")
    return settings()


# ── Helpers ──────────────────────────────────────────────────────────────

def normalize(tool: str) -> str:
    t = str(tool or "")
    if t.startswith("mcp__email__"):
        return t[len("mcp__email__"):]
    return t


def _args(content: str) -> Dict:
    raw = (content or "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _short(s: Any, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _email_rows() -> List[dict]:
    """The enabled mailboxes, in the order the email tools pick them
    (mcp_servers/email_server.py:_list_accounts_raw)."""
    try:
        from core.database import EmailAccount, SessionLocal
        db = SessionLocal()
        try:
            q = db.query(EmailAccount).filter(EmailAccount.enabled == True)  # noqa: E712
            rows = q.order_by(EmailAccount.is_default.desc(), EmailAccount.created_at.asc()).all()
            return [{"id": r.id, "name": r.name or "", "is_default": bool(r.is_default),
                     "imap_host": r.imap_host or "", "smtp_host": r.smtp_host or "",
                     "imap_user": r.imap_user or "", "from_address": r.from_address or ""} for r in rows]
        finally:
            db.close()
    except Exception as e:
        logger.debug("email accounts lookup failed: %s", e)
        return []


def _resolve_mailbox(selector: Optional[str]) -> Optional[dict]:
    """Same matching as the email tools: none = default, else id, then a
    substring of name, user or address, then a close match."""
    rows = _email_rows()
    if not rows:
        return _legacy_mailbox()
    if not selector:
        return next((r for r in rows if r["is_default"]), rows[0])
    sel = str(selector).strip().lower()
    for r in rows:
        if r["id"] == selector:
            return r
    for r in rows:
        if any(sel in (f or "").lower() for f in (r["name"], r["imap_user"], r["from_address"])):
            return r
    try:
        from difflib import get_close_matches
        cands = {}
        for r in rows:
            for f in (r["name"], r["imap_user"], r["from_address"]):
                if f:
                    cands[f.lower()] = r
        close = get_close_matches(sel, list(cands), n=1, cutoff=0.72)
        if close:
            return cands[close[0]]
    except Exception:
        pass
    return None


def _legacy_mailbox() -> Optional[dict]:
    """No email_accounts rows: the tools fall back to settings.json / env."""
    try:
        from src.settings import load_settings
        s = load_settings()
    except Exception:
        s = {}
    imap = str(s.get("imap_host") or os.environ.get("IMAP_HOST") or "")
    smtp = str(s.get("smtp_host") or os.environ.get("SMTP_HOST") or "")
    user = str(s.get("imap_user") or os.environ.get("IMAP_USER") or "")
    addr = str(s.get("from_address") or os.environ.get("EMAIL_FROM") or "")
    if not (imap or smtp or user or addr):
        return None
    return {"id": "", "name": "Email", "imap_host": imap, "smtp_host": smtp, "imap_user": user,
            "from_address": addr}


def _host_is_google(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in _GOOGLE_MAIL_HOSTS)


def is_gmail(box: Optional[dict]) -> bool:
    if not box:
        return False
    if _host_is_google(box.get("imap_host")) or _host_is_google(box.get("smtp_host")):
        return True
    return any(_GOOGLE_ADDRESS.search(str(box.get(k) or "")) for k in ("imap_user", "from_address"))


def _mailbox_label(box: dict) -> str:
    addr = box.get("from_address") or box.get("imap_user") or ""
    name = box.get("name") or ""
    if name and addr and name.lower() != addr.lower():
        return f"{name} ({addr})"
    return name or addr or "your Gmail"


def _describe_email(tool: str, a: Dict) -> Optional[Dict]:
    box = _resolve_mailbox(a.get("account"))
    if not is_gmail(box):
        return None
    acct = _mailbox_label(box)
    folder = str(a.get("folder") or "INBOX")
    uid = str(a.get("uid") or "")
    details: List[Tuple[str, str]] = [("Account", acct)]
    body, body_label = "", ""
    if tool == "send_email":
        to = str(a.get("to") or "")
        title = f"Send an email to {_short(to, 120) or '(no recipient)'} from {acct}"
        details += [("To", to)]
        if a.get("cc"):
            details.append(("Cc", str(a["cc"])))
        if a.get("bcc"):
            details.append(("Bcc", str(a["bcc"])))
        details.append(("Subject", str(a.get("subject") or "(no subject)")))
        body, body_label, action = str(a.get("body") or ""), "Message", "Send email"
    elif tool == "reply_to_email":
        title = (f"Reply{' to all' if a.get('reply_all') else ''} to email {uid} in {folder}, "
                 f"sent from {acct}")
        details += [("Email", f"UID {uid} in {folder}"), ("Reply to all", "yes" if a.get("reply_all") else "no")]
        body, body_label, action = str(a.get("body") or ""), "Reply", "Reply to email"
    elif tool == "archive_email":
        title, action = f"Archive email {uid} in {folder} ({acct})", "Archive email"
        details.append(("Email", f"UID {uid} in {folder}"))
    elif tool == "delete_email":
        perm = bool(a.get("permanent"))
        title = f"Delete email {uid} in {folder} ({acct})" + (", permanently" if perm else ", to Trash")
        action = "Delete email"
        details += [("Email", f"UID {uid} in {folder}"), ("Permanent", "yes, cannot be undone" if perm else "no, moves to Trash")]
    elif tool == "mark_email_read":
        read = a.get("read", True) is not False
        title, action = f"Mark email {uid} in {folder} as {'read' if read else 'unread'} ({acct})", "Mark email"
        details.append(("Email", f"UID {uid} in {folder}"))
    elif tool == "bulk_email":
        act = str(a.get("action") or "")
        words = {"mark_read": "Mark as read", "mark_unread": "Mark as unread", "archive": "Archive",
                 "delete": "Delete", "junk": "Move to Junk"}.get(act, act or "Change")
        uids = a.get("uids") if isinstance(a.get("uids"), list) else []
        which = (f"every unread email in {folder}" if a.get("all_unread")
                 else f"{len(uids)} email{'s' if len(uids) != 1 else ''} in {folder}")
        title, action = f"{words} {which} ({acct})", f"Bulk email: {words.lower()}"
        details.append(("Emails", ", ".join(str(u) for u in uids[:50]) + (" ..." if len(uids) > 50 else "")
                        if uids else which))
        if act == "delete":
            details.append(("Permanent", "yes, cannot be undone" if a.get("permanent") else "no, moves to Trash"))
    else:
        return None
    return {"action": action, "title": title, "details": details, "body": body, "body_label": body_label,
            "account": acct, "target": {"name": acct}}


# ── Google Calendar (connected account or CalDAV) ────────────────────────

def _url_is_google(url: str) -> bool:
    try:
        p = urlparse(str(url or ""))
    except Exception:
        return False
    host = (p.hostname or "").lower()
    if host.endswith("googleusercontent.com") or host.endswith("googleapis.com"):
        return True
    return host in ("www.google.com", "google.com", "calendar.google.com") and (
        "/calendar/dav/" in p.path or "/caldav/" in p.path or host == "calendar.google.com")


def calendar_is_google(owner: Optional[str], cal) -> bool:
    """A calendar whose writes land in the user's Google account: one linked
    through the connected Google account (source "google",
    src/google_calendar_sync.py), or a CalDAV calendar whose account is
    Google's (mirrors which account caldav_writeback.writeback_event pushes
    to)."""
    if cal is None:
        return False
    if getattr(cal, "source", "") == "google":
        return True
    if getattr(cal, "source", "") != "caldav":
        return False
    try:
        from src.caldav_sync import _load_caldav_accounts
        accounts = _load_caldav_accounts(owner) or []
    except Exception:
        accounts = []
    if not accounts:
        return False
    acc = None
    if getattr(cal, "account_id", None):
        acc = next((a for a in accounts if a.get("id") == cal.account_id), None)
    if acc is None:
        acc = accounts[0]
    return _url_is_google(acc.get("url") or "")


def _fmt_when(dt, all_day=False) -> str:
    try:
        return dt.strftime("%b %d, %Y") if all_day else dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    except Exception:
        return str(dt or "")


def _describe_calendar(owner: Optional[str], a: Dict) -> Optional[Dict]:
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    if isinstance(a.get("events"), list) and not a.get("action"):
        found = []
        for ev in a["events"]:
            if isinstance(ev, dict):
                d = _describe_calendar(owner, {**ev, "action": "create_event"})
                if d:
                    found.append(d)
        if not found:
            return None
        n = len([e for e in a["events"] if isinstance(e, dict)])
        return {"action": "Create events", "title": f"Create {n} events on Google Calendar",
                "details": [("Events", "; ".join(f["title"] for f in found)[:900])],
                "body": "\n".join(f["title"] for f in found), "body_label": "Events",
                "target": found[0].get("target") or {}}
    action = str(a.get("action") or "list_events").replace("-", "_").strip().lower()
    action = {"create": "create_event", "update": "update_event", "delete": "delete_event"}.get(action, action)
    if action not in CAL_WRITE_ACTIONS:
        return None
    db = SessionLocal()
    try:
        def cals():
            q = db.query(CalendarCal)
            return q.filter(CalendarCal.owner == owner) if owner is not None else q
        if action == "create_event":
            href = a.get("calendar_href") or a.get("calendar")
            cal = None
            if href:
                cal = (cals().filter(CalendarCal.id == href).first()
                       or cals().filter(CalendarCal.name.ilike(str(href))).first()
                       or cals().filter(CalendarCal.id.like(f"{href}%")).first())
            if cal is None:
                from routes.calendar_routes import _get_default_calendar
                cal = _get_default_calendar(db, owner)
            if not calendar_is_google(owner, cal):
                return None
            when = a.get("dtstart") or a.get("start") or a.get("start_time") or a.get("when") or ""
            summary = str(a.get("summary") or "(untitled)")
            details = [("Calendar", f"{cal.name} (Google)"), ("Title", summary), ("Starts", str(when))]
            end = a.get("dtend") or a.get("end") or a.get("end_time") or a.get("duration")
            if end:
                details.append(("Ends", str(end)))
            if a.get("location"):
                details.append(("Location", str(a["location"])))
            return {"action": "Create event", "title": f"Add \"{_short(summary, 100)}\" ({when}) to Google Calendar \"{cal.name}\"",
                    "details": details, "body": str(a.get("description") or ""), "body_label": "Description",
                    "target": {"name": cal.name}}
        uid = str(a.get("uid") or "")
        base = uid.split("::", 1)[0] if uid else ""
        q = db.query(CalendarEvent).join(CalendarCal).filter(CalendarEvent.uid == base)
        if owner is not None:
            q = q.filter(CalendarCal.owner == owner)
        ev = q.first() if base else None
        if ev is None or not calendar_is_google(owner, ev.calendar):
            return None
        cal = ev.calendar
        name = ev.summary or "(untitled)"
        when = _fmt_when(ev.dtstart, ev.all_day)
        if action == "delete_event":
            return {"action": "Delete event", "title": f"Delete \"{_short(name, 100)}\" ({when}) from Google Calendar \"{cal.name}\"",
                    "details": [("Calendar", f"{cal.name} (Google)"), ("Event", name), ("When", when)],
                    "body": "", "target": {"name": name}}
        changes = []
        for k, label in (("summary", "Title"), ("dtstart", "Starts"), ("dtend", "Ends"), ("location", "Location"),
                         ("all_day", "All day"), ("importance", "Importance"), ("event_type", "Tag")):
            if a.get(k) is not None:
                changes.append((label, str(a[k])))
        return {"action": "Update event", "title": f"Change \"{_short(name, 100)}\" ({when}) on Google Calendar \"{cal.name}\"",
                "details": [("Calendar", f"{cal.name} (Google)"), ("Event", name), ("When now", when)] + changes,
                "body": str(a.get("description") or "") if a.get("description") is not None else "",
                "body_label": "New description", "target": {"name": name}}
    finally:
        db.close()




# ── Deciding ─────────────────────────────────────────────────────────────

def _app_api_write(content: str) -> str:
    a = _args(content)
    if str(a.get("action") or "call").lower() != "call":
        return ""
    method = str(a.get("method") or "GET").upper()
    path = str(a.get("path") or "")
    if not path.startswith("/"):
        path = "/" + path
    if method != "GET" and any(path == p or path.startswith(p + "/") for p in APP_API_GUARDED):
        return f"{method} {path}"
    return ""


def is_candidate(tool: str, content: str = "") -> bool:
    """Cheap first look, no lookups: could this call be a Google write?"""
    t = normalize(tool)
    if t in EMAIL_WRITE_TOOLS or t == "manage_calendar":
        return True
    return t == "app_api" and bool(_app_api_write(content))


async def classify(tool: str, content: str, owner: Optional[str]) -> Optional[Dict]:
    """What the call would do, in plain words, when it writes to the user's
    Google account; None when it does not (or is only a read)."""
    t = normalize(tool)
    a = _args(content)
    if t == "manage_calendar":
        info = await asyncio.to_thread(_describe_calendar, owner, a)
    elif t in EMAIL_WRITE_TOOLS:
        info = await asyncio.to_thread(_describe_email, t, a)
    else:
        return None
    if info is not None:
        info.setdefault("kind", "google_write")
    return info


async def needs_approval(tool: str, content: str, owner: Optional[str]) -> bool:
    if not enabled() or not is_candidate(tool, content):
        return False
    if normalize(tool) == "app_api":
        return True
    try:
        return (await classify(tool, content, owner)) is not None
    except Exception:
        return True


def _context() -> Tuple[str, Dict[str, Any]]:
    """Where this call comes from: a scheduled task, or a chat."""
    try:
        from src.usage import current_scope
        sc = current_scope() or {}
        if sc.get("source") in ("task", "scheduled_task", "tasks"):
            return "task", dict(sc)
    except Exception:
        pass
    from src.action_approvals import run_context
    rc = run_context.get()
    if rc:
        return rc, {}
    return "chat", {}


async def intercept(tool: str, content: str, owner: Optional[str], session_id: Optional[str]) -> Optional[Tuple[str, Dict]]:
    """None to let the call run; else (description, result) for a call held
    for the user's approval (or refused, for app_api writes)."""
    if not is_candidate(tool, content):
        return None
    from src import action_approvals as AA
    t = normalize(tool)
    if AA.take_approved(tool, content):
        return None
    if not enabled():
        return None
    if t == "app_api":
        what = _app_api_write(content)
        return ("app_api: refused", {
            "error": (f"Not done: {what} could change the user's Google account or mailbox, and app_api "
                      "cannot ask them first. Use the named tool instead (send_email, manage_calendar, "
                      "manage_calendar): it shows the change to the user for approval."),
            "exit_code": 1})
    try:
        info = await classify(tool, content, owner)
    except Exception as e:
        # Could not tell: ask rather than write unseen.
        logger.warning("[google-approval] could not classify %s: %s", tool, e, exc_info=True)
        info = {"action": tool, "title": f"Use {tool} (it may change your Google account)",
                "details": [("Tool", tool)], "body": (content or "")[:4000], "body_label": "Arguments"}
    if info is None:
        return None
    kind, ctx = _context()
    entry = AA.create(owner, session_id or "", tool, content, info, source=kind)
    await _notify(entry, kind, session_id)
    result = {
        "action_approval": {"id": entry["id"], "title": entry["title"], "status": entry["status"]},
        "output": AA.held_output(entry),
        "exit_code": 0,
    }
    return f"{t}: waiting for approval", result


async def _notify(entry: dict, kind: str, session_id: Optional[str]) -> None:
    """Background work (a scheduled task, the mail listener, a chat nobody
    has open) must not wait unseen: send it where the user will see it."""
    try:
        from src import agent_runs
        watched = bool(session_id) and agent_runs.has_watchers(session_id)
    except Exception:
        watched = False
    if kind == "chat" and watched:
        return
    try:
        from src.chat_queue import send_notification
        heading = "Approve a Google change?"
        body = entry["title"][:200] + " Open it to approve or deny."
        asyncio.create_task(send_notification(session_id or "", {}, heading, body, kind="approval",
                                              anchor=None if session_id else "settings-google"))
    except Exception as e:
        logger.debug("approval notification failed: %s", e)
