"""The Workspace interface's tab shell (static/js/workspace/shell.js).

Asked for 2026-09-30: a redesign of "everything layout of ui how ui interacts
differnt ui types etc.", with tabs like a browser and a dashboard Home. The
shell doesn't rewrite the tools; it notices their windows and turns them into
tab pages. What matters to the user:

  * opening a tool from the sidebar opens it as a tab in front, and Home or
    another tab can come in front without closing it (its state survives);
  * the sidebar button of a tool that's already open brings its tab back
    rather than toggling the tool shut;
  * a tool's own close button closes its tab, and closing the tab closes
    the tool;
  * each chat is a tab, and switching tabs switches the chat;
  * split view shows two tabs side by side, with the chat narrowed to its
    half;
  * tabs survive a reload, and a tool tab reopens its tool when picked;
  * switching to another Interface design takes the shell away cleanly;
  * the Home composer starts a new chat with what was typed;
  * the scroll-to-bottom button (outside <main>) hides when no chat is on
    screen, and is told to re-measure when the chat moves (reported
    2026-09-30: it showed "randomly" over Home and tool pages).

The real shell.js and home.js run in Chromium against a small page with
stand-in tools and a stand-in session module; only modalSnap.js and
chatRenderer.js are stubbed.
"""
import json
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_STATIC = Path(__file__).resolve().parent.parent / "static"

_PAGE = """<!doctype html><html class="ui-workspace ui-studio"><head>
<link rel="stylesheet" href="/static/css/workspace.css"></head><body>
<nav id="sidebar">
  <div id="sidebar-new-chat-btn" class="list-item">New chat</div>
  <div id="session-list">
    <div class="list-item" data-session-id="s1">Chat one</div>
    <div class="list-item" data-session-id="s2">Chat two</div>
  </div>
  <button id="tool-calendar-btn">Calendar</button>
  <button id="tool-notes-btn">Notes</button>
  <button id="tool-terminal-btn">Terminal</button>
  <span id="user-bar-name">Jaron Wilson</span>
</nav>
<button id="rail-new-session">+</button>
<main id="chat-container" class="chat-container" style="flex:1;min-width:0">
  <textarea id="message"></textarea>
  <form id="chat-form"></form>
</main>
<button id="scroll-bottom-btn" class="scroll-nav-btn show" style="position:fixed;bottom:100px;right:20px">v</button>
<script>
  document.body.style.cssText = 'display:flex;margin:0;height:100vh';
  document.getElementById('sidebar').style.cssText = 'width:240px;flex:none';
  // Stand-in session module: the calls the shell makes, recorded.
  window.calls = [];
  window.sessionModule = {
    cur: null,
    list: [{id: 's1', name: 'Chat one', last_message_at: '2026-09-30T10:00:00Z'},
           {id: 's2', name: 'Chat two', last_message_at: '2026-09-30T09:00:00Z'}],
    getCurrentSessionId() { return this.cur; },
    getSessions() { return this.list; },
    selectSession(id) { this.cur = id; calls.push(['select', id]); },
  };
  document.getElementById('rail-new-session').addEventListener('click', () => {
    sessionModule.cur = null; calls.push(['new']);
    document.getElementById('chat-container').classList.add('welcome-active');
  });
  document.getElementById('session-list').addEventListener('click', e => {
    const row = e.target.closest('[data-session-id]');
    if (row) sessionModule.selectSession(row.dataset.sessionId);
  });
  document.getElementById('chat-form').addEventListener('submit', e => {
    e.preventDefault(); calls.push(['send', document.getElementById('message').value]);
  });
  // Calendar: a static modal toggled with .hidden, like the real one.
  const cal = document.createElement('div');
  cal.id = 'calendar-modal'; cal.className = 'modal hidden';
  cal.innerHTML = '<div class="modal-content"><div class="modal-header">Calendar' +
                  '<button class="close-btn">x</button></div><input id="cal-state"></div>';
  document.body.appendChild(cal);
  cal.querySelector('.close-btn').addEventListener('click', () => cal.classList.add('hidden'));
  document.getElementById('tool-calendar-btn').addEventListener('click', () => {
    calls.push(['calendar-toggle']);
    cal.classList.toggle('hidden');
  });
  // Notes: created on open and removed on close, like the real pane.
  document.getElementById('tool-notes-btn').addEventListener('click', () => {
    const open = document.getElementById('notes-pane');
    if (open) { open.remove(); return; }
    const p = document.createElement('div');
    p.id = 'notes-pane'; p.className = 'notes-pane';
    p.innerHTML = '<div class="notes-pane-header">Notes<button class="notes-close-btn">x</button></div>';
    p.querySelector('.notes-close-btn').addEventListener('click', () => p.remove());
    document.body.appendChild(p);
  });
  // Terminal: a backdrop with a panel in it, appended on open, like bgPanel.js.
  document.getElementById('tool-terminal-btn').addEventListener('click', () => {
    const b = document.createElement('div');
    b.className = 'bg-panel-backdrop term-backdrop';
    b.innerHTML = '<div class="bg-panel"><div class="bg-panel-head">Terminal' +
                  '<button class="bg-close">x</button></div></div>';
    b.querySelector('.bg-close').addEventListener('click', () => b.remove());
    document.body.appendChild(b);
  });
</script></body></html>"""

