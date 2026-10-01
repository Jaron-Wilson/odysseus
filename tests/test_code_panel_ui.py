"""The Code tool (static/js/codePanel.js) in the page.

Asked for: "can we implement a vs code or an integrated ide into it, one for
modifying the odysseus dev and also for other projects too please." The
killer use is a chat on one side and the editor on the other, so:

  * in the Workspace interface the Code tool opens as a tab, named after its
    project, and sits in split view beside a chat;
  * the project picker lists the repos from /api/ide/projects with Odysseus
    dev first, and picking one points the editor frame at /ide/?folder=...;
  * "Open folder" goes through the server's path check and shows its error;
  * "Open in editor" links open the tool at their folder;
  * in Studio and Classic it opens like the other tool windows;
  * people the server says no to don't see the entry.

The real codePanel.js, shell.js and fillChatArea.js run in Chromium against
a small page; the API and /ide/ are answered by the test.
"""
import json
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_STATIC = Path(__file__).resolve().parent.parent / "static"

_PROJECTS = [
    {"name": "Odysseus dev", "path": "/home/u/odysseus", "branch": "dev", "dirty": True, "pinned": True},
    {"name": "flysdown", "path": "/home/u/flysdown", "branch": "main", "dirty": False, "pinned": False},
]


def _page_html(html_class):
    return f"""<!doctype html><html class="{html_class}"><head>
<link rel="stylesheet" href="/static/style.css">
<link rel="stylesheet" href="/static/css/workspace.css">
<link rel="stylesheet" href="/static/css/codePanel.css"></head><body>
<nav id="sidebar">
  <div id="session-list"><div class="list-item" data-session-id="s1">Chat one</div></div>
  <div class="list-item" id="tool-code-btn">Code</div>
  <a href="#" id="job-link" data-open-editor="/home/u/flysdown">Open in editor</a>
</nav>
<button id="rail-new-session">+</button>
<main id="chat-container" style="flex:1;min-width:0"><div id="chat-box">chat</div></main>
<script>
  document.body.style.cssText = 'display:flex;margin:0;height:100vh';
  document.getElementById('sidebar').style.cssText = 'width:240px;flex:none';
  window.sessionModule = {{
    cur: null, list: [{{id: 's1', name: 'Chat one'}}],
    getCurrentSessionId() {{ return this.cur; }}, getSessions() {{ return this.list; }},
    selectSession(id) {{ this.cur = id; }},
  }};
  document.getElementById('session-list').addEventListener('click', e => {{
    const row = e.target.closest('[data-session-id]');
    if (row) sessionModule.selectSession(row.dataset.sessionId);
  }});
</script>
<script type="module" src="/static/js/codePanel.js"></script>
</body></html>"""


