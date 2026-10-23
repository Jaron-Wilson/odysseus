"""Links into mods and old page keys (static/js/modLinksCore.js, modLinks.js,
workspace/shell.js; src/mods/placement.py): #mod/<id>/<tab> deep links,
`ui.tabs[].replaces`, saved Workspace tabs mapped to the mod that replaces a
built-in page, and the "This feature is now a mod" fallback.

The JS rules run under node (skipped without it), the same way as
tests/test_match_model_key_js.py."""
import json
import os
import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import manifest as mf, placement, routes
from tests.mods_helpers import ROOT, add_mod, mods_dir  # noqa: F401

CORE = os.path.join(ROOT, "static", "js", "modLinksCore.js")
HAS_NODE = shutil.which("node") is not None
needs_node = pytest.mark.skipif(not HAS_NODE, reason="node binary not on PATH")


def js(expr):
    """Evaluate `expr` with modLinksCore's exports in scope; returns its JSON."""
    src = f"import * as L from '{CORE}'; const r = (() => {{ {expr} }})(); console.log(JSON.stringify(r === undefined ? null : r));"
    p = subprocess.run(["node", "--input-type=module"], input=src, capture_output=True, text=True, cwd=ROOT, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip())


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# ── The legacy page list is the same on both sides ──────────────────────

def test_python_and_js_legacy_lists_match():
    text = _read("static", "js", "modLinksCore.js")
    block = re.search(r"export const LEGACY_PAGES = \{(.*?)\};", text, re.S).group(1)
    pairs = {k: a or b for k, a, b in re.findall(r"'([a-z-]+)':\s*(?:'([^']*)'|\"([^\"]*)\")", block)}
    assert pairs == placement.LEGACY_PAGES
    for key in ("adsb", "games", "print", "devops", "company"):
        assert key in placement.LEGACY_PAGES


def test_legacy_keys_are_built_in_page_keys_today():
    """Each key is (or was) a sidebar page and a Workspace tab key, so a
    saved tab or a #<key> link with it is what the mapping meets. A page that
    already left the core is in toolPages.js's `moved` list instead, and has
    no built-in Workspace tab any more."""
    from src import tool_pages
    keys = {p["key"] for p in tool_pages.PAGES}
    moved = {p["key"] for p in tool_pages.MOVED}
    assert moved == {"print", "adsb", "terminal", "claude-sessions", "devops", "games", "phone", "company"}
    assert not keys & moved
    shell = _read("static", "js", "workspace", "shell.js")
    for key in placement.LEGACY_PAGES:
        if key in moved:
            assert f"key: '{key}'" not in shell, key
            continue
        assert key in keys, key
        assert f"key: '{key}'" in shell, key


# ── Deep links ───────────────────────────────────────────────────────────

@needs_node
def test_parse_hash():
    got = js("""return [
      L.parseHash('#mod/print-hub/printer'),
      L.parseHash('#mod/print-hub/printer?path=%2Fjobs%2F42%3Fx%3D1'),
      L.parseHash('#mod/print-hub'),
      L.parseHash('#adsb'), L.parseHash('#games?play=elden'),
      L.parseHash('#chat-123'), L.parseHash(''), L.parseHash('#mod/'),
      L.parseHash('#mod/Print/printer'), L.parseHash('#mod/print-hub/printer/x'),
      L.parseHash('#mod/print-hub/printer?path=https://evil.example/'),
      L.parseHash('#mod/print-hub/printer?path=/a/../../api/x'),
      L.parseHash('#mod/print-hub/printer?path=//evil.example/x'),
      L.parseHash('#mod/print-hub/printer?path=javascript:alert(1)'),
      L.parseHash('#toString'), L.parseHash('#__proto__'),
    ];""")
    more = js("""return [
      L.parseHash('#claude-sessions?project=-home-jaron-x&id=7238cfa3-1111-4222-8333-444455556666'),
      L.parseHash('#claude-sessions/-home-jaron-x/7238cfa3'),
      L.parseHash('#games?play=<script>'),
      L.parseHash('#nope?x=1'),
    ];""")
    assert more[0] == {"kind": "legacy", "key": "claude-sessions",
                       "query": "project=-home-jaron-x&id=7238cfa3-1111-4222-8333-444455556666"}
    # A link from before the page moved opens the tab; a query it can't
    # pass on safely is dropped.
    assert more[1] == {"kind": "legacy", "key": "claude-sessions"}
    assert more[2] == {"kind": "legacy", "key": "games"} and more[3] is None
    assert got[0] == {"kind": "mod", "mod": "print-hub", "tab": "printer", "path": None}
    assert got[1] == {"kind": "mod", "mod": "print-hub", "tab": "printer", "path": "/jobs/42?x=1"}
    assert got[2] == {"kind": "mod", "mod": "print-hub", "tab": None, "path": None}
    assert got[3] == {"kind": "legacy", "key": "adsb"}
    assert got[4] == {"kind": "legacy", "key": "games", "query": "play=elden"}
    assert got[5:] == [None] * (len(got) - 5)


