"""Drive, Docs, Sheets, Slides, Tasks and Contacts left the core for the
Google add-on (mod `google`). The core keeps Calendar on its
Google connection, has none of the Workspace tools, points to the add-on,
and does not revoke a grant the add-on may share. Nothing reaches Google."""
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_no_workspace_tools_in_the_core():
    import src.agent_tools  # noqa: F401  (import order)
    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    moved = {"google_status", "google_drive_search", "google_doc_create", "google_sheet_write",
             "google_slides_create", "google_task_create", "google_contacts_search"}
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert not moved & names and not moved & set(TOOL_TAGS) and not moved & set(BUILTIN_TOOL_DESCRIPTIONS)
    assert "google_meet" not in names  # Meet is the Phone & Messages mod's now


def test_only_calendar_is_a_core_product():
    from src.google_workspace import products as P
    assert list(P.PRODUCTS) == ["calendar"]
    assert set(P.ADDON_PRODUCTS) == {"drive", "drive_all", "docs", "sheets", "slides", "tasks", "contacts"}
    assert P.ADDON_ID == "google"


def test_settings_points_to_the_add_on():
    js = open(os.path.join(ROOT, "static/js/googleSettings.js"), encoding="utf-8").read()
    assert "Install the Google add-on" in js and "Open the Google add-on" in js
    assert "/api/mods/ui" in js and "google_drive_search" not in js


@pytest.fixture
def conn(tmp_path, monkeypatch):
    import src.constants
    from src import secret_storage
    from src.google_workspace import connection
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    revoked = []

    async def fake_revoke(token):
        revoked.append(token)
    monkeypatch.setattr(connection, "_revoke", fake_revoke)
    connection._save("ann", {"email": "a@x.com", "refresh_token": secret_storage.encrypt("rt-1"),
                             "scopes": "openid email https://www.googleapis.com/auth/calendar.events",
                             "products": ["calendar", "docs"]})
    return connection, revoked


def test_an_old_connection_listing_moved_products_still_works_for_calendar(conn):
    connection, _ = conn
    assert connection.enabled_products("ann") == ["calendar"]
    assert [p["key"] for p in connection.status("ann")["products"]] == ["calendar"]


def test_disconnect_revokes_without_the_add_on(conn, monkeypatch):
    connection, revoked = conn
    monkeypatch.setattr(connection, "_addon_installed", lambda: False)
    asyncio.run(connection.disconnect("ann"))
    assert revoked == ["rt-1"] and connection.load("ann") == {}


def test_disconnect_keeps_the_grant_when_the_add_on_is_installed(conn, monkeypatch):
    connection, revoked = conn
    monkeypatch.setattr(connection, "_addon_installed", lambda: True)
    asyncio.run(connection.disconnect("ann"))
    assert revoked == [] and connection.load("ann") == {}
