"""Mod tutorials (src/mods/tutorial.py): the manifest field, its Markdown,
first-open and replay state per user, the image route, the "no tutorial"
warning, the catalog's Verified rule and the index validator. Also the tool
list refresh call (POST /api/mods/v1/tools/refresh)."""
import importlib.util
import json
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import catalog, manifest as mf, paths, registry, routes, tools, tutorial
from tests.mods_helpers import ROOT, add_mod, mods_dir  # noqa: F401

BASE = {"id": "garage-door", "name": "Garage Door", "version": "1.0.0",
        "runtime": {"type": "process", "command": ["python", "-m", "x"]}}
STEPS = [{"title": "Open it", "body": "Press **Open**."},
         {"title": "Settings", "body": "Pick the relay.", "action": {"label": "Open settings", "settings": True}},
         {"title": "Ask", "body": "Say *is the garage open?*", "action": {"label": "History", "path": "/history"}}]


def man(**kw):
    raw = dict(BASE, ui={"tabs": [{"id": "main", "path": "/"}], "settings_path": "/settings"})
    raw.update(kw)
    return mf.validate({k: v for k, v in raw.items() if v is not None})


# ── The manifest field ──────────────────────────────────────────────────

def test_steps_are_normalized():
    m = man(tutorial={"version": 2, "steps": STEPS})
    t = m["tutorial"]
    assert t["version"] == 2 and t["path"] is None and len(t["steps"]) == 3
    assert t["steps"][1]["action"] == {"label": "Open settings", "path": None, "settings": True}
    assert t["steps"][2]["action"]["path"] == "/history"
    assert not any("tutorial" in w for w in m["warnings"])


def test_a_page_tutorial_and_the_default_version():
    t = man(tutorial={"path": "/tutorial"})["tutorial"]
    assert t == {"version": 1, "steps": [], "path": "/tutorial"}


def test_missing_tutorial_is_a_warning_not_an_error():
    m = man()
    assert m["tutorial"] is None
    assert any("no `tutorial`" in w and "Verified" in w for w in m["warnings"])


@pytest.mark.parametrize("tut, words", [
    ("yes", "must be an object"),
    ({}, "needs `steps`"),
    ({"steps": []}, "non-empty list"),
    ({"steps": [{"title": "x", "body": "y"}] * 9}, "at most 8"),
    ({"version": 0, "steps": STEPS[:1]}, "tutorial.version"),
    ({"version": True, "steps": STEPS[:1]}, "tutorial.version"),
    ({"steps": [{"title": "", "body": "y"}]}, "title"),
    ({"steps": [{"title": "x"}]}, "body"),
    ({"steps": [{"title": "x", "body": "y" * 1201}]}, "body"),
    ({"steps": [{"title": "x", "body": "y", "image": "../secret.png"}]}, "image"),
    ({"steps": [{"title": "x", "body": "y", "image": "/etc/a.png"}]}, "image"),
    ({"steps": [{"title": "x", "body": "y", "image": "docs/a.svg"}]}, "image"),
    ({"steps": [{"title": "x", "body": "y", "action": {"label": "Go"}}]}, "exactly one"),
    ({"steps": [{"title": "x", "body": "y", "action": {"label": "Go", "path": "/a", "settings": True}}]}, "exactly one"),
    ({"steps": [{"title": "x", "body": "y", "action": {"label": "Go", "path": "https://evil.example/"}}]}, "path"),
    ({"steps": [{"title": "x", "body": "y", "action": {"path": "/a"}}]}, "label"),
    ({"path": "javascript:alert(1)"}, "tutorial.path"),
])
def test_bad_tutorials_are_errors(tut, words):
    with pytest.raises(mf.ManifestError) as e:
        man(tutorial=tut)
    assert any(words in x for x in e.value.errors), e.value.errors


def test_settings_action_needs_a_settings_page():
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(dict(BASE, tutorial={"steps": [STEPS[1]]}))
    assert any("settings_path" in x for x in e.value.errors)


def test_missing_image_files_are_warned_about_at_load(tmp_path):
    raw = dict(BASE, tutorial={"steps": [{"title": "x", "body": "y", "image": "docs/a.png"}]})
    (tmp_path / mf.MANIFEST_FILE).write_text(json.dumps(raw))
    assert any("docs/a.png" in w for w in mf.load(str(tmp_path))["warnings"])
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert not any("docs/a.png" in w for w in mf.load(str(tmp_path))["warnings"])


def test_the_cli_validate_prints_the_warning(tmp_path, capsys):
    from src.mods.__main__ import main
    (tmp_path / mf.MANIFEST_FILE).write_text(json.dumps(BASE))
    assert main(["validate", str(tmp_path)]) == 0
    assert "warning: no `tutorial`" in capsys.readouterr().err


