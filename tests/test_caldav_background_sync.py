"""Periodic background CalDAV sync (src/caldav_background_sync.py).

Covers: the interval is configurable via CALDAV_SYNC_INTERVAL_S with a sane
floor and default, and one pass syncs every known owner (not just whichever
user happens to have the calendar open).
"""
import asyncio
import json

import pytest

from src import caldav_background_sync as bg


def test_interval_defaults_to_300(monkeypatch):
    monkeypatch.delenv("CALDAV_SYNC_INTERVAL_S", raising=False)
    assert bg.interval_s() == 300


def test_interval_reads_env_var(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_INTERVAL_S", "600")
    assert bg.interval_s() == 600


def test_interval_has_a_floor_against_misconfiguration(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_INTERVAL_S", "1")
    assert bg.interval_s() == 30  # _MIN_INTERVAL_S


def test_interval_falls_back_to_default_on_garbage_value(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_INTERVAL_S", "not-a-number")
    assert bg.interval_s() == 300


def test_known_owners_reads_auth_file(monkeypatch, tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"users": {"alice": {}, "bob": {}}}))

    class _Const:
        AUTH_FILE = str(auth_path)

    import sys
    monkeypatch.setitem(sys.modules, "src.constants", _Const())
    assert bg._known_owners() == ["alice", "bob"]


def test_known_owners_empty_when_auth_file_missing(monkeypatch, tmp_path):
    class _Const:
        AUTH_FILE = str(tmp_path / "does-not-exist.json")

    import sys
    monkeypatch.setitem(sys.modules, "src.constants", _Const())
    assert bg._known_owners() == []


def test_sync_all_owners_calls_sync_caldav_for_each_owner(monkeypatch):
    calls = []

    async def fake_sync_caldav(owner):
        calls.append(owner)
        return {"calendars": 1, "events": 0, "deleted": 0, "errors": []}

    monkeypatch.setattr(bg, "_known_owners", lambda: ["alice", "bob"])
    sync_mod = __import__("src.caldav_sync", fromlist=["sync_caldav"])
    monkeypatch.setattr(sync_mod, "sync_caldav", fake_sync_caldav)

    results = asyncio.run(bg.sync_all_owners())
    assert sorted(calls) == ["alice", "bob"]
    assert results["alice"]["calendars"] == 1
    assert results["bob"]["calendars"] == 1


def test_sync_all_owners_keeps_going_after_one_owner_errors(monkeypatch):
    async def fake_sync_caldav(owner):
        if owner == "alice":
            raise RuntimeError("boom")
        return {"calendars": 1, "events": 0, "deleted": 0, "errors": []}

    monkeypatch.setattr(bg, "_known_owners", lambda: ["alice", "bob"])
    sync_mod = __import__("src.caldav_sync", fromlist=["sync_caldav"])
    monkeypatch.setattr(sync_mod, "sync_caldav", fake_sync_caldav)

    results = asyncio.run(bg.sync_all_owners())
    assert "boom" in results["alice"]["errors"][0]
    assert results["bob"]["calendars"] == 1


def test_is_notable_ignores_the_common_unconfigured_case():
    assert bg._is_notable({"errors": ["CalDAV is not configured"]}) is False
    assert bg._is_notable({"events": 0, "deleted": 0, "errors": []}) is False
    assert bg._is_notable({"events": 3, "deleted": 0, "errors": []}) is True
    assert bg._is_notable({"events": 0, "deleted": 1, "errors": []}) is True
    assert bg._is_notable({"errors": ["auth failed"]}) is True
