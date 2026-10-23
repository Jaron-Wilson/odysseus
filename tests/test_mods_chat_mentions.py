"""@-mentions of mods in chats (src/mods/chat_mentions.py, manifest `chat`,
permission `chat.mention`): the picker's data, tool forcing, several mods in one
message, the note a mod adds, and the permission gates."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.mods import chat_mentions as cm, manifest as mf, routes
from tests.mods_helpers import add_mod, mark_running, mods_dir, write_tools_cache  # noqa: F401

CHAT = {"mention": {"label": "Trove", "description": "Your collection", "icon": "box"},
        "mention_items": "/mod/chat/items", "mention_context": "/mod/chat/context"}
TOOLS = [{"name": "find_item", "description": "Find an item", "parameters": {"type": "object"}, "keywords": [],
          "controls_devices": False, "read_only": True},
         {"name": "add_item", "description": "Add an item", "parameters": {"type": "object"}, "keywords": [],
          "controls_devices": False, "read_only": False}]


class _Users:
    """alice is an admin, bob is not (admins-only mods are hidden from him)."""
    users = {"alice": {}, "bob": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"


@pytest.fixture
def mods(mods_dir, monkeypatch):
    from src.mods import access
    monkeypatch.setattr(access, "_auth_manager", _Users())
    monkeypatch.setenv("AUTH_ENABLED", "true")
    add_mod({"chat": CHAT, "tools": {"keywords": ["trove"]}}, mod_id="trove", permissions=["chat.mention"],
            access="users")
    write_tools_cache("trove", TOOLS)
    add_mod({"chat": {"mention": {"label": "Company"}}, "tools": {"keywords": ["company"]}}, mod_id="company",
            permissions=["chat.mention"], access="admins")
    write_tools_cache("company", [dict(TOOLS[0], name="company_overview")])
    return mods_dir


# ── the manifest ─────────────────────────────────────────────────────────

def _raw(**kw):
    raw = {"id": "demo", "name": "Demo", "version": "1.0.0",
           "runtime": {"type": "process", "command": ["python", "-m", "x"]}, "permissions": ["chat.mention"]}
    raw.update(kw)
    return raw


def test_manifest_chat_section_is_cleaned():
    m = mf.validate(_raw(chat={"mention": {"label": "My Stuff!", "description": "d" * 400},
                               "mention_items": "/i", "mention_context": "/c"}))
    assert m["chat"]["mention"]["handle"] == "my-stuff" and len(m["chat"]["mention"]["description"]) == 160
    assert m["chat"]["mention_items"] == "/i" and m["chat"]["mention_context"] == "/c"
    assert mf.validate(_raw())["chat"] is None


def test_manifest_chat_needs_the_permission_and_a_sane_handle():
    with pytest.raises(mf.ManifestError, match="chat.mention permission"):
        mf.validate(_raw(permissions=[], chat={"mention": {"label": "X"}}))
    with pytest.raises(mf.ManifestError, match="handle"):
        mf.validate(_raw(chat={"mention": {"label": "X", "handle": "Bad Handle"}}))
    with pytest.raises(mf.ManifestError, match="mention_items"):
        mf.validate(_raw(chat={"mention": {"label": "X"}, "mention_items": "nope"}))
    assert "chat.mention" in mf.PERMISSIONS


# ── the picker's data ────────────────────────────────────────────────────

def test_the_picker_lists_the_mods_a_user_may_use(mods):
    rows = {r["id"]: r for r in cm.catalog("alice")}
    assert set(rows) == {"trove", "company"}
    assert rows["trove"] == {"id": "trove", "handle": "trove", "label": "Trove", "description": "Your collection",
                             "icon": "box", "name": "Test Mod", "items": True, "context": True}
    assert rows["company"]["items"] is False
    assert [r["id"] for r in cm.catalog("bob")] == ["trove"]          # the admins-only mod is hidden


def test_a_mod_without_the_granted_permission_or_turned_off_is_not_listed(mods):
    from src.mods import registry
    with registry._lock:
        data = registry._load()
        data["mods"]["trove"]["granted_permissions"] = []
        data["mods"]["company"]["enabled"] = False
        registry._save(data)
    assert cm.catalog("alice") == []
    assert cm.parse("hi @trove and @company", "alice") == []


def test_the_picker_routes(mods, monkeypatch):
    from src.mods import proxy
    app = FastAPI()
    app.include_router(routes.setup_mods_routes(_Users()))
    monkeypatch.setattr(proxy, "cookie_user", lambda request: request.headers.get("x-test-user"))
    with TestClient(app) as c:
        assert c.get("/api/mods/chat/mentions").status_code == 401
        r = c.get("/api/mods/chat/mentions", headers={"x-test-user": "bob"})
        assert [m["id"] for m in r.json()["mods"]] == ["trove"]
        # Items come from the mod's endpoint, as the signed-in user.
        srv = _ModServer()
        mark_running("trove", srv.port)
        try:
            r = c.get("/api/mods/chat/mentions/trove/items?q=lam", headers={"x-test-user": "bob"})
            assert r.status_code == 200
            assert r.json()["items"] == [{"ref": "lamp-1", "label": "Brass lamp", "help": "shelf 2"}]
            assert srv.calls[-1][:2] == ("GET", "/mod/chat/items?q=lam") and srv.calls[-1][2] == "bob"
            # Not for a mod this user may not use, or one without an items endpoint.
            assert c.get("/api/mods/chat/mentions/company/items", headers={"x-test-user": "bob"}).status_code == 404
            assert c.get("/api/mods/chat/mentions/company/items", headers={"x-test-user": "alice"}).status_code == 404
        finally:
            srv.srv.shutdown()


# ── parsing, tool forcing, several mods ──────────────────────────────────

def test_parse_finds_mods_items_and_ignores_other_ats(mods):
    got = cm.parse("ask @Trove/lamp-1 and @trove about it, mail me@trove.com, @nobody, @company", "alice")
    assert [(r["mod_id"], r["item"]) for r in got] == [("trove", "lamp-1"), ("trove", None), ("company", None)]
    assert cm.parse("no mention here", "alice") == []
    assert [r["mod_id"] for r in cm.parse("@trove @company", "bob")] == ["trove"]


def test_a_mention_forces_all_of_that_mods_tools_in_order(mods):
    names, labels = cm.mentioned_tools("what is in @trove?", "alice")
    assert names == ["mod__trove__find_item", "mod__trove__add_item"] and labels == ["Trove"]
    names, labels = cm.mentioned_tools("@company then @trove", "alice")
    assert names == ["mod__company__company_overview", "mod__trove__find_item", "mod__trove__add_item"]
    assert labels == ["Company", "Trove"]
    assert cm.mentioned_tools("trove without the at", "alice") == ([], [])
    assert "@Trove" in cm.system_note(["Trove"]) and "use its tools" in cm.system_note(["Trove"])


def test_at_most_five_mods_in_one_message(mods):
    for i in range(6):
        add_mod({"chat": {"mention": {"label": f"M{i}"}}}, mod_id=f"m{i}", permissions=["chat.mention"],
                access="users")
    got = cm.parse(" ".join(f"@m{i}" for i in range(6)), "alice")
    assert len({r["mod_id"] for r in got}) == cm.MAX_MENTIONS


def test_the_agent_loop_forces_mentioned_tools_first():
    import inspect
    from src import agent_loop
    src = inspect.getsource(agent_loop)
    assert "_cm.mentioned_tools(_last_user, owner)" in src and "_cm.system_note(_mention_labels)" in src
    assert "_mod_forced = _m_names" in src            # preferred: they head the priority list


# ── the note a mod adds ──────────────────────────────────────────────────

class _ModServer:
    def __init__(self, context="Lamp 1 is brass, on shelf 2."):
        outer = self
        self.calls = []
        self.context = context

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                outer.calls.append(("GET", self.path, self.headers.get("X-Odysseus-User")))
                self._send({"items": [{"ref": "lamp-1", "label": "Brass lamp", "help": "shelf 2"},
                                      {"ref": "bad ref!", "label": "dropped"}]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append(("POST", self.path, self.headers.get("X-Odysseus-User"), body))
                self._send({"context": outer.context})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


def test_the_mods_note_is_added_to_the_message_and_capped(mods):
    srv = _ModServer(context="x" * 5000)
    mark_running("trove", srv.port)
    try:
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "what about @trove/lamp-1?"}]
        n = asyncio.run(cm.apply_context(msgs, msgs[-1]["content"], "alice"))
        assert n == 1 and msgs[0]["content"] == "s"
        body = msgs[-1]["content"]
        assert body.startswith("what about @trove/lamp-1?") and "[Trove, for this message]" in body
        assert len(body) < 2300                                         # CONTEXT_PER_MOD
        assert srv.calls[-1][0] == "POST" and srv.calls[-1][1] == "/mod/chat/context"
        assert srv.calls[-1][3]["items"] == ["lamp-1"] and srv.calls[-1][3]["handle"] == "trove"
        # No mention, no call.
        before = len(srv.calls)
        assert asyncio.run(cm.apply_context([{"role": "user", "content": "hi"}], "hi", "alice")) == 0
        assert len(srv.calls) == before
    finally:
        srv.srv.shutdown()


def test_a_mod_that_is_down_or_not_allowed_adds_nothing(mods):
    # Not running and cannot start: the turn goes on without a note.
    assert asyncio.run(cm.context_block("@trove", "alice")) in ("",)
    # A mod the user may not use is not even asked.
    srv = _ModServer()
    mark_running("company", srv.port)
    try:
        assert asyncio.run(cm.context_block("@company", "bob")) == "" and srv.calls == []
    finally:
        srv.srv.shutdown()


def test_the_composer_picker_and_chips_are_wired():
    import os
    from tests.mods_helpers import ROOT
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "static", "js", "modMentions.js"), encoding="utf-8").read()
    assert "/static/js/modMentions.js" in html
    for needle in ("/api/mods/chat/mentions", "/items?q=", "mod-mention-chip", "stopImmediatePropagation"):
        assert needle in js, needle