def test_shipped_template_and_example_have_tutorials():
    for d in ("templates/mod-template", "examples/hello-mod"):
        m = mf.load(os.path.join(ROOT, d))
        assert m["tutorial"] and m["tutorial"]["steps"], d
        assert not any("tutorial" in w for w in m["warnings"]), (d, m["warnings"])
    hello = mf.load(os.path.join(ROOT, "examples/hello-mod"))
    assert len(hello["tutorial"]["steps"]) == 3


def test_a_scaffolded_mod_has_a_tutorial(tmp_path):
    from src.mods import scaffold
    out = scaffold.create(str(tmp_path / "garage"), "garage-door", "Garage Door")
    m = mf.load(out["folder"])
    assert m["tutorial"]["steps"] and "Garage Door" in m["tutorial"]["steps"][0]["title"]


# ── Markdown ─────────────────────────────────────────────────────────────

def test_markdown_subset():
    h = tutorial.render_markdown("Press **Open** and *wait*.\nThen `go`.\n\n- one\n- two\n\n1. a\n2. b")
    assert h == ("<p>Press <strong>Open</strong> and <em>wait</em>.<br>Then <code>go</code>.</p>"
                 "<ul><li>one</li><li>two</li></ul><ol><li>a</li><li>b</li></ol>")


@pytest.mark.parametrize("src, bad", [
    ("<script>alert(1)</script>", "<script"),
    ("<img src=x onerror=alert(1)>", "<img"),
    ("[x](javascript:alert(1))", "javascript:"),
    ("[x](http://plain.example)", "href"),
    ('[x](https://a.example/"onmouseover="alert(1))', 'onmouseover="'),
    ("`<b>` **<i>**", "<b>"),
    ("![pic](https://a.example/p.png)", "<img"),
])
def test_markdown_never_lets_html_or_bad_links_through(src, bad):
    assert bad not in tutorial.render_markdown(src)


def test_markdown_links():
    h = tutorial.render_markdown("[docs](https://example.com/a?b=1&c=2) and [jobs](#mod/print-hub/printer?path=/jobs)")
    assert '<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">docs</a>' in h
    assert 'data-mod-link="#mod/print-hub/printer?path=/jobs"' in h


# ── First open, replay, version bump (the routes) ───────────────────────

