"""Google Calendar on the Calendar page, through the Google account connected
in Settings > Google (OAuth), not CalDAV.

Google's CalDAV endpoint turns passwords away, so a CalDAV account can never
sync a Google calendar. This uses the Google Calendar API with the user's own
connection instead (src/google_workspace/connection.py, product "calendar").

Which calendars: each Google calendar the user picks (their primary one plus
any others on their calendar list, shared and family calendars included) is
one row in `calendars` with source "google" and the Google calendar id in
`account_id`. The primary calendar is always stored as "primary". Until the
user picks, a connected account gets its primary calendar linked by itself
(the pref `google_calendar_ids` stays unset); picking none unlinks.

Listing the calendars needs calendar.calendarlist.readonly, asked for with
Calendar since this change. A connection made before it can still sync the
primary calendar (calendar.events covers events on every calendar); the page
says to reconnect Calendar to list the others.

Sync: events.list with singleEvents=true, so a recurring series arrives as
its occurrences (each one a row, edited or deleted on its own, as in Google).
The first pull covers the same window as the CalDAV sync (90 days back, a
year ahead) and keeps Google's nextSyncToken; later pulls send only the
token and get only what changed. Cancelled events are removed. A 410 Gone
(token expired) or a day since the last full pull starts over with a full
pull, which also moves the window forward and prunes what is gone.

Writes: the Calendar page's create, edit and delete go to Google first and
change the local copy only when Google took them, so a failure shows instead
of an event that silently never reaches Google. The assistant's
manage_calendar writes on these calendars are held for the user's approval by
src/google_write_gate.py before they get here.

Problems (not connected, revoked, a permission missing, the API off) are kept
per user and shown on the Calendar page with a link to Settings > Google,
never as a silently empty calendar.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import quote

from src.google_workspace import connection
from src.google_workspace.connection import GoogleError
from src.google_workspace import products as P

logger = logging.getLogger(__name__)

SOURCE = "google"
ORIGIN = "google"
API = "https://www.googleapis.com/calendar/v3"
CALENDAR_LIST_URL = API + "/users/me/calendarList"
PREF_KEY = "google_calendar_ids"
LIST_SCOPES = (P.CALENDAR_LIST_SCOPE, P.G + "calendar.readonly", P.G + "calendar")
RECONNECT_URL = "/api/google/connect?products=calendar"
LOOKBACK_DAYS = 90
LOOKAHEAD_DAYS = 365
FULL_EVERY_S = 24 * 3600
MAX_PAGES = 40
STATE_DIRNAME = "google_calendar_sync"
DEFAULT_COLOR = "#4285f4"

_in_progress: set = set()


class NeedsReconnect(GoogleError):
    """Google refused because of the grant: not connected, revoked, or a
    permission missing. The page links to Settings > Google."""


# ── who ──

def google_user(owner: Optional[str]) -> Optional[str]:
    """The Google connection's user for a calendar owner. With auth off the
    calendar uses FALLBACK_OWNER while Settings > Google uses no user."""
    from routes.calendar_routes import FALLBACK_OWNER
    if not owner or owner == FALLBACK_OWNER:
        return None
    return owner


def local_cal_id(owner: Optional[str], gid: str) -> str:
    h = hashlib.sha256(f"{owner or ''}\n{gid}".encode("utf-8")).hexdigest()[:24]
    return f"gcal-{h}"


def local_uid(cal_id: str, event_id: str) -> str:
    """Event ids are unique per calendar only (an invite can sit on two), so
    the local uid carries the calendar too. Never contains '::', the
    occurrence separator of locally expanded series."""
    short = cal_id[5:13] if cal_id.startswith("gcal-") else hashlib.sha256(cal_id.encode()).hexdigest()[:8]
    return f"g{short}_{event_id}"


def event_id_of(uid: str) -> str:
    return uid.split("_", 1)[1] if uid.startswith("g") and "_" in uid else uid


# ── per-user state: sync tokens and the last problem ──

def _state_path(owner: Optional[str]) -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, STATE_DIRNAME, connection._key(owner) + ".json")


def _load_state(owner: Optional[str]) -> Dict:
    try:
        with open(_state_path(owner), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(owner: Optional[str], d: Dict) -> None:
    from core.atomic_io import atomic_write_json
    os.makedirs(os.path.dirname(_state_path(owner)), exist_ok=True)
    atomic_write_json(_state_path(owner), d, indent=1)


def _set_problem(owner: Optional[str], text: str, reconnect: bool = False) -> None:
    st = _load_state(owner)
    if text:
        st["problem"] = {"text": text, "reconnect": bool(reconnect), "at": time.time()}
    else:
        st.pop("problem", None)
    _save_state(owner, st)


# ── prefs: which Google calendars ──

def _selected(owner: Optional[str]) -> Optional[List[str]]:
    from routes.prefs_routes import _load_for_user
    v = (_load_for_user(owner) or {}).get(PREF_KEY)
    return [str(x) for x in v] if isinstance(v, list) else None


def _save_selected(owner: Optional[str], ids: List[str]) -> None:
    from routes.prefs_routes import _load_for_user, _save_for_user
    prefs = _load_for_user(owner) or {}
    prefs[PREF_KEY] = list(dict.fromkeys(ids))
    _save_for_user(owner, prefs)


def _rows(db, owner: Optional[str]):
    from core.database import CalendarCal
    return db.query(CalendarCal).filter(CalendarCal.owner == owner, CalendarCal.source == SOURCE).all()


# ── connection facts (no network) ──

def can_list(guser: Optional[str]) -> bool:
    scopes = connection.granted_scopes(guser)
    return any(s in scopes for s in LIST_SCOPES)


def _not_ready(guser: Optional[str]) -> str:
    """Why Calendar cannot be used right now, or "" when it can."""
    from src import google_oauth
    if not google_oauth.client_config()["configured"]:
        return "No Google OAuth client is set up on this server yet (Settings > Google)."
    d = connection.load(guser)
    if not d.get("refresh_token"):
        return "Google is not connected. Connect Google Calendar in Settings > Google."
    if "calendar" in connection.needs_reconnect(guser):
        return "Google no longer gives Odysseus access to your calendar. Reconnect Google Calendar in Settings > Google."
    if not connection.is_enabled(guser, "calendar"):
        return "Google Calendar is off for your Google account. Connect it in Settings > Google."
    return ""


def notice(owner: Optional[str]) -> Optional[Dict]:
    """What the Calendar page shows above the grid, or None. No network."""
    from core.database import SessionLocal
    db = SessionLocal()
    try:
        linked = bool(_rows(db, owner))
    finally:
        db.close()
    guser = google_user(owner)
    if linked:
        why = _not_ready(guser)
        if why:
            return {"text": why, "action": "settings"}
        p = _load_state(owner).get("problem") or {}
        if p.get("text"):
            return {"text": p["text"], "action": "settings" if p.get("reconnect") else ""}
    return None


# ── Google API ──

def _cal_url(gid: str) -> str:
    return f"{API}/calendars/{quote(gid, safe='')}/events"


def _explain(r, what: str) -> GoogleError:
    e = connection.explain(r, "calendar", what)
    why = connection.reason(r)
    if r.status_code == 401 or "insufficient" in why.lower() or "SCOPE" in why:
        return NeedsReconnect(str(e))
    return e


async def _call(c, guser, method, url, what="", **kw):
    try:
        r = await connection.call(c, guser, method, url, **kw)
    except GoogleError as e:
        msg = str(e)
        if "not connected" in msg or "revoked" in msg or "no longer" in msg:
            raise NeedsReconnect(msg)
        raise
    return r


async def list_google_calendars(owner: Optional[str]) -> List[Dict]:
    """The user's calendar list (needs the list permission). The primary
    calendar's id is reported as "primary"."""
    guser = google_user(owner)
    out: List[Dict] = []
    page = None
    async with connection.client(20) as c:
        for _ in range(10):
            params = {"maxResults": "250"}
            if page:
                params["pageToken"] = page
            r = await _call(c, guser, "GET", CALENDAR_LIST_URL, "calendar list", params=params)
            if r.status_code != 200:
                raise _explain(r, "calendar list")
            d = r.json()
            for it in d.get("items") or []:
                if it.get("deleted"):
                    continue
                out.append({
                    "id": "primary" if it.get("primary") else str(it.get("id") or ""),
                    "google_id": str(it.get("id") or ""),
                    "name": str(it.get("summaryOverride") or it.get("summary") or it.get("id") or "Google Calendar"),
                    "color": str(it.get("backgroundColor") or DEFAULT_COLOR),
                    "primary": bool(it.get("primary")),
                    "access_role": str(it.get("accessRole") or ""),
                    "writable": str(it.get("accessRole") or "") in ("owner", "writer"),
                })
            page = d.get("nextPageToken")
            if not page:
                break
    out.sort(key=lambda x: (not x["primary"], x["name"].lower()))
    return out


