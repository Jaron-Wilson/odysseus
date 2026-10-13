"""Google Calendar on the Calendar page through the Google account connected
in Settings > Google (src/google_calendar_sync.py), not CalDAV passwords.

What matters:
  * the calendar list (primary, shared, family) with the list permission, and
    the primary calendar alone, with a reconnect hint, without it;
  * a connected account gets its primary calendar linked by itself;
  * the first pull covers the window and keeps Google's sync token; later
    pulls send only the token; cancelled events go; a 410 starts over;
  * the Calendar page's create, edit and delete go to Google (no approval
    card: the user did it), and a refused write is an error, not a phantom;
  * not connected, revoked, or a permission missing shows as a notice with a
    way to Settings > Google, never as a silently empty calendar.

Google is never reached: an httpx.MockTransport plays the token endpoint,
tokeninfo and the Calendar API. No real tokens are used.
"""

import asyncio
import json
import time
import urllib.parse
from types import SimpleNamespace

import httpx
import pytest

import core.database as cdb
import routes.calendar_routes as croutes
from routes.calendar_routes import EventCreate, EventUpdate
from src import google_calendar_sync as gs
from src import google_oauth
from src.google_workspace import connection, products as P

OWNER = "ann"
ACCESS = "ya29.fake-access"
REFRESH = "1//fake-refresh"
FAMILY = "family123@group.calendar.google.com"
_DB = {}


def _item(iid, summary, start, end=None, **kw):
    if len(start) == 10:
        it = {"id": iid, "status": "confirmed", "summary": summary,
              "start": {"date": start}, "end": {"date": end or start}}
    else:
        it = {"id": iid, "status": "confirmed", "summary": summary,
              "start": {"dateTime": start}, "end": {"dateTime": end or start}}
    it.update(kw)
    return it


