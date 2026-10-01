"""Ctrl+K searches everything (static/js/search-chat.js, searchEverything.js).

Asked for 2026-09-30: "when i press ctl k it should aso search settings not
just chats, like search everything, not just chats, like library settings,
etc". What matters:

  * one box finds pages, settings, chats, Library documents, notes, tasks,
    calendar events, Brain memories, Gallery images, research reports,
    email and skills, each in its own group, in a fixed order;
  * every source asks the owner-scoped route its own page uses (no new
    endpoint), and the list-only ones (notes, tasks, calendar) are fetched
    once per opening, not per keystroke;
  * local groups show at once and the rest fill in as they answer; a slow
    source doesn't hold the others up, and one past its time limit is dropped;
  * a few rows per group, then "Show N more";
  * arrow keys move across groups and Enter opens; "n: ..." searches notes
    only;
  * each result opens where its link in a chat would (the note, the
    document, the event, ...), a memory opens Brain at that memory;
  * with nothing typed: recent chats and the pages.

The real modules run in Chromium; the API routes are answered by the test
and every request is recorded. chatRenderer.js is a stand-in that records
which link it was asked to open.
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "static"
_INDEX = (_STATIC / "index.html").read_text()


def _sidebar_markup():
    start = _INDEX.index('<nav class="sidebar" id="sidebar"')
    return _INDEX[start:_INDEX.index("</nav>", start) + len("</nav>")]


_PAGE = """<!doctype html><html class="__CLASSES__"><head>
<link rel="stylesheet" href="/static/style.css">__STUDIO__</head><body>
__SIDEBAR__
<main id="chat-container" class="chat-container" style="flex:1"><textarea id="message"></textarea></main>
<div class="search-overlay hidden" id="search-overlay"><div class="search-popup">
  <input type="text" id="search-input"><div class="search-results" id="search-results"></div></div></div>
<script>
  document.body.style.cssText = 'display:flex;margin:0;height:100vh';
  window._isAdmin = true;
  window.calls = [];
  window.sessionModule = { cur: null,
    list: [{id: 's1', name: 'Trip planning', last_message_at: '2026-09-30T10:00:00Z'},
           {id: 's2', name: 'Old chat', last_message_at: '2026-09-01T10:00:00Z'}],
    getCurrentSessionId() { return this.cur; }, getSessions() { return this.list; },
    selectSession(id) { this.cur = id; calls.push(['select', id]); } };
  document.querySelectorAll('#tools-section .list-item').forEach(b => b.addEventListener('click', () => calls.push(b.id)));
  // Brain: opening it lists the memories, like memory.js does.
  document.getElementById('tool-memory-btn').addEventListener('click', () => {
    const m = document.createElement('div');
    m.innerHTML = '<div class="memory-item" data-memory-id="m1">Grows tomatoes</div>';
    document.body.appendChild(m);
  });