class FakeAuth:
    users = {"alice": {}, "bob": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"


class As(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        u = request.headers.get("x-test-user")
        if u:
            request.state.current_user = u
            request.state.api_token = False
        return await call_next(request)


@pytest.fixture
def client(mods_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(As)
    routes._calls.clear()
    return TestClient(app)


ALICE, BOB = {"x-test-user": "alice"}, {"x-test-user": "bob"}
UI = {"tabs": [{"id": "main", "title": "Garage", "path": "/"}], "settings_path": "/settings"}


def _ui_tut(client, who=ALICE):
    mods = client.get("/api/mods/ui", headers=who).json()["mods"]
    return mods[0]["tutorial"] if mods else "hidden"


def test_first_open_then_seen_per_user(client):
    add_mod({"ui": UI, "tutorial": {"steps": STEPS}}, access="users")
    assert _ui_tut(client) == {"version": 1, "steps": 3, "has_page": False, "seen": False}
    assert _ui_tut(client, BOB)["seen"] is False
    d = client.get("/api/mods/test-mod/tutorial", headers=ALICE).json()["tutorial"]
    assert [s["title"] for s in d["steps"]] == ["Open it", "Settings", "Ask"]
    assert d["steps"][0]["body_html"] == "<p>Press <strong>Open</strong>.</p>"
    assert d["steps"][1]["action"] == {"label": "Open settings", "path": None, "settings": True}
    assert d["seen"] is False and d["url"] is None
    # Shown once (finished or skipped): seen for alice only.
    assert client.post("/api/mods/test-mod/tutorial/seen", headers=ALICE).json() == {"ok": True, "version": 1}
    assert _ui_tut(client)["seen"] is True and _ui_tut(client, BOB)["seen"] is False
    # Replay is always possible.
    assert client.get("/api/mods/test-mod/tutorial", headers=ALICE).json()["tutorial"]["seen"] is True


def test_a_new_tutorial_version_shows_it_again_and_updates_keep_the_state(client):
    add_mod({"ui": UI, "tutorial": {"steps": STEPS}})
    client.post("/api/mods/test-mod/tutorial/seen", headers=ALICE)
    e = registry.get("test-mod")
    registry._update("test-mod", manifest=dict(e["manifest"], tutorial=dict(e["manifest"]["tutorial"], version=1)))
    assert _ui_tut(client)["seen"] is True
    registry._update("test-mod", manifest=dict(e["manifest"], tutorial=dict(e["manifest"]["tutorial"], version=2)))
    assert _ui_tut(client)["seen"] is False
    client.post("/api/mods/test-mod/tutorial/seen", headers=ALICE)
    assert registry.get("test-mod")["tutorial_seen"] == {"alice": 2}


def test_a_page_tutorial_gets_a_frame_url(client):
    add_mod({"ui": UI, "tutorial": {"path": "/tutorial"}})
    d = client.get("/api/mods/test-mod/tutorial", headers=ALICE).json()["tutorial"]
    assert d["steps"] == [] and d["url"].startswith("/mods/test-mod/~") and d["url"].endswith("/tutorial")


def test_tutorial_routes_respect_who_may_use_the_mod(client):
    add_mod({"ui": UI, "tutorial": {"steps": STEPS}})   # admins only
    assert client.get("/api/mods/test-mod/tutorial", headers=BOB).status_code == 404
    assert client.post("/api/mods/test-mod/tutorial/seen", headers=BOB).status_code == 404
    assert client.get("/api/mods/test-mod/tutorial").status_code == 401
    registry.set_enabled("test-mod", False)
    assert client.get("/api/mods/test-mod/tutorial", headers=ALICE).status_code == 404


def test_no_tutorial_answers_404_and_ui_says_none(client):
    add_mod({"ui": UI})
    assert _ui_tut(client) is None
    assert client.get("/api/mods/test-mod/tutorial", headers=ALICE).status_code == 404
    assert client.post("/api/mods/test-mod/tutorial/seen", headers=ALICE).status_code == 404


def test_step_images_are_served_from_the_mod_folder_only(client):
    steps = [{"title": "a", "body": "b", "image": "docs/a.png"}, {"title": "c", "body": "d", "image": "docs/b.png"},
             {"title": "e", "body": "f"}]
    add_mod({"ui": UI, "tutorial": {"steps": steps}})
    src = paths.src_dir("test-mod")
    os.makedirs(os.path.join(src, "docs"), exist_ok=True)
    with open(os.path.join(src, "docs", "a.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\nfake")
    # docs/b.png is a link out of the mod folder: refused.
    secret = os.path.join(os.path.dirname(src), "secret.png")
    with open(secret, "wb") as f:
        f.write(b"secret")
    os.symlink(secret, os.path.join(src, "docs", "b.png"))
    r = client.get("/api/mods/test-mod/tutorial/image/0", headers=ALICE)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.endswith(b"fake")
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/mods/test-mod/tutorial/image/1", headers=ALICE).status_code == 404
    assert client.get("/api/mods/test-mod/tutorial/image/2", headers=ALICE).status_code == 404
    assert client.get("/api/mods/test-mod/tutorial/image/9", headers=ALICE).status_code == 404
    assert client.get("/api/mods/test-mod/tutorial/image/0", headers=BOB).status_code == 404
    d = client.get("/api/mods/test-mod/tutorial", headers=ALICE).json()["tutorial"]
    assert d["steps"][0]["image_url"] == "/api/mods/test-mod/tutorial/image/0" and d["steps"][2]["image_url"] is None


def test_install_review_shows_the_tutorial_and_the_warning(client, tmp_path):
    hello = os.path.join(ROOT, "examples", "hello-mod")
    d = client.post("/api/mods/install/prepare", headers=ALICE, json={"source": hello}).json()
    assert d["tutorial"] == {"version": 1, "steps": 3, "has_page": False}
    assert not any("tutorial" in w for w in d["warnings"])
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / mf.MANIFEST_FILE).write_text(json.dumps(dict(BASE, id="bare-mod")))
    d = client.post("/api/mods/install/prepare", headers=ALICE, json={"source": str(bare)}).json()
    assert d["tutorial"] is None and any("no `tutorial`" in w for w in d["warnings"])


# ── Tool list refresh ────────────────────────────────────────────────────

def test_tools_refresh_waits_or_queues(client, monkeypatch):
    calls = []

    async def fake_refresh(mod_id):
        calls.append(mod_id)
        return [{"name": "door_open"}]

    monkeypatch.setattr(tools, "refresh", fake_refresh)
    _, tok = add_mod({"tools": {"keywords": ["garage"]}})
    h = {"Authorization": f"Bearer {tok}"}
    r = client.post("/api/mods/v1/tools/refresh", headers=h, json={"wait": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "tools": ["door_open"]}
    r = client.post("/api/mods/v1/tools/refresh", headers=h)
    assert r.status_code == 200 and r.json() == {"ok": True, "queued": True}
    assert calls[0] == "test-mod"
    # Rate limited: 6 a minute.
    codes = [client.post("/api/mods/v1/tools/refresh", headers=h, json={"wait": True}).status_code for _ in range(6)]
    assert codes[-1] == 429 and codes.count(200) == 4


def test_tools_refresh_needs_a_token_and_tools(client):
    assert client.post("/api/mods/v1/tools/refresh").status_code == 401
    _, tok = add_mod()
    r = client.post("/api/mods/v1/tools/refresh", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400 and "no `tools`" in r.json()["detail"]


def test_tools_refresh_rereads_the_running_mods_list(client, monkeypatch):
    """Through the real tools.refresh: the cached list changes and the
    selection generation moves, so the next message sees the new tools."""
    import asyncio

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    served = {"tools": []}

    class Client:
        async def get(self, url, headers=None, timeout=None):
            return Resp(served)

    from src.mods import runtime
    from tests.mods_helpers import mark_running
    monkeypatch.setattr(runtime, "_client", lambda: Client())
    _, tok = add_mod({"tools": {"keywords": ["garage"]}})
    mark_running("test-mod", 1)
    asyncio.run(tools.refresh("test-mod"))
    assert tools.cached("test-mod") == []
    served["tools"] = [{"type": "function", "function": {"name": "door_open", "description": "Open it"}}]
    r = client.post("/api/mods/v1/tools/refresh", headers={"Authorization": f"Bearer {tok}"}, json={"wait": True})
    assert r.json()["tools"] == ["door_open"] and [t["name"] for t in tools.cached("test-mod")] == ["door_open"]


# ── The catalog: no tutorial, no Verified ───────────────────────────────

def test_verified_needs_a_tutorial():
    with_tut = catalog.summarize(dict(BASE, tutorial={"steps": STEPS[:1]}))
    without = catalog.summarize(dict(BASE))
    assert with_tut["has_tutorial"] and with_tut["tutorial_steps"] == 1 and not without["has_tutorial"]
    assert catalog.verified_status(True, with_tut) == (True, "")
    ok, why = catalog.verified_status(True, without)
    assert not ok and "no tutorial" in why
    assert catalog.verified_status(False, without) == (False, "")
    assert catalog.verified_status(True, catalog.summarize(None))[0] is False


def test_public_entry_drops_verified_with_a_reason():
    entry = {"full_name": "a/b", "owner": "a", "repo": "b", "verified": True,
             "index": {"verified": True, "notes": "Reviewed"}, "manifest": catalog.summarize(dict(BASE))}
    p = catalog._public_entry(entry, {}, {})
    assert p["verified"] is False and p["badge"] == "community" and "no tutorial" in p["verified_dropped"]
    assert p["has_tutorial"] is False
    entry["manifest"] = catalog.summarize(dict(BASE, tutorial={"path": "/t"}))
    p = catalog._public_entry(entry, {}, {})
    assert p["verified"] is True and p["badge"] == "verified" and p["verified_dropped"] == ""


# ── The index validator (templates/mods-index) ─────────────────────────

def _validator():
    spec = importlib.util.spec_from_file_location(
        "validate_index", os.path.join(ROOT, "templates", "mods-index", "scripts", "validate_index.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_index_validator_requires_a_tutorial_for_verified_entries(monkeypatch):
    v = _validator()
    manifests = {
        "a/with": dict(BASE, id="with-tut", tutorial={"steps": STEPS[:1]}),
        "a/without": dict(BASE, id="without"),
        "a/community": dict(BASE, id="community"),
        "a/broken": dict(BASE, id="broken", tutorial={"steps": [{"title": "x"}]}),
    }

    def fake_get(url, token):
        repo = "/".join(url.split("/repos/")[1].split("/")[:2])
        return json.dumps(manifests[repo]).encode()

    monkeypatch.setattr(v, "_get", fake_get)
    data = {"schema_version": 1, "mods": [
        {"repo": "https://github.com/a/with", "id": "with-tut"},
        {"repo": "https://github.com/a/without", "id": "without", "verified": True},
        {"repo": "https://github.com/a/community", "id": "community", "verified": False},
        {"repo": "https://github.com/a/broken", "id": "broken"},
    ]}
    assert v.check(data) == []
    problems = v.check_remote(data, "")
    assert len(problems) == 2
    assert problems[0].startswith("mods[1]") and "no `tutorial`" in problems[0]
    assert problems[1].startswith("mods[3]") and "title" in problems[1]
