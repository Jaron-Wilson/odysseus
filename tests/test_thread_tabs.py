"""Threads under their chat's tab (static/js/chatThreadTabs.js).

Asked for on 2026-10-10: "I want threads on the website instead of a new
tab, show it like under the tab, so that I don't switch between the 2, and
also make it so it asks if it can open split view."

What matters to the user:

  * a row under the chat's tab shows Main and the chat's threads, with a dot
    while a subagent runs, and only when the chat has threads;
  * opening a thread asks "Open side by side?" (Split view / Just switch,
    Remember my choice), and a remembered choice is used without asking;
  * split view shows the thread beside the main chat, as its own page
    (/thread-pane), which the server lets this origin frame and nothing else;
  * a phone never splits and never asks;
  * × hides a thread from the row only.

The real chatThreadTabs.js runs in Chromium with stand-in hooks for the
thread data (chatThreads.js owns that).
"""
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_STATIC = ROOT / "static"

_PAGE = """<!doctype html><html class="ui-studio"><head>
<link rel="stylesheet" href="/static/css/chatThreads.css">
<link rel="stylesheet" href="/static/css/chatThreadTabs.css"></head>
<body style="margin:0;display:flex;height:100vh">
<main id="chat-container" class="chat-container" style="flex:1;position:relative;display:flex;flex-direction:column;padding:0 16px">
  <div class="chat-top-bar">Trip planning</div>
  <div id="chat-history" style="flex:1"></div>
  <textarea id="message"></textarea>
</main>
<script>
  window.calls = [];
  window.state = { cur: 'root', threads: [
    { id: 't-branch', name: 'Cheaper hotels', kind: 'branch', status: null },
    { id: 't-agent', name: 'Day trips', kind: 'subagent', status: 'running' },
  ] };
  window.showToast = (m) => calls.push(['toast', m]);
</script></body></html>"""

_BOOT = """async () => {
  const m = await import('/static/js/chatThreadTabs.js');
  window.TT = m.default;
  const KINDS = { branch: { label: 'Branch', icon: 'B' }, subagent: { label: 'Subagent', icon: 'S' }, side: { label: 'Side thread', icon: 'T' } };
  window.render = () => TT.renderRow({ root: 'root', rootName: 'Trip planning', view: state.cur, threads: state.threads });
  TT.init({
    current: () => state.cur, root: () => 'root',
    select: (id) => { calls.push(['select', id]); state.cur = id; render(); },
    session: (id) => ({ id, name: id }), name: (t) => t.name, kind: (k) => KINDS[k] || KINDS.side,
    rerender: () => render(), refresh: () => calls.push(['refresh']), openPanel: () => calls.push(['panel']),
    reference: () => {}, merged: () => {},
  });
  render();
}"""


@pytest.fixture
def browser():
    api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def _page(browser, width=1400, height=900, prefs=None):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    pg = ctx.new_page()
    prefs_seen = []

    def route(r):
        path = r.request.url.split("example.test", 1)[-1].split("?", 1)[0]
        if path in ("", "/"):
            r.fulfill(body=_PAGE, content_type="text/html")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            r.fulfill(body=f.read_text(), content_type="text/css" if f.suffix == ".css" else "text/javascript")
        elif path == "/api/prefs/thread-open":
            if r.request.method == "PUT":
                prefs_seen.append(json.loads(r.request.post_data)["value"])
            r.fulfill(body=json.dumps({"value": prefs}), content_type="application/json")
        elif path.startswith("/thread-pane"):
            r.fulfill(body="<!doctype html><title>pane</title>", content_type="text/html")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    pg.goto("https://example.test/")
    pg.evaluate(_BOOT)
    pg.prefs_seen = prefs_seen
    return pg


