"""Settings "Go to..." (static/js/settingsNav.js).

Reported 2026-09-30: the voice call said "Speech to text is off. Pick an
engine in Settings > AI > Voice call." and Jaron went there and it wasn't
there (the tab is "AI Defaults", and the control is "Hears with"). Asked for:
"i want to be able to say bring me to the ai voice settings ... ask a small
helper model to get it to take me to that page please." What matters:

  * the list of places is read from the real Settings markup, so it can't go
    stale, and hidden cards aren't in it;
  * "voice settings" lands on the speech to text control (Voice call >
    Hears with), scrolled into view, focused and flashed, in Classic,
    Studio and Workspace (where Settings is a tab);
  * a sure local match goes without asking; an unsure one asks the Utility
    model (POST /api/settings/locate) and goes where it says; with no model
    set up, the matches are listed instead;
  * the Ctrl+K palette lists matching settings above the chats;
  * typing "take me to ..." in a composer offers a one-click chip;
  * a [data-goto-setting] link, like the voice call's, opens its setting;
  * every pointer in the app's own messages names a real place.

The real settingsNav.js, search-chat.js and (for Workspace) shell.js run in
Chromium over a page holding the real Settings modal from index.html; the
modal's open and tab-switch behavior is a small stand-in for settings.js.
"""
import json
import re
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "static"
_INDEX = (_STATIC / "index.html").read_text()


def _settings_markup():
    start = _INDEX.index('<div id="settings-modal"')
    end = _INDEX.index("<!-- Search overlay")
    return _INDEX[start:end]


_PAGE = """<!doctype html><html class="__CLASSES__"><head>
<link rel="stylesheet" href="/static/style.css">
__STUDIO__
<link rel="stylesheet" href="/static/css/settingsNav.css"></head><body>
<nav id="sidebar">
  <div id="sidebar-new-chat-btn" class="list-item">New chat</div>
  <div id="session-list"></div>
  <span id="user-bar-name">Jaron Wilson</span>
  <button type="button" id="user-bar-settings">Settings</button>
</nav>
<button id="rail-new-session">+</button>
<main id="chat-container" class="chat-container" style="flex:1;min-width:0;display:flex;flex-direction:column;justify-content:flex-end;padding-bottom:40px">
  <div class="chat-input-bar"><div class="chat-input-top"><textarea id="message"></textarea></div></div>
  <form id="chat-form"></form>
  <p>Mail: <a href="#" id="mail-link" data-goto-setting="mail-listener-card">Settings › Email › Inbound mail</a></p>
</main>
<div class="search-overlay hidden" id="search-overlay"><div class="search-popup">
  <input type="text" id="search-input"><div class="search-results" id="search-results"></div></div></div>
__SETTINGS__
<script>
  document.body.style.cssText = 'display:flex;margin:0;height:100vh';
  document.getElementById('sidebar').style.cssText = 'width:240px;flex:none';
  window._isAdmin = true;
  window.calls = [];
  window.sessionModule = { cur: null, list: [], getCurrentSessionId() { return this.cur; },
    getSessions() { return this.list; }, selectSession(id) { this.cur = id; } };
  // Stand-in for settings.js: the cog opens the modal, the nav switches panels.
  const m = document.getElementById('settings-modal');
  document.getElementById('user-bar-settings').addEventListener('click', () => {
    calls.push('open'); m.classList.remove('hidden');
  });
  m.querySelector('.close-btn').addEventListener('click', () => m.classList.add('hidden'));
  m.querySelectorAll('[data-settings-tab]').forEach(btn => btn.addEventListener('click', () => {
    const tab = btn.dataset.settingsTab;
    m.querySelectorAll('[data-settings-tab]').forEach(b => b.classList.toggle('active', b.dataset.settingsTab === tab));
    m.querySelectorAll('[data-settings-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.settingsPanel !== tab));
  }));
</script></body></html>"""

_STUBS = {
    "/static/js/modalSnap.js": "export function clearRightDock() {}",
    "/static/js/chatRenderer.js": "export function openEntityHash(h) { return true; }",
    "/static/js/ui.js": "export default { esc: s => String(s).replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';') };",
    "/static/js/sessions.js": "export default { selectSession(id) { (window.calls ||= []).push(['select', id]); } };",
}

