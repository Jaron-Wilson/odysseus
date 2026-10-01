"""The Terminal's window (static/js/terminal.js) in a real browser.

Asked for 2026-09-30: "can i also get a command line in mine? ... a live
command line so that i can ssh and do stuff myself please."

What matters to the user here:

  * in the Workspace interface the Terminal opens as a tab, and can sit in
    split view beside a chat;
  * what the shell prints shows up, and what is typed goes to the shell;
  * on a phone the extra key row sends the keys a phone keyboard lacks
    (Esc, Tab, Ctrl+key, Alt+key, arrows, | ~ /);
  * a dropped connection reconnects to the same shell by itself, but a
    shell opened in another window is not fought over.

The real terminal.js, xterm.js and workspace/shell.js run in Chromium; the
WebSocket is a stand-in that records what is sent.
"""
import json
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_STATIC = Path(__file__).resolve().parent.parent / "static"

_PAGE = """<!doctype html><html class="ui-workspace ui-studio"><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/workspace.css">
<link rel="stylesheet" href="/static/css/terminal.css">
<style>
  :root { --bg: #17150f; --fg: #ede9e0; --red: #e06c75; --border: #35322a; }
  .bg-panel { display: flex; flex-direction: column; background: var(--bg); color: var(--fg); }
  .bg-panel-head { display: flex; align-items: center; }
</style></head><body>
<nav id="sidebar">
  <div id="session-list"><div class="list-item" data-session-id="s1">Chat one</div></div>
  <div class="list-item" id="tool-terminal-btn">Terminal</div>
</nav>
<button id="rail-new-session">+</button>
<main id="chat-container" class="chat-container" style="flex:1;min-width:0"></main>
<script>
  document.body.style.cssText = 'display:flex;margin:0;height:100vh;background:#17150f';
  document.getElementById('sidebar').style.cssText = 'width:240px;flex:none';
  window.sessionModule = {
    cur: null,
    list: [{id: 's1', name: 'Chat one'}],
    getCurrentSessionId() { return this.cur; },
    getSessions() { return this.list; },
    selectSession(id) { this.cur = id; },
  };
  document.getElementById('session-list').addEventListener('click', e => {
    const row = e.target.closest('[data-session-id]');
    if (row) sessionModule.selectSession(row.dataset.sessionId);
  });
  // Stand-in WebSocket: says hello like the server, records what is sent.
  window.sockets = [];
  window.helloReplay = false;
  class FakeWS {
    constructor(url) {
      this.url = url; this.readyState = 0; this.sent = [];
      sockets.push(this);
      setTimeout(() => {
        if (window.refuse) { this.drop(1006); return; }      // refused at the handshake
        this.readyState = 1;
        this.onopen && this.onopen();
        const id = new URL(url).searchParams.get('id') || 'sess-' + sockets.length;
        this.recv(JSON.stringify({type: 'hello', id, title: 'Shell 1', replay: !!new URL(url).searchParams.get('id')}));
        this.recvText('jaron@box:~$ ');
      }, 20);
    }
    send(d) {
      if (typeof d === 'string') this.sent.push(JSON.parse(d));
      else this.sent.push(new TextDecoder().decode(d));
    }
    close(code) { if (this.readyState === 3) return; this.readyState = 3; this.onclose && this.onclose({code: code || 1000}); }
    recv(data) { this.onmessage && this.onmessage({data}); }
    recvText(s) { this.recv(new TextEncoder().encode(s).buffer); }
    drop(code) { this.readyState = 3; this.onclose && this.onclose({code}); }
  }
  window.WebSocket = FakeWS;
  window.typed = () => sockets.flatMap(s => s.sent).filter(x => typeof x === 'string');
</script></body></html>"""

_MODALSNAP_STUB = "export function clearRightDock() {}"
_RENDERER_STUB = "export function openEntityHash() { return true; }"
_FILL_STUB = "export function addFillChatAreaButton() { return null; }"
_DRAG_STUB = "export function makeWindowDraggable() {}"