@pytest.fixture
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def _ctx(browser, html_class="ui-workspace ui-studio", admin=True, width=1440, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    seen = []

    def route(r):
        url = r.request.url
        rest = url.split("example.test", 1)[-1]
        path, _, query = rest.partition("?")
        seen.append(rest)
        if path in ("", "/"):
            r.fulfill(body=_page_html(html_class), content_type="text/html")
        elif path == "/static/js/chatRenderer.js":
            r.fulfill(body="export function openEntityHash() { return false; }", content_type="text/javascript")
        elif path == "/static/js/modalSnap.js":
            r.fulfill(body="export function clearRightDock() {}", content_type="text/javascript")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            if f.suffix not in (".css", ".js") or not f.is_file():
                r.fulfill(status=404, body="")         # fonts and images: not needed here
            else:
                r.fulfill(body=f.read_text(), content_type="text/css" if f.suffix == ".css" else "text/javascript")
        elif path.startswith("/api/ide/") and not admin:
            r.fulfill(status=403, body=json.dumps({"detail": "Admin only"}), content_type="application/json")
        elif path == "/api/ide/config":
            r.fulfill(body=json.dumps({"upstream": "http://127.0.0.1:8080", "roots": ["/home/u"]}),
                      content_type="application/json")
        elif path == "/api/ide/projects":
            r.fulfill(body=json.dumps({"projects": _PROJECTS, "roots": ["/home/u"]}), content_type="application/json")
        elif path == "/api/ide/resolve":
            from urllib.parse import parse_qs
            p = parse_qs(query).get("path", [""])[0]
            if p.startswith("/home/u/"):
                r.fulfill(body=json.dumps({"path": p, "name": p.rsplit("/", 1)[-1]}), content_type="application/json")
            else:
                r.fulfill(status=400, body=json.dumps({"detail": "That folder isn't under the project folders"}),
                          content_type="application/json")
        elif path.startswith("/ide/"):
            r.fulfill(body="<!doctype html><title>fake code-server</title><body>editor " + query, content_type="text/html")
        elif path.startswith("/api/"):
            r.fulfill(body=json.dumps({"events": [], "plans": [], "emails": [], "jobs": [], "notes": []}),
                      content_type="application/json")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    pg = ctx.new_page()
    pg.seen = seen
    return ctx, pg


def _boot(pg, workspace=True):
    pg.goto("https://example.test/")
    pg.wait_for_function("() => window.codePanel")
    if workspace:
        pg.evaluate("() => import('/static/js/workspace/shell.js').then(m => { window.__ws = m; })")
        pg.wait_for_function("() => window.__ws && document.getElementById('ws-tabbar')")
    pg.wait_for_timeout(500)


def _settle(pg):
    pg.wait_for_timeout(500)


def _tabs(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('.ws-tab')].map(t =>
        (t.classList.contains('active') ? '*' : '') + t.dataset.tab)""")


def _click(pg, sel):
    pg.evaluate(f"document.querySelector({json.dumps(sel)}).click()")
    _settle(pg)


def test_code_opens_as_a_workspace_tab_and_the_picker_opens_a_project(browser):
    ctx, pg = _ctx(browser)
    try:
        _boot(pg)
        _click(pg, "#tool-code-btn")
        assert _tabs(pg) == ["home", "*tool:code"]
        assert "ws-docked" in pg.get_attribute(".ide-backdrop", "class")
        # First use: the picker, Odysseus dev pinned first.
        assert pg.is_visible(".ide-picker")
        titles = pg.eval_on_selector_all(".ide-project .ide-project-title", "els => els.map(e => e.textContent)")
        assert titles == ["Odysseus dev", "flysdown"]
        assert pg.is_visible(".ide-project.pinned .ide-chip-dirty")
        _click(pg, ".ide-project[data-i='0']")
        assert pg.get_attribute(".ide-frame", "src") == "/ide/?folder=/home/u/odysseus"
        assert not pg.is_visible(".ide-picker")
        assert pg.inner_text(".ws-tab[data-tab='tool:code'] .ws-tab-label") == "Code: Odysseus dev"
        assert pg.inner_text(".ide-project-branch") == "dev"
        pg.wait_for_function("() => document.querySelector('.ide-frame').contentDocument?.title === 'fake code-server'")
    finally:
        ctx.close()


def test_code_sits_beside_a_chat_in_split_view(browser):
    ctx, pg = _ctx(browser)
    try:
        _boot(pg)
        _click(pg, "#session-list [data-session-id=s1]")
        _click(pg, "#tool-code-btn")
        _click(pg, ".ide-project[data-i='1']")
        _click(pg, ".ws-split-btn")
        assert "ws-split" in pg.evaluate("document.documentElement.className")
        geo = pg.evaluate("""() => {
          const m = document.getElementById('chat-container').getBoundingClientRect();
          const f = document.querySelector('.ide-frame').getBoundingClientRect();
          return {chatL: m.left, chatR: m.right, frameL: f.left, frameR: f.right, frameW: f.width, frameH: f.height};
        }""")
        # Both on screen, side by side, the editor frame a real size.
        assert geo["frameW"] > 400 and geo["frameH"] > 600
        assert geo["frameR"] <= geo["chatL"] + 2 or geo["chatR"] <= geo["frameL"] + 2
    finally:
        ctx.close()


def test_open_folder_checks_the_path_on_the_server(browser):
    ctx, pg = _ctx(browser)
    try:
        _boot(pg)
        _click(pg, "#tool-code-btn")
        pg.fill(".ide-folder-input", "/etc")
        pg.click(".ide-open-btn")
        _settle(pg)
        assert "isn't under the project folders" in pg.inner_text(".ide-msg")
        assert pg.get_attribute(".ide-frame", "src") is None
        pg.fill(".ide-folder-input", "/home/u/other thing")
        pg.click(".ide-open-btn")
        _settle(pg)
        assert pg.get_attribute(".ide-frame", "src") == "/ide/?folder=/home/u/other%20thing"
        # An "Open in editor" link while the tab is open switches its folder.
        _click(pg, "#job-link")
        assert pg.get_attribute(".ide-frame", "src") == "/ide/?folder=/home/u/flysdown"
        assert _tabs(pg) == ["home", "*tool:code"]
    finally:
        ctx.close()


def test_open_in_editor_link_opens_the_tool_at_its_folder(browser):
    ctx, pg = _ctx(browser)
    try:
        _boot(pg)
        _click(pg, "#job-link")
        assert _tabs(pg) == ["home", "*tool:code"]
        assert pg.get_attribute(".ide-frame", "src") == "/ide/?folder=/home/u/flysdown"
        assert pg.inner_text(".ws-tab[data-tab='tool:code'] .ws-tab-label") == "Code: flysdown"
    finally:
        ctx.close()


def test_in_classic_it_opens_as_a_window_with_reload_and_new_window(browser):
    ctx, pg = _ctx(browser, html_class="ui-classic")
    try:
        _boot(pg, workspace=False)
        _click(pg, "#tool-code-btn")
        assert pg.is_visible(".ide-panel")
        _click(pg, ".ide-project[data-i='1']")
        assert pg.get_attribute(".ide-frame", "src") == "/ide/?folder=/home/u/flysdown"
        with ctx.expect_page() as popup:
            pg.click("[data-ide-newwin]")
        assert popup.value.url.endswith("/ide/?folder=/home/u/flysdown")
        popup.value.close()
        n = sum(1 for s in pg.seen if s.startswith("/ide/"))
        pg.click("[data-ide-reload]")
        _settle(pg)
        assert sum(1 for s in pg.seen if s.startswith("/ide/")) > n
        _click(pg, ".ide-panel .bg-close")
        assert pg.evaluate("!document.querySelector('.ide-backdrop')")
    finally:
        ctx.close()


def test_phones_get_a_hint_but_can_still_use_it(browser):
    ctx, pg = _ctx(browser, html_class="ui-classic", width=390, height=844)
    try:
        _boot(pg, workspace=False)
        _click(pg, "#tool-code-btn")
        assert pg.is_visible(".ide-phone-hint")
        _click(pg, ".ide-project[data-i='0']")
        box = pg.eval_on_selector(".ide-frame", "f => { const r = f.getBoundingClientRect(); return [r.width, r.height]; }")
        assert box[0] >= 380 and box[1] > 600
    finally:
        ctx.close()


def test_non_admins_do_not_see_the_entry(browser):
    ctx, pg = _ctx(browser, admin=False)
    try:
        _boot(pg)
        assert pg.evaluate("document.getElementById('tool-code-btn').style.display") == "none"
    finally:
        ctx.close()