_DESIGNS = {
    "classic": "ui-classic",
    "studio": "ui-studio",
    "workspace": "ui-workspace ui-studio",
}


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def _make_page(browser, design="classic", locate=None):
    """A page in one design. `locate` answers POST /api/settings/locate
    (a dict, or a function of the request body); requests are recorded."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.locate_requests = []
    studio = "" if design == "classic" else (
        '<link rel="stylesheet" href="/static/css/studio.css">'
        '<link rel="stylesheet" href="/static/css/studio-overlays.css">'
        '<link rel="stylesheet" href="/static/css/workspace.css">')
    html = (_PAGE.replace("__CLASSES__", _DESIGNS[design]).replace("__STUDIO__", studio)
            .replace("__SETTINGS__", _settings_markup()))

    def route(r):
        url = r.request.url
        path = url.split("example.test", 1)[-1].split("?", 1)[0]
        if path in ("", "/"):
            r.fulfill(body=html, content_type="text/html")
        elif path in _STUBS and (design == "workspace" or path != "/static/js/modalSnap.js"):
            r.fulfill(body=_STUBS[path], content_type="text/javascript")
        elif path == "/api/settings/locate":
            body = json.loads(r.request.post_data or "{}")
            pg.locate_requests.append(body)
            ans = locate(body) if callable(locate) else (locate or {"id": None, "model": False})
            r.fulfill(body=json.dumps(ans), content_type="application/json")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            if f.suffix not in (".js", ".css") or not f.exists():
                r.fulfill(status=404, body="")          # fonts and images aren't needed
                return
            ctype = "text/css" if f.suffix == ".css" else "text/javascript"
            r.fulfill(body=f.read_text(), content_type=ctype)
        elif path.startswith("/api/"):
            r.fulfill(body=json.dumps({"events": [], "plans": [], "emails": [], "jobs": [], "notes": []}),
                      content_type="application/json")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    pg.goto("https://example.test/")
    pg.evaluate("() => import('/static/js/settingsNav.js').then(m => { window.__nav = m.default; })")
    if design == "workspace":
        pg.evaluate("() => import('/static/js/workspace/shell.js').then(m => { window.__ws = m; })")
        pg.wait_for_function("() => window.__ws && document.getElementById('ws-tabbar')")
    pg.wait_for_function("() => window.__nav")
    pg.wait_for_timeout(200)
    return pg


@pytest.fixture
def make_page(browser):
    pages = []

    def make(*a, **kw):
        pg = _make_page(browser, *a, **kw)
        pages.append(pg)
        return pg
    yield make
    for pg in pages:
        pg.context.close()


def _landed(pg):
    """Where the last jump went: the flashed element's control, if any."""
    return pg.evaluate("""() => {
      const f = document.querySelector('.settings-goto-flash');
      const ctl = f && (f.matches('select,input,textarea,button') ? f : f.querySelector('select,input,textarea,button'));
      const box = f && f.getBoundingClientRect();
      const m = document.getElementById('settings-modal');
      const panel = [...document.querySelectorAll('[data-settings-panel]')].find(p => !p.classList.contains('hidden'));
      return { flashed: f ? (f.id || (ctl && ctl.id) || f.className) : null,
               card: f && f.closest('.admin-card') ? f.closest('.admin-card').id : null,
               focus: document.activeElement && document.activeElement.id,
               inView: !!box && box.width > 0 && box.top >= 0 && box.bottom <= innerHeight,
               panel: panel && panel.dataset.settingsPanel,
               open: !m.classList.contains('hidden') && !m.classList.contains('ws-away') };
    }""")


def _wait_landed(pg, ms=2500):
    pg.wait_for_function("() => document.querySelector('.settings-goto-flash')", timeout=ms)
    pg.wait_for_timeout(450)          # smooth scroll


# ── The index ───────────────────────────────────────────────────────────

def test_the_index_is_read_from_the_real_settings_page(make_page):
    pg = make_page()
    idx = pg.evaluate("() => window.__nav.buildIndex().map(e => [e.id, e.kind, e.path])")
    paths = {i: p for i, _, p in idx}
    assert len(paths) == len(idx), "ids are unique"
    assert paths["set-vcStt"] == "AI Defaults > Voice call > Hears with"
    assert paths["voice-call-settings"] == "AI Defaults > Voice call"
    assert paths["sms-card"] == "Devices > Phone SMS"
    assert paths["tab:system"] == "System"
    # Cards the page hides (the old Text to Speech card) are not places to go.
    assert not any(p.startswith("AI Defaults > Text to Speech") for p in paths.values())
    # A non-admin can't open the admin tabs, so they're left out.
    pg.evaluate("window._isAdmin = false")
    ids = pg.evaluate("() => window.__nav.buildIndex().map(e => e.id)")
    assert "tab:system" not in ids and "set-vcStt" in ids


