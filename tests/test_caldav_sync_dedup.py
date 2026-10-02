"""`sync_caldav` must not run two syncs for the same owner concurrently.

Covers the "no duplicate work" requirement for the periodic background sync
(src/caldav_background_sync.py): a manual "Sync now" click and a background
pass can land on the same owner at nearly the same moment. `sync_caldav`
tracks in-flight owners in `_sync_in_progress` and short-circuits a second
call for an owner already syncing, instead of running both REPORT requests
and letting two writers race on the same local rows.
"""
import asyncio

import pytest

from src import caldav_sync


def test_sync_caldav_skips_when_already_in_progress(monkeypatch):
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda owner: [
        {"id": "a1", "label": "Work", "url": "https://dav.example.com/cal", "username": "u", "password": "enc:pw"}
    ])

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(caldav_sync.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda u: u)

    secret_mod = __import__("types").ModuleType("src.secret_storage")
    secret_mod.decrypt = lambda v: "pw"
    import sys
    monkeypatch.setitem(sys.modules, "src.secret_storage", secret_mod)

    # Mark "bob" as already syncing, as a concurrent call would.
    caldav_sync._sync_in_progress.add("bob")
    try:
        called = []
        monkeypatch.setattr(caldav_sync, "_sync_blocking", lambda *a, **k: called.append(1) or {
            "calendars": 1, "events": 1, "deleted": 0, "errors": [],
        })
        result = asyncio.run(caldav_sync.sync_caldav("bob"))
        assert result["skipped"] == "sync already in progress"
        assert called == [], "a second sync must not have run the blocking sync at all"
    finally:
        caldav_sync._sync_in_progress.discard("bob")


def test_sync_caldav_runs_normally_for_an_owner_not_in_progress(monkeypatch):
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda owner: [
        {"id": "a1", "label": "Work", "url": "https://dav.example.com/cal", "username": "u", "password": "enc:pw"}
    ])

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(caldav_sync.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda u: u)

    import sys, types
    secret_mod = types.ModuleType("src.secret_storage")
    secret_mod.decrypt = lambda v: "pw"
    monkeypatch.setitem(sys.modules, "src.secret_storage", secret_mod)

    monkeypatch.setattr(caldav_sync, "_sync_blocking", lambda *a, **k: {
        "calendars": 1, "events": 2, "deleted": 0, "errors": [],
    })
    assert "carol" not in caldav_sync._sync_in_progress
    result = asyncio.run(caldav_sync.sync_caldav("carol"))
    assert result["events"] == 2
    assert "skipped" not in result
    # The in-progress marker is cleared once the sync finishes, so a later
    # call for the same owner is not permanently blocked.
    assert "carol" not in caldav_sync._sync_in_progress


def test_in_progress_marker_cleared_even_on_failure(monkeypatch):
    """An exception raised while iterating accounts must still release the
    lock, or that owner's calendar would never sync again until a restart.
    (Per-account CalDAV/network failures are already caught and folded into
    `errors`; this covers anything unexpected that is not.)"""
    # A malformed account entry (not a dict) blows up on the first `.get()`
    # call in the per-account loop, outside any of its try/except blocks.
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda owner: ["not-a-dict"])
    with pytest.raises(AttributeError):
        asyncio.run(caldav_sync.sync_caldav("dave"))
    assert "dave" not in caldav_sync._sync_in_progress