@needs_node
def test_safe_sub_path_and_mod_hash():
    got = js("""return [L.safeSubPath('/a/b?c=1'), L.safeSubPath('a'), L.safeSubPath('/a/./b'),
      L.safeSubPath('/a\\\\b'), L.safeSubPath('/a b'), L.safeSubPath('/x'.repeat(300)),
      L.modHash('print-hub', 'printer', '/jobs/42'), L.modHash('print-hub', 'printer'),
      L.parseHash(L.modHash('print-hub', 'printer', '/jobs/42?y=2'))];""")
    assert got[0] == "/a/b?c=1" and got[1:6] == [None] * 5
    assert got[6] == "#mod/print-hub/printer?path=%2Fjobs%2F42" and got[7] == "#mod/print-hub/printer"
    assert got[8]["path"] == "/jobs/42?y=2"


# ── Old page keys: mod, built-in or the fallback card ───────────────────

@needs_node
def test_resolve_legacy():
    got = js("""const rep = {adsb: {mod: 'odysseus-adsb', tab: 'receiver'}};
      return [L.resolveLegacy('adsb', rep, true), L.resolveLegacy('games', rep, true),
              L.resolveLegacy('games', rep, false), L.resolveLegacy('adsb', {}, false),
              L.resolveLegacy('toString', {}, false).to];""")
    assert got[0] == {"to": "mod", "mod": "odysseus-adsb", "tab": "receiver"}, "an installed mod wins over the built-in"
    assert got[1] == {"to": "builtin"}
    assert got[2] == {"to": "fallback", "key": "games", "label": "Games"}
    assert got[3] == {"to": "fallback", "key": "adsb", "label": "ADS-B receiver"}
    assert got[4] == "fallback"


@needs_node
def test_saved_workspace_tabs_are_mapped():
    got = js("""
      const rep = {adsb: {mod: 'odysseus-adsb', tab: 'receiver'}};
      const builtins = new Set(['email', 'games', 'settings']);
      const b = (k) => builtins.has(k);
      const t = (key, extra) => ({id: 'tool:' + key, kind: 'tool', key, ...extra});
      return {
        adsbToMod: L.mapSavedTab(t('adsb'), rep, b),
        gamesStays: L.mapSavedTab(t('games'), rep, b),
        printMoved: L.mapSavedTab(t('print'), rep, b),
        movedToMod: L.mapSavedTab(t('moved:adsb'), rep, b),
        movedStays: L.mapSavedTab(t('moved:print'), rep, b),
        modGoneBack: L.mapSavedTab(t('mod:odysseus-adsb:receiver', {legacy: 'adsb'}), {}, b, () => false),
        modKept: L.mapSavedTab(t('mod:odysseus-adsb:receiver', {legacy: 'adsb'}), rep, b, () => true),
        modUnknownYet: L.mapSavedTab(t('mod:hello-mod:main'), {}, b),
        modGone: L.mapSavedTab(t('mod:hello-mod:main'), {}, b, () => false),
        email: L.mapSavedTab(t('email'), rep, b),
        gone: L.mapSavedTab(t('nonsense'), rep, b),
        chat: L.mapSavedTab({id: 'c1', kind: 'chat', sid: 'x'}, rep, b),
      };""")
    assert got["adsbToMod"] == {"id": "tool:mod:odysseus-adsb:receiver", "kind": "tool",
                                "key": "mod:odysseus-adsb:receiver", "legacy": "adsb"}
    assert got["gamesStays"] == {"id": "tool:games", "kind": "tool", "key": "games"}
    assert got["printMoved"] == {"id": "tool:moved:print", "kind": "tool", "key": "moved:print", "legacy": "print"}
    assert got["movedToMod"]["key"] == "mod:odysseus-adsb:receiver" and got["movedToMod"]["legacy"] == "adsb"
    assert got["movedStays"]["key"] == "moved:print"
    assert got["modGoneBack"]["key"] == "moved:adsb", "never a blank tab when the replacing mod is removed"
    assert got["modKept"]["key"] == "mod:odysseus-adsb:receiver"
    assert got["modUnknownYet"]["key"] == "mod:hello-mod:main" and got["modGone"] is None
    assert got["email"]["key"] == "email" and got["gone"] is None and got["chat"]["kind"] == "chat"


