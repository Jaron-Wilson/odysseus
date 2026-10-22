"""Where mod tabs go in the navigation (src/mods/placement.py): the manifest's
`section`/`after`/`order`, their validation, the order within a group, and the
admin's override in Settings > Mods winning over the manifest."""
import os
import re

import pytest

from src import tool_pages
from src.mods import manifest as mf, placement, registry
from tests.mods_helpers import HELLO, ROOT, add_mod, mods_dir  # noqa: F401
from tests.test_mods_api import FakeAuth, client  # noqa: F401


def _man(tabs):
    return mf.validate({"id": "print-hub", "name": "Print Hub", "version": "0.1.0",
                        "runtime": {"type": "process", "command": ["python", "-m", "printhub"]},
                        "ui": {"tabs": tabs},
                        "tutorial": {"steps": [{"title": "Start", "body": "Open the tab."}]}})


# ── Manifest validation ─────────────────────────────────────────────────

def test_group_ids_are_the_sidebar_groups_plus_mods():
    assert placement.group_ids() == [g["id"] for g in tool_pages.GROUPS] + ["mods"]
    assert placement.group_ids() == ["organize", "create", "build", "system", "mods"]
    assert "print" in placement.page_keys("create")


def test_section_after_and_order_are_kept():
    m = _man([{"id": "printer", "title": "Printer", "path": "/", "section": "create", "after": "print", "order": 2}])
    t = m["ui"]["tabs"][0]
    assert (t["section"], t["after"], t["order"]) == ("create", "print", 2)
    assert m["warnings"] == []


def test_old_manifests_still_validate_and_go_in_mods():
    m = _man([{"id": "main", "path": "/"}])
    t = m["ui"]["tabs"][0]
    assert (t["section"], t["after"], t["order"]) == (None, None, 0)
    assert m["warnings"] == [] and m["manifest_version"] == 1
    assert placement.resolve(t)["section"] == "mods"


@pytest.mark.parametrize("tab, words", [
    ({"section": "kitchen"}, "not a nav group"),
    ({"section": "create", "after": "terminal"}, "not a page in"),
    ({"after": "print"}, "not a page in"),
    ({"section": "create", "after": "nope"}, "not a page in"),
])
def test_unknown_places_are_warnings_not_errors(tab, words):
    m = _man([dict(tab, id="t", path="/")])
    assert any(words in w for w in m["warnings"]), m["warnings"]
    t = m["ui"]["tabs"][0]
    assert t["after"] is None
    assert placement.resolve(t)["after"] is None
    if tab.get("section") == "kitchen":
        assert placement.resolve(t)["section"] == "mods"


@pytest.mark.parametrize("tab, words", [
    ({"section": 3}, "section"),
    ({"section": ""}, "section"),
    ({"after": ["print"]}, "after"),
    ({"order": "1"}, "order"),
    ({"order": True}, "order"),
    ({"order": 5000}, "order"),
])
def test_wrong_types_are_errors(tab, words):
    with pytest.raises(mf.ManifestError) as e:
        _man([dict(tab, id="t", path="/")])
    assert any(words in err for err in e.value.errors), e.value.errors


def test_hello_mod_declares_create_after_print():
    t = mf.load(HELLO)["ui"]["tabs"][0]
    assert (t["section"], t["after"]) == ("create", "print")


def test_template_declares_a_section():
    t = mf.load(os.path.join(ROOT, "templates", "mod-template"))["ui"]["tabs"][0]
    assert t["section"] in placement.group_ids()


# ── Placement and order ─────────────────────────────────────────────────

def _entry(mod_id, tabs, name=None, placement_=None):
    e = {"id": mod_id, "name": name or mod_id, "manifest": _man(tabs)}
    e["manifest"]["id"] = mod_id
    if placement_ is not None:
        e["placement"] = placement_
    return e


def test_nav_order_anchor_then_order_then_title():
    entries = [
        _entry("zeta", [{"id": "a", "title": "Zeta", "path": "/", "section": "create", "after": "print"}]),
        _entry("alpha", [{"id": "a", "title": "Alpha", "path": "/", "section": "create", "after": "print"}]),
        _entry("early", [{"id": "a", "title": "Early", "path": "/", "section": "create", "after": "print", "order": -1}]),
        _entry("lib", [{"id": "a", "title": "After library", "path": "/", "section": "create", "after": "library"}]),
        _entry("end", [{"id": "a", "title": "At the end", "path": "/", "section": "create"}]),
        _entry("org", [{"id": "a", "title": "Org", "path": "/", "section": "organize"}]),
        _entry("plain", [{"id": "a", "title": "Plain", "path": "/"}]),
    ]
    order = [(n["mod"], n["section"], n["after"]) for n in placement.nav(entries)]
    assert order == [
        ("org", "organize", None),
        ("lib", "create", "library"),
        ("early", "create", "print"),
        ("alpha", "create", "print"),
        ("zeta", "create", "print"),
        ("end", "create", None),
        ("plain", "mods", None),
    ]


