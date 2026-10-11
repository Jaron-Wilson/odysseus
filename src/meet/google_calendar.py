"""Making Google Meet meetings from the user's own Google account.

The calendar Odysseus syncs (src/caldav_sync.py) is CalDAV, which cannot ask
for a Meet link. This goes through the Google Calendar API instead: an event
inserted with conferenceData.createRequest (hangoutsMeet,
conferenceDataVersion=1) comes back with its hangoutLink, and Google sends
the invites from the user's account (sendUpdates=all).

Each user connects their own Google account once (Settings > Google >
Google Calendar, or Connect Google Calendar on the Google Meet card). The
connection, its encrypted refresh token and the access tokens are shared with
the other Google products: src/google_workspace/connection.py.

Meeting access: Meet turns a guest away when the meeting's access is
"Trusted" or "Restricted" (and Google may refuse a signed-out automated
browser even then; see GUEST_DENIED in session.py). The Calendar API
has no field for that, so after the event is made the Meet REST API sets the
new meeting's space to OPEN (spaces.patch, scope meetings.space.settings).
That is best effort: when it cannot (the Meet REST API not enabled in the
Cloud project, the permission not granted, or Google refusing), the meeting
is still made and the reply says to set access to Open in Meet's host
controls.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from src import google_oauth

from src.google_workspace import connection
from src.google_workspace.connection import GoogleError  # noqa: F401  (re-exported)
from src.google_workspace.products import CALENDAR_SCOPE, MEET_SETTINGS_SCOPE  # noqa: F401

logger = logging.getLogger(__name__)

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MEET_API = "https://meet.googleapis.com/v2"
DEFAULT_MINUTES = 60
MAX_MINUTES = 24 * 60
MAX_ATTENDEES = 50
MAX_TITLE = 200
RECONNECT = "Connect Google Calendar again in Settings > Google."
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

_sleep = asyncio.sleep


# ── connecting (the shared Google connection, Calendar product) ──

def status(user: Optional[str]) -> Dict:
    """What the Meet card shows. Nothing secret."""
    d = connection.load(user)
    return {
        "configured": google_oauth.client_config()["configured"],
        "connected": connection.is_enabled(user, "calendar"),
        "email": str(d.get("email") or ""),
        "can_open_access": MEET_SETTINGS_SCOPE in connection.granted_scopes(user),
        "connected_at": d.get("connected_at") or 0,
    }


def connect_url(request, user: Optional[str]) -> str:
    return connection.connect_url(request, user, ["calendar"])


async def finish_connect(user: str, code: str, state_entry: Dict) -> Tuple[bool, str]:
    return await connection.finish_connect(user, code, state_entry)


async def disconnect(user: Optional[str]) -> None:
    """Stop making meetings; the connection goes too when nothing else uses it."""
    await connection.disconnect(user, "calendar")


def _client(timeout: float = 20) -> httpx.AsyncClient:
    return connection.client(timeout)


async def _call(client: httpx.AsyncClient, user: Optional[str], method: str, url: str, **kw) -> httpx.Response:
    if not connection.is_enabled(user, "calendar"):
        raise GoogleError("Google Calendar is not connected. " + RECONNECT)
    return await connection.call(client, user, method, url, **kw)


_google_reason = connection.reason


# ── making a meeting ──

def clean_attendees(raw) -> List[str]:
    """Email addresses from a list or a comma separated string. Raises
    GoogleError naming the first one that is not an address."""
    items = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw or ""))
    out: List[str] = []
    for a in items:
        a = str(a or "").strip().strip("<>").lower()
        if not a:
            continue
        if not _EMAIL_RE.match(a):
            raise GoogleError(f"That is not an email address: {a[:60]}")
        if a not in out:
            out.append(a)
    if len(out) > MAX_ATTENDEES:
        raise GoogleError(f"At most {MAX_ATTENDEES} people.")
    return out


def parse_start(text: str) -> datetime:
    """A start time from ISO or words ("tomorrow at 3pm"), in the user's
    timezone when it is known. Raises GoogleError."""
    text = str(text or "").strip()
    if not text or text.lower() == "now":
        return datetime.now(timezone.utc)
    try:
        from routes.calendar_routes import parse_due_for_user
        iso = parse_due_for_user(text)
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        raise GoogleError(f"Could not read the start time: {text[:60]}")
    if dt.tzinfo is None:
        dt = dt.astimezone()          # server local, as the calendar does
    return dt


def _space_code(event: Dict) -> str:
    from src.meet import links
    conf = event.get("conferenceData") or {}
    return str(conf.get("conferenceId") or "") or links.meeting_code(str(event.get("hangoutLink") or ""))


async def _open_access(client: httpx.AsyncClient, user: Optional[str], code: str) -> Tuple[bool, str]:
    """Set the meeting's space to OPEN so a guest gets in. (done, why not)."""
    if MEET_SETTINGS_SCOPE not in connection.granted_scopes(user):
        return False, "Odysseus was not allowed to change Meet settings when Google Calendar was connected."
    r = await _call(client, user, "GET", f"{MEET_API}/spaces/{code}")
    if r.status_code == 200:
        name = str(r.json().get("name") or "")
        if name.startswith("spaces/"):
            r = await _call(client, user, "PATCH", f"{MEET_API}/{name}",
                            params={"updateMask": "config.accessType"},
                            json={"config": {"accessType": "OPEN"}})
            if r.status_code == 200:
                got = ((r.json().get("config") or {}).get("accessType") or "OPEN")
                if got == "OPEN":
                    return True, ""
                return False, f"Google kept the meeting's access at {got}."
    why = _google_reason(r)
    logger.info("[meet] could not open meeting access: %s", r.status_code)
    if "SERVICE_DISABLED" in why or "accessNotConfigured" in why:
        return False, "The Google Meet REST API is not enabled in the Google Cloud project."
    if "insufficient" in why.lower() or "SCOPE" in why:
        return False, "Odysseus is not allowed to change Meet settings. " + RECONNECT
    return False, f"Google would not change the meeting's access ({r.status_code})."


