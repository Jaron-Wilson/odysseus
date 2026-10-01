"""The What's new page (static/js/whatsNew.js) in the page.

Asked for on 2026-10-01: "add a whats new page? on it? and then basically for
each pr i can see then ask questions too." What matters here:

  * it lists the merged PRs newest first, by day, with New and Running
    markers and the "merged but not running yet" banner;
  * an entry expands to its description rendered as (sanitized) markdown and
    its files;
  * the search box and the New / Not running chips filter the list;
  * "Ask about this" (one, or several ticked) asks the server for a chat,
    opens it and leaves "Ask about #N ..." in the composer, without sending;
  * the sidebar entry has a dot while there are unseen merges, and opening
    the page clears it;
  * in Workspace it is a tab, and stays one when a chat is opened from it.

The real whatsNew.js, markdown.js, toolGroups.js and shell.js run in
Chromium against a small page; the API is answered by the test.
"""
import json
import re
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "static"
_INDEX = (_STATIC / "index.html").read_text()

_NOW = 1790000000


def _entry(pr, title, *, hours_ago, running=True, new=False, body="", files=None):
    files = files or [{"path": "src/x.py", "additions": 3, "deletions": 1, "binary": False}]
    t = _NOW - hours_ago * 3600
    return {"pr": pr, "title": title, "summary": f"Summary of {title}.", "summary_source": "ai", "body": body,
            "has_body": bool(body), "merged_at": "", "time": t, "author": "Jaron-Wilson", "branch": f"b{pr}",
            "sha": f"{pr:040d}", "short": f"{pr:08d}", "url": f"https://github.com/Jaron-Wilson/odysseus/pull/{pr}",
            "files": files, "file_count": len(files), "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files), "running": running, "new": new, "from_github": True}


_DATA = {
    "entries": [
        _entry(12, "Phone calls: call a number", hours_ago=1, running=False, new=True,
               body="## Why\n\nYou can **call** Odysseus.\n\n<img src=x onerror=\"window.__xss=1\">\n<script>window.__xss=2</script>",
               files=[{"path": "src/telephony/call.py", "additions": 378, "deletions": 0, "binary": False},
                      {"path": "static/js/devicesSettings.js", "additions": 131, "deletions": 0, "binary": False}]),
        _entry(11, "Theme colors", hours_ago=2, new=True),
        _entry(10, "Voice call: talk to the agent", hours_ago=60),
    ],
    "repo": "Jaron-Wilson/odysseus", "running_commit": "00000011", "head_commit": "00000012", "running_pr": 11,
    "started": 1, "pending": [12], "restart_needed": True,
    "github": {"fetched": _NOW - 300, "error": "", "token": False}, "last_seen": 0, "new_after": 0, "new_count": 2,
}


def _sidebar_markup():
    start = _INDEX.index('<nav class="sidebar" id="sidebar"')
    return _INDEX[start:_INDEX.index("</nav>", start) + len("</nav>")]


_PAGE = """<!doctype html><html class="__CLASSES__"><head>
<link rel="stylesheet" href="/static/style.css">
__STUDIO__
<link rel="stylesheet" href="/static/css/whatsNew.css"></head><body>
__SIDEBAR__
<button id="rail-new-session">+</button>
<main id="chat-container" class="chat-container" style="flex:1;min-width:0;display:flex;flex-direction:column;justify-content:flex-end">
  <div id="chat-box">chat</div>
  <div class="chat-input-bar"><textarea id="message" placeholder="Message Odysseus"></textarea></div>
</main>
<script>
  document.body.style.cssText = 'display:flex;margin:0;height:100vh';
  document.getElementById('sidebar').style.cssText += ';width:240px;flex:none;position:relative;transform:none;left:0';
  window.calls = [];
  window._isAdmin = true;
  window.sessionModule = { cur: null, list: [], getCurrentSessionId() { return this.cur; },
    getSessions() { return this.list; },
    async loadSessions() { calls.push(['load']); },
    async selectSession(id) { calls.push(['select', id]); this.cur = id; } };
  Date.now = () => __NOW__ * 1000;
</script>
<script type="module" src="/static/js/whatsNew.js"></script>
</body></html>"""

_STUBS = {
    "/static/js/ui.js": "export default { esc: s => String(s ?? '').replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';'), showToast(m) { (window.toasts ||= []).push(m); } };",
    "/static/js/chatRenderer.js": "export function openEntityHash() { return false; }",
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


def _make(browser, design="classic", unseen=2, width=1400):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    pg = ctx.new_page()
    studio = "" if design == "classic" else (
        '<link rel="stylesheet" href="/static/css/studio.css">'
        '<link rel="stylesheet" href="/static/css/studio-pages-b.css">'
        '<link rel="stylesheet" href="/static/css/workspace.css">')
    cls = {"classic": "ui-classic", "studio": "ui-studio", "workspace": "ui-workspace ui-studio"}[design]
    html = (_PAGE.replace("__CLASSES__", cls).replace("__STUDIO__", studio)
            .replace("__SIDEBAR__", _sidebar_markup()).replace("__NOW__", str(_NOW)))
    api = {"posts": [], "unseen": unseen}

    def route(r):
        path = r.request.url.split("example.test", 1)[-1].split("?", 1)[0]
        method = r.request.method
        if path in ("", "/"):
            r.fulfill(body=html, content_type="text/html")
        elif path in _STUBS:
            r.fulfill(body=_STUBS[path], content_type="text/javascript")
        elif path == "/static/js/modalSnap.js" and design != "workspace":
            r.fulfill(body="export function clearRightDock() {}", content_type="text/javascript")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            if f.suffix not in (".js", ".css") or not f.is_file():
                r.fulfill(status=404, body="")
            else:
                r.fulfill(body=f.read_text(), content_type="text/css" if f.suffix == ".css" else "text/javascript")
        elif path == "/api/whats-new":
            r.fulfill(body=json.dumps(_DATA), content_type="application/json")
        elif path == "/api/whats-new/unseen":
            r.fulfill(body=json.dumps({"count": api["unseen"]}), content_type="application/json")
        elif path == "/api/whats-new/seen":
            api["posts"].append(("seen", None))
            api["unseen"] = 0
            r.fulfill(body=json.dumps({"last_seen": _NOW}), content_type="application/json")
        elif path == "/api/whats-new/ask" and method == "POST":
            prs = json.loads(r.request.post_data)["prs"]
            api["posts"].append(("ask", prs))
            ph = (f"Ask about #{prs[0]} {next(e['title'] for e in _DATA['entries'] if e['pr'] == prs[0])}..."
                  if len(prs) == 1 else "Ask about " + ", ".join(f"#{n}" for n in prs) + "...")
            r.fulfill(body=json.dumps({"id": f"chat-{'-'.join(map(str, prs))}", "name": "About", "prs": prs,
                                       "placeholder": ph}), content_type="application/json")
        elif path.startswith("/api/"):
            r.fulfill(body=json.dumps({}), content_type="application/json")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    pg.goto("https://example.test/")
    pg.wait_for_function("() => window.whatsNew")
    pg.wait_for_timeout(300)
    return ctx, pg, api


def _open(pg):
    pg.click("#tool-whats-new-btn")
    pg.wait_for_selector(".wn-item")
    pg.wait_for_timeout(200)


def _items(pg):
    return pg.eval_on_selector_all(".wn-item", "els => els.filter(e => e.getClientRects().length).map(e => +e.dataset.pr)")


@pytest.mark.parametrize("design", ["classic", "studio"])
def test_the_list_markers_and_banner(browser, design):
    ctx, pg, api = _make(browser, design)
    try:
        assert pg.is_visible("#whats-new-dot")                       # 2 unseen
        _open(pg)
        assert _items(pg) == [12, 11, 10]
        days = pg.eval_on_selector_all(".wn-day", "els => els.map(e => e.textContent)")
        assert days[0] == "Today" and len(days) == 2
        assert pg.inner_text(".wn-item[data-pr='12'] .wn-pending") == "Not running yet"
        assert pg.inner_text(".wn-item[data-pr='11'] .wn-running") == "Running"
        assert pg.eval_on_selector_all(".wn-item.is-new", "els => els.map(e => +e.dataset.pr)") == [12, 11]
        assert "1 change is merged but not running yet: restart Odysseus to load it." in pg.inner_text(".wn-banner")
        assert "Summary of Theme colors." in pg.inner_text(".wn-item[data-pr='11']")
        assert pg.get_attribute(".wn-item[data-pr='12'] a.wn-link", "href").endswith("/pull/12")
        # Opening it marks everything seen: the dot goes, the markers stay for this visit.
        pg.wait_for_function("() => document.getElementById('whats-new-dot').style.display === 'none'")
        assert ("seen", None) in api["posts"] and pg.locator(".wn-item.is-new").count() == 2
        # A page, not a popup: in Classic it fills the chat column.
        box = pg.locator(".wn-panel").bounding_box()
        assert box["width"] > 900 and box["height"] > 800
    finally:
        ctx.close()


def test_details_render_sanitized_markdown_and_files(browser):
    ctx, pg, api = _make(browser)
    try:
        _open(pg)
        pg.click(".wn-item[data-pr='12'] .wn-title")
        pg.wait_for_selector(".wn-item[data-pr='12'] .wn-detail")
        body = pg.locator(".wn-item[data-pr='12'] .wn-body")
        assert body.locator("strong").inner_text() == "call"
        assert body.locator("script").count() == 0 and body.locator("img[onerror]").count() == 0
        pg.wait_for_timeout(200)
        assert pg.evaluate("window.__xss") is None
        pg.click(".wn-item[data-pr='12'] .wn-files summary")
        files = pg.eval_on_selector_all(".wn-item[data-pr='12'] .wn-files li code", "els => els.map(e => e.textContent)")
        assert files == ["src/telephony/call.py", "static/js/devicesSettings.js"]
        assert "+509" in pg.inner_text(".wn-item[data-pr='12'] .wn-files summary")
        pg.click(".wn-item[data-pr='12'] [data-toggle].wn-btn")              # Hide details
        assert pg.locator(".wn-item[data-pr='12'] .wn-detail").count() == 0
    finally:
        ctx.close()


def test_search_and_filters(browser):
    ctx, pg, api = _make(browser)
    try:
        _open(pg)
        pg.fill(".wn-search", "telephony")                    # a file name
        assert _items(pg) == [12]
        pg.fill(".wn-search", "#10")
        assert _items(pg) == [10]
        pg.fill(".wn-search", "")
        pg.click(".wn-chip[data-filter='pending']")
        assert _items(pg) == [12] and pg.inner_text(".wn-count") == "1 of 3"
        pg.click(".wn-chip[data-filter='new']")
        assert _items(pg) == [12, 11]
        pg.click(".wn-chip[data-filter='all']")
        pg.fill(".wn-search", "nothing like this")
        assert _items(pg) == [] and "Nothing matches" in pg.inner_text(".wn-list")
    finally:
        ctx.close()


def test_ask_opens_a_chat_ready_with_a_placeholder(browser):
    ctx, pg, api = _make(browser)
    try:
        _open(pg)
        pg.click(".wn-item[data-pr='10'] [data-ask]")
        pg.wait_for_function("() => calls.some(c => c[0] === 'select')")
        assert ("ask", [10]) in api["posts"]
        assert pg.evaluate("calls") == [["load"], ["select", "chat-10"]]
        assert pg.get_attribute("#message", "placeholder") == "Ask about #10 Voice call: talk to the agent..."
        assert pg.input_value("#message") == ""                             # nothing typed or sent
        assert pg.locator(".wn-backdrop").count() == 0                      # Classic: out of the way
        # Another chat gets its own placeholder back; this one keeps it.
        pg.evaluate("sessionModule.cur = 'other'")
        pg.wait_for_function("() => document.getElementById('message').placeholder === 'Message Odysseus'")
        pg.evaluate("sessionModule.cur = 'chat-10'")
        pg.wait_for_function("() => document.getElementById('message').placeholder.startsWith('Ask about #10')")
        # Several at once.
        _open(pg)
        assert pg.is_hidden(".wn-selbar")
        pg.check(".wn-item[data-pr='12'] [data-pick]")
        pg.check(".wn-item[data-pr='10'] [data-pick]")
        assert pg.inner_text(".wn-selcount") == "2 selected"
        pg.click(".wn-ask-picked")
        pg.wait_for_function("() => calls.some(c => c[1] === 'chat-12-10')")
        assert ("ask", [12, 10]) in api["posts"]
        assert pg.get_attribute("#message", "placeholder") == "Ask about #12, #10..."
    finally:
        ctx.close()


def test_the_dot_shows_on_a_folded_group(browser):
    ctx, pg, api = _make(browser, unseen=3)
    try:
        pg.evaluate("() => import('/static/js/toolGroups.js').then(m => { m.init(); m.setOpen('system', false); m.refresh(); })")
        pg.wait_for_timeout(200)
        assert "has-badge" in pg.get_attribute(".tool-group[data-tool-group='system']", "class")
        assert pg.evaluate("document.getElementById('whats-new-dot').style.display") == ""
    finally:
        ctx.close()


def test_no_dot_when_nothing_is_new(browser):
    ctx, pg, api = _make(browser, unseen=0)
    try:
        assert pg.evaluate("document.getElementById('whats-new-dot').style.display") == "none"
    finally:
        ctx.close()


def test_workspace_tab_and_ask_keeps_it(browser):
    ctx, pg, api = _make(browser, "workspace")
    try:
        pg.evaluate("() => import('/static/js/workspace/shell.js').then(m => { window.__ws = m; })")
        pg.wait_for_function("() => window.__ws && document.getElementById('ws-tabbar')")
        pg.wait_for_timeout(300)
        pg.evaluate("() => window.toolPages.openPage('whats-new')")
        pg.wait_for_selector(".wn-item")
        pg.wait_for_timeout(400)
        tabs = pg.eval_on_selector_all(".ws-tab", "els => els.map(t => (t.classList.contains('active') ? '*' : '') + t.dataset.tab)")
        assert "*tool:whats-new" in tabs
        assert "ws-docked" in pg.get_attribute(".wn-backdrop", "class")
        assert re.search(r"What.s new", pg.inner_text(".ws-tab[data-tab='tool:whats-new']"))
        pg.click(".wn-item[data-pr='11'] [data-ask]")
        pg.wait_for_function("() => calls.some(c => c[0] === 'select')")
        assert pg.locator(".wn-backdrop").count() == 1                      # still a tab
    finally:
        ctx.close()