@pytest.fixture
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def _page(browser, width, height, touch=False):
    ctx = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch, is_mobile=touch)

    calls = []

    def route(r):
        path = r.request.url.split("example.test", 1)[-1].split("?", 1)[0]
        if path.startswith("/api/terminal/"):
            calls.append((r.request.method, path, r.request.post_data))
        stubs = {"/static/js/modalSnap.js": _MODALSNAP_STUB, "/static/js/chatRenderer.js": _RENDERER_STUB,
                 "/static/js/fillChatArea.js": _FILL_STUB, "/static/js/windowDrag.js": _DRAG_STUB}
        if path in ("", "/"):
            r.fulfill(body=_PAGE, content_type="text/html")
        elif path in stubs:
            r.fulfill(body=stubs[path], content_type="text/javascript")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            ctype = "text/css" if f.suffix == ".css" else "text/javascript"
            r.fulfill(body=f.read_text(), content_type=ctype)
        elif path == "/api/terminal/sessions":
            r.fulfill(body=json.dumps({"sessions": []}), content_type="application/json")
        elif path == "/api/terminal/info":
            r.fulfill(body=json.dumps({"shell": "/bin/bash", "home": "/home/jaron", "grace_s": 600,
                                       "max_sessions": 8}), content_type="application/json")
        elif path.startswith("/api/"):
            r.fulfill(body="{}", content_type="application/json")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    pg = ctx.new_page()
    pg.api_calls = calls
    pg.goto("https://example.test/")
    pg.evaluate("""() => Promise.all([import('/static/js/workspace/shell.js'), import('/static/js/terminal.js')])
                   .then(([ws, t]) => { window.__ws = ws; window.__term = t; })""")
    pg.wait_for_function("() => window.__term && document.getElementById('ws-tabbar')")
    pg.wait_for_timeout(600)
    return pg