def _tabs(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('#thread-tabs .tt-tab')].map(t =>
        (t.classList.contains('active') ? '*' : '') + (t.classList.contains('in-split') ? '|' : '')
        + t.textContent.replace('\\u00d7', '').trim())""")


def test_the_row_shows_main_and_the_threads_with_a_running_dot(browser):
    pg = _page(browser)
    assert _tabs(pg) == ["*Main", "BCheaper hotels", "SDay trips"]
    assert pg.locator("#thread-tabs [data-tt-id=t-agent] .tt-dot-running").count() == 1
    # Under the chat's header in Classic and Studio.
    assert pg.evaluate("document.querySelector('.chat-top-bar').nextElementSibling.id") == "thread-tabs"
    # No threads, no row.
    pg.evaluate("state.threads = []; render()")
    assert pg.locator("#thread-tabs").is_hidden()


def test_opening_a_thread_asks_and_just_switch_stays_in_place(browser):
    pg = _page(browser)
    pg.click("#thread-tabs [data-tt-open=t-branch]")
    ask = pg.locator(".tt-ask")
    assert ask.is_visible() and "Open side by side?" in ask.inner_text()
    assert pg.evaluate("calls.filter(c => c[0] === 'select').length") == 0     # nothing yet
    pg.click(".tt-ask [data-ask=switch]")
    assert pg.evaluate("state.cur") == "t-branch"
    assert _tabs(pg)[1] == "*BCheaper hotels"
    assert pg.locator("#thread-split").count() == 0
    # Main goes back without asking.
    pg.click("#thread-tabs [data-tt-main]")
    assert pg.evaluate("state.cur") == "root" and pg.locator(".tt-ask").count() == 0


def test_split_view_puts_the_thread_beside_the_main_chat(browser):
    pg = _page(browser)
    pg.click("#thread-tabs [data-tt-open=t-branch]")
    pg.click(".tt-ask [data-ask=split]")
    assert pg.evaluate("state.cur") == "root"                      # the main chat stays
    frame = pg.locator("#thread-split .ts-frame")
    assert frame.get_attribute("src") == "/thread-pane#t-branch"
    assert "th-split-on" in pg.evaluate("document.documentElement.className")
    assert _tabs(pg) == ["*Main", "|BCheaper hotels", "SDay trips"]
    geo = pg.evaluate("""() => {
      const m = document.getElementById('chat-container').getBoundingClientRect();
      const p = document.getElementById('thread-split').getBoundingClientRect();
      const ta = document.getElementById('message').getBoundingClientRect();
      return { mainR: m.right, paneL: p.left, paneR: p.right, inputR: ta.right };
    }""")
    assert abs(geo["paneR"] - geo["mainR"]) <= 1 and geo["inputR"] <= geo["paneL"]
    # Another thread goes into the pane, no question asked.
    pg.click("#thread-tabs [data-tt-open=t-agent]")
    assert pg.locator(".tt-ask").count() == 0
    assert pg.evaluate("TT.paneId()") == "t-agent"
    # Closing the split gives the chat its width back.
    pg.click("#thread-split [data-ts-close]")
    assert pg.locator("#thread-split").count() == 0
    assert "th-split-on" not in pg.evaluate("document.documentElement.className")


def test_remember_my_choice_is_stored_and_used(browser):
    pg = _page(browser)
    pg.click("#thread-tabs [data-tt-open=t-branch]")
    pg.check(".tt-ask input[name=remember]")
    pg.click(".tt-ask [data-ask=split]")
    pg.wait_for_timeout(100)
    assert pg.evaluate("localStorage.getItem('odysseus-thread-open')") == "split"
    assert pg.prefs_seen == ["split"]                              # and on the server
    pg.click("#thread-split [data-ts-close]")
    pg.click("#thread-tabs [data-tt-open=t-agent]")
    assert pg.locator(".tt-ask").count() == 0 and pg.evaluate("TT.paneId()") == "t-agent"
    # Changed back (the Threads panel calls this).
    pg.evaluate("TT.setOpenMode('ask')")
    pg.click("#thread-split [data-ts-close]")
    pg.click("#thread-tabs [data-tt-open=t-branch]")
    assert pg.locator(".tt-ask").is_visible()


def test_a_choice_from_another_device_counts_until_this_one_has_its_own(browser):
    pg = _page(browser, prefs="switch")
    pg.wait_for_timeout(150)
    assert pg.evaluate("TT.getOpenMode()") == "switch"
    pg.click("#thread-tabs [data-tt-open=t-branch]")
    assert pg.locator(".tt-ask").count() == 0 and pg.evaluate("state.cur") == "t-branch"


def test_a_phone_never_splits_and_never_asks(browser):
    pg = _page(browser, width=390, height=844)
    pg.evaluate("localStorage.setItem('odysseus-thread-open', 'split')")
    pg.click("#thread-tabs [data-tt-open=t-branch]")
    assert pg.locator(".tt-ask").count() == 0 and pg.locator("#thread-split").count() == 0
    assert pg.evaluate("state.cur") == "t-branch"
    pg.evaluate("localStorage.setItem('odysseus-thread-open', 'ask')")
    pg.click("#thread-tabs [data-tt-open=t-agent]")
    assert pg.locator(".tt-ask").count() == 0 and pg.evaluate("state.cur") == "t-agent"


def test_hiding_a_thread_from_the_row_keeps_it(browser):
    pg = _page(browser)
    pg.hover("#thread-tabs [data-tt-id=t-branch]")
    pg.click("#thread-tabs [data-tt-hide=t-branch]")
    assert _tabs(pg) == ["*Main", "SDay trips"]
    assert pg.inner_text("#thread-tabs .tt-more") == "+1"
    pg.click("#thread-tabs .tt-more")
    assert pg.evaluate("calls.some(c => c[0] === 'panel')")         # all of them are in Threads
    # Opening it again (from the panel or a card) brings it back.
    pg.evaluate("TT.openThread('t-branch')")
    pg.click(".tt-ask [data-ask=switch]")
    assert _tabs(pg)[1] == "*BCheaper hotels"


def test_the_pane_page_may_be_framed_by_this_origin_only():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.testclient import TestClient
    from core.middleware import SecurityHeadersMiddleware

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    for path in ("/", "/thread-pane"):
        app.add_api_route(path, lambda: HTMLResponse("<html></html>"))
    c = TestClient(app)
    pane = c.get("/thread-pane")
    assert pane.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in pane.headers["Content-Security-Policy"]
    assert "script-src 'self' 'nonce-" in pane.headers["Content-Security-Policy"]   # still the strict policy
    for url in ("/", "/?pane=thread"):
        r = c.get(url)
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_the_pane_is_a_plain_chat_that_leaves_the_page_around_it_alone():
    read = lambda *p: (ROOT.joinpath(*p)).read_text(encoding="utf-8")
    assert '@app.get("/thread-pane")' in read("app.py")
    head = read("static", "index.html")
    assert "location.pathname === '/thread-pane' && window.parent !== window" in head
    assert "/static/css/chatThreadTabs.css" in head
    # No Workspace tabs inside the pane.
    assert "root.classList.contains('ody-pane')) design = 'studio'" in read("static", "js", "uiDesign.js")
    sessions = read("static", "js", "sessions.js")
    assert "if (!_IN_PANE) Storage.set('lastSessionId', id);" in sessions
    assert "if (_IN_PANE) return;" in sessions                     # incognito cleanup
    threads = read("static", "js", "chatThreads.js")
    assert "ThreadTabs.openThread(" in threads and "ThreadTabs.goTo(" in threads
    # Threads stay in their chat's tab in Workspace.
    assert "existing.view = root !== sid ? sid : undefined;" in read("static", "js", "workspace", "shell.js")
