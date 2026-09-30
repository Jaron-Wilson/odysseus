"""The Interface setting: Workspace (tabs + Home), Studio (the restyle) or
Classic (the original look).

Asked for 2026-09-30: "add a setting to rebrand/redesign everything, im not
liking the ui very much so im wanting it to be completely redone and more
professional looking". Studio is a CSS layer over style.css, scoped to
html.ui-studio, so the promise that matters is that Classic stays exactly
what it was:

  * every rule in the Studio stylesheets is scoped to html.ui-studio (the
    Interface picker's own .ui-design-* rules are the one exception, shown
    in both designs);
  * workspace.css is scoped to html.ui-workspace, so Studio never gets the
    tab shell's rules either;
  * the head script in index.html sets the design class before first
    paint: Workspace (which also carries ui-studio, the same look) unless
    Studio or Classic was chosen;
  * switching swaps only the stock dark/light palettes for Studio's and
    back, and leaves a theme picked by hand alone.

The head script is sliced out of index.html and static/js/uiDesign.js is
loaded as the real module (with theme.js stubbed), so this pins the
shipped code.
"""
import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_STATIC = _REPO / "static"
_STUDIO_CSS = sorted((_STATIC / "css").glob("studio*.css"))
_WORKSPACE_CSS = _STATIC / "css" / "workspace.css"
_ALL_CSS = _STUDIO_CSS + [_WORKSPACE_CSS]


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _selectors(css: str):
    """Top-level and @media/@container/@supports rule selectors."""
    css = _strip_comments(css)
    out, depth, buf = [], 0, ""
    at_stack = []
    for ch in css:
        if ch == "{":
            head = buf.strip()
            if head.startswith("@"):
                at_stack.append(depth)
            elif head and not head.startswith(("from", "to")) and not re.fullmatch(r"[\d.%\s,]+", head):
                out.append(head)
            depth += 1
            buf = ""
        elif ch == "}":
            depth -= 1
            if at_stack and at_stack[-1] == depth:
                at_stack.pop()
            buf = ""
        elif ch == ";" and depth > len(at_stack):
            buf = ""
        else:
            buf += ch
    return out


def test_the_studio_stylesheets_are_linked_and_precached():
    html = (_STATIC / "index.html").read_text()
    sw = (_STATIC / "sw.js").read_text()
    assert _STUDIO_CSS, "no static/css/studio*.css"
    for f in _ALL_CSS:
        href = f"/static/css/{f.name}"
        assert f'href="{href}"' in html, f"{f.name} not linked in index.html"
        assert f"'{href}'" in sw, f"{f.name} not precached in sw.js"
    # After style.css, so Studio overrides it.
    assert html.index('href="/static/style.css"') < html.index('href="/static/css/studio.css"')


def test_studios_quick_starts_ship_hidden_so_classic_never_shows_them():
    html = (_STATIC / "index.html").read_text()
    m = re.search(r'<div id="welcome-starters"[^>]*>', html)
    assert m and " hidden" in m.group(0)
    # Only a Studio-scoped rule shows them; nothing unhides them in JS.
    assert "welcome-starters" not in (_STATIC / "style.css").read_text()
    assert ".hidden = false" not in (_STATIC / "js" / "uiDesign.js").read_text()


def _split_list(sel: str):
    """A selector list split at its top-level commas (not those inside :is())."""
    parts, depth, buf = [], 0, ""
    for ch in sel:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    parts.append(buf.strip())
    return [p for p in parts if p]


def _scoped(part: str, cls: str = "ui-studio") -> bool:
    """The first compound selector is `html` carrying the design class,
    e.g. html.ui-studio or html[data-theme-mode="light"].ui-studio."""
    m = re.match(r"^html((?:\[[^\]]*\]|[.#:][\w-]+(?:\([^)]*\))?)*)", part)
    return bool(m) and re.search(r"\." + cls + r"(?![\w-])", m.group(1)) is not None


def test_the_scope_check_catches_a_leak():
    css = ("html.ui-studio :is(#a, #b) .x{c:d} html[data-theme-mode=\"light\"].ui-studio .y{c:d}"
           " .leak{c:d} @media (x){ .leak2, html.ui-studio .z{c:d} } html .ui-studio-ish{c:d}")
    parts = [p for s in _selectors(css) for p in _split_list(s)]
    assert [p for p in parts if not _scoped(p)] == [".leak", ".leak2", "html .ui-studio-ish"]


@pytest.mark.parametrize("path", _ALL_CSS, ids=lambda p: p.name)
def test_every_studio_rule_is_scoped_so_classic_is_untouched(path):
    cls = "ui-workspace" if path == _WORKSPACE_CSS else "ui-studio"
    bad = []
    for sel in _selectors(path.read_text()):
        for part in _split_list(sel):
            if _scoped(part, cls):
                continue
            if re.match(r"^\.ui-design-[\w-]+", part):
                continue            # the Interface picker, shown in both designs
            bad.append(part)
    assert not bad, f"selectors in {path.name} not scoped to html.{cls}: {bad[:10]}"


# --- Browser checks ----------------------------------------------------------

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")


def _head_script() -> str:
    html = (_STATIC / "index.html").read_text()
    m = re.search(r"<script[^>]*>\s*window\._odysseusLoadTime.*?</script>", html, re.S)
    assert m, "first-paint head script not found"
    return re.sub(r"^<script[^>]*>|</script>$", "", m.group(0))


