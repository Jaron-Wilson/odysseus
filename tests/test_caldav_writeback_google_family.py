"""Write-back must target the right Google calendar, including a shared
family calendar (#2507 follow-up).

`_writeback_blocking` used generic principal discovery (`_discover_calendars`)
to find the remote calendar to push to. On Google that discovery only ever
returns the account's own (primary) calendar, the same limitation
`caldav_sync._sync_blocking` already works around on the pull side via
`_is_google_calendar_url`/direct-URL open. Before the fix, an edit on an
event living on a family/shared Google calendar would silently get pushed to
the account's primary calendar instead (or fail to push at all), while the
pull side correctly read it from the family calendar.

This mirrors the fake-caldav harness in test_caldav_google_principal_url.py:
no live Google account, no network, just a fake `caldav` module standing in
for the real one.
"""
import sys
import types

from datetime import datetime

from src import caldav_writeback as wb
from src.caldav_sync import _stable_cal_id

_PRIMARY_EVENTS = "https://apidata.googleusercontent.com/caldav/v2/me@gmail.com/events"
_FAMILY_EVENTS = ("https://apidata.googleusercontent.com/caldav/v2/"
                  "family01234567890@group.calendar.google.com/events")

OWNER = "alice"
ACCOUNT_ID = "acct-1"
FAMILY_CAL_ID = _stable_cal_id(_FAMILY_EVENTS, owner=OWNER, account_id=ACCOUNT_ID)


class _FakeEvent:
    def __init__(self):
        self.data = "OLD"
        self.saved = False

    def save(self):
        self.saved = True


class _FakeCalendar:
    def __init__(self, url, existing=None):
        self.url = url
        self._existing = existing
        self.saved_ical = None

    def event_by_uid(self, uid):
        if self._existing is None:
            raise Exception("not found")
        return self._existing

    def save_event(self, ical):
        self.saved_ical = ical


class _FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


class _FakeClient:
    """Mimics Google: principal discovery only ever surfaces the primary
    calendar, never the family one. Opening a URL directly works for both."""

    def __init__(self, url=None, username=None, password=None):
        self.url = url
        self.session = types.SimpleNamespace(max_redirects=30)
        self._primary = _FakeCalendar(_PRIMARY_EVENTS, existing=None)
        self._family_existing = _FakeEvent()
        self._family = _FakeCalendar(_FAMILY_EVENTS, existing=self._family_existing)

    def principal(self):
        return _FakePrincipal([self._primary])

    def calendar(self, url=None):
        if url == _FAMILY_EVENTS:
            return self._family
        if url == _PRIMARY_EVENTS:
            return self._primary
        raise Exception(f"unexpected calendar url {url!r}")


def _install_fake_caldav(monkeypatch):
    fake = types.ModuleType("caldav")
    fake.DAVClient = _FakeClient
    err = types.ModuleType("caldav.lib.error")

    class AuthorizationError(Exception):
        pass

    class NotFoundError(Exception):
        pass

    err.AuthorizationError = AuthorizationError
    err.NotFoundError = NotFoundError
    lib = types.ModuleType("caldav.lib")
    lib.error = err
    fake.lib = lib
    monkeypatch.setitem(sys.modules, "caldav", fake)
    monkeypatch.setitem(sys.modules, "caldav.lib", lib)
    monkeypatch.setitem(sys.modules, "caldav.lib.error", err)


def _ev(**over):
    base = dict(
        uid="evt-1@google", summary="Soccer practice", description="",
        location="", dtstart=datetime(2026, 6, 10, 14, 0),
        dtend=datetime(2026, 6, 10, 15, 0), all_day=False, is_utc=True, rrule="",
    )
    base.update(over)
    return base


def test_edit_on_family_calendar_pushes_to_family_url_not_primary(monkeypatch):
    _install_fake_caldav(monkeypatch)
    asked_principal = []
    real_principal = _FakeClient.principal
    monkeypatch.setattr(_FakeClient, "principal", lambda self: asked_principal.append(1) or real_principal(self))

    result = wb._writeback_blocking(
        FAMILY_CAL_ID, _ev(summary="Moved practice"), delete=False,
        url=_FAMILY_EVENTS, username="me@gmail.com", password="app-pw",
        owner=OWNER, account_id=ACCOUNT_ID,
    )

    assert result == {"ok": True, "updated": True}, result
    # Discovery never ran, the family URL was opened directly.
    assert asked_principal == []


def test_edit_pushes_to_family_event_object_directly(monkeypatch):
    _install_fake_caldav(monkeypatch)
    import caldav as fake_caldav
    client = fake_caldav.DAVClient(url=_FAMILY_EVENTS, username="me@gmail.com", password="app-pw")
    # Patch _build_dav_client to return this same instance so we can inspect it.
    monkeypatch.setattr(wb, "_discover_calendars", lambda c: (_ for _ in ()).throw(
        AssertionError("discovery must not run for a Google calendar URL")))

    def fake_build_dav_client(url, username, password):
        return client

    import src.caldav_sync as sync_mod
    monkeypatch.setattr(sync_mod, "_build_dav_client", fake_build_dav_client)

    result = wb._writeback_blocking(
        FAMILY_CAL_ID, _ev(summary="Moved practice"), delete=False,
        url=_FAMILY_EVENTS, username="me@gmail.com", password="app-pw",
        owner=OWNER, account_id=ACCOUNT_ID,
    )

    assert result == {"ok": True, "updated": True}, result
    assert client._family_existing.saved is True
    assert "SUMMARY:Moved practice" in client._family_existing.data
    # The primary calendar was never touched.
    assert client._primary.saved_ical is None
