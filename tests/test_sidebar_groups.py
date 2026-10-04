"""The sidebar's tool groups and "take me to <any page>" (toolPages.js,
toolGroups.js, settingsNav.js, workspace/shell.js).

Asked for 2026-09-30: "that sidebar looks complicated has lots of stuff, are
we able to break it down more, like terminal devices browser code devops,
odysseus dev like that whole section looks complex and hard to navigate."
And "take me to devices page please" in a chat got "I don't have an
app-navigation tool available here." What matters:

  * the 17 tools sit in four labeled groups (Organize, Create & research,
    Build, System), in the order toolPages.js gives, with every button id,
    click handler and badge where it was;
  * a group folds with a click on its header, says what's inside while
    folded, and stays folded after a reload; a folded group with news in it
    shows a dot;
  * a non-admin doesn't see the admin-only tools, Build starts folded for
    them, and a group with nothing left in it is hidden;
  * it renders in Classic, Studio and Workspace, and the Workspace "+" menu
    lists the tools in the same groups;
  * "take me to devices page please" offers a chip that opens Devices (as a
    tab in Workspace), Ctrl+K lists pages above settings, and the agent's
    ui_control open_panel opens any page, old panel names included.

The real modules run in Chromium over the sidebar markup from index.html;
each tool button is a stand-in that records its click.
"""
import json
import re
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "static"
_INDEX = (_STATIC / "index.html").read_text()
_JS = (_STATIC / "js" / "toolPages.js").read_text()
_TAXONOMY = json.loads(re.search(r"/\*\s*tool-pages:begin\s*\*/(.*?)/\*\s*tool-pages:end\s*\*/", _JS, re.S).group(1))

_TOOL_IDS = ["tool-memory-btn", "tool-calendar-btn", "tool-compare-btn", "tool-cookbook-btn", "tool-research-btn",
             "tool-gallery-btn", "tool-library-btn", "tool-notes-btn", "tool-tasks-btn", "tool-bg-btn",
             "tool-devops-btn", "tool-code-btn", "tool-browser-btn", "tool-devices-btn", "tool-terminal-btn",
             "tool-claude-sessions-btn",
             "tool-odysseus-dev-btn", "tool-theme-btn", "tool-whats-new-btn"]


def _sidebar_markup():
    start = _INDEX.index('<nav class="sidebar" id="sidebar"')
    return _INDEX[start:_INDEX.index("</nav>", start) + len("</nav>")]