</script></body></html>"""

_STUBS = {
    "/static/js/chatRenderer.js": "export function openEntityHash(h) { (window.calls ||= []).push(['hash', h]); return true; }",
    "/static/js/ui.js": ("export function showToast() {} export default { showToast() {}, "
                         "esc: s => String(s).replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';') };"),
    "/static/js/sessions.js": "export default { selectSession(id) { (window.calls ||= []).push(['select', id]); } };",
    "/static/js/modalSnap.js": "export function clearRightDock() {}",
}

# What each owner-scoped route answers for "tomato".
_DATA = {
    "/api/search": [{"message_id": 1, "session_id": "s1", "session_name": "Trip planning", "role": "user",
                     "content_snippet": "buy tomato seeds", "timestamp": "2026-09-30T10:00:00Z"}],
    "/api/documents/library": {"documents": [{"id": f"d{i}", "title": f"Tomato guide {i}", "preview": "Stake them early",
                                              "updated_at": "2026-09-29T10:00:00Z"} for i in range(1, 6)]},
    "/api/notes": {"notes": [{"id": "n1", "title": "Garden plan", "content": "Plant tomatoes by the fence"},
                             {"id": "n2", "title": "Groceries", "content": "", "items": [{"text": "tomato"}, {"text": "milk"}]},
                             {"id": "n3", "title": "Unrelated", "content": "nothing here"},
                             {"id": "n4", "title": "Tomato old", "content": "", "archived": True}]},
    "/api/tasks": {"tasks": [{"id": "t1", "name": "Water the tomatoes", "prompt": "Remind me", "status": "active"},
                             {"id": "t2", "name": "Inbox digest", "prompt": "Summarize mail"}]},
    "/api/calendar/events": {"events": [
        {"uid": "e1", "summary": "Tomato swap", "dtstart": "2026-10-03T10:00:00", "location": "Garden"},
        {"uid": "e1", "summary": "Tomato swap", "dtstart": "2026-10-10T10:00:00", "location": "Garden"},
        {"uid": "e2", "summary": "Dentist", "dtstart": "2026-10-04T10:00:00"}]},
    "/api/memory/search": {"memories": [{"id": "m1", "text": "Grows tomatoes on the balcony", "category": "fact"}]},
    "/api/gallery/library": {"items": [{"id": "g1", "prompt": "a tomato plant", "url": "/api/generated-image/x.png"}]},
    "/api/research/library": {"research": [{"id": "r1", "query": "Best tomato varieties", "status": "complete", "source_count": 12}]},
    "/api/email/search": {"emails": [{"uid": "u7", "subject": "Your tomato order", "from_name": "Seed Co"}]},
    "/api/skills/search": {"skills": [{"name": "garden-helper", "description": "Tomato care"}]},
}

_ORDER = ["Chats", "Library", "Notes", "Tasks", "Calendar", "Brain", "Gallery", "Research", "Email", "Skills"]


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def _make_page(browser, design="classic", delay=None):
    """`delay`: {path: seconds} to answer a route late."""
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    pg = ctx.new_page()
    pg.reqs = []
    studio = "" if design == "classic" else (
        '<link rel="stylesheet" href="/static/css/studio.css"><link rel="stylesheet" href="/static/css/studio-overlays.css">'
        '<link rel="stylesheet" href="/static/css/workspace.css">')
    classes = {"classic": "ui-classic", "studio": "ui-studio", "workspace": "ui-workspace ui-studio"}[design]
    html = _PAGE.replace("__CLASSES__", classes).replace("__STUDIO__", studio).replace("__SIDEBAR__", _sidebar_markup())

    def route(r):
        u = urlparse(r.request.url)
        path = u.path
        if path in ("", "/"):
            r.fulfill(body=html, content_type="text/html")
        elif path in _STUBS:
            r.fulfill(body=_STUBS[path], content_type="text/javascript")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            if f.suffix not in (".js", ".css") or not f.exists():
                r.fulfill(status=404, body="")
                return
            r.fulfill(body=f.read_text(), content_type="text/css" if f.suffix == ".css" else "text/javascript")
        elif path in _DATA:
            pg.reqs.append((r.request.method, path, parse_qs(u.query), r.request.post_data))
            r.fulfill(body=json.dumps(_DATA[path]), content_type="application/json")
        elif path.startswith("/api/"):
            r.fulfill(body="{}", content_type="application/json")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    if delay:
        # Answer some routes late, in the page (a sleeping route handler
        # would stall every other request too).
        ctx.add_init_script("""(() => { const d = %s, f = window.fetch;
          window.fetch = (u, o) => { const p = new URL(u, location.href).pathname;
            return d[p] ? new Promise(r => setTimeout(r, d[p] * 1000)).then(() => f(u, o)) : f(u, o); }; })()""" % json.dumps(delay))
    pg.goto("https://example.test/")
    pg.evaluate("""() => import('/static/js/search-chat.js').then(m => { m.init(''); window.__search = m; })""")
    pg.wait_for_function("() => window.__search")
    if design == "workspace":
        pg.evaluate("() => import('/static/js/workspace/shell.js')")
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
    return pg.evaluate("""() => [...document.querySelectorAll('#search-results .search-group')].map(g => [
      g.querySelector('.search-group-header').textContent,
      [...g.querySelectorAll('.search-result-item')].map(i => i.querySelector('.search-result-snippet').textContent.replace(/\\s+/g, ' ').trim())])""")


def _search(pg, q, wait_for="Skills"):
    pg.evaluate("window.__search.openSearch()")
    pg.fill("#search-input", q)
    if wait_for:
        pg.wait_for_function(f"() => [...document.querySelectorAll('.search-group-header')].some(h => h.textContent === {json.dumps(wait_for)})")
        pg.wait_for_function("() => !document.querySelector('.search-pending')")


# ── Everything, grouped ─────────────────────────────────────────────────

@pytest.mark.parametrize("design", ["classic", "studio", "workspace"])
def test_one_box_finds_everything_in_groups(make_page, design):
    pg = make_page(design)
    _search(pg, "tomato")
    gs = dict(_groups(pg))
    assert [h for h, _ in _groups(pg)] == _ORDER
    assert gs["Library"] == ["Tomato guide 1 Stake them early", "Tomato guide 2 Stake them early",
                             "Tomato guide 3 Stake them early", "Show 2 more"]
    assert gs["Notes"] == ["Garden plan Plant tomatoes by the fence", "Groceries"]   # not the archived one
    assert gs["Tasks"] == ["Water the tomatoes Remind me"]
    assert gs["Calendar"] == ["Tomato swap Garden"], "a recurring event is listed once"
    assert gs["Chats"] == ["Trip planning buy tomato seeds"]
    assert gs["Email"] == ["Your tomato order Seed Co"]
    assert pg.is_visible("#search-results .search-result-item")


def test_each_source_asks_its_own_route_and_lists_load_once(make_page):
    pg = make_page()
    _search(pg, "tomato")
    pg.fill("#search-input", "tomatoes")
    pg.wait_for_timeout(700)
    seen = {}
    for method, path, qs, body in pg.reqs:
        seen.setdefault(path, []).append((method, qs, body))
    assert seen["/api/documents/library"][0][1]["search"] == ["tomato"]
    assert seen["/api/gallery/library"][0][1]["search"] == ["tomato"]
    assert seen["/api/research/library"][0][1]["search"] == ["tomato"]
    assert seen["/api/email/search"][0][1]["q"] == ["tomato"]
    assert seen["/api/search"][0][1]["q"] == ["tomato"]
    assert seen["/api/memory/search"][0][0] == "POST" and "query=tomato" in seen["/api/memory/search"][0][2]
    assert seen["/api/skills/search"][0][0] == "POST" and json.loads(seen["/api/skills/search"][0][2]) == {"query": "tomato"}
    # No search parameter on these routes: fetched once per opening, matched here.
    for path in ("/api/notes", "/api/tasks", "/api/calendar/events"):
        assert len(seen[path]) == 1, path
    assert {"start", "end"} <= set(seen["/api/calendar/events"][0][1])
    # Searched again for the second query.
    assert len(seen["/api/documents/library"]) == 2


def test_a_slow_source_doesnt_hold_the_others_up(make_page):
    pg = make_page(delay={"/api/documents/library": 2.0})
    pg.evaluate("window.__search.openSearch()")
    pg.fill("#search-input", "tomato")
    pg.wait_for_function("() => [...document.querySelectorAll('.search-group-header')].some(h => h.textContent === 'Notes')", timeout=1500)
    assert "Library" in pg.inner_text(".search-pending")
    assert "Library" not in [h for h, _ in _groups(pg)]
    pg.wait_for_function("() => [...document.querySelectorAll('.search-group-header')].some(h => h.textContent === 'Library')", timeout=5000)
    # ...and it lands in its own place, not at the end.
    assert [h for h, _ in _groups(pg)] == _ORDER


def test_a_source_past_its_time_limit_is_dropped(make_page):
    pg = make_page(delay={"/api/email/search": 3.0})
    pg.evaluate("() => import('/static/js/searchEverything.js').then(m => { m.SOURCES.find(s => s.key === 'email').timeout = 400; })")
    _search(pg, "tomato", wait_for="Skills")
    assert "Email" not in [h for h, _ in _groups(pg)]
    assert not pg.is_visible(".search-pending")


def test_show_more_opens_the_rest_of_a_group(make_page):
    pg = make_page()
    _search(pg, "tomato")
    pg.click(".search-group[data-group='documents'] .search-result-more")
    lib = dict(_groups(pg))["Library"]
    assert len(lib) == 5 and "Show" not in lib[-1]
    assert pg.evaluate("document.activeElement.id") == "search-input"


# ── Keyboard and prefixes ───────────────────────────────────────────────

def test_arrow_keys_move_across_groups_and_enter_opens(make_page):
    pg = make_page()
    _search(pg, "tomato")
    # Chats (1), Library (3 + more), Notes: the 6th row is the first note.
    for _ in range(6):
        pg.press("#search-input", "ArrowDown")
    assert pg.evaluate("document.querySelector('.search-result-item.selected').dataset.row") == "notes:0"
    pg.press("#search-input", "ArrowUp")
    assert pg.evaluate("document.querySelector('.search-result-item.selected').dataset.row") == "more:documents"
    pg.press("#search-input", "ArrowDown")
    pg.press("#search-input", "Enter")
    pg.wait_for_function("() => calls.some(c => c[0] === 'hash' && c[1] === '#note-n1')")
    assert pg.evaluate("document.getElementById('search-overlay').classList.contains('hidden')")


def test_a_late_group_doesnt_move_the_selection(make_page):
    pg = make_page(delay={"/api/search": 1.2})
    pg.evaluate("window.__search.openSearch()")
    pg.fill("#search-input", "tomato")
    pg.wait_for_function("() => document.querySelector('.search-group[data-group=\"notes\"]')")
    pg.press("#search-input", "ArrowDown")
    first = pg.evaluate("document.querySelector('.search-result-item.selected').dataset.row")
    pg.wait_for_function("() => document.querySelector('.search-group[data-group=\"chats\"]')", timeout=4000)
    assert pg.evaluate("document.querySelector('.search-result-item.selected').dataset.row") == first


def test_a_prefix_searches_one_group(make_page):
    pg = make_page()
    _search(pg, "n: tomato", wait_for="Notes")
    assert [h for h, _ in _groups(pg)] == ["Notes"]
    assert {p for _, p, _, _ in pg.reqs} == {"/api/notes"}
    pg.fill("#search-input", "s: voice")
    pg.wait_for_timeout(400)
    assert all(h == "Settings" for h, _ in _groups(pg))


def test_parse_query(make_page):
    pg = make_page()
    got = pg.evaluate("""() => ['n: groceries', 'd:guide', 'mail: invoice', 'e: dentist', 'what: is this', 'plain words']
        .map(t => window.__search.parseQuery(t))""")
    assert got == [{"q": "groceries", "only": "notes"}, {"q": "guide", "only": "documents"},
                   {"q": "invoice", "only": "email"}, {"q": "dentist", "only": "events"},
                   {"q": "what: is this", "only": None}, {"q": "plain words", "only": None}]


# ── Opening each kind ───────────────────────────────────────────────────

@pytest.mark.parametrize("group,expect", [
    ("documents", ["hash", "#document-d1"]), ("notes", ["hash", "#note-n1"]), ("tasks", ["hash", "#task-t1"]),
    ("events", ["hash", "#event-e1"]), ("gallery", ["hash", "#image-g1"]), ("research", ["hash", "#research-r1"]),
    ("email", ["hash", "#email-u7"]), ("skills", ["hash", "#skill-garden-helper"]), ("chats", ["select", "s1"]),
])
def test_each_result_opens_where_its_link_would(make_page, group, expect):
    pg = make_page()
    _search(pg, "tomato")
    pg.click(f".search-result-item[data-row='{group}:0']")
    pg.wait_for_function(f"() => calls.some(c => JSON.stringify(c) === {json.dumps(json.dumps(expect, separators=(",", ":")))})")


def test_a_memory_opens_brain_at_that_memory(make_page):
    pg = make_page()
    _search(pg, "tomato")
    pg.click(".search-result-item[data-row='memories:0']")
    pg.wait_for_function("() => document.querySelector('.memory-item[data-memory-id=\"m1\"].settings-goto-flash')")
    assert "tool-memory-btn" in pg.evaluate("calls")


def test_pages_and_settings_still_come_first(make_page):
    pg = make_page()
    pg.evaluate("window.__search.openSearch()")
    pg.fill("#search-input", "terminal")
    pg.wait_for_timeout(200)
    assert _groups(pg)[0] == ["Pages", ["Terminal page"]]
    pg.press("#search-input", "Enter")
    pg.wait_for_function("() => calls.includes('tool-terminal-btn')")


# ── Nothing typed ───────────────────────────────────────────────────────

def test_with_nothing_typed_recent_chats_and_pages(make_page):
    pg = make_page()
    pg.evaluate("window.__search.openSearch()")
    gs = _groups(pg)
    assert [h for h, _ in gs] == ["Pages", "Recent chats"]
    assert gs[1][1] == ["Trip planning", "Old chat"]
    assert gs[0][1][:3] == ["Calendar Organize", "Tasks Organize", "Notes Organize"]
    assert "n:" in pg.inner_text(".search-hint")
    assert pg.reqs == [], "nothing is asked of the server until something is typed"
    pg.press("#search-input", "ArrowDown")
    pg.press("#search-input", "Enter")
    pg.wait_for_function("() => calls.includes('tool-calendar-btn')")