# The floating-window look the tools have outside Workspace (style.css): a
# dimmed full-screen backdrop and a centered window that scales and fades in.
_WINDOW_CSS = """<style>
.modal, .bg-panel-backdrop { position: fixed; inset: 0; z-index: 250; display: flex;
  align-items: center; justify-content: center; background: rgba(0,0,0,.5); }
.modal.hidden { display: none; }
.modal-content, .bg-panel { width: 520px; height: 300px; background: #222;
  animation: stub-enter .25s ease-out both; transition: left .3s, top .3s, width .3s, height .3s; }
.notes-pane { position: fixed; top: 20px; right: 20px; width: 400px; height: 600px; background: #222; }
@keyframes stub-enter { from { opacity: 0; transform: scale(.95) translateY(8px); } }
</style>"""

_MODALSNAP_STUB = "export function clearRightDock() {}"
_RENDERER_STUB = "export function openEntityHash(h) { (window.calls ||= []).push(['hash', h]); return true; }"


@pytest.fixture
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()

    def route(r):
        url = r.request.url
        path = url.split("example.test", 1)[-1].split("?", 1)[0]
        if path in ("", "/"):
            r.fulfill(body=pg.html, content_type="text/html")
        elif path == "/static/js/modalSnap.js":
            r.fulfill(body=_MODALSNAP_STUB, content_type="text/javascript")
        elif path == "/static/js/chatRenderer.js":
            r.fulfill(body=_RENDERER_STUB, content_type="text/javascript")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            ctype = "text/css" if f.suffix == ".css" else "text/javascript"
            r.fulfill(body=f.read_text(), content_type=ctype)
        elif path.startswith("/api/"):
            r.fulfill(body=json.dumps({"events": [], "plans": [], "emails": [], "jobs": [], "notes": []}),
                      content_type="application/json")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    pg.html = _PAGE
    pg.boot = lambda: _boot(pg)
    yield pg
    ctx.close()


def _boot(pg):
    pg.goto("https://example.test/")
    pg.evaluate("() => import('/static/js/workspace/shell.js').then(m => { window.__ws = m; })")
    pg.wait_for_function("() => window.__ws && document.getElementById('ws-tabbar')")
    pg.wait_for_timeout(700)                       # first session poll


