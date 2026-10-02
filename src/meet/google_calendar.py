"""Making Google Meet meetings from the user's own Google account.

The calendar Odysseus syncs (src/caldav_sync.py) is CalDAV, which cannot ask
for a Meet link. This goes through the Google Calendar API instead: an event
inserted with conferenceData.createRequest (hangoutsMeet,
conferenceDataVersion=1) comes back with its hangoutLink, and Google sends
the invites from the user's account (sendUpdates=all).

Each user connects their own Google account once (Settings > Calls & Meetings >
Google Meet > Connect Google Calendar). It uses the server's Google OAuth
client (src/google_oauth.py, the one Sign in with Google uses) and comes
back through the same callback, with access_type=offline so there is a
refresh token. That token is kept per user in DATA_DIR/google_calendar/,
Fernet-encrypted with the app key (src/secret_storage.py), in a 0600 file.
It is never sent to the browser and never logged; access tokens are made
from it as needed and kept only in memory.

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
import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from src import google_oauth

logger = logging.getLogger(__name__)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
MEET_SETTINGS_SCOPE = "https://www.googleapis.com/auth/meetings.space.settings"
SCOPES = f"openid email {CALENDAR_SCOPE} {MEET_SETTINGS_SCOPE}"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MEET_API = "https://meet.googleapis.com/v2"
DEFAULT_MINUTES = 60
MAX_MINUTES = 24 * 60
MAX_ATTENDEES = 50
MAX_TITLE = 200
RECONNECT = "Connect Google Calendar again in Settings > Calls & Meetings > Google Meet."
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Tests swap in an httpx.MockTransport; nothing here talks to Google then.
_transport: Optional[httpx.AsyncBaseTransport] = None
_sleep = asyncio.sleep
# user key -> (access token, expires at)
_access: Dict[str, Tuple[str, float]] = {}


class GoogleError(Exception):
    """A sentence for the user about why it did not work."""


# ── storage ──

def _dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "google_calendar")


def _key(user: Optional[str]) -> str:
    u = user or ""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", u)[:40] or "_default"
    return f"{safe}-{hashlib.sha256(u.encode('utf-8')).hexdigest()[:8]}"


def _path(user: Optional[str]) -> str:
    return os.path.join(_dir(), _key(user) + ".json")


def _load(user: Optional[str]) -> Dict:
    try:
        with open(_path(user), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(user: Optional[str], data: Dict) -> None:
    from core.atomic_io import atomic_write_json
    from core.platform_compat import safe_chmod
    os.makedirs(_dir(), exist_ok=True)
    atomic_write_json(_path(user), data, indent=2)
    safe_chmod(_path(user), 0o600)


def _forget(user: Optional[str]) -> None:
    _access.pop(_key(user), None)
    try:
        os.remove(_path(user))
    except FileNotFoundError:
        pass


def _refresh_token(user: Optional[str]) -> str:
    from src import secret_storage
    return secret_storage.decrypt(str(_load(user).get("refresh_token") or ""))


# ── connecting ──

def status(user: Optional[str]) -> Dict:
    """What the card shows. Nothing secret."""
    d = _load(user)
    scopes = str(d.get("scopes") or "").split()
    return {
        "configured": google_oauth.client_config()["configured"],
        "connected": bool(d.get("refresh_token")),
        "email": str(d.get("email") or ""),
        "can_open_access": MEET_SETTINGS_SCOPE in scopes,
        "connected_at": d.get("connected_at") or 0,
    }


def connect_url(request, user: Optional[str]) -> str:
    if not google_oauth.client_config()["configured"]:
        raise GoogleError("No Google OAuth client is set up on this server yet.")
    return google_oauth.authorize_url(
        request, {"purpose": "calendar", "user": user or ""},
        scope=SCOPES,
        # Offline with consent so Google hands over a refresh token every
        # time, even for an account that connected before.
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )


def _claims(id_token: str) -> Dict:
    # Straight from Google's token endpoint over TLS, as in auth_routes.
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def _client(timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


async def finish_connect(user: str, code: str, redirect_uri: str) -> Tuple[bool, str]:
    """Trade the code for tokens and keep the refresh token. (ok, message)."""
    cfg = google_oauth.client_config()
    if not cfg["configured"]:
        return False, "No Google OAuth client is set up on this server."
    try:
        async with _client() as client:
            r = await client.post(google_oauth.TOKEN_URL, data={
                "code": code, "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    except httpx.HTTPError as e:
        logger.warning("[meet] Google Calendar connect: %s", type(e).__name__)
        return False, "Could not reach Google to finish connecting."
    if r.status_code != 200:
        logger.warning("[meet] Google Calendar connect: token endpoint said %s", r.status_code)
        return False, ("Google turned the connection down. The redirect URI must match the one "
                       f"registered in the Google console exactly: {redirect_uri}")
    tok = r.json()
    granted = str(tok.get("scope") or "").split()
    if CALENDAR_SCOPE not in granted:
        return False, ("Google did not grant access to your calendar. Connect again and leave the "
                       "calendar box ticked on Google's consent screen.")
    refresh = str(tok.get("refresh_token") or "")
    if not refresh:
        return False, ("Google did not send a refresh token. Remove Odysseus from your Google "
                       "account's third-party access, then connect again.")
    from src import secret_storage
    email = str(_claims(str(tok.get("id_token") or "")).get("email") or "")
    _save(user, {"email": email, "refresh_token": secret_storage.encrypt(refresh),
                 "scopes": " ".join(granted), "connected_at": time.time()})
    if tok.get("access_token"):
        _access[_key(user)] = (str(tok["access_token"]), time.time() + int(tok.get("expires_in") or 3600) - 60)
    logger.info("[meet] Google Calendar connected for %s", user or "-")
    extra = "" if MEET_SETTINGS_SCOPE in granted else (
        " Meet settings were not allowed, so meetings it makes keep your default access.")
    return True, (f"Odysseus can now make Google Meet meetings as {email or 'your Google account'}."
                  f"{extra} You can close this tab.")


async def disconnect(user: Optional[str]) -> None:
    """Forget the token, and ask Google to revoke it (best effort)."""
    refresh = _refresh_token(user)
    _forget(user)
    if refresh:
        try:
            async with _client(10) as client:
                await client.post(google_oauth.REVOKE_URL, data={"token": refresh})
        except httpx.HTTPError:
            pass
    logger.info("[meet] Google Calendar disconnected for %s", user or "-")


async def _token(client: httpx.AsyncClient, user: Optional[str], fresh: bool = False) -> str:
    k = _key(user)
    cached = _access.get(k)
    if cached and not fresh and cached[1] > time.time():
        return cached[0]
    refresh = _refresh_token(user)
    if not refresh:
        raise GoogleError("Google Calendar is not connected. " + RECONNECT)
    cfg = google_oauth.client_config()
    if not cfg["configured"]:
        raise GoogleError("The Google OAuth client is no longer set up on this server.")
    try:
        r = await client.post(google_oauth.TOKEN_URL, data={
            "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
            "refresh_token": refresh, "grant_type": "refresh_token"})
    except httpx.HTTPError:
        raise GoogleError("Could not reach Google.")
    if r.status_code != 200:
        err = ""
        try:
            err = str(r.json().get("error") or "")
        except ValueError:
            pass
        if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
            _forget(user)
            raise GoogleError("Google no longer accepts the saved connection (revoked or expired). " + RECONNECT)
        raise GoogleError(f"Google would not refresh access ({r.status_code}).")
    tok = r.json()
    access = str(tok.get("access_token") or "")
    _access[k] = (access, time.time() + int(tok.get("expires_in") or 3600) - 60)
    return access


def _google_reason(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
    except ValueError:
        return ""
    if not isinstance(err, dict):
        return str(err)
    reasons = " ".join(str(e.get("reason") or "") for e in err.get("errors") or [] if isinstance(e, dict))
    details = " ".join(str(d.get("reason") or "") for d in err.get("details") or [] if isinstance(d, dict))
    return f"{err.get('status') or ''} {reasons} {details} {err.get('message') or ''}"


async def _call(client: httpx.AsyncClient, user: Optional[str], method: str, url: str, **kw) -> httpx.Response:
    """One API call with a fresh access token, retried once on a 401."""
    for attempt in (0, 1):
        token = await _token(client, user, fresh=bool(attempt))
        try:
            r = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.HTTPError:
            raise GoogleError("Could not reach Google.")
        if r.status_code != 401:
            return r
    return r


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
    if MEET_SETTINGS_SCOPE not in str(_load(user).get("scopes") or "").split():
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