def _primary_entry(guser: Optional[str]) -> Dict:
    email = str(connection.load(guser).get("email") or "")
    return {"id": "primary", "google_id": email or "primary", "name": email or "Google Calendar",
            "color": DEFAULT_COLOR, "primary": True, "access_role": "owner", "writable": True}


# ── status and picking calendars ──

async def status(owner: Optional[str]) -> Dict:
    """What the Calendar page's Google section shows. Nothing secret."""
    from src import google_oauth
    guser = google_user(owner)
    try:
        await connection.recheck(guser)
    except Exception:
        pass
    d = connection.load(guser)
    why = _not_ready(guser)
    enabled = not why
    out: Dict = {
        "configured": google_oauth.client_config()["configured"],
        "connected": bool(d.get("refresh_token")),
        "enabled": enabled,
        "email": str(d.get("email") or ""),
        "needs_reconnect": "calendar" in connection.needs_reconnect(guser),
        "can_list": enabled and can_list(guser),
        "calendars": [],
        "selected": [],
        "auto": False,
        "message": why,
        "reconnect_url": RECONNECT_URL,
        "problem": (_load_state(owner).get("problem") or {}).get("text") or "",
    }
    from core.database import SessionLocal
    db = SessionLocal()
    try:
        linked = {r.account_id: r for r in _rows(db, owner)}
    finally:
        db.close()
    sel = _selected(owner)
    out["auto"] = sel is None
    out["selected"] = list(linked)
    if not enabled:
        out["calendars"] = [{"id": g, "name": r.name, "color": r.color, "primary": g == "primary",
                             "writable": True, "selected": True} for g, r in linked.items()]
        return out
    cals: List[Dict] = []
    if out["can_list"]:
        try:
            cals = await list_google_calendars(owner)
        except NeedsReconnect:
            out["can_list"] = False
        except GoogleError as e:
            out["list_error"] = str(e)
    if not cals:
        cals = [_primary_entry(guser)]
        for g, r in linked.items():
            if g != "primary":
                cals.append({"id": g, "google_id": g, "name": r.name, "color": r.color, "primary": False,
                             "access_role": "", "writable": True})
    for cal in cals:
        cal["selected"] = cal["id"] in linked
    out["calendars"] = cals
    return out