_PAGE = """<!doctype html><html class="__CLASSES__"><head>
<link rel="stylesheet" href="/static/style.css">
__STUDIO__
<link rel="stylesheet" href="/static/css/settingsNav.css"></head><body>
__SIDEBAR__
<button id="rail-new-session">+</button>
<main id="chat-container" class="chat-container welcome-active" style="flex:1;min-width:0;display:flex;flex-direction:column;justify-content:flex-end;padding-bottom:40px">
  <div class="chat-input-bar"><div class="chat-input-top"><textarea id="message"></textarea></div></div>
  <form id="chat-form"></form>
</main>
<div class="search-overlay hidden" id="search-overlay"><div class="search-popup">
  <input type="text" id="search-input"><div class="search-results" id="search-results"></div></div></div>
<script>
  document.body.style.cssText = 'display:flex;margin:0;height:100vh';
  document.getElementById('sidebar').style.cssText += ';width:240px;flex:none;position:relative;transform:none;left:0';
  window.calls = [];
  window.sessionModule = { cur: null, list: [], getCurrentSessionId() { return this.cur; },
    getSessions() { return this.list; }, selectSession(id) { this.cur = id; } };
  // Stand-ins for the tools: each sidebar button records its click; Devices
  // also opens a window the way devicesPanel.js does, so Workspace docks it.
  document.querySelectorAll('#tools-section .list-item, #user-bar-settings, #email-section-title, #chats-library-btn')
    .forEach(b => b.addEventListener('click', () => calls.push(b.id)));
  document.getElementById('tool-devices-btn').addEventListener('click', () => {
    if (document.querySelector('.dp-backdrop')) return;
    const d = document.createElement('div');
    d.className = 'bg-panel-backdrop dp-backdrop';
    d.innerHTML = '<div class="bg-panel"><button class="bg-panel-close">x</button>Devices</div>';
    document.body.appendChild(d);
  });
  document.getElementById('tool-memory-btn').addEventListener('click', () => {
    if (document.getElementById('memory-modal')) return;
    const m = document.createElement('div');
    m.id = 'memory-modal';
    m.innerHTML = '<button class="memory-tab" data-memory-tab="skills">Skills</button>';
    m.querySelector('button').addEventListener('click', () => calls.push('skills-tab'));
    document.body.appendChild(m);
  });
  window._isAdmin = __ADMIN__;
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


def _make_page(browser, design="classic", admin=True, storage=None, viewport=(1400, 900)):
    ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    pg = ctx.new_page()
    studio = "" if design == "classic" else (
        '<link rel="stylesheet" href="/static/css/studio.css">'
        '<link rel="stylesheet" href="/static/css/workspace.css">')
    html = (_PAGE.replace("__CLASSES__", _DESIGNS[design]).replace("__STUDIO__", studio)
            .replace("__SIDEBAR__", _sidebar_markup())
            .replace("__ADMIN__", "true" if admin else "false"))

    def route(r):
        path = r.request.url.split("example.test", 1)[-1].split("?", 1)[0]
        if path in ("", "/"):
            r.fulfill(body=html, content_type="text/html")
        elif path in _STUBS and (design == "workspace" or path != "/static/js/modalSnap.js"):
            r.fulfill(body=_STUBS[path], content_type="text/javascript")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            if f.suffix not in (".js", ".css") or not f.exists():
                r.fulfill(status=404, body="")
                return
            r.fulfill(body=f.read_text(), content_type="text/css" if f.suffix == ".css" else "text/javascript")
        elif path.startswith("/api/"):
            r.fulfill(body=json.dumps({"events": [], "plans": [], "emails": [], "jobs": [], "notes": []}),
                      content_type="application/json")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    if storage:
        ctx.add_init_script(f"try {{ localStorage.setItem('odysseus-tool-groups', {json.dumps(json.dumps(storage))}); }} catch (e) {{}}")
    pg.goto("https://example.test/")
    pg.evaluate("""() => Promise.all([import('/static/js/toolGroups.js'), import('/static/js/settingsNav.js'),
        import('/static/js/search-chat.js')]).then(([g, n, s]) => { window.__groups = g.default; window.__nav = n.default;
        s.init(''); window.__search = s; })""")
    if design == "workspace":
        pg.evaluate("() => import('/static/js/workspace/shell.js').then(m => { window.__ws = m; })")
        pg.wait_for_function("() => window.__ws && document.getElementById('ws-tabbar')")
    pg.wait_for_function("() => window.__groups && window.__nav")
    pg.wait_for_timeout(150)
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


def _groups(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('#tools-section .tool-group')].map(g => ({
      id: g.dataset.toolGroup, hidden: g.hidden, folded: g.classList.contains('collapsed'),
      label: g.querySelector('.tool-group-label').textContent,
      expanded: g.querySelector('.tool-group-head').getAttribute('aria-expanded'),
      summary: g.querySelector('.tool-group-summary').textContent,
      items: [...g.querySelectorAll('.tool-group-items > .list-item')].filter(i => i.style.display !== 'none').map(i => i.id),
      shown: [...g.querySelectorAll('.tool-group-items > .list-item')].filter(i => i.getClientRects().length).map(i => i.id) }))""")


# ── The markup and the list agree ───────────────────────────────────────

def test_every_tool_is_in_exactly_the_group_toolpages_says():
    sidebar = _sidebar_markup()
    for tid in _TOOL_IDS:
        assert sidebar.count(f'id="{tid}"') == 1, tid
    groups = dict(re.findall(r'data-tool-group="([a-z]+)"(.*?)(?=data-tool-group=|</nav>)', sidebar, re.S))
    assert list(groups) == [g["id"] for g in _TAXONOMY["groups"]]
    grouped = [p for p in _TAXONOMY["pages"] if p["group"]]
    assert sorted(p["open"].lstrip("#") for p in grouped) == sorted(_TOOL_IDS)
    for p in grouped:
        ids = re.findall(r'class="list-item" id="([a-z-]+)"', groups[p["group"]])
        assert p["open"].lstrip("#") in ids, (p["key"], p["group"])
    # ...and in the same order within each group.
    for g in _TAXONOMY["groups"]:
        ids = re.findall(r'class="list-item" id="([a-z-]+)"', groups[g["id"]])
        assert ids == [p["open"].lstrip("#") for p in grouped if p["group"] == g["id"]]
    # Badges and the Library + stay inside their rows.
    for inner, row in [("cookbook-notif-dot", "tool-cookbook-btn"), ("cookbook-bg-status", "tool-cookbook-btn"),
                       ("assistant-notif-dot", "tool-tasks-btn"), ("bg-tasks-count", "tool-bg-btn"),
                       ("library-new-doc-btn", "tool-library-btn")]:
        row_html = sidebar[sidebar.index(f'id="{row}"'):]
        row_html = row_html[:row_html.index('class="list-item"', 10) if 'class="list-item"' in row_html[10:] else len(row_html)]
        assert f'id="{inner}"' in row_html, (inner, row)