async def create_meeting(user: Optional[str], title: str = "", start: Optional[datetime] = None,
                         minutes: int = DEFAULT_MINUTES, attendees: Optional[List[str]] = None,
                         description: str = "", open_access: bool = True) -> Dict:
    """Insert a calendar event with a new Meet in the user's primary
    calendar. Returns the link and the event; raises GoogleError."""
    title = " ".join(str(title or "").split())[:MAX_TITLE] or "Meeting"
    start = start or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.astimezone()
    try:
        minutes = max(5, min(MAX_MINUTES, int(minutes or DEFAULT_MINUTES)))
    except (TypeError, ValueError):
        minutes = DEFAULT_MINUTES
    end = start + timedelta(minutes=minutes)
    people = attendees or []
    body: Dict = {
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "conferenceData": {"createRequest": {"requestId": uuid.uuid4().hex,
                                             "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
    }
    try:
        from src.user_time import get_user_tz_name
        tz = get_user_tz_name()
        if tz and "/" in tz:
            body["start"]["timeZone"] = body["end"]["timeZone"] = tz
    except Exception:
        pass
    if description:
        body["description"] = str(description)[:4000]
    if people:
        body["attendees"] = [{"email": a} for a in people]
    async with _client() as client:
        r = await _call(client, user, "POST", EVENTS_URL, json=body, params={
            "conferenceDataVersion": "1", "sendUpdates": "all" if people else "none"})
        if r.status_code not in (200, 201):
            why = _google_reason(r)
            # Google's reason says what is wrong (an API switched off, a
            # scope not granted); it carries no token or event content.
            logger.warning("[meet] Calendar events.insert said %s: %s", r.status_code, why.strip()[:300])
            if "SERVICE_DISABLED" in why or "accessNotConfigured" in why:
                raise GoogleError("The Google Calendar API is not enabled in the Google Cloud project "
                                  "(APIs & Services > Library > Google Calendar API > Enable).")
            if r.status_code == 401 or "insufficient" in why.lower():
                raise GoogleError("Google did not accept the saved access to your calendar. " + RECONNECT)
            raise GoogleError(f"Google Calendar would not make the event ({r.status_code}).")
        event = r.json()
        # Meet usually answers at once; when it is still "pending", ask again.
        for _ in range(4):
            status = (((event.get("conferenceData") or {}).get("createRequest") or {})
                      .get("status") or {}).get("statusCode")
            if event.get("hangoutLink") or status not in ("pending", None):
                break
            await _sleep(1.0)
            g = await _call(client, user, "GET", f"{EVENTS_URL}/{event.get('id')}",
                            params={"conferenceDataVersion": "1"})
            if g.status_code == 200:
                event = g.json()
        url = str(event.get("hangoutLink") or "")
        if not url:
            for ep in (event.get("conferenceData") or {}).get("entryPoints") or []:
                if ep.get("entryPointType") == "video" and ep.get("uri"):
                    url = str(ep["uri"])
                    break
        if not url:
            raise GoogleError("Google made the event but did not give it a Meet link yet. "
                              "Open it in Google Calendar to add one.")
        opened, access_note = None, ""
        code = _space_code(event)
        if open_access and code:
            try:
                opened, access_note = await _open_access(client, user, code)
            except GoogleError as e:
                opened, access_note = False, str(e)
    from src.meet import links
    logger.info("[meet] made a meeting for %s (%d invited)", user or "-", len(people))
    return {
        "url": links.meet_url(url) or url,
        "title": title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "minutes": minutes,
        "attendees": people,
        "event_id": str(event.get("id") or ""),
        "html_link": str(event.get("htmlLink") or ""),
        "invites_sent": bool(people),
        "open_access": opened,
        "access_note": access_note,
    }


def access_hint(made: Dict) -> str:
    """One sentence on whether a guest will get in."""
    if made.get("open_access"):
        return "Meeting access is set to Open."
    why = made.get("access_note") or ""
    return ((why + " " if why else "") + "If Odysseus (AI) cannot get in, set Meeting access to Open in "
            "Meet's host controls, or let it in from the lobby.")
