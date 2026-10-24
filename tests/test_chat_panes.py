"""Two independent chats in the workspace split view (static/js/workspace/shell.js).

The plan "independent chats in both split-screen panes": either side of the
split shows an independently chosen chat. The pane's chat runs the app's own
page in an iframe (/chat-pane) — the same way a thread sits beside its chat
(chatThreadTabs.js, /thread-pane) — so each side is a whole chat with its own
composer, stream and selection, and neither can touch the other's state.

What matters to the user:

  * two different chats can be on screen at once: the front one is the real
    view in <main>, the other runs in the pane beside it;
  * each pane has a chat selector: the main sidebar and tab bar for the front
    pane, and a small name button with a picker for the pane;
  * choosing a chat in one pane never replaces the other pane's chat;
  * sending and streaming in both panes keeps each message attached to its
    own chat;
  * the chosen chats and the split layout survive a refresh, and a chat that
    no longer exists is dropped safely;
  * there is an easy way back to a single pane (× on the pane, the split
    button, Alt+\\).

The real shell.js, chatPane.js and home.js run in Chromium against a small
page with stand-in tools and a stand-in session module (modalSnap.js and
chatRenderer.js are stubbed, as in test_workspace_shell.py). The pane's page
(/chat-pane) is a stand-in that behaves like the real one: it selects the
session from its hash, switches in place through sessionModule, POSTs its
stream to /api/chat_stream, and reports what it settled on to the page around
it (sessions.js does that for ody-pane pages).
"""
import json
import re
from pathlib import Path
from urllib.parse import parse_qs

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

ROOT = Path(__file__).resolve().parent.parent
_STATIC = ROOT / "static"