def test_every_page_opener_exists_in_the_app():
    for p in _TAXONOMY["pages"]:
        sel = p["open"]
        assert sel.startswith("#")
        assert f'id="{sel[1:]}"' in _INDEX, (p["key"], sel)


# ── Groups render, fold and remember ────────────────────────────────────

@pytest.mark.parametrize("design", ["classic", "studio", "workspace"])
def test_the_groups_render_in_every_design(make_page, design):
    pg = make_page(design)
    gs = _groups(pg)
    assert [g["label"] for g in gs] == ["Organize", "Create & research", "Build", "System"]
    assert [g["items"] for g in gs] == [
        ["tool-calendar-btn", "tool-tasks-btn", "tool-notes-btn", "tool-memory-btn"],
        ["tool-library-btn", "tool-gallery-btn", "tool-research-btn", "tool-compare-btn"],
        ["tool-code-btn", "tool-terminal-btn", "tool-browser-btn", "tool-bg-btn", "tool-claude-sessions-btn", "tool-devops-btn",
         "tool-odysseus-dev-btn"],
        ["tool-devices-btn", "tool-cookbook-btn", "tool-theme-btn", "tool-whats-new-btn"]]
    assert all(not g["folded"] and g["expanded"] == "true" and g["shown"] == g["items"] for g in gs)
    # Headers are on screen, in order, each above its own rows.
    tops = pg.evaluate("""() => [...document.querySelectorAll('.tool-group')].map(g => {
      const h = g.querySelector('.tool-group-head').getBoundingClientRect();
      const first = g.querySelector('.list-item').getBoundingClientRect();
      return [h.height, h.top, first.top]; })""")
    assert all(h >= 20 and ht < ft for h, ht, ft in tops)
    assert [t[1] for t in tops] == sorted(t[1] for t in tops)
    # The rows still open their tools.
    pg.click("#tool-terminal-btn")
    assert "tool-terminal-btn" in pg.evaluate("calls")


def test_a_group_folds_says_whats_inside_and_stays_folded(make_page, browser):
    pg = make_page("studio")
    pg.click('.tool-group[data-tool-group="system"] .tool-group-head')
    g = {x["id"]: x for x in _groups(pg)}["system"]
    assert g["folded"] and g["expanded"] == "false" and g["shown"] == []
    assert g["summary"] == "Devices, Cookbook, Theme, What's new"
    assert pg.is_visible('.tool-group[data-tool-group="system"] .tool-group-summary')
    assert json.loads(pg.evaluate("localStorage.getItem('odysseus-tool-groups')")) == {"system": "closed"}
    pg.reload()
    pg.evaluate("() => import('/static/js/toolGroups.js')")
    pg.wait_for_timeout(150)
    g = {x["id"]: x for x in _groups(pg)}
    assert g["system"]["folded"] and not g["organize"]["folded"]
    # Keyboard: the header is a button.
    pg.focus('.tool-group[data-tool-group="system"] .tool-group-head')
    pg.keyboard.press("Enter")
    assert not {x["id"]: x for x in _groups(pg)}["system"]["folded"]


def test_a_folded_group_with_news_shows_a_dot(make_page):
    pg = make_page("classic", storage={"system": "closed", "organize": "closed"})
    dot = lambda gid: pg.evaluate(f"""() => {{ const d = document.querySelector('.tool-group[data-tool-group="{gid}"] .tool-group-dot');
        return getComputedStyle(d).display !== 'none'; }}""")
    assert not dot("system")
    pg.evaluate("document.getElementById('cookbook-notif-dot').style.display = 'inline-block'")
    pg.wait_for_timeout(100)
    assert dot("system")
    pg.evaluate("document.getElementById('assistant-notif-dot').style.display = 'inline-block'")
    pg.wait_for_timeout(100)
    assert dot("organize")
    pg.evaluate("document.getElementById('cookbook-notif-dot').style.display = 'none'")
    pg.wait_for_timeout(100)
    assert not dot("system")