@needs_node
def test_needs_tutorial():
    assert js("""return [L.needsTutorial({seen: false}, false), L.needsTutorial({seen: true}, false),
      L.needsTutorial({seen: false}, true), L.needsTutorial(null, false)];""") == [True, False, False, False]


# ── The manifest and the API ────────────────────────────────────────────

RUNTIME = {"type": "process", "command": ["python", "-m", "x"]}
TUT = {"steps": [{"title": "a", "body": "b"}]}


def _m(tabs):
    return mf.validate({"id": "odysseus-adsb", "name": "ADS-B", "version": "1.0.0", "runtime": RUNTIME,
                        "ui": {"tabs": tabs}, "tutorial": TUT})


def test_replaces_in_the_manifest():
    m = _m([{"id": "receiver", "path": "/", "section": "system", "replaces": "adsb"}])
    assert m["ui"]["tabs"][0]["replaces"] == "adsb" and m["warnings"] == []
    m = _m([{"id": "receiver", "path": "/", "replaces": "settings"}])
    assert m["ui"]["tabs"][0]["replaces"] is None and any("replaces" in w for w in m["warnings"])
    with pytest.raises(mf.ManifestError, match="replaces"):
        _m([{"id": "a", "path": "/", "replaces": 3}])
    with pytest.raises(mf.ManifestError, match="only one tab"):
        _m([{"id": "a", "path": "/", "replaces": "adsb"}, {"id": "b", "path": "/b", "replaces": "adsb"}])


def test_replacements_first_mod_by_id_wins():
    a = {"id": "b-mod", "manifest": {"ui": {"tabs": [{"id": "x", "title": "X", "replaces": "adsb"}]}}}
    b = {"id": "a-mod", "manifest": {"ui": {"tabs": [{"id": "y", "title": "Y", "replaces": "adsb"},
                                                    {"id": "z", "title": "Z", "replaces": "games"}]}}}
    assert placement.replacements([a, b]) == {"adsb": {"mod": "a-mod", "tab": "y", "title": "Y"},
                                              "games": {"mod": "a-mod", "tab": "z", "title": "Z"}}


class FakeAuth:
    users = {"alice": {}, "bob": {}}

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
    return TestClient(app)


def test_ui_list_gives_base_replaces_and_legacy_pages(client):
    add_mod({"ui": {"tabs": [{"id": "receiver", "title": "Receiver", "path": "/", "replaces": "adsb"}]},
             "tutorial": TUT}, mod_id="odysseus-adsb")
    d = client.get("/api/mods/ui", headers={"x-test-user": "alice"}).json()
    m = d["mods"][0]
    assert m["base"].startswith("/mods/odysseus-adsb/~") and m["tabs"][0]["url"] == m["base"] + "/"
    assert m["tabs"][0]["replaces"] == "adsb"
    assert d["replaces"] == {"adsb": {"mod": "odysseus-adsb", "tab": "receiver", "title": "Receiver"}}
    assert d["legacy_pages"] == placement.LEGACY_PAGES
    # bob may not use it: nothing replaces adsb for him, so his #adsb falls back.
    assert client.get("/api/mods/ui", headers={"x-test-user": "bob"}).json()["replaces"] == {}


def test_notifications_may_link_into_a_mod_tab():
    from src.mods import notify
    assert notify.clean({"title": "Done", "url": "/#mod/print-hub/printer?path=/jobs/42"})["url"].startswith("/#mod/")


# ── The front end is wired as described ─────────────────────────────────

def test_mod_links_load_before_the_built_in_pages():
    html = _read("static", "index.html")
    order = [m.group(1) for m in re.finditer(r'<script type="module" src="/static/js/([^"?]+)', html)]
    first_page = order.index("sessions.js")
    assert order.index("modLinks.js") < first_page


def test_shell_maps_saved_tabs_and_has_moved_tabs():
    shell = _read("static", "js", "workspace", "shell.js")
    assert "mapSavedTab(t, ModPanel.replacements(), _builtin)" in shell
    assert "'moved:' + k" in shell and ":not([data-moved-key])" in shell
    assert "legacy" in re.search(r"function _save\(\) \{.*?\n\}", shell, re.S).group(0)
    links = _read("static", "js", "modLinks.js")
    assert "This feature is now a mod" in links and "Find it in Browse" in links
    assert "ev.origin !== 'null' || !_isModFrame(ev.source)" in links