def _form(req):
    """The request body as {field: [values]}, multipart or urlencoded."""
    body = req.post_data or ""
    content_type = req.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        # The raw body has no headers, so tell the parser the multipart
        # boundary up front; without it the body is read as one opaque string
        # and the fields never come back.
        from email.parser import BytesParser
        from email.policy import default
        m = re.search(r"boundary=([^;]+)", content_type)
        boundary = m.group(1).strip('"') if m else ""
        raw = (f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n" + body).encode()
        msg = BytesParser(policy=default).parsebytes(raw)
        out = {}
        for p in msg.get_payload():
            cd = p.get("content-disposition") or ""
            nm = re.search(r'name=(\"[^\"]*\"|\S+)', cd)
            out[nm.group(1).strip('"') if nm else None] = [p.get_content()]
        return out
    return parse_qs(body)

_PAGES = [{"id": "s1", "name": "Chat one", "last_message_at": "2026-10-01T10:00:00Z"},
          {"id": "s2", "name": "Chat two", "last_message_at": "2026-10-01T09:00:00Z"},
          {"id": "s3", "name": "Chat three", "last_message_at": "2026-10-01T08:00:00Z"}]

_PAGE = """<!doctype html><html class="ui-workspace ui-studio"><head>
<link rel="stylesheet" href="/static/css/workspace.css"></head><body>
<nav id="sidebar">
  <div id="sidebar-new-chat-btn" class="list-item">New chat</div>
  <div id="session-list">
    <div class="list-item" data-session-id="s1">Chat one</div>
    <div class="list-item" data-session-id="s2">Chat two</div>
    <div class="list-item" data-session-id="s3">Chat three</div>
  </div>
  <button id="tool-calendar-btn">Calendar</button>
  <span id="user-bar-name">Jaron Wilson</span>
</nav>
<button id="rail-new-session">+</button>
<main id="chat-container" class="chat-container" style="flex:1;min-width:0;display:flex;flex-direction:column">
  <div id="chat-history" style="flex:1"></div>
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
    list: __PAGES__,
    getCurrentSessionId() { return this.cur; },
    getSessions() { return this.list; },
    selectSession(id) { this.cur = id; calls.push(['select', id]); },
  };
  document.getElementById('rail-new-session').addEventListener('click', () => {
    sessionModule.cur = null; calls.push(['new']);
  });
  document.getElementById('session-list').addEventListener('click', e => {
    const row = e.target.closest('[data-session-id]');
    if (row) sessionModule.selectSession(row.dataset.sessionId);
  });
  // The main composer, like the real app: the message goes with the chat
  // that is current in this (main) view.
  document.getElementById('chat-form').addEventListener('submit', e => {
    e.preventDefault(); calls.push(['send', document.getElementById('message').value, sessionModule.cur]);
  });
  // Calendar: a static modal toggled with .hidden, like the real one.
  const cal = document.createElement('div');
  cal.id = 'calendar-modal'; cal.className = 'modal hidden';
  cal.innerHTML = '<div class="modal-content"><div class="modal-header">Calendar' +
                  '<button class="close-btn">x</button></div><input id="cal-state"></div>';
  document.body.appendChild(cal);
  cal.querySelector('.close-btn').addEventListener('click', () => cal.classList.add('hidden'));
  document.getElementById('tool-calendar-btn').addEventListener('click', () => cal.classList.toggle('hidden'));
</script></body></html>""".replace("__PAGES__", json.dumps(_PAGES))

# The second chat's pane: a stand-in for the /chat-pane page. It behaves like
# the real one — the app in an iframe (the head script turns it into a plain
# chat page, ody-pane): it selects the session from its hash, switches in
# place through sessionModule when asked, reports what it settled on to the
# page around it, and POSTs its own stream to /api/chat_stream.
_PANE_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;font-family:sans-serif;display:flex;flex-direction:column;height:100vh">
<div id="out" style="flex:1"></div>
<form id="pane-form" style="display:flex;gap:6px;padding:8px">
  <input id="pane-msg" name="message" style="flex:1">
  <button>Send</button>
</form>
<script>
  const id = decodeURIComponent(location.hash.slice(1) || '');
  window.__pane = { cur: id, selected: [], sent: [] };
  window.sessionModule = {
    getCurrentSessionId: () => __pane.cur,
    getSessions: () => [],
    selectSession: (sid) => {
      __pane.selected.push(sid);
      if (sid !== __pane.cur) { __pane.cur = sid; history.replaceState(null, '', '#' + sid); }
      try { window.parent.postMessage({ odyChatPane: 1, action: 'changed', id: sid }, location.origin); } catch {}
    },
  };
  // The real page's loadSessions: the hash's session, or the newest live
  // one when it no longer exists.
  if (id) sessionModule.selectSession(id);
  fetch('/api/sessions').then(r => r.json()).then(list => {
    if (!id || list.some(s => s.id === id)) return;
    const live = list.find(s => !s.archived);
    if (live) sessionModule.selectSession(live.id);
  }).catch(() => {});
  document.getElementById('pane-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const m = document.getElementById('pane-msg').value;
    const fd = new FormData();
    fd.append('message', m);
    fd.append('session_id', __pane.cur);
    __pane.sent.push(__pane.cur + ':' + m);
    const r = await fetch('/api/chat_stream', { method: 'POST', body: fd, headers: { 'Accept': 'text/event-stream' } });
    const txt = await r.text();
    for (const line of txt.split('\\n')) if (line.startsWith('data: ')) document.getElementById('out').textContent += line.slice(6) + '\\n';
  });