def test_ties_sort_by_title_not_by_mod_id():
    entries = [_entry("aaa", [{"id": "t", "title": "Weather", "path": "/", "section": "system"}]),
               _entry("zzz", [{"id": "t", "title": "Backups", "path": "/", "section": "system"}])]
    assert [n["title"] for n in placement.nav(entries)] == ["Backups", "Weather"]


# ── The user's override ─────────────────────────────────────────────────

def test_override_section_wins_and_drops_after():
    tab = _man([{"id": "p", "path": "/", "section": "create", "after": "print", "order": 3}])["ui"]["tabs"][0]
    assert placement.resolve(tab, {"section": "build"}) == {"section": "build", "after": None, "order": 3, "hidden": False}
    # Choosing the manifest's own group keeps its spot.
    assert placement.resolve(tab, {"section": "create"})["after"] == "print"
    # No choice (or a group that has gone away) means the manifest's.
    assert placement.resolve(tab, {"section": None})["section"] == "create"
    assert placement.resolve(tab, {"section": "gone"})["section"] == "create"


def test_hidden_tabs_are_left_out_of_nav():
    e = _entry("h", [{"id": "p", "path": "/", "section": "create"}], placement_={"section": None, "hidden": True})
    assert placement.nav([e]) == []
    assert placement.resolve(e["manifest"]["ui"]["tabs"][0], e["placement"])["hidden"] is True


def test_clean_override():
    assert placement.clean_override("", 0) == {"section": None, "hidden": False}
    assert placement.clean_override("system", 1) == {"section": "system", "hidden": True}
    with pytest.raises(ValueError):
        placement.clean_override("kitchen", False)


def test_registry_keeps_the_override(mods_dir):
    add_mod({"ui": {"tabs": [{"id": "main", "path": "/", "section": "create", "after": "print"}]}})
    assert registry.public(registry.get("test-mod"))["placement"] == {"section": None, "hidden": False}
    registry.set_placement("test-mod", "system", False)
    assert registry.get("test-mod")["placement"] == {"section": "system", "hidden": False}
    with pytest.raises(registry.RegistryError):
        registry.set_placement("test-mod", "kitchen", False)


# ── Through the routes ──────────────────────────────────────────────────

def test_ui_route_places_tabs_and_honors_the_override(client):
    add_mod({"ui": {"tabs": [{"id": "main", "title": "Printer", "path": "/", "section": "create", "after": "print"}]}},
            access="users")
    add_mod({"ui": {"tabs": [{"id": "main", "path": "/"}]}}, mod_id="admin-only")
    add_mod({"ui": {"tabs": [{"id": "main", "path": "/", "section": "create"}]}}, mod_id="off-mod", enabled=False,
            access="users")
    alice, bob = {"x-test-user": "alice"}, {"x-test-user": "bob"}
    d = client.get("/api/mods/ui", headers=alice).json()
    assert [(n["mod"], n["section"], n["after"]) for n in d["nav"]] == [
        ("test-mod", "create", "print"), ("admin-only", "mods", None)]
    assert d["groups"][-1] == {"id": "mods", "label": "Mods"}
    tab = next(m for m in d["mods"] if m["id"] == "test-mod")["tabs"][0]
    assert (tab["section"], tab["after"], tab["hidden"]) == ("create", "print", False)
    # Who can use it still applies.
    assert [n["mod"] for n in client.get("/api/mods/ui", headers=bob).json()["nav"]] == ["test-mod"]

    # Admin moves it, then hides it; non-admins cannot.
    assert client.post("/api/mods/test-mod/placement", headers=bob, json={"section": "build"}).status_code == 403
    r = client.post("/api/mods/test-mod/placement", headers=alice, json={"section": "build", "hidden": False})
    assert r.status_code == 200 and r.json()["mod"]["placement"] == {"section": "build", "hidden": False}
    n = client.get("/api/mods/ui", headers=alice).json()["nav"][0]
    assert (n["mod"], n["section"], n["after"]) == ("test-mod", "build", None)
    client.post("/api/mods/test-mod/placement", headers=alice, json={"section": "build", "hidden": True})
    assert [n["mod"] for n in client.get("/api/mods/ui", headers=alice).json()["nav"]] == ["admin-only"]
    assert client.post("/api/mods/test-mod/placement", headers=alice,
                       json={"section": "kitchen"}).status_code == 400
    groups = client.get("/api/mods", headers=alice).json()["nav_groups"]
    assert [g["id"] for g in groups] == placement.group_ids()


# ── The docs list the groups this core has ──────────────────────────────

def test_docs_table_matches_the_sidebar():
    with open(os.path.join(ROOT, "docs", "mods.md"), encoding="utf-8") as f:
        doc = f.read()
    for g in tool_pages.GROUPS:
        row = re.search(rf"^\| `{re.escape(g['id'])}` \| {re.escape(g['label'])} \| (.*) \|$", doc, re.M)
        assert row, f"docs/mods.md has no row for {g['id']}"
        assert re.findall(r"`([a-z0-9-]+)`", row.group(1)) == placement.page_keys(g["id"])
