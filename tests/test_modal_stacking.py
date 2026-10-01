"""Opening a modal puts it on top, and several open modals don't fight.

ui.js bumps a modal's z-index when it opens so the most recently opened one
is on top. It used to re-bump on every class/style change of a visible
modal: with two visible modals changing in the same batch, each bump left
the other below the counter and they leapfrogged forever. That hung the tab
(Chrome then killed it) when switching Interface design away from Workspace
with several tools open, since Workspace hands them all back as windows at
once. Reported 2026-09-30.

The real static/js/ui.js (and its imports) runs in Chromium over a bare page.
"""
import json
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_STATIC = Path(__file__).resolve().parent.parent / "static"

_PAGE = """<!doctype html><html><head><style>
.modal { position: fixed; inset: 0; z-index: 250; } .hidden { display: none; }
</style></head><body>
<div id="a" class="modal hidden"></div>
<div id="b" class="modal hidden"></div>
<div id="c" class="modal hidden"></div>
</body></html>"""


@pytest.fixture
def page():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        pg = b.new_page()

        def route(r):
            path = r.request.url.split("example.test", 1)[-1].split("?", 1)[0]
            if path in ("", "/"):
                r.fulfill(body=_PAGE, content_type="text/html")
            elif path.startswith("/static/") and (_STATIC / path[len("/static/"):]).is_file():
                f = _STATIC / path[len("/static/"):]
                r.fulfill(body=f.read_bytes(), content_type="text/javascript" if f.suffix == ".js" else "text/css")
            elif path.startswith("/api/"):
                r.fulfill(body="{}", content_type="application/json")
            else:
                r.fulfill(status=404, body="")
        pg.route("**/*", route)
        pg.goto("https://example.test/")
        pg.evaluate("() => import('/static/js/ui.js').then(() => { window.__ready = true; })")
        pg.wait_for_function("() => window.__ready")
        yield pg
        b.close()


def _z(pg):
    return pg.evaluate("['a','b','c'].map(id => document.getElementById(id).style.zIndex)")


def test_the_most_recently_opened_modal_is_on_top(page):
    page.evaluate("document.getElementById('a').classList.remove('hidden')")
    page.wait_for_timeout(50)
    page.evaluate("document.getElementById('b').classList.remove('hidden')")
    page.wait_for_timeout(50)
    za, zb, _ = _z(page)
    assert int(zb) > int(za)


def test_several_open_modals_changing_together_settle(page):
    # Open all three at once, then keep touching their classes and styles in
    # the same batches, as a layout handing windows back does.
    page.evaluate("['a','b','c'].forEach(id => document.getElementById(id).classList.remove('hidden'))")
    page.wait_for_timeout(100)
    before = _z(page)
    page.evaluate("""() => { setTimeout(() => { for (let i = 0; i < 20; i++) for (const id of ['a','b','c']) {
        const el = document.getElementById(id);
        el.classList.toggle('touched');
        el.style.left = i + 'px';
    } }, 0); }""")
    page.wait_for_timeout(300)
    # The page is still responsive. (The old leapfrog loop blocks the main
    # thread for good, so a regression shows up as this test hanging.)
    page.wait_for_function("() => true", timeout=5000)
    assert _z(page) == before                      # nothing re-bumped


def test_closing_and_reopening_brings_a_modal_back_on_top(page):
    page.evaluate("['a','b'].forEach(id => document.getElementById(id).classList.remove('hidden'))")
    page.wait_for_timeout(50)
    page.evaluate("document.getElementById('a').classList.add('hidden')")
    page.wait_for_timeout(50)
    page.evaluate("document.getElementById('a').classList.remove('hidden')")
    page.wait_for_timeout(50)
    za, zb, _ = _z(page)
    assert int(za) > int(zb)