async def set_selection(owner: Optional[str], ids: List[str]) -> Dict:
    """Link exactly these Google calendars ("primary" for the main one).
    New ones get a row and their first pull; unpicked ones go, with their
    events (they stay in Google)."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    guser = google_user(owner)
    why = _not_ready(guser)
    ids = [str(i).strip() for i in ids or [] if str(i).strip()]
    if ids and why:
        raise NeedsReconnect(why)
    meta: Dict[str, Dict] = {"primary": _primary_entry(guser)}
    if ids and can_list(guser):
        try:
            for cal in await list_google_calendars(owner):
                meta[cal["id"]] = cal
                if cal["google_id"] and cal["google_id"] in ids and cal["primary"]:
                    ids = ["primary" if i == cal["google_id"] else i for i in ids]
        except NeedsReconnect:
            pass
    ids = list(dict.fromkeys(ids))
    db = SessionLocal()
    added = []
    try:
        have = {r.account_id: r for r in _rows(db, owner)}
        for g, row in have.items():
            if g not in ids:
                db.query(CalendarEvent).filter(CalendarEvent.calendar_id == row.id).delete()
                db.delete(row)
        for g in ids:
            if g in have:
                continue
            m = meta.get(g) or {"name": g, "color": DEFAULT_COLOR}
            row = CalendarCal(id=local_cal_id(owner, g), owner=owner, name=m["name"][:120],
                              color=m.get("color") or DEFAULT_COLOR, source=SOURCE, account_id=g)
            db.add(row)
            added.append(row.id)
        db.commit()
    finally:
        db.close()
    st = _load_state(owner)
    cals = st.get("calendars") or {}
    st["calendars"] = {k: v for k, v in cals.items() if k in {local_cal_id(owner, g) for g in ids}}
    _save_state(owner, st)
    _save_selected(owner, ids)
    result = await sync(owner) if ids else {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    if not ids:
        _set_problem(owner, "")
    return {"ok": True, "selected": ids, "sync": result}


def ensure_auto(owner: Optional[str]) -> bool:
    """A connected account with Calendar on and no choice made yet gets its
    primary calendar linked. True when it linked it now."""
    if _selected(owner) is not None:
        return False
    guser = google_user(owner)
    if _not_ready(guser):
        return False
    from core.database import CalendarCal, SessionLocal
    db = SessionLocal()
    try:
        if any(r.account_id == "primary" for r in _rows(db, owner)):
            return False
        m = _primary_entry(guser)
        db.add(CalendarCal(id=local_cal_id(owner, "primary"), owner=owner, name=m["name"][:120],
                           color=DEFAULT_COLOR, source=SOURCE, account_id="primary"))
        db.commit()
    finally:
        db.close()
    _save_selected(owner, ["primary"])
    logger.info("[gcal] linked the primary Google calendar for %s", owner or "-")
    return True


# ── events: Google item <-> local row ──

def _parse_when(when: Dict, is_end: bool = False):
    """(naive datetime, all_day, is_utc) from a Google start/end object."""
    if not isinstance(when, dict):
        return None, False, False
    if when.get("date"):
        d = date.fromisoformat(str(when["date"])[:10])
        return datetime(d.year, d.month, d.day), True, False
    if when.get("dateTime"):
        dt = datetime.fromisoformat(str(when["dateTime"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt, False, False
        return dt.astimezone(timezone.utc).replace(tzinfo=None), False, True
    return None, False, False


def item_values(item: Dict) -> Optional[Dict]:
    start, all_day, is_utc = _parse_when(item.get("start") or {})
    if start is None:
        return None
    end, _, _ = _parse_when(item.get("end") or {}, True)
    if end is None or end <= start:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    return {
        "summary": str(item.get("summary") or "(No title)"),
        "description": str(item.get("description") or ""),
        "location": str(item.get("location") or ""),
        "dtstart": start, "dtend": end, "all_day": all_day, "is_utc": is_utc and not all_day,
    }


def _upsert(db, cal, item: Dict) -> Optional[str]:
    from core.database import CalendarEvent
    vals = item_values(item)
    if vals is None:
        return None
    uid = local_uid(cal.id, str(item.get("id") or ""))
    row = db.query(CalendarEvent).filter(CalendarEvent.uid == uid, CalendarEvent.calendar_id == cal.id).first()
    if row is None:
        row = CalendarEvent(uid=uid, calendar_id=cal.id, origin=ORIGIN, rrule="", **vals)
        db.add(row)
    else:
        for k, v in vals.items():
            setattr(row, k, v)
        row.origin = ORIGIN
        row.rrule = ""
        row.status = "confirmed"
    return uid


def _remove(db, cal, item_id: str) -> int:
    from core.database import CalendarEvent
    return db.query(CalendarEvent).filter(CalendarEvent.uid == local_uid(cal.id, item_id),
                                          CalendarEvent.calendar_id == cal.id).delete()


def _rfc(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def event_body(v: Dict) -> Dict:
    """A Google event resource from local values (summary, description,
    location, dtstart, dtend, all_day, is_utc, rrule)."""
    start, end = v["dtstart"], v.get("dtend") or v["dtstart"]
    body: Dict = {"summary": v.get("summary") or "", "description": v.get("description") or "",
                  "location": v.get("location") or ""}
    if v.get("all_day"):
        sd, ed = start.date(), end.date()
        if ed <= sd:
            ed = sd + timedelta(days=1)
        body["start"], body["end"] = {"date": sd.isoformat()}, {"date": ed.isoformat()}
    elif v.get("is_utc"):
        body["start"], body["end"] = {"dateTime": _rfc(start)}, {"dateTime": _rfc(end)}
    else:
        tz = None
        try:
            from src.user_time import get_user_tz_name
            tz = get_user_tz_name()
        except Exception:
            tz = None
        if tz and "/" in tz:
            body["start"] = {"dateTime": start.replace(tzinfo=None).isoformat(), "timeZone": tz}
            body["end"] = {"dateTime": end.replace(tzinfo=None).isoformat(), "timeZone": tz}
        else:   # server local, as the calendar reads naive times
            body["start"] = {"dateTime": start.astimezone().isoformat()}
            body["end"] = {"dateTime": end.astimezone().isoformat()}
    rr = str(v.get("rrule") or "").strip()
    if rr:
        body["recurrence"] = [rr if rr.upper().startswith("RRULE:") else "RRULE:" + rr]
    return body


# ── writes ──

async def _write(cal, method: str, url: str, what: str, **kw):
    guser = google_user(cal.owner)
    why = _not_ready(guser)
    if why:
        raise NeedsReconnect(why)
    async with connection.client(20) as c:
        r = await _call(c, guser, method, url, what, **kw)
    return r


async def remote_create(cal, values: Dict) -> Dict:
    """Insert an event on Google. Returns Google's event. Raises GoogleError."""
    r = await _write(cal, "POST", _cal_url(cal.account_id or "primary"), "event",
                     json=event_body(values), params={"sendUpdates": "none"})
    if r.status_code not in (200, 201):
        raise _explain(r, "event")
    logger.info("[gcal] created an event on %s for %s", cal.id, cal.owner or "-")
    return r.json()


