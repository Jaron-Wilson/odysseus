""""Save new events to" default-calendar preference.

New events created without an explicit calendar (the UI's bare "+" button,
and the `manage_calendar` agent tool with no calendar/calendar_href arg) used
to always land on `_ensure_default_calendar`, a hardcoded local-only
calendar. This pins the "Save new events to" pref (routes/calendar_routes.py
`_get_default_calendar`, backed by the generic /api/prefs/<key> store under
key "calendar_default_cal_id") so events can default onto a CalDAV/Google
calendar instead, and that doing so actually reaches the remote (write-back
fires) for both the route and the `manage_calendar` tool.
"""
import json
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.calendar_routes as croutes
import src.caldav_writeback as wb
from core.database import CalendarCal, CalendarEvent
from routes.calendar_routes import EventCreate, _get_default_calendar

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)
croutes.SessionLocal = _TS
cdb.SessionLocal = _TS


@pytest.fixture
def writeback_calls(monkeypatch):
    recorded = []

    async def _fake_writeback(owner, source, cal_id, ev, *, delete=False):
        recorded.append({
            "owner": owner, "source": source, "cal_id": cal_id,
            "uid": ev.get("uid"), "delete": delete,
        })
        return {"ok": True}

    monkeypatch.setattr(wb, "writeback_event", _fake_writeback)
    return recorded


def _make_cal(owner, source):
    cid = f"{source}-{uuid.uuid4().hex[:10]}"
    db = _TS()
    try:
        db.add(CalendarCal(id=cid, owner=owner, name="C", source=source))
        db.commit()
        return cid
    finally:
        db.close()


def _set_pref(monkeypatch, owner, default_cal_id):
    def fake_load_for_user(u):
        if u == owner:
            return {"calendar_default_cal_id": default_cal_id}
        return {}

    monkeypatch.setattr("routes.prefs_routes._load_for_user", fake_load_for_user)


def _req(owner):
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(current_user=owner))


def _endpoint(method, suffix):
    router = croutes.setup_calendar_routes()
    for r in router.routes:
        if getattr(r, "path", "").endswith(suffix) and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} *{suffix} not found")


# ── _get_default_calendar (the shared helper) ──

def test_get_default_calendar_honors_pref(monkeypatch):
    owner = "pref-" + uuid.uuid4().hex[:6]
    caldav_id = _make_cal(owner, "caldav")
    _set_pref(monkeypatch, owner, caldav_id)
    db = _TS()
    try:
        cal = _get_default_calendar(db, owner)
        assert cal.id == caldav_id
    finally:
        db.close()


def test_get_default_calendar_falls_back_when_pref_unset(monkeypatch):
    owner = "nopref-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda u: {})
    db = _TS()
    try:
        cal = _get_default_calendar(db, owner)
        assert cal.owner == owner
        assert cal.source == "local"
    finally:
        db.close()


def test_get_default_calendar_ignores_cross_owner_pref(monkeypatch):
    """A pref pointing at a calendar id that belongs to someone else (or was
    deleted) must not leak events into it, fall back instead."""
    owner = "victim-" + uuid.uuid4().hex[:6]
    other_owner = "other-" + uuid.uuid4().hex[:6]
    other_cal = _make_cal(other_owner, "caldav")
    _set_pref(monkeypatch, owner, other_cal)
    db = _TS()
    try:
        cal = _get_default_calendar(db, owner)
        assert cal.id != other_cal
        assert cal.owner == owner
    finally:
        db.close()


# ── Route: POST /api/calendar/events with no calendar_href ──

async def test_route_create_event_defaults_onto_preferred_caldav_calendar(monkeypatch, writeback_calls):
    owner = "route-" + uuid.uuid4().hex[:6]
    caldav_id = _make_cal(owner, "caldav")
    _set_pref(monkeypatch, owner, caldav_id)
    create_event = _endpoint("POST", "/events")

    res = await create_event(_req(owner), EventCreate(
        summary="Family dinner", dtstart="2026-06-10T18:00:00Z"))

    assert res["ok"] is True
    db = _TS()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == res["uid"]).first()
        assert ev.calendar_id == caldav_id
    finally:
        db.close()
    assert len(writeback_calls) == 1
    assert writeback_calls[0]["cal_id"] == caldav_id


# ── manage_calendar agent tool ──

async def test_manage_calendar_create_defaults_onto_preferred_calendar_and_pushes(monkeypatch, writeback_calls):
    from src.tool_implementations import do_manage_calendar

    owner = "agent-" + uuid.uuid4().hex[:6]
    caldav_id = _make_cal(owner, "caldav")
    _set_pref(monkeypatch, owner, caldav_id)

    payload = {
        "action": "create_event",
        "summary": "Pickup kids",
        "dtstart": "2026-06-11T15:00:00",
    }
    res = await do_manage_calendar(json.dumps(payload), owner=owner)
    assert res.get("exit_code") == 0, res

    db = _TS()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == res["uid"]).first()
        assert ev.calendar_id == caldav_id
    finally:
        db.close()
    assert len(writeback_calls) == 1
    assert writeback_calls[0]["cal_id"] == caldav_id
    assert writeback_calls[0]["owner"] == owner


async def test_manage_calendar_update_on_caldav_event_pushes(monkeypatch, writeback_calls):
    from src.tool_implementations import do_manage_calendar

    owner = "agentupd-" + uuid.uuid4().hex[:6]
    caldav_id = _make_cal(owner, "caldav")
    _set_pref(monkeypatch, owner, caldav_id)
    created = await do_manage_calendar(json.dumps({
        "action": "create_event", "summary": "Soccer", "dtstart": "2026-06-12T09:00:00",
    }), owner=owner)
    writeback_calls.clear()

    res = await do_manage_calendar(json.dumps({
        "action": "update_event", "uid": created["uid"], "summary": "Soccer (moved)",
    }), owner=owner)
    assert res.get("exit_code") == 0, res
    assert len(writeback_calls) == 1
    assert writeback_calls[0]["cal_id"] == caldav_id
    assert writeback_calls[0]["delete"] is False


async def test_manage_calendar_delete_on_caldav_event_pushes_delete(monkeypatch, writeback_calls):
    from src.tool_implementations import do_manage_calendar

    owner = "agentdel-" + uuid.uuid4().hex[:6]
    caldav_id = _make_cal(owner, "caldav")
    _set_pref(monkeypatch, owner, caldav_id)
    created = await do_manage_calendar(json.dumps({
        "action": "create_event", "summary": "Dentist", "dtstart": "2026-06-13T09:00:00",
    }), owner=owner)
    writeback_calls.clear()

    res = await do_manage_calendar(json.dumps({
        "action": "delete_event", "uid": created["uid"],
    }), owner=owner)
    assert res.get("exit_code") == 0, res
    assert len(writeback_calls) == 1
    assert writeback_calls[0]["delete"] is True
    assert writeback_calls[0]["cal_id"] == caldav_id


async def test_manage_calendar_does_not_push_for_local_calendar(monkeypatch, writeback_calls):
    from src.tool_implementations import do_manage_calendar

    owner = "agentlocal-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda u: {})
    res = await do_manage_calendar(json.dumps({
        "action": "create_event", "summary": "Just local", "dtstart": "2026-06-14T09:00:00",
    }), owner=owner)
    assert res.get("exit_code") == 0, res
    assert writeback_calls == []