@pytest.mark.parametrize("query,lands", [
    ("voice settings", "set-vcStt"),
    ("bring me to the ai voice settings", "set-vcStt"),
    ("where do I change the speech to text engine", "set-vcStt"),
    ("peech to text", "set-vcStt"),
    ("open sms settings", "sms-card"),
    ("push to talk", "set-vcMode"),
    ("utility model", None),           # the Utility Model card, which has no id
    ("inbound mail", "mail-listener-card"),
    ("take me to two factor", "settings-2fa-card"),
])
def test_sure_local_matches(make_page, query, lands):
    pg = make_page()
    got = pg.evaluate("""async (q) => {
      const r = await window.__nav.locate(q, { useModel: false });
      if (!r.entry) return null;
      const land = r.entry.primary ? document.getElementById(r.entry.primary) : r.entry.el;
      return { id: land.id || null, path: r.entry.path };
    }""", query)
    assert got is not None, f"{query!r} should be a sure match"
    if lands:
        assert got["id"] == lands
    else:
        assert got["path"] == "AI Defaults > Utility Model"


def test_nonsense_is_not_a_sure_match(make_page):
    pg = make_page()
    assert pg.evaluate("async () => (await window.__nav.locate('take me to the beach', { useModel: false })).entry") is None


def test_navigation_requests_are_told_apart_from_questions(make_page):
    pg = make_page()
    checks = pg.evaluate("""() => ['take me to the voice settings', 'bring me to sms', 'open sms settings',
      'where do I change the speech to text engine', 'go to appearance',
      'what is speech to text?', 'write me a poem about settings', 'open the pod bay doors'].map(t => window.__nav.isNavRequest(t))""")
    assert checks == [True, True, True, True, True, False, False, False]


# ── Landing on the control, in every design ─────────────────────────────

@pytest.mark.parametrize("design", ["classic", "studio", "workspace"])
def test_voice_settings_lands_on_speech_to_text_in_every_design(make_page, design):
    pg = make_page(design)
    # From closed: one call opens Settings at the control.
    pg.evaluate("() => window.__nav.goToSetting('voice settings')")
    _wait_landed(pg)
    got = _landed(pg)
    assert got["open"] and got["panel"] == "ai"
    assert got["flashed"] == "set-vcStt" and got["focus"] == "set-vcStt" and got["inView"]
    if design == "workspace":
        assert "ws-docked" in pg.get_attribute("#settings-modal", "class")
        assert pg.evaluate("document.querySelector('.ws-tab.active').dataset.tab") == "tool:settings"

    # From another tab, through the Go to box.
    pg.evaluate("document.querySelector('[data-settings-tab=devices]').click()")
    pg.evaluate("document.querySelectorAll('.settings-goto-flash').forEach(e => e.classList.remove('settings-goto-flash'))")
    pg.fill("#settings-goto-input", "voice settings")
    first = pg.inner_text("#settings-goto-results .settings-goto-item")
    assert "Voice call" in first
    pg.press("#settings-goto-input", "Enter")
    _wait_landed(pg)
    got = _landed(pg)
    assert got["panel"] == "ai" and got["flashed"] == "set-vcStt" and got["inView"]
    assert pg.input_value("#settings-goto-input") == ""
    assert pg.locate_requests == [], "a sure match doesn't ask the model"


def test_workspace_brings_a_background_settings_tab_forward(make_page):
    pg = make_page("workspace")
    pg.evaluate("document.getElementById('user-bar-settings').click()")
    pg.wait_for_timeout(500)
    pg.evaluate("document.querySelector('.ws-tab[data-tab=home]').click()")
    pg.wait_for_timeout(400)
    assert "ws-away" in pg.get_attribute("#settings-modal", "class")
    pg.evaluate("() => window.__nav.goToSetting('open sms settings')")
    _wait_landed(pg)
    got = _landed(pg)
    assert got["open"] and got["panel"] == "devices" and got["card"] == "sms-card"
    assert pg.evaluate("document.querySelector('.ws-tab.active').dataset.tab") == "tool:settings"
    assert pg.evaluate("document.querySelectorAll('.ws-tab[data-tab=\"tool:settings\"]').length") == 1