class FakeCalendarAPI:
    def __init__(self):
        self.calls = []
        self.scopes = [P.CALENDAR_SCOPE, P.CALENDAR_LIST_SCOPE]
        self.refresh = "ok"
        self.events = {"primary": [
            _item("ev1", "Dentist", "2026-10-14T15:00:00-04:00", "2026-10-14T16:00:00-04:00"),
            _item("ev2", "Trip", "2026-10-20", "2026-10-23"),
            _item("rec_20261015T130000Z", "Standup", "2026-10-15T13:00:00Z", "2026-10-15T13:15:00Z",
                  recurringEventId="rec"),
        ], FAMILY: [_item("fam1", "Soccer", "2026-10-18T09:00:00-04:00", "2026-10-18T10:00:00-04:00")]}
        self.changes = {}          # calendar -> items returned for the sync token
        self.gone = False          # answer the sync token with 410
        self.events_status = 200   # e.g. 403 insufficient scope
        self.write_status = 200
        self.created = 0

    def last(self, method, part=""):
        for c in reversed(self.calls):
            if c["method"] == method and part in c["path"]:
                return c
        raise AssertionError(f"no {method} {part}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        u = urllib.parse.urlsplit(str(request.url))
        q = dict(urllib.parse.parse_qsl(u.query))
        body = request.content.decode() if request.content else ""
        self.calls.append({"method": request.method, "path": urllib.parse.unquote(u.path), "q": q, "body": body})
        if str(request.url).startswith(connection.TOKENINFO_URL):
            return httpx.Response(200, json={"aud": "cid.apps.googleusercontent.com",
                                             "scope": " ".join(["openid", "email"] + self.scopes)})
        if str(request.url).startswith(google_oauth.TOKEN_URL):
            if self.refresh == "invalid_grant":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        if str(request.url).startswith(google_oauth.REVOKE_URL):
            return httpx.Response(200)
        assert u.netloc == "www.googleapis.com", u.netloc
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        path = urllib.parse.unquote(u.path)
        if path == "/calendar/v3/users/me/calendarList":
            if P.CALENDAR_LIST_SCOPE not in self.scopes:
                return _insufficient()
            return httpx.Response(200, json={"items": [
                {"id": "ann@example.com", "summary": "ann@example.com", "primary": True,
                 "backgroundColor": "#9fe1e7", "accessRole": "owner"},
                {"id": FAMILY, "summary": "Family", "backgroundColor": "#f83a22", "accessRole": "writer"},
                {"id": "en.usa#holiday@group.v.calendar.google.com", "summary": "Holidays",
                 "accessRole": "reader"}]})
        prefix = "/calendar/v3/calendars/"
        assert path.startswith(prefix), path
        rest = path[len(prefix):]
        cal, _, tail = rest.partition("/events")
        if request.method == "GET":
            if self.events_status != 200:
                return _insufficient() if self.events_status == 403 else httpx.Response(self.events_status)
            assert q.get("singleEvents") == "true"
            if "syncToken" in q:
                assert "timeMin" not in q and "timeMax" not in q
                if self.gone:
                    self.gone = False
                    return httpx.Response(410, json={"error": {"code": 410, "message": "Sync token is no longer valid"}})
                return httpx.Response(200, json={"items": self.changes.get(cal, []),
                                                 "nextSyncToken": f"tok-{cal}-2"})
            assert "timeMin" in q and "timeMax" in q
            return httpx.Response(200, json={"items": self.events.get(cal, []), "nextSyncToken": f"tok-{cal}-1"})
        if self.write_status != 200:
            return _insufficient() if self.write_status == 403 else httpx.Response(self.write_status)
        if request.method == "POST":
            self.created += 1
            item = {"id": f"new{self.created}", "status": "confirmed", **json.loads(body)}
            return httpx.Response(200, json=item)
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": tail.strip("/"), "status": "confirmed", **json.loads(body)})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)


def _insufficient():
    return httpx.Response(403, json={"error": {
        "code": 403, "status": "PERMISSION_DENIED", "message": "Request had insufficient authentication scopes.",
        "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}})


@pytest.fixture
def api(tmp_path, monkeypatch):
    import src.constants
    import routes.prefs_routes as prefs
    from tests.helpers.sqlite_db import make_temp_sqlite
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    if _DB.get("base") is not cdb.Base:
        ts, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
        _DB.update(base=cdb.Base, ts=ts, engine=engine, tmp=tmp)
    ts = _DB["ts"]
    db = ts()
    try:
        db.query(cdb.CalendarEvent).delete()
        db.query(cdb.CalendarCal).delete()
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(cdb, "SessionLocal", ts)
    monkeypatch.setattr(croutes, "SessionLocal", ts)

    store = {}
    monkeypatch.setattr(prefs, "_load_for_user", lambda user=None: dict(store.get(user, {})))
    monkeypatch.setattr(prefs, "_save_for_user", lambda user, p: store.__setitem__(user, dict(p)))

    fake = FakeCalendarAPI()
    monkeypatch.setattr(connection, "_transport", httpx.MockTransport(fake))
    monkeypatch.setattr(connection, "_dir", lambda: str(tmp_path / "google_calendar"))
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.delenv(google_oauth.API_BASE_ENV, raising=False)
    monkeypatch.setattr(google_oauth, "_setting", lambda key: "")
    connection._access.clear()
    gs._in_progress.clear()
    fake.prefs = store
    yield fake
    connection._access.clear()


def _connect(fake, scopes=None, products=("calendar",)):
    from src import secret_storage
    fake.scopes = list(scopes if scopes is not None else fake.scopes)
    connection._save(OWNER, {"email": "ann@example.com", "refresh_token": secret_storage.encrypt(REFRESH),
                             "scopes": " ".join(["openid", "email"] + fake.scopes),
                             "products": list(products), "unverified": [], "checked_at": time.time(),
                             "connected_at": time.time()})


def _events(cal_id=None):
    db = cdb.SessionLocal()
    try:
        q = db.query(cdb.CalendarEvent)
        if cal_id:
            q = q.filter(cdb.CalendarEvent.calendar_id == cal_id)
        return {e.uid: e for e in q.all()}
    finally:
        db.close()


def _cals():
    db = cdb.SessionLocal()
    try:
        return {c.account_id: c for c in db.query(cdb.CalendarCal).filter(cdb.CalendarCal.source == "google").all()}
    finally:
        db.close()


def _req():
    return SimpleNamespace(state=SimpleNamespace(current_user=OWNER))


def _endpoint(method, suffix):
    for r in croutes.setup_calendar_routes().routes:
        if getattr(r, "path", "").endswith(suffix) and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} *{suffix} not found")


def run(coro):
    return asyncio.run(coro)


# ── The calendar list ────────────────────────────────────────────────────

def test_calendar_product_asks_for_the_calendar_list():
    assert P.CALENDAR_LIST_SCOPE in P.scopes_for(["calendar"])
    # Optional: a connection made before it still counts as Calendar on.
    assert P.CALENDAR_LIST_SCOPE not in P.PRODUCTS["calendar"]["scopes"]


def test_calendar_list_with_the_list_permission(api):
    _connect(api)
    st = run(gs.status(OWNER))
    assert st["enabled"] and st["can_list"] and st["email"] == "ann@example.com"
    ids = [c["id"] for c in st["calendars"]]
    assert ids[0] == "primary" and FAMILY in ids
    hol = next(c for c in st["calendars"] if c["name"] == "Holidays")
    assert hol["writable"] is False
    assert REFRESH not in json.dumps(st) and ACCESS not in json.dumps(st)


def test_without_the_list_permission_only_primary_and_a_reconnect_hint(api):
    _connect(api, scopes=[P.CALENDAR_SCOPE])
    st = run(gs.status(OWNER))
    assert st["enabled"] and not st["can_list"]
    assert [c["id"] for c in st["calendars"]] == ["primary"]
    assert st["reconnect_url"] == "/api/google/connect?products=calendar"
    assert not any("calendarList" in c["path"] for c in api.calls)
    # Settings > Google says the optional permission is missing.
    card = next(p for p in connection.status(OWNER)["products"] if p["key"] == "calendar")
    assert card["enabled"] and P.CALENDAR_LIST_SCOPE in card["missing_extra"]
    # The primary calendar still syncs on calendar.events alone.
    res = run(gs.sync(OWNER))
    assert res["errors"] == [] and res["events"] == 3


def test_picking_a_family_calendar_links_and_pulls_it(api):
    _connect(api)
    res = run(gs.set_selection(OWNER, ["primary", FAMILY]))
    assert res["selected"] == ["primary", FAMILY]
    cals = _cals()
    assert set(cals) == {"primary", FAMILY}
    assert cals[FAMILY].name == "Family" and cals[FAMILY].color == "#f83a22"
    assert cals["primary"].owner == OWNER and cals["primary"].source == "google"
    fam = _events(cals[FAMILY].id)
    assert [e.summary for e in fam.values()] == ["Soccer"]
    # The calendar id goes to Google path-encoded.
    assert any(c["path"] == f"/calendar/v3/calendars/{FAMILY}/events" for c in api.calls)
    # Unpicking removes it here (it stays in Google).
    run(gs.set_selection(OWNER, ["primary"]))
    assert set(_cals()) == {"primary"} and _events(cals[FAMILY].id) == {}
    assert api.prefs[OWNER][gs.PREF_KEY] == ["primary"]


# ── Sync ─────────────────────────────────────────────────────────────────

def test_a_connected_account_links_primary_by_itself_and_pulls_the_window(api):
    _connect(api)
    res = run(_endpoint("POST", "/sync")(_req()))
    assert res["errors"] == [], res        # "CalDAV is not configured" is not an error now
    assert res["google"]["linked"] and res["events"] == 3
    cal = _cals()["primary"]
    evs = _events(cal.id)
    by = {e.summary: e for e in evs.values()}
    # Timed: stored as UTC instants.
    assert by["Dentist"].is_utc and by["Dentist"].dtstart.isoformat() == "2026-10-14T19:00:00"
    # All day: dates, end exclusive.
    assert by["Trip"].all_day and not by["Trip"].is_utc
    assert by["Trip"].dtstart.isoformat() == "2026-10-20T00:00:00" and by["Trip"].dtend.isoformat() == "2026-10-23T00:00:00"
    # A recurring series arrives as occurrences; uids never contain "::".
    assert all("::" not in u for u in evs) and all(e.origin == "google" for e in evs.values())
    first = api.last("GET", "/events")
    assert first["q"]["singleEvents"] == "true" and "timeMin" in first["q"] and "syncToken" not in first["q"]
    assert gs._load_state(OWNER)["calendars"][cal.id]["sync_token"] == "tok-primary-1"
    # The page lists it.
    listed = run(_endpoint("GET", "/calendars")(_req()))
    assert any(c["source"] == "google" for c in listed["calendars"]) and "google_notice" not in listed


def test_incremental_sync_sends_only_the_token_and_removes_cancelled(api):
    _connect(api)
    run(gs.sync(OWNER))
    cal = _cals()["primary"]
    api.changes["primary"] = [
        {"id": "ev1", "status": "cancelled"},
        _item("ev3", "Lunch", "2026-10-16T12:00:00Z", "2026-10-16T13:00:00Z"),
        _item("ev2", "Trip (moved)", "2026-10-21", "2026-10-24"),
    ]
    res = run(gs.sync(OWNER))
    assert res["errors"] == [] and res["deleted"] == 1
    call = api.last("GET", "/events")
    assert call["q"]["syncToken"] == "tok-primary-1" and "timeMin" not in call["q"]
    names = sorted(e.summary for e in _events(cal.id).values())
    assert names == ["Lunch", "Standup", "Trip (moved)"]
    assert gs._load_state(OWNER)["calendars"][cal.id]["sync_token"] == "tok-primary-2"


def test_410_gone_starts_over_with_a_full_pull(api):
    _connect(api)
    run(gs.sync(OWNER))
    cal = _cals()["primary"]
    api.gone = True
    api.events["primary"] = [_item("ev9", "Only one left", "2026-10-30T10:00:00Z", "2026-10-30T11:00:00Z")]
    res = run(gs.sync(OWNER))
    assert res["errors"] == []
    gets = [c for c in api.calls if c["method"] == "GET" and c["path"].endswith("/events")]
    assert "syncToken" in gets[-2]["q"] and "timeMin" in gets[-1]["q"]
    # The full pull pruned what Google no longer has.
    assert [e.summary for e in _events(cal.id).values()] == ["Only one left"]
    assert gs._load_state(OWNER)["calendars"][cal.id]["sync_token"] == "tok-primary-1"


def test_an_old_token_gets_a_full_pull_to_move_the_window(api):
    _connect(api)
    run(gs.sync(OWNER))
    cal = _cals()["primary"]
    st = gs._load_state(OWNER)
    st["calendars"][cal.id]["full_at"] = time.time() - gs.FULL_EVERY_S - 5
    gs._save_state(OWNER, st)
    run(gs.sync(OWNER))
    assert "timeMin" in api.last("GET", "/events")["q"]


def test_unlinking_stays_unlinked(api):
    _connect(api)
    run(gs.sync(OWNER))
    run(_endpoint("DELETE", "/google")(_req()))
    assert _cals() == {}
    res = run(gs.sync(OWNER))
    assert _cals() == {} and not res["linked"]


# ── Writes from the Calendar page ────────────────────────────────────────

def test_ui_create_edit_delete_go_to_google(api):
    _connect(api)
    run(gs.sync(OWNER))
    cal = _cals()["primary"]
    create = _endpoint("POST", "/events")
    res = run(create(_req(), EventCreate(summary="Haircut", dtstart="2026-10-22T17:00:00Z",
                                         dtend="2026-10-22T17:30:00Z", calendar_href=cal.id)))
    post = api.last("POST", "/events")
    sent = json.loads(post["body"])
    assert sent["summary"] == "Haircut" and sent["start"] == {"dateTime": "2026-10-22T17:00:00Z"}
    assert post["path"] == "/calendar/v3/calendars/primary/events"
    uid = res["uid"]
    assert uid == gs.local_uid(cal.id, "new1") and _events(cal.id)[uid].summary == "Haircut"

    update = _endpoint("PUT", "/events/{uid}")
    run(update(_req(), uid, EventUpdate(summary="Haircut (Ana)")))
    patch = api.last("PATCH")
    assert patch["path"].endswith("/events/new1") and json.loads(patch["body"])["summary"] == "Haircut (Ana)"
    assert _events(cal.id)[uid].summary == "Haircut (Ana)"

    # An occurrence of a series is edited on its own.
    occ = gs.local_uid(cal.id, "rec_20261015T130000Z")
    run(update(_req(), occ, EventUpdate(location="Room 2")))
    assert api.last("PATCH")["path"].endswith("/events/rec_20261015T130000Z")

    delete = _endpoint("DELETE", "/events/{uid}")
    run(delete(_req(), uid))
    assert api.last("DELETE")["path"].endswith("/events/new1") and uid not in _events(cal.id)

    # An all-day event goes as dates.
    run(create(_req(), EventCreate(summary="Holiday", dtstart="2026-11-01", all_day=True, calendar_href=cal.id)))
    sent = json.loads(api.last("POST", "/events")["body"])
    assert sent["start"] == {"date": "2026-11-01"} and sent["end"] == {"date": "2026-11-02"}


def test_ui_write_google_refuses_is_an_error_not_a_phantom(api):
    from fastapi import HTTPException
    _connect(api)
    run(gs.sync(OWNER))
    cal = _cals()["primary"]
    before = set(_events(cal.id))
    api.write_status = 500
    with pytest.raises(HTTPException) as e:
        run(_endpoint("POST", "/events")(_req(), EventCreate(summary="X", dtstart="2026-10-22T17:00:00Z",
                                                              calendar_href=cal.id)))
    assert e.value.status_code == 502
    assert set(_events(cal.id)) == before
    uid = gs.local_uid(cal.id, "ev1")
    with pytest.raises(HTTPException):
        run(_endpoint("PUT", "/events/{uid}")(_req(), uid, EventUpdate(summary="Changed")))
    assert _events(cal.id)[uid].summary == "Dentist"       # rolled back
    with pytest.raises(HTTPException):
        run(_endpoint("DELETE", "/events/{uid}")(_req(), uid))
    assert uid in _events(cal.id)


def test_ui_write_without_the_connection_says_to_reconnect(api):
    from fastapi import HTTPException
    _connect(api)
    run(gs.sync(OWNER))
    cal = _cals()["primary"]
    connection._forget(OWNER)
    with pytest.raises(HTTPException) as e:
        run(_endpoint("POST", "/events")(_req(), EventCreate(summary="X", dtstart="2026-10-22T17:00:00Z",
                                                              calendar_href=cal.id)))
    assert e.value.status_code == 409 and "Settings > Google" in e.value.detail


# ── Problems are visible ─────────────────────────────────────────────────

def test_revoked_connection_shows_a_notice_not_an_empty_calendar(api):
    _connect(api)
    run(gs.sync(OWNER))
    connection._access.clear()
    api.refresh = "invalid_grant"
    res = run(_endpoint("POST", "/sync")(_req()))
    assert res.get("google_reconnect") and any("Google Calendar:" in e for e in res["errors"])
    listed = run(_endpoint("GET", "/calendars")(_req()))
    n = listed["google_notice"]
    assert n["action"] == "settings" and "Settings > Google" in n["text"]
    # The events already pulled stay on the page meanwhile.
    assert _events(_cals()["primary"].id)


def test_missing_scope_on_events_is_surfaced(api):
    _connect(api)
    api.events_status = 403
    res = run(gs.sync(OWNER))
    assert res.get("reconnect") and "Settings > Google" in res["errors"][0]
    n = gs.notice(OWNER)
    assert n and n["action"] == "settings"
    # It clears once a sync works again.
    api.events_status = 200
    run(gs.sync(OWNER))
    assert gs.notice(OWNER) is None


def test_calendar_turned_off_in_settings_is_a_notice(api):
    _connect(api)
    run(gs.sync(OWNER))
    _connect(api, products=("drive",))
    n = gs.notice(OWNER)
    assert n and "Settings > Google" in n["text"]
    st = run(gs.status(OWNER))
    assert not st["enabled"] and st["calendars"] and "Settings > Google" in st["message"]


def test_another_owner_does_not_see_anns_google_calendar(api):
    _connect(api)
    run(gs.sync(OWNER))
    bob = SimpleNamespace(state=SimpleNamespace(current_user="bob"))
    listed = run(_endpoint("GET", "/calendars")(bob))
    assert all(c["source"] != "google" for c in listed["calendars"])
    st = run(gs.status("bob"))
    assert not st["connected"] and st["calendars"] == []