def test_non_admins_lose_the_admin_tools_and_empty_groups_hide(make_page):
    pg = make_page("studio", admin=False)
    g = {x["id"]: x for x in _groups(pg)}
    assert g["build"]["items"] == ["tool-browser-btn", "tool-bg-btn"]
    assert g["build"]["folded"], "Build starts folded for someone who isn't an admin"
    assert g["build"]["summary"] == "Browser, Background"
    assert g["system"]["items"] == ["tool-cookbook-btn", "tool-theme-btn", "tool-whats-new-btn"]
    # A tool that removes its own button (Terminal does) and a group left empty.
    pg.evaluate("""() => { document.getElementById('tool-browser-btn').style.display = 'none';
                           document.getElementById('tool-bg-btn').remove(); }""")
    pg.wait_for_timeout(100)
    assert {x["id"]: x for x in _groups(pg)}["build"]["hidden"]
    assert not pg.is_visible('.tool-group[data-tool-group="build"] .tool-group-head')


def test_admin_status_arriving_late_still_applies(make_page):
    pg = make_page("classic")
    pg.evaluate("() => { window._isAdmin = false; document.dispatchEvent(new CustomEvent('odysseus:auth', { detail: {} })); }")
    pg.wait_for_timeout(100)
    g = {x["id"]: x for x in _groups(pg)}
    assert "tool-terminal-btn" not in g["build"]["items"] and g["build"]["folded"]


def test_narrow_screens_get_the_groups_with_bigger_headers(make_page):
    pg = make_page("workspace", viewport=(390, 844))
    h = pg.evaluate("document.querySelector('.tool-group-head').getBoundingClientRect().height")
    assert h >= 34


# ── The Workspace "+" menu ─────────────────────────────────────────────

def test_the_new_tab_menu_lists_the_tools_in_the_same_groups(make_page):
    pg = make_page("workspace")
    pg.click(".ws-new-btn")
    menu = pg.evaluate("""() => ({
      top: [...document.querySelectorAll('.ws-menu > button')].map(b => b.textContent.trim()),
      groups: [...document.querySelectorAll('.ws-menu-group')].map(g => [g.querySelector('.ws-menu-head').textContent,
        [...g.querySelectorAll('button')].map(b => b.textContent.trim())]) })""")
    assert menu["top"] == ["New chat", "Inbox", "Settings"]
    assert menu["groups"] == [
        ["Organize", ["Calendar", "Tasks", "Notes", "Brain"]],
        ["Create & research", ["Library", "Gallery", "Deep Research", "Compare"]],
        ["Build", ["Code", "Terminal", "Browser", "Background", "Claude sessions", "DevOps", "Odysseus dev"]],
        ["System", ["Devices", "Cookbook", "Theme", "What's new"]]]
    pg.click(".ws-menu-group button:has-text('Devices')")
    pg.wait_for_function("() => [...document.querySelectorAll('.ws-tab')].some(t => t.dataset.tab === 'tool:devices')")
    assert "tool-devices-btn" in pg.evaluate("calls")


def test_the_new_tab_menu_leaves_out_what_a_non_admin_cant_open(make_page):
    pg = make_page("workspace", admin=False)
    pg.click(".ws-new-btn")
    names = pg.evaluate("[...document.querySelectorAll('.ws-menu button')].map(b => b.textContent.trim())")
    for gone in ("Code", "Terminal", "DevOps", "Odysseus dev", "Devices"):
        assert gone not in names
    assert "Browser" in names and "Calendar" in names


# ── "Take me to <any page>" ─────────────────────────────────────────────

def test_page_names_and_the_words_people_use(make_page):
    pg = make_page()
    got = pg.evaluate("""() => ['command line', 'shell', 'console', 'computers', 'phones', 'machines', 'vs code',
        'editor', 'ide', 'Deep Research', 'memories', 'documents', 'sessions', 'odysseus dev', 'nope'].map(n => {
        const p = window.toolPages.page(n); return p ? p.key : null; })""")
    assert got == ["terminal", "terminal", "terminal", "devices", "devices", "devices", "code",
                   "code", "code", "research", "brain", "library", "chats", "odysseus-dev", None]