def _tabs(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('.ws-tab')].map(t =>
        (t.classList.contains('active') ? '*' : '') + t.dataset.tab)""")


def _settle(pg):
    pg.wait_for_timeout(650)


def _click(pg, sel):
    pg.evaluate(f"document.querySelector({json.dumps(sel)}).click()")
    _settle(pg)


def test_home_is_the_first_tab_and_what_you_see_on_first_run(page):
    page.boot()
    assert _tabs(page) == ["*home"]
    assert page.evaluate("!document.getElementById('ws-home').hidden")
    assert "Jaron" in page.inner_text(".ws-greeting")


def test_a_tool_opens_as_a_tab_page_and_keeps_its_state_behind_other_tabs(page):
    page.boot()
    _click(page, "#tool-calendar-btn")
    assert _tabs(page) == ["home", "*tool:calendar"]
    cal = page.locator("#calendar-modal")
    assert "ws-docked" in cal.get_attribute("class") and cal.get_attribute("data-ws-pane") == "left"
    assert page.evaluate("document.getElementById('ws-home').hidden")
    page.fill("#cal-state", "typed in the calendar")

    _click(page, ".ws-tab[data-tab=home]")
    assert _tabs(page) == ["*home", "tool:calendar"]
    assert "hidden" not in cal.get_attribute("class")          # not closed, only behind Home
    assert "ws-away" in cal.get_attribute("class")
    assert not cal.is_visible()

    _click(page, ".ws-tab[data-tab='tool:calendar']")
    assert "ws-away" not in cal.get_attribute("class")
    assert page.input_value("#cal-state") == "typed in the calendar"


def test_the_sidebar_button_of_an_open_tool_brings_its_tab_back(page):
    page.boot()
    _click(page, "#tool-calendar-btn")
    _click(page, ".ws-tab[data-tab=home]")
    _click(page, "#tool-calendar-btn")
    assert _tabs(page) == ["home", "*tool:calendar"]
    assert "hidden" not in page.get_attribute("#calendar-modal", "class")
    # The tool's own toggle ran once (the open), not a second time (a close).
    assert page.evaluate("calls.filter(c => c[0] === 'calendar-toggle').length") == 1


def test_a_tools_own_close_closes_its_tab_and_closing_the_tab_closes_the_tool(page):
    page.boot()
    _click(page, "#tool-calendar-btn")
    _click(page, "#calendar-modal .close-btn")
    assert _tabs(page) == ["*home"]
    assert "ws-docked" not in page.get_attribute("#calendar-modal", "class")

    _click(page, "#tool-notes-btn")
    assert _tabs(page) == ["home", "*tool:notes"]
    _click(page, ".ws-tab[data-tab='tool:notes'] .ws-tab-x")
    assert _tabs(page) == ["*home"]
    assert page.evaluate("!document.getElementById('notes-pane')")


def test_each_chat_is_a_tab_and_switching_tabs_switches_the_chat(page):
    page.boot()
    _click(page, "#session-list [data-session-id=s1]")
    _click(page, "#session-list [data-session-id=s2]")
    assert _tabs(page) == ["home", "chat:s1", "*chat:s2"]
    assert page.inner_text(".ws-tab[data-tab='chat:s1'] .ws-tab-label") == "Chat one"
    assert "ws-no-chat" not in page.evaluate("document.documentElement.className")

    _click(page, ".ws-tab[data-tab='chat:s1']")
    assert page.evaluate("sessionModule.cur") == "s1"
    _click(page, ".ws-tab[data-tab=home]")
    assert "ws-no-chat" in page.evaluate("document.documentElement.className")
    # Clicking the chat that's still current brings its tab back.
    _click(page, "#session-list [data-session-id=s1]")
    assert _tabs(page)[1] == "*chat:s1"


def test_a_thread_opens_under_its_chats_tab_not_as_a_tab_of_its_own(page):
    # Asked for 2026-10-10: "I want threads on the website instead of a new
    # tab, show it like under the tab, so that I don't switch between the 2".
    page.boot()
    page.evaluate("""() => sessionModule.list.push(
      {id: 't1', name: 'Branch of one', parent_session_id: 's1'},
      {id: 't2', name: 'Thread of the branch', parent_session_id: 't1'})""")
    _click(page, "#session-list [data-session-id=s1]")
    page.evaluate("sessionModule.selectSession('t1')")       # chatThreads.js opening a thread
    _settle(page)
    assert _tabs(page) == ["home", "*chat:s1"]
    assert page.inner_text(".ws-tab[data-tab='chat:s1'] .ws-tab-label") == "Chat one"
    page.evaluate("sessionModule.selectSession('t2')")       # a thread of a thread too
    _settle(page)
    assert _tabs(page) == ["home", "*chat:s1"]

    # The tab remembers the thread on screen: away and back, and over a reload.
    _click(page, "#session-list [data-session-id=s2]")
    _click(page, ".ws-tab[data-tab='chat:s1']")
    assert page.evaluate("sessionModule.cur") == "t2"
    page.boot()
    assert page.evaluate("sessionModule.cur") == "t2"
    assert _tabs(page) == ["home", "*chat:s1", "chat:s2"]
    # Back to the main chat stays in the same tab.
    page.evaluate("sessionModule.selectSession('s1')")
    _settle(page)
    assert _tabs(page) == ["home", "*chat:s1", "chat:s2"]


def test_split_view_puts_two_tabs_side_by_side_with_the_chat_narrowed(page):
    page.boot()
    _click(page, "#session-list [data-session-id=s1]")
    _click(page, "#tool-calendar-btn")
    _click(page, ".ws-split-btn")
    html = page.evaluate("document.documentElement.className")
    assert "ws-split" in html
    cal = page.locator("#calendar-modal")
    assert cal.get_attribute("data-ws-pane") == "left" and "ws-away" not in cal.get_attribute("class")
    geo = page.evaluate("""() => {
      const m = document.getElementById('chat-container').getBoundingClientRect();
      const c = document.getElementById('calendar-modal').getBoundingClientRect();
      return {chatL: m.left, chatR: m.right, calL: c.left, calR: c.right, w: innerWidth};
    }""")
    assert abs(geo["calR"] - geo["chatL"]) <= 2                  # calendar left, chat right, no overlap
    assert geo["calL"] >= 239 and geo["chatR"] <= geo["w"] + 1
    _click(page, ".ws-split-btn")
    assert "ws-split" not in page.evaluate("document.documentElement.className")


def test_tabs_survive_a_reload_and_a_tool_tab_reopens_its_tool(page):
    page.boot()
    _click(page, "#session-list [data-session-id=s2]")
    _click(page, "#tool-calendar-btn")
    page.boot()
    assert _tabs(page) == ["home", "chat:s2", "*tool:calendar"]
    _click(page, ".ws-tab[data-tab=home]")
    _click(page, ".ws-tab[data-tab='tool:calendar']")
    assert "hidden" not in page.get_attribute("#calendar-modal", "class")
    assert page.get_attribute("#calendar-modal", "data-ws-pane") == "left"


def test_switching_to_another_design_takes_the_shell_away(page):
    page.boot()
    _click(page, "#tool-calendar-btn")
    page.evaluate("""() => { document.documentElement.classList.remove('ui-workspace');
      window.dispatchEvent(new CustomEvent('odysseus:ui-design', {detail: {design: 'studio'}})); }""")
    _settle(page)
    assert page.evaluate("!document.getElementById('ws-tabbar') && !document.getElementById('ws-home')")
    cls = page.get_attribute("#calendar-modal", "class")
    assert "ws-docked" not in cls and "hidden" not in cls        # the tool stays open, as a window again


def test_the_home_composer_starts_a_new_chat_with_the_text(page):
    page.boot()
    page.fill(".ws-compose-input", "plan my week")
    page.press(".ws-compose-input", "Enter")
    page.wait_for_function("() => calls.some(c => c[0] === 'send')")
    calls = page.evaluate("calls")
    assert ["new"] in calls and ["send", "plan my week"] in calls
    assert calls.index(["new"]) < calls.index(["send", "plan my week"])
    assert _tabs(page)[-1] == "*chat:new"


def test_the_scroll_to_bottom_button_steps_back_with_the_chat(page):
    page.boot()
    page.evaluate("window.wsLayouts = 0; addEventListener('odysseus:ws-layout', () => wsLayouts++)")
    btn = page.locator("#scroll-bottom-btn")
    assert not btn.is_visible()                                # Home is in front
    _click(page, "#session-list [data-session-id=s1]")
    assert btn.is_visible()                                    # the chat is back
    _click(page, "#tool-calendar-btn")
    assert not btn.is_visible()                                # a tool page is in front
    _click(page, ".ws-tab[data-tab='chat:s1']")
    _click(page, ".ws-split-btn")                              # the chat moves into a pane
    assert btn.is_visible()
    assert page.evaluate("wsLayouts") >= 3                      # each move told it to re-measure


# Every animation frame while a tool opens: is a tool window on screen
# anywhere but in its tab page?
_FRAME_RECORDER = """() => {
  window.wsFrames = [];
  const roots = '.modal, .bg-panel-backdrop, .notes-pane';
  const px = (n) => parseFloat(getComputedStyle(document.documentElement).getPropertyValue(n)) || 0;
  const tick = () => {
    if (!window.wsRec) return;
    const bad = [];
    for (const el of document.querySelectorAll(roots)) {
      if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
      const win = el.querySelector('.modal-content, .bg-panel') || el;
      const docked = el.classList.contains('ws-docked') && !el.classList.contains('ws-away');
      const r = win.getBoundingClientRect();
      const x = px('--ws-x'), w = px('--ws-w'), lw = px('--ws-lw');
      const [l, pw] = el.dataset.wsPane === 'right' ? [x + lw, w - lw] : [x, lw];
      const anim = win === el ? 'none' : getComputedStyle(win).animationName;
      if (!docked) bad.push(el.className + ': floating');
      else if (Math.abs(r.left - l) > 2 || Math.abs(r.width - pw) > 2 || Math.abs(r.top - 40) > 8) bad.push(el.className + ': not in its pane');
      else if (anim !== 'none') bad.push(el.className + ': its window animation ' + anim + ' runs');
    }
    wsFrames.push(bad);
    requestAnimationFrame(tick);
  };
  window.wsRec = true;
  requestAnimationFrame(tick);
}"""


@pytest.mark.parametrize("how", ["calendar", "notes", "terminal", "open-tool", "split", "back-from-home"])
def test_a_tool_never_paints_as_a_floating_window_on_its_way_into_a_tab(page, how):
    # Reported 2026-10-01: "when i click a new tab or get routed to one, it
    # Flickers tries to do the old way where it opens up in a new windows
    # above everything then realizes that it shouldnt be like then then goes
    # properly". The shell docked a tool a moment after it opened, so the
    # first frames showed it as the old centered window over a dimmed page.
    page.html = _PAGE.replace("</head>", _WINDOW_CSS + "</head>")
    page.boot()
    if how == "split":
        _click(page, "#tool-calendar-btn")
        _click(page, ".ws-split-btn")
    if how == "back-from-home":
        _click(page, "#tool-calendar-btn")
        _click(page, ".ws-tab[data-tab=home]")
    page.evaluate(_FRAME_RECORDER)
    if how == "open-tool":                                     # "+" menu, Home, Ctrl+K, the agent
        page.evaluate("__ws.openTool('calendar')")
    elif how == "back-from-home":
        page.evaluate("document.querySelector(\".ws-tab[data-tab='tool:calendar']\").click()")
    else:
        btn = {"calendar": "#tool-calendar-btn", "notes": "#tool-notes-btn"}.get(how, "#tool-terminal-btn")
        page.evaluate(f"document.querySelector('{btn}').click()")
    page.wait_for_timeout(500)
    frames = page.evaluate("window.wsRec = false, wsFrames")
    assert len(frames) > 10                                    # the page really was painting
    flashes = [f for f in frames if f]
    assert not flashes, f"{len(flashes)} of {len(frames)} frames: {flashes[0]}"
    if how == "split":
        assert page.get_attribute(".term-backdrop", "data-ws-pane") == "right"