def test_a_fold_out_is_opened_on_the_way(make_page):
    pg = make_page()
    pg.evaluate("() => window.__nav.goToSetting('sms-card/details0')")
    _wait_landed(pg)
    assert pg.evaluate("document.querySelector('#sms-card details').open")


# ── Asking the Utility model ────────────────────────────────────────────

def test_an_unsure_match_asks_the_model_and_goes_where_it_says(make_page):
    pg = make_page(locate={"id": "set-vcTts", "model": True})
    pg.evaluate("document.getElementById('user-bar-settings').click()")
    pg.fill("#settings-goto-input", "text to speech voice")
    pg.press("#settings-goto-input", "Enter")
    _wait_landed(pg)
    assert _landed(pg)["flashed"] == "set-vcTts"
    [req] = pg.locate_requests
    assert req["query"] == "text to speech voice"
    ids = {c["id"]: c["path"] for c in req["candidates"]}
    assert ids["set-vcTts"] == "AI Defaults > Voice call > Speaks with" and "set-vcStt" in ids


def test_a_model_answer_off_the_list_is_ignored(make_page):
    pg = make_page(locate={"id": "made-up-place", "model": True})
    r = pg.evaluate("async () => { const r = await window.__nav.locate('text to speech voice'); return r.entry && r.entry.id; }")
    assert r is None and len(pg.locate_requests) == 1


def test_no_model_lists_the_matches_instead(make_page):
    pg = make_page(locate={"id": None, "model": False})
    pg.evaluate("document.getElementById('user-bar-settings').click()")
    pg.fill("#settings-goto-input", "text to speech voice")
    pg.press("#settings-goto-input", "Enter")
    pg.wait_for_function("() => /Utility model/.test(document.getElementById('settings-goto-results').innerText)")
    items = pg.eval_on_selector_all("#settings-goto-results .settings-goto-item", "els => els.map(e => e.innerText)")
    assert any("Speaks with" in t for t in items) and any("Voice" in t for t in items)
    # Picking one with the arrow keys goes there.
    pg.press("#settings-goto-input", "ArrowDown")
    pg.press("#settings-goto-input", "ArrowUp")
    pg.press("#settings-goto-input", "Enter")
    _wait_landed(pg)
    assert _landed(pg)["card"] == "voice-call-settings"


def test_escape_in_the_box_clears_it_first(make_page):
    pg = make_page()
    pg.evaluate("document.getElementById('user-bar-settings').click()")
    pg.fill("#settings-goto-input", "sms")
    assert pg.is_visible("#settings-goto-results")
    pg.press("#settings-goto-input", "Escape")
    assert pg.input_value("#settings-goto-input") == ""
    assert not pg.is_visible("#settings-goto-results")
    assert "hidden" not in pg.get_attribute("#settings-modal", "class")


# ── The other ways in ───────────────────────────────────────────────────

def test_the_ctrl_k_palette_lists_settings_above_chats(make_page):
    pg = make_page()
    pg.evaluate("""() => import('/static/js/search-chat.js').then(m => { m.init(''); m.openSearch(); window.__search = m; })""")
    pg.wait_for_function("() => window.__search")
    pg.type("#search-input", "voice settings")
    pg.wait_for_function("() => document.querySelector('#search-results .search-result-setting')")
    first = pg.inner_text("#search-results .search-result-item")
    assert "Voice call" in first
    pg.press("#search-input", "Enter")
    _wait_landed(pg)
    assert _landed(pg)["flashed"] == "set-vcStt"
    assert pg.evaluate("document.getElementById('search-overlay').classList.contains('hidden')")


def test_the_palette_offers_to_ask_when_unsure(make_page):
    pg = make_page(locate={"id": "set-vcTts", "model": True})
    pg.evaluate("""() => import('/static/js/search-chat.js').then(m => { m.init(''); m.openSearch(); window.__search = m; })""")
    pg.wait_for_function("() => window.__search")
    pg.type("#search-input", "take me to text to speech voice")
    pg.wait_for_function("() => document.querySelector('#search-results [data-setting-ask]')")
    pg.click("#search-results [data-setting-ask]")
    _wait_landed(pg)
    assert _landed(pg)["flashed"] == "set-vcTts"