@pytest.mark.parametrize("design", ["classic", "studio", "workspace"])
def test_take_me_to_devices_offers_a_chip_that_opens_it(make_page, design):
    pg = make_page(design)
    # Workspace starts on Home, whose composer has the same chip.
    box = ".ws-compose-input" if design == "workspace" else "#message"
    pg.fill(box, "take me to devices page please")
    chip = pg.locator(".settings-goto-chip:not([hidden])").first
    chip.wait_for(state="visible")
    assert re.sub(r"\s+", " ", chip.inner_text()).startswith("Open Devices page")
    chip.locator(".settings-goto-chip-btn").click()
    pg.wait_for_function("() => calls.includes('tool-devices-btn')")
    if design == "workspace":
        pg.wait_for_function("() => document.querySelector('.ws-tab.active')?.dataset.tab === 'tool:devices'")
    assert pg.input_value(box) == ""


def test_what_reads_as_going_somewhere(make_page):
    pg = make_page()
    checks = pg.evaluate("""() => ['take me to devices page please', 'open the terminal', 'show me my calendar',
      'open command line', 'bring up devops', 'open the pod bay doors', 'open a document about tides',
      'show me how to write a loop'].map(t => window.__nav.isNavRequest(t))""")
    assert checks == [True, True, True, True, True, False, False, False]


def test_words_alone_go_to_the_page_and_settings_words_to_settings(make_page):
    pg = make_page()
    top = lambda q: pg.evaluate(f"""() => {{ const r = window.__nav.rank({json.dumps(q)}, window.__nav.buildNavIndex());
        return r.length ? r[0].entry.id : null; }}""")
    assert top("take me to the command line") == "page:terminal"
    assert top("open vs code") == "page:code"
    assert top("go to my computers") == "page:devices"
    assert top("take me to settings") == "page:settings"


def test_ctrl_k_lists_pages_above_settings_and_enter_opens_it(make_page):
    pg = make_page("classic")
    pg.evaluate("window.__search.openSearch()")
    pg.fill("#search-input", "terminal")
    pg.wait_for_timeout(150)
    rows = pg.evaluate("[...document.querySelectorAll('#search-results .search-group-header, #search-results .search-result-item')].map(e => e.textContent.replace(/\\s+/g, ' ').trim())")
    assert rows[0] == "Pages" and rows[1] == "Open Terminal page"
    pg.press("#search-input", "Enter")
    pg.wait_for_function("() => calls.includes('tool-terminal-btn')")


@pytest.mark.parametrize("panel,clicked", [
    ("devices", "tool-devices-btn"), ("terminal", "tool-terminal-btn"), ("code", "tool-code-btn"),
    ("browser", "tool-browser-btn"), ("background", "tool-bg-btn"), ("devops", "tool-devops-btn"),
    ("claude sessions", "tool-claude-sessions-btn"),
    ("odysseus-dev", "tool-odysseus-dev-btn"), ("calendar", "tool-calendar-btn"), ("tasks", "tool-tasks-btn"),
    ("compare", "tool-compare-btn"), ("research", "tool-research-btn"), ("theme", "tool-theme-btn"),
    ("memories", "tool-memory-btn"), ("documents", "tool-library-btn"), ("email", "email-section-title"),
    ("sessions", "chats-library-btn"),
])
def test_the_agents_open_panel_opens_any_page(make_page, panel, clicked):
    pg = make_page("classic")
    pg.evaluate(f"""() => import('/static/js/chatStream.js').then(m =>
        m.handleUIControl({{ ui_event: 'open_panel', panel: {json.dumps(panel)} }}))""")
    pg.wait_for_function(f"() => calls.includes({json.dumps(clicked)})")


def test_the_agents_open_panel_skills_opens_brain_at_skills(make_page):
    pg = make_page("classic")
    pg.evaluate("""() => import('/static/js/chatStream.js').then(m =>
        m.handleUIControl({ ui_event: 'open_panel', panel: 'skills' }))""")
    pg.wait_for_function("() => calls.includes('skills-tab')")
    assert pg.evaluate("calls").index("tool-memory-btn") < pg.evaluate("calls").index("skills-tab")


def test_the_agents_open_panel_devices_is_a_tab_in_workspace(make_page):
    pg = make_page("workspace")
    # chatStream.js hands every page but Settings to toolPages.openPage.
    pg.evaluate("() => window.toolPages.openPage('devices')")
    pg.wait_for_function("() => document.querySelector('.ws-tab.active')?.dataset.tab === 'tool:devices'")