async def remote_update(cal, uid: str, values: Dict) -> Dict:
    body = event_body(values)
    body.pop("recurrence", None)            # one occurrence or a single event
    r = await _write(cal, "PATCH", f"{_cal_url(cal.account_id or 'primary')}/{quote(event_id_of(uid), safe='')}",
                     "event", json=body, params={"sendUpdates": "none"})
    if r.status_code != 200:
        raise _explain(r, "event")
    return r.json()


async def remote_delete(cal, uid: str) -> None:
    r = await _write(cal, "DELETE", f"{_cal_url(cal.account_id or 'primary')}/{quote(event_id_of(uid), safe='')}",
                     "event", params={"sendUpdates": "none"})
    if r.status_code in (200, 204, 404, 410):
        return
    raise _explain(r, "event")


def store_created(cal, item: Dict) -> Optional[str]:
    """Keep Google's answer to a create as the local row. A recurring event
    comes back as its series, which the next pull brings as occurrences, so
    that calendar starts a full pull at once."""
    from core.database import SessionLocal
    if item.get("recurrence"):
        return None
    db = SessionLocal()
    try:
        uid = _upsert(db, cal, item)
        db.commit()
        return uid
    finally:
        db.close()


async def after_series_change(cal) -> None:
    st = _load_state(cal.owner)
    (st.setdefault("calendars", {}).setdefault(cal.id, {})).pop("sync_token", None)
    _save_state(cal.owner, st)
    try:
        await sync(cal.owner, only=cal.id)
    except Exception as e:
        logger.info("[gcal] pull after a series change: %s", e)