</script></body></html>"""

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
        elif path == "/api/sessions":
            r.fulfill(body=json.dumps(_PAGES), content_type="application/json")
        elif path.startswith("/api/chat_stream"):
            if r.request.method == "POST":
                pg.stream_posts.append(_form(r.request))
            r.fulfill(body="event: message\ndata: pane-stream-1\n\n", content_type="text/event-stream")
        elif path.startswith("/api/"):
            r.fulfill(body=json.dumps({"events": [], "plans": [], "emails": [], "jobs": [], "notes": []}),
                      content_type="application/json")
        elif path.startswith("/chat-pane"):
            r.fulfill(body=_PANE_PAGE, content_type="text/html")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    pg.html = _PAGE
    pg.sessions = _PAGES
    pg.stream_posts = []
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


# Open two different chats (s1 then s2), then the split: s2 stays the front
# (real) chat, s1 takes the pane.
def _two_chats(pg):
    _click(pg, "#session-list [data-session-id=s1]")
    _click(pg, "#session-list [data-session-id=s2]")
    _click(pg, ".ws-split-btn")


def _pane_ready(pg, timeout=8000):
    pg.wait_for_function("""() => {
      const f = document.querySelector('#chat-pane-page .cp-frame');
      return f && f.contentWindow && f.contentWindow.__pane;
    }""", timeout=timeout)


def test_two_different_chats_can_be_shown_at_once(page):
    page.boot()
    _two_chats(page)
    html = page.evaluate("document.documentElement.className")
    assert "ws-split" in html
    assert _tabs(page) == ["home", "chat:s1", "*chat:s2"]
    pane = page.locator("#chat-pane-page")
    assert pane.get_attribute("data-ws-pane") == "right"
    assert page.evaluate("document.querySelector('#chat-pane-page .cp-frame').src.includes('/chat-pane#s1')")
    _pane_ready(page)
    # The front pane is the main view (s2); the pane shows s1 on its own.
    assert page.evaluate("sessionModule.cur") == "s2"
    assert page.evaluate("document.querySelector('#chat-pane-page .cp-frame').contentWindow.__pane.cur") == "s1"
    # The front chat's messages and history stay in <main>, out of the pane.
    geo = page.evaluate("""() => {
      const m = document.getElementById('chat-container').getBoundingClientRect();
      const p = document.getElementById('chat-pane-page').getBoundingClientRect();
      const f = document.querySelector('#chat-pane-page .cp-frame').getBoundingClientRect();
      return { mainL: m.left, mainR: m.right, paneL: p.left, paneR: p.right, frameL: f.left, frameR: f.right, w: innerWidth };
    }""")
    assert abs(geo["paneL"] - geo["mainR"]) <= 2           # no overlap
    assert geo["mainR"] <= geo["w"] + 1 and geo["paneR"] <= geo["w"] + 1
    # The pane's header names the pane's chat.
    assert page.inner_text("#chat-pane-page .cp-name-text") == "Chat one"


def _composer_boxes(pg):
    """Both composers' boxes (page coordinates) and the two panes' boxes."""
    return pg.evaluate("""() => {
      const box = (r, dx = 0, dy = 0) => ({ l: r.left + dx, t: r.top + dy, r: r.right + dx, b: r.bottom + dy,
                                            w: r.width, h: r.height });
      const m = document.getElementById('chat-container').getBoundingClientRect();
      const p = document.getElementById('chat-pane-page').getBoundingClientRect();
      const f = document.querySelector('#chat-pane-page .cp-frame');
      const fr = f.getBoundingClientRect();
      const main = document.getElementById('message').getBoundingClientRect();
      const pane = f.contentDocument.getElementById('pane-msg').getBoundingClientRect();
      return { main: box(main), pane: box(pane, fr.left, fr.top), mainPane: box(m), sidePane: box(p), vh: innerHeight };
    }""")


@pytest.mark.parametrize("width", [1440, 1100])
@pytest.mark.parametrize("ratio", [None, 0.25, 0.75])
def test_both_composers_are_visible_in_their_own_pane(page, width, ratio):
    # Regression: the pane's iframe kept its default 150px height (a docked
    # page's display:block beat the pane's flex column), so the second chat's
    # composer was cut off and only one chatbox showed.
    page.set_viewport_size({"width": width, "height": 900})
    page.boot()
    _two_chats(page)
    _pane_ready(page)
    if ratio is not None:
        page.evaluate("""(r) => { const d = document.getElementById('ws-divider');
          const x0 = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ws-x'));
          const w = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ws-w'));
          const fire = (t, x) => d.dispatchEvent(new PointerEvent(t, { clientX: x, pointerId: 1, bubbles: true }));
          fire('pointerdown', x0 + w * 0.5); fire('pointermove', x0 + w * r); fire('pointerup', x0 + w * r); }""", ratio)
        _settle(page)
    g = _composer_boxes(page)
    for name, pane in (("main", "mainPane"), ("pane", "sidePane")):
        c, box = g[name], g[pane]
        assert c["w"] > 100 and c["h"] > 10, (name, c)
        assert c["l"] >= box["l"] - 1 and c["r"] <= box["r"] + 1, (name, c, box)
        assert c["t"] >= box["t"] - 1 and c["b"] <= box["b"] + 1, (name, c, box)
        assert c["b"] <= g["vh"], (name, c)
        # At the bottom of its own pane, not stranded at the top.
        assert box["b"] - c["b"] < 200, (name, c, box)
    # Each pane is usable (never squeezed under a composer's width) and they do not overlap.
    assert g["mainPane"]["w"] >= 320 and g["sidePane"]["w"] >= 320, g
    assert g["main"]["r"] <= g["pane"]["l"] + 1 or g["pane"]["r"] <= g["main"]["l"] + 1, g


def test_the_pane_header_picks_another_chat_for_the_pane_only(page):
    page.boot()
    _two_chats(page)
    _pane_ready(page)
    # The pane's own selector: a picker over the app's chats.
    _click(page, "#chat-pane-page .cp-name")
    assert page.locator(".cp-menu").count() == 1
    assert [b.strip() for b in page.inner_text(".cp-menu").splitlines()] == ["Chat one", "Chat two", "Chat three"]
    page.evaluate("""() => {
      const b = [...document.querySelectorAll('.cp-menu button')].find(b => b.textContent.trim() === 'Chat three');
      b.click();
    }""")
    _settle(page)
    _pane_ready(page)
    # The pane switched in place; the front chat is untouched.
    assert page.evaluate("document.querySelector('#chat-pane-page .cp-frame').contentWindow.__pane.selected.includes('s3')")
    assert page.evaluate("document.querySelector('#chat-pane-page .cp-frame').contentWindow.__pane.cur") == "s3"
    assert page.evaluate("sessionModule.cur") == "s2"
    assert _tabs(page) == ["home", "chat:s3", "*chat:s2"]
    assert page.inner_text("#chat-pane-page .cp-name-text") == "Chat three"


def test_the_pane_picking_the_front_chat_swaps_the_panes(page):
    page.boot()
    _two_chats(page)
    _pane_ready(page)
    # The pane asks for the chat that is in the other pane: the two swap, so
    # the pick lands in the pane and the other one takes its side.
    _click(page, "#chat-pane-page .cp-name")
    page.evaluate("""() => {
      const b = [...document.querySelectorAll('.cp-menu button')].find(b => b.textContent.trim() === 'Chat two');
      b.click();
    }""")
    _settle(page)
    _pane_ready(page)
    assert _tabs(page) == ["home", "chat:s1", "*chat:s2"]
    assert page.evaluate("sessionModule.cur") == "s2"
    assert page.locator("#chat-pane-page").get_attribute("data-ws-pane") == "left"
    assert page.evaluate("document.querySelector('#chat-pane-page .cp-frame').contentWindow.__pane.cur") == "s1"


def test_sending_and_streaming_in_both_panes_stays_attached(page):
    page.boot()
    _two_chats(page)
    _pane_ready(page)
    # Front pane: a normal send from the main composer.
    page.fill("#message", "hello from the front")
    page.evaluate("document.getElementById('chat-form').requestSubmit()")
    _settle(page)
    # Pane: a send from the pane's own composer, streaming on its own.
    page.evaluate("""() => {
      const f = document.querySelector('#chat-pane-page .cp-frame');
      f.contentWindow.document.getElementById('pane-msg').value = 'hello from the pane';
      f.contentWindow.document.getElementById('pane-form').requestSubmit();
    }""")
    page.wait_for_function("""() => {
      const f = document.querySelector('#chat-pane-page .cp-frame');
      return f && f.contentWindow.__pane.sent.length === 1 &&
             f.contentWindow.document.getElementById('out').textContent.includes('pane-stream-1');
    }""", timeout=8000)
    # The pane's stream reached the server under the pane's own chat...
    assert page.stream_posts == [{"message": ["hello from the pane"], "session_id": ["s1"]}]
    # ...and the front pane's message stayed with the front chat.
    calls = page.evaluate("calls")
    assert ["send", "hello from the front", "s2"] in calls
    assert page.evaluate("sessionModule.cur") == "s2"
    assert page.evaluate("document.querySelector('#chat-pane-page .cp-frame').contentWindow.__pane.cur") == "s1"
    # Nothing the pane said leaked into the front pane's history or state.
    assert page.evaluate("sessionModule.cur") != "s1"
    assert page.locator("#chat-history .msg").count() == 0


def test_the_split_and_both_chats_survive_a_refresh(page):
    page.boot()
    _two_chats(page)
    _pane_ready(page)
    # The layout and both chats are stored the workspace way (odysseus-ws-tabs-v1).
    stored = json.loads(page.evaluate("localStorage.getItem('odysseus-ws-tabs-v1')"))
    assert {t["id"] for t in stored["tabs"] if t["kind"] == "chat"} == {"chat:s1", "chat:s2"}
    assert stored["left"] == "chat:s2" and stored["right"] == "chat:s1"
    # Back across a reload: the same two chats, in the same panes.
    page.boot()
    assert _tabs(page) == ["home", "chat:s1", "*chat:s2"]
    assert page.evaluate("sessionModule.cur") == "s2"
    assert page.evaluate("document.querySelector('#chat-pane-page .cp-frame').src.includes('/chat-pane#s1')")
    _pane_ready(page)
    assert page.evaluate("document.querySelector('#chat-pane-page .cp-frame').contentWindow.__pane.cur") == "s1"


def test_a_deleted_chat_in_a_saved_layout_is_dropped_safely(page):
    # The saved split held a chat that no longer exists.
    page.goto("https://example.test/")
    page.evaluate("""() => localStorage.setItem('odysseus-ws-tabs-v1', JSON.stringify({
      tabs: [{ id: 'home', kind: 'home' },
              { id: 'chat:s1', kind: 'chat', sid: 's1' },
              { id: 'chat:gone', kind: 'chat', sid: 'gone' }],
      left: 'chat:s1', right: 'chat:gone', focus: 'left', ratio: 0.5,
    }))""")
    page.boot()
    # The pane's page settles on a live chat; once both panes would show the
    # same one, the split drops the dead pane instead of keeping it.
    page.wait_for_function(
        "() => !document.getElementById('chat-pane-page') && "
        "![...document.querySelectorAll('.ws-tab')].some(t => t.dataset.tab === 'chat:gone')",
        timeout=15000)
    assert _tabs(page) == ["home", "*chat:s1"]
    assert page.evaluate("sessionModule.cur") == "s1"


def test_closing_the_pane_returns_to_a_single_pane(page):
    page.boot()
    _two_chats(page)
    _pane_ready(page)
    # The pane's own × closes the split; the front chat keeps its pane.
    _click(page, "#chat-pane-page [data-cp-close]")
    assert page.locator("#chat-pane-page").count() == 0
    assert "ws-split" not in page.evaluate("document.documentElement.className")
    assert _tabs(page) == ["home", "chat:s1", "*chat:s2"]
    assert page.evaluate("sessionModule.cur") == "s2"
    # And the split button closes it the same way.
    _click(page, ".ws-split-btn")
    _pane_ready(page)
    assert page.locator("#chat-pane-page").count() == 1
    _click(page, ".ws-split-btn")
    assert page.locator("#chat-pane-page").count() == 0
    assert "ws-split" not in page.evaluate("document.documentElement.className")
    assert _tabs(page) == ["home", "chat:s1", "*chat:s2"]


def test_a_tool_still_docks_beside_the_split_chats(page):
    page.boot()
    _two_chats(page)
    _pane_ready(page)
    # A tool asked for while two chats are shown takes a pane the same way a
    # tool always did: the front chat keeps the main view, the other steps back.
    _click(page, "#tool-calendar-btn")
    cal = page.locator("#calendar-modal")
    assert cal.get_attribute("data-ws-pane") == "left"
    assert "ws-docked" in cal.get_attribute("class")
    assert page.evaluate("sessionModule.cur") == "s1"           # the other chat is now the front view
    assert page.locator("#chat-pane-page").count() == 0
    # The calendar's own close takes its tab away; the front chat comes back.
    _click(page, "#calendar-modal .close-btn")
    assert page.locator("#calendar-modal").get_attribute("data-ws-pane") in (None, "")


def test_the_pane_page_may_be_framed_by_this_origin_only():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.testclient import TestClient
    from core.middleware import SecurityHeadersMiddleware

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    for path in ("/", "/thread-pane", "/chat-pane"):
        app.add_api_route(path, lambda: HTMLResponse("<html></html>"))
    c = TestClient(app)
    for pane in ("/thread-pane", "/chat-pane"):
        r = c.get(pane)
        assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]
        assert "script-src 'self' 'nonce-" in r.headers["Content-Security-Policy"]   # still the strict policy
    r = c.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_the_pane_wiring_is_in_the_sources():
    read = lambda *p: (ROOT.joinpath(*p)).read_text(encoding="utf-8")
    assert '@app.get("/chat-pane")' in read("app.py")
    head = read("static", "index.html")
    assert "location.pathname === '/chat-pane' && window.parent !== window" in head
    assert "odyChatPane: 1" in read("static", "js", "sessions.js")
    assert "'/static/js/workspace/chatPane.js'" in read("static", "sw.js")
    assert 'is_pane_page = path in ("/thread-pane", "/chat-pane")' in read("core", "middleware.py")
    # Threads keep their old wiring, untouched.
    assert "location.pathname === '/thread-pane' && window.parent !== window" in head