_THEME_STUB = """
export const calls = [];
let saved = JSON.parse(localStorage.getItem('odysseus-theme') || 'null');
const THEMES = {
  dark: {bg:'#17150f', fg:'#ede9e0', panel:'#201d16', border:'#35322a', red:'#e06c75'},
  light: {bg:'#faf8f4', fg:'#1a1a17', panel:'#ffffff', border:'#e8e4dc', red:'#c0524a'},
  studio: {bg:'#0f1115', fg:'#e6e8ec', panel:'#16191e', border:'#262a31', red:'#7c8cff'},
  'studio-light': {bg:'#f7f8fa', fg:'#15171c', panel:'#ffffff', border:'#e2e5ea', red:'#4f5fd8'},
};
export default {
  THEMES,
  getSaved: () => saved,
  applyColors: c => { document.documentElement.style.setProperty('--bg', c.bg); calls.push(['apply', c.bg]); },
  save: (name, colors) => { saved = {name, colors}; localStorage.setItem('odysseus-theme', JSON.stringify(saved)); },
};
"""


@pytest.fixture
def page():
    sync_api = playwright_api
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page()
        ui_js = (_STATIC / "js" / "uiDesign.js").read_text()

        def route(r):
            url = r.request.url
            if url.endswith("/static/js/uiDesign.js"):
                r.fulfill(body=ui_js, content_type="text/javascript")
            elif url.endswith("/static/js/theme.js"):
                r.fulfill(body=_THEME_STUB, content_type="text/javascript")
            elif "/api/prefs/ui-design" in url:
                r.fulfill(body=json.dumps({"key": "ui-design", "value": None}), content_type="application/json")
            elif url.rstrip("/").endswith("example.test"):
                r.fulfill(body="<!doctype html><html><head></head><body>"
                               "<select id='theme-design-select'><option value='workspace'>W</option>"
                               "<option value='studio'>S</option><option value='classic'>C</option></select>"
                               "<div data-ui-design-choice='workspace'></div>"
                               "<div data-ui-design-choice='studio'></div>"
                               "<div data-ui-design-choice='classic'></div></body></html>",
                          content_type="text/html")
            else:
                r.fulfill(status=404, body="")
        pg.route("**/*", route)
        yield pg
        browser.close()


def _boot(pg, storage: dict):
    """Load the page with this localStorage, run the head script, then the module."""
    pg.goto("https://example.test/")
    pg.evaluate("s => { localStorage.clear(); for (const [k, v] of Object.entries(s)) localStorage.setItem(k, v); }",
                storage)
    pg.evaluate(_head_script())
    early = pg.evaluate("document.documentElement.className")
    pg.evaluate("() => import('/static/js/uiDesign.js').then(m => { window.__ui = m; })")
    pg.wait_for_function("() => window.__ui")
    return early


def _state(pg):
    return pg.evaluate("""() => ({
      cls: document.documentElement.className,
      theme: (JSON.parse(localStorage.getItem('odysseus-theme') || 'null') || {}).name,
      stored: localStorage.getItem('odysseus-ui-design'),
      sel: document.getElementById('theme-design-select').value,
      active: [...document.querySelectorAll('[data-ui-design-choice].active')].map(e => e.dataset.uiDesignChoice),
    })""")


_DARK = json.dumps({"name": "dark", "colors": {"bg": "#17150f", "fg": "#ede9e0", "panel": "#201d16",
                                               "border": "#35322a", "red": "#e06c75"}})


def test_workspace_is_the_default_and_is_set_before_first_paint(page):
    early = _boot(page, {"odysseus-theme": _DARK})
    assert "ui-workspace" in early and "ui-studio" in early      # by the head script, before any module
    s = _state(page)
    assert s["theme"] == "studio"                                 # stock dark swapped once for Studio's
    assert s["active"] == ["workspace"] and s["sel"] == "workspace"


def test_studio_once_chosen_has_the_look_without_the_tab_shell(page):
    early = _boot(page, {"odysseus-ui-design": "studio", "odysseus-theme": _DARK})
    assert "ui-studio" in early and "ui-workspace" not in early
    s = _state(page)
    assert "ui-workspace" not in s["cls"] and s["active"] == ["studio"]
    page.evaluate("window.__ui.setDesign('workspace')")
    s = _state(page)
    assert "ui-workspace" in s["cls"] and "ui-studio" in s["cls"] and s["stored"] == "workspace"


def test_classic_is_kept_once_chosen(page):
    early = _boot(page, {"odysseus-ui-design": "classic", "odysseus-theme": _DARK})
    assert "ui-classic" in early and "ui-studio" not in early and "ui-workspace" not in early
    assert _state(page)["theme"] == "dark"                       # Classic never swaps the palette


def test_switching_swaps_the_stock_palette_both_ways(page):
    _boot(page, {"odysseus-theme": _DARK})
    page.evaluate("window.__ui.setDesign('classic')")
    s = _state(page)
    assert "ui-classic" in s["cls"] and "ui-studio" not in s["cls"] and "ui-workspace" not in s["cls"]
    assert s["theme"] == "dark" and s["stored"] == "classic" and s["active"] == ["classic"]
    page.evaluate("window.__ui.setDesign('studio')")
    assert _state(page)["theme"] == "studio"


def test_a_theme_picked_by_hand_is_left_alone(page):
    ocean = json.dumps({"name": "ocean", "colors": {"bg": "#0b1a2c", "fg": "#64d2ff", "panel": "#091422",
                                                    "border": "#1e5074", "red": "#4facfe"}})
    _boot(page, {"odysseus-theme": ocean})
    for d in ("classic", "studio", "workspace", "classic"):
        page.evaluate(f"window.__ui.setDesign('{d}')")
        assert _state(page)["theme"] == "ocean"


def test_an_unknown_design_value_falls_back_to_workspace(page):
    early = _boot(page, {"odysseus-ui-design": "neon", "odysseus-theme": _DARK})
    assert "ui-workspace" in early
    page.evaluate("window.__ui.setDesign('neon')")                # ignored
    assert "ui-workspace" in _state(page)["cls"]