# ── sync ──

async def _pull(c, guser, cal, entry: Dict) -> Dict:
    """One calendar. Mutates `entry` (sync_token, full_at)."""
    from core.database import CalendarEvent, SessionLocal
    res = {"events": 0, "deleted": 0}
    token = entry.get("sync_token")
    if token and time.time() - float(entry.get("full_at") or 0) > FULL_EVERY_S:
        token = None
    for attempt in (0, 1):
        full = not token
        now = datetime.now(timezone.utc)
        base = {"singleEvents": "true", "maxResults": "250"}
        if full:
            base["timeMin"] = _rfc((now - timedelta(days=LOOKBACK_DAYS)).replace(tzinfo=None))
            base["timeMax"] = _rfc((now + timedelta(days=LOOKAHEAD_DAYS)).replace(tzinfo=None))
        else:
            base["syncToken"] = token
        items: List[Dict] = []
        page, next_token, gone = None, None, False
        for _ in range(MAX_PAGES):
            params = dict(base)
            if page:
                params["pageToken"] = page
            r = await _call(c, guser, "GET", _cal_url(cal.account_id or "primary"), "events", params=params)
            if r.status_code == 410 and not full:
                gone = True
                break
            if r.status_code != 200:
                raise _explain(r, f"events of {cal.name}")
            d = r.json()
            items.extend(d.get("items") or [])
            page = d.get("nextPageToken")
            if not page:
                next_token = d.get("nextSyncToken")
                break
        if gone:
            logger.info("[gcal] sync token for %s expired (410); pulling everything again", cal.id)
            token = None
            continue
        db = SessionLocal()
        try:
            seen = set()
            for it in items:
                iid = str(it.get("id") or "")
                if not iid:
                    continue
                if it.get("status") == "cancelled":
                    res["deleted"] += _remove(db, cal, iid)
                    continue
                uid = _upsert(db, cal, it)
                if uid:
                    seen.add(uid)
                    res["events"] += 1
            db.commit()
            if full and not page:
                q = db.query(CalendarEvent).filter(CalendarEvent.calendar_id == cal.id)
                stale = [e for e in q.all() if e.uid not in seen]
                for e in stale:
                    db.delete(e)
                res["deleted"] += len(stale)
                db.commit()
        finally:
            db.close()
        if next_token:
            entry["sync_token"] = next_token
        else:
            entry.pop("sync_token", None)
        if full:
            entry["full_at"] = time.time()
        res["full"] = full
        return res
    return res