def _tabs(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('.ws-tab')].map(t =>
        (t.classList.contains('active') ? '*' : '') + t.dataset.tab)""")


def _open_terminal(pg):
    pg.evaluate("document.getElementById('tool-terminal-btn').click()")
    pg.wait_for_function("() => document.querySelector('.term-view .xterm') && sockets.length")
    pg.wait_for_function("() => document.querySelector('.term-view:not([hidden]) .xterm-rows')?.textContent.includes('jaron@box')")


def test_it_opens_as_a_workspace_tab_and_shows_the_shell(browser):
    pg = _page(browser, 1440, 900)
    _open_terminal(pg)
    pg.wait_for_timeout(300)
    assert _tabs(pg) == ["home", "*tool:terminal"]
    back = pg.locator(".term-backdrop")
    assert "ws-docked" in back.get_attribute("class") and back.get_attribute("data-ws-pane") == "left"
    url = pg.evaluate("sockets[0].url")
    assert "/api/terminal/ws?" in url and "id=" not in url and "rows=" in url
    # It fills the page, and the shell was told the size it got.
    box = pg.locator(".term-panel").bounding_box()
    assert box["width"] > 1000 and box["height"] > 700
    resize = pg.evaluate("sockets[0].sent.filter(m => m.type === 'resize').pop()")
    assert resize and resize["cols"] > 100
    # What is typed goes to the shell.
    pg.click(".term-view:not([hidden]) .xterm")
    pg.keyboard.type("ls ~")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(100)
    assert "".join(pg.evaluate("typed()")) == "ls ~\r"
    # Home in front: the terminal stays open behind it, not closed.
    pg.click(".ws-tab[data-tab=home]")
    pg.wait_for_timeout(300)
    assert "ws-away" in back.get_attribute("class")
    assert pg.evaluate("sockets[0].readyState") == 1
    # A tab going behind doesn't squeeze the shell to a sliver.
    last = pg.evaluate("sockets[0].sent.filter(m => m.type === 'resize').pop()")
    assert last["cols"] > 100
    pg.context.close()


def test_it_sits_in_split_view_beside_a_chat(browser):
    pg = _page(browser, 1440, 900)
    pg.click("[data-session-id=s1]")
    pg.wait_for_timeout(700)
    _open_terminal(pg)
    pg.click(".ws-split-btn")
    pg.wait_for_timeout(400)
    shown = pg.evaluate("[...document.querySelectorAll('.ws-tab.shown')].map(t => t.dataset.tab)")
    assert set(shown) == {"chat:s1", "tool:terminal"}
    term = pg.locator(".term-panel").bounding_box()
    chat = pg.locator("#chat-container").bounding_box()
    assert 500 < term["width"] < 700
    assert term["x"] >= chat["x"] + chat["width"] - 700 or term["x"] + term["width"] <= chat["x"] + 700
    pg.context.close()


def test_the_phone_key_row_sends_the_keys_a_phone_keyboard_lacks(browser):
    pg = _page(browser, 390, 844, touch=True)
    _open_terminal(pg)
    assert pg.is_visible(".term-keys")
    before = len(pg.evaluate("typed()"))

    def key(k):
        pg.click(f".term-key[data-key='{k}']")

    key("esc")
    key("tab")
    key("up")
    key("left")
    key("|")
    key("~")
    key("/")
    key("ctrl")
    assert pg.get_attribute(".term-key[data-key=ctrl]", "aria-pressed") == "true"
    pg.keyboard.type("c")                              # Ctrl+C from the phone keyboard
    assert pg.get_attribute(".term-key[data-key=ctrl]", "aria-pressed") == "false"
    key("alt")
    pg.keyboard.type(".")                              # Alt+. (last argument)
    key("ctrl")
    key("right")                                       # Ctrl+Right (word forward)
    pg.wait_for_timeout(100)
    sent = pg.evaluate("typed()")[before:]
    assert sent == ["\x1b", "\t", "\x1b[A", "\x1b[D", "|", "~", "/", "\x03", "\x1b.", "\x1b[1;5C"]
    # The terminal kept focus, so the soft keyboard stays up.
    assert pg.evaluate("document.activeElement === document.querySelector('.term-view:not([hidden]) textarea')")
    pg.context.close()


def test_it_fits_above_the_phone_keyboard(browser):
    pg = _page(browser, 390, 844, touch=True)
    _open_terminal(pg)
    full = pg.locator(".term-stage").bounding_box()["height"]
    # Stand in for the keyboard taking the bottom 300px of the visual viewport.
    pg.evaluate("""() => {
        Object.defineProperty(window.visualViewport, 'height', { get: () => innerHeight - 300, configurable: true });
        window.visualViewport.dispatchEvent(new Event('resize'));
    }""")
    pg.wait_for_timeout(200)
    keys = pg.locator(".term-keys").bounding_box()
    assert keys["y"] + keys["height"] <= 844 - 300 + 1
    assert pg.locator(".term-stage").bounding_box()["height"] < full - 250
    pg.context.close()


def test_a_dropped_connection_reconnects_to_the_same_shell(browser):
    pg = _page(browser, 1440, 900)
    _open_terminal(pg)
    pg.evaluate("sockets[0].drop(1006)")
    pg.wait_for_function("() => sockets.length === 2", timeout=5000)
    assert "id=sess-1" in pg.evaluate("sockets[1].url")
    pg.wait_for_function("() => document.querySelector('.term-dot.live')")
    # Opened in another window: it says so and doesn't grab it back.
    pg.evaluate("sockets[1].recv(JSON.stringify({type: 'detached'})); sockets[1].drop(4000)")
    pg.wait_for_timeout(1500)
    assert pg.evaluate("sockets.length") == 2
    assert "another window" in pg.inner_text(".term-view:not([hidden]) .xterm-rows")
    pg.context.close()


def test_the_colors_follow_the_theme(browser):
    pg = _page(browser, 1440, 900)
    assert pg.evaluate("__term.theme().background") == "rgb(23, 21, 15)"
    pg.evaluate("document.documentElement.style.setProperty('--bg', '#fafafa'); "
                "document.documentElement.style.setProperty('--red', '#c0524a')")
    th = pg.evaluate("__term.theme()")
    assert th["background"] == "rgb(250, 250, 250)" and th["cursor"] == "rgb(192, 82, 74)"
    assert th["brightWhite"] != "#ffffff"                  # the light palette
    pg.context.close()


def test_a_refused_connection_finds_out_it_is_not_allowed(browser):
    pg = _page(browser, 1440, 900)
    _open_terminal(pg)
    # Signed out (or no longer an admin): the handshake is refused, which a
    # browser only reports as 1006, so the Terminal asks the server why.
    pg.route("**/api/terminal/info", lambda r: r.fulfill(status=403, body="{}", content_type="application/json"))
    pg.evaluate("window.refuse = true; sockets[0].drop(1006)")
    pg.wait_for_function("() => document.querySelector('.term-dot.denied')", timeout=10000)
    assert "only for admins" in pg.inner_text(".term-view:not([hidden]) .xterm-rows")
    n = pg.evaluate("sockets.length")
    pg.wait_for_timeout(2500)
    assert pg.evaluate("sockets.length") == n              # and stops trying
    pg.context.close()


def test_session_tabs_new_rename_and_close(browser):
    pg = _page(browser, 1440, 900)
    _open_terminal(pg)
    pg.click(".term-new")
    pg.wait_for_function("() => sockets.length === 2 && document.querySelector('.term-tab.active .term-dot.live')")
    assert "id=" not in pg.evaluate("sockets[1].url")           # a new shell, not a reattach
    assert pg.evaluate("document.querySelectorAll('.term-tab').length") == 2
    assert pg.evaluate("document.querySelectorAll('.term-view:not([hidden])').length") == 1
    # Rename the one in front.
    pg.click(".term-bar [data-act=rename]")
    pg.fill(".term-rename", "ssh pi")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(150)
    assert pg.inner_text(".term-tab.active .term-tab-label") == "ssh pi"
    assert {"type": "title", "title": "ssh pi"} in pg.evaluate("sockets[1].sent")
    assert ("PATCH", "/api/terminal/sessions/sess-2", '{"title":"ssh pi"}') in pg.api_calls
    # Its x ends that shell, and the other one comes to the front.
    pg.click(".term-tab.active .term-tab-x")
    pg.wait_for_timeout(150)
    assert pg.evaluate("document.querySelectorAll('.term-tab').length") == 1
    assert ("DELETE", "/api/terminal/sessions/sess-2", None) in pg.api_calls
    assert pg.evaluate("document.querySelectorAll('.term-view:not([hidden])').length") == 1
    assert pg.inner_text(".term-tab.active .term-tab-label") == "Shell 1"
    pg.context.close()