@pytest.mark.parametrize("design", ["classic", "studio"])
def test_take_me_to_in_a_composer_offers_a_chip(make_page, design):
    pg = make_page(design)
    pg.fill("#message", "take me to the sms settings")
    chip = pg.locator("#chat-container .settings-goto-chip")
    assert chip.is_visible() and "Phone SMS" in chip.inner_text()
    pg.fill("#message", "hello there")
    assert not chip.is_visible()
    pg.fill("#message", "bring me to the ai voice settings")
    assert "Hears with" in chip.inner_text()
    chip.locator(".settings-goto-chip-btn").click()
    _wait_landed(pg)
    assert _landed(pg)["flashed"] == "set-vcStt"
    assert pg.input_value("#message") == ""


def test_workspace_home_opens_the_setting_instead_of_starting_a_chat(make_page):
    pg = make_page("workspace")
    pg.wait_for_selector("#ws-home .ws-compose-input")
    pg.fill("#ws-home .ws-compose-input", "where do I change the speech to text engine")
    assert "Hears with" in pg.inner_text("#ws-home .settings-goto-chip")
    pg.press("#ws-home .ws-compose-input", "Enter")
    _wait_landed(pg)
    assert _landed(pg)["flashed"] == "set-vcStt"
    assert pg.evaluate("document.querySelector('.ws-tab.active').dataset.tab") == "tool:settings"
    assert pg.input_value("#ws-home .ws-compose-input") == ""


def test_a_setting_link_opens_its_setting(make_page):
    pg = make_page()
    pg.click("#mail-link")
    _wait_landed(pg)
    got = _landed(pg)
    assert got["panel"] == "email" and got["flashed"] == "mail-listener-card"


def test_the_agents_open_panel_settings_lands_there(make_page):
    pg = make_page()
    pg.evaluate("""() => import('/static/js/chatStream.js').then(m =>
        m.handleUIControl({ ui_event: 'open_panel', panel: 'settings', settings_target: 'speech to text engine' }))""")
    _wait_landed(pg, 5000)
    assert _landed(pg)["flashed"] == "set-vcStt"


# ── Pointers in the app's own messages ──────────────────────────────────

def _tab_labels():
    return dict(re.findall(r'data-settings-tab="([a-z]+)">\s*<svg.*?</svg>\s*<span>([^<]+)</span>', _INDEX, re.S))


def test_the_voice_call_points_at_a_real_place_and_links_it():
    js = (_STATIC / "js" / "voiceCall.js").read_text()
    assert "Settings > AI >" not in js
    labels = _tab_labels()
    assert labels["ai"] == "AI Defaults"
    assert js.count("in Settings > AI Defaults > Voice call.") == 2 and js.count("goto: 'set-vcStt'") == 2
    assert 'for="set-vcStt">Hears with<' in _INDEX


def test_every_settings_link_targets_something_on_the_page():
    ids = set(re.findall(r'\bid="([^"]+)"', _INDEX))
    for f in (_STATIC / "js").rglob("*.js"):
        for target in re.findall(r'data-goto-setting="([^"$]+)"', f.read_text()):
            assert target in ids, f"{f.name}: data-goto-setting={target!r} is not in index.html"


def test_pointer_strings_name_real_tabs():
    """`Settings > X` anywhere in the code must start with a tab that exists
    (by its label, the way the user sees it). "Settings > AI > Voice call"
    is how the voice call sent Jaron looking for a tab that isn't there."""
    labels = sorted(_tab_labels().values(), key=len, reverse=True)
    pat = re.compile(r"Settings ?(?:>|&gt;|›|&rsaquo;|→|->) ?([A-Z][^\n]*)")
    elsewhere = ("github.com", "linear.app", "todoist.com", "ChatGPT Settings", "Server Settings", "Developer Settings")
    bad = []
    for base in ("static", "src", "routes"):
        for f in (_ROOT / base).rglob("*"):
            if f.suffix not in (".js", ".py", ".html") or "vendor" in f.parts or "lib" in f.parts:
                continue
            for line in f.read_text(errors="ignore").splitlines():
                if any(e in line for e in elsewhere) or "BaseSettings" in line or 'pointed at "Settings > AI >' in line:
                    continue
                for m in pat.finditer(line):
                    if line[:m.start()].rstrip().endswith(("Cookbook >", '"Settings > AI >')):
                        continue          # the Cookbook window's own tab; the old pointer, quoted
                    rest = m.group(1)
                    if not any(re.match(re.escape(lab) + r"(?![A-Za-z])", rest) for lab in labels):
                        bad.append(f"{f.relative_to(_ROOT)}: {line.strip()[:140]}")
    assert not bad, "\n".join(bad)