async def sync(owner: Optional[str], only: Optional[str] = None) -> Dict:
    """Pull every linked Google calendar of `owner`. Same shape as the
    CalDAV sync: {calendars, events, deleted, errors}, plus `linked` and,
    when the grant is the problem, `reconnect`."""
    from core.database import SessionLocal
    out: Dict = {"calendars": 0, "events": 0, "deleted": 0, "errors": [], "linked": False}
    key = owner or ""
    if key in _in_progress:
        out["skipped"] = "sync already in progress"
        return out
    _in_progress.add(key)
    try:
        ensure_auto(owner)
        db = SessionLocal()
        try:
            rows = [r for r in _rows(db, owner) if not only or r.id == only]
            for r in rows:
                db.expunge(r)
        finally:
            db.close()
        if not rows:
            return out
        out["linked"] = True
        guser = google_user(owner)
        why = _not_ready(guser)
        if why:
            out["errors"].append(why)
            out["reconnect"] = True
            return out
        st = _load_state(owner)
        cals = st.setdefault("calendars", {})
        problem, reconnect = "", False
        async with connection.client(30) as c:
            for cal in rows:
                entry = cals.setdefault(cal.id, {})
                try:
                    res = await _pull(c, guser, cal, entry)
                    out["calendars"] += 1
                    out["events"] += res["events"]
                    out["deleted"] += res["deleted"]
                except NeedsReconnect as e:
                    problem, reconnect = str(e), True
                    out["errors"].append(f"{cal.name}: {e}")
                    break
                except GoogleError as e:
                    problem = problem or f"{cal.name}: {e}"
                    out["errors"].append(f"{cal.name}: {e}")
                except Exception as e:      # a bad item must not stop the others
                    logger.exception("[gcal] sync failed for %s", cal.id)
                    out["errors"].append(f"{cal.name}: {str(e)[:200]}")
        st = {**_load_state(owner), "calendars": cals, "synced_at": time.time()}
        if problem:
            st["problem"] = {"text": problem, "reconnect": reconnect, "at": time.time()}
        else:
            st.pop("problem", None)
        _save_state(owner, st)
        if reconnect:
            out["reconnect"] = True
        return out
    finally:
        _in_progress.discard(key)


def owners_with_google() -> List[Optional[str]]:
    """Every owner with a linked Google calendar, for the background loop."""
    from core.database import CalendarCal, SessionLocal
    db = SessionLocal()
    try:
        return sorted({r[0] for r in db.query(CalendarCal.owner).filter(CalendarCal.source == SOURCE).distinct()
                       if r[0]})
    finally:
        db.close()


async def unlink(owner: Optional[str]) -> Dict:
    return await set_selection(owner, [])
