"""Selection and find highlights must sit on the line they name.

The badge said "L7-8 selected" while the stripe covered the lines below
it. Both overlay paths measured with a hidden mirror and then placed the
result in the wrong coordinate system:

  * the line-based paths took `mirror.scrollHeight - paddingTop`, but
    scrollHeight spans the padding box and so carries padding at *both*
    ends -- every top was padding-bottom too low (10px, about 0.6 of a
    15.95px line, enough for a two-line band to straddle its neighbours);
  * the character-precise path measured against the mirror's border box,
    whose numbers already include the editor's padding, and then added
    paddingTop and paddingLeft a second time -- 10px low and a whole
    48px line-number gutter to the right.

Heights were never wrong, which is why this read as "right line named,
wrong line highlighted": the surplus cancelled between start and end.

The real functions are sliced out of document.js and run in a headless
browser, so this pins the shipped code rather than a copy of its
arithmetic. Ground truth is analytic: with no wrapping, line N's box top
is paddingTop + (N-1) * lineHeight.
"""
import pytest
from pathlib import Path

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
)

_REPO = Path(__file__).resolve().parent.parent
_DOC_JS = _REPO / "static" / "js" / "document.js"

# Matches the editor's real metrics (static/style.css, .doc-editor-textarea).
_FONT_PX = 11
_LINE_HEIGHT = 11 * 1.45
_PAD_TOP = 10
_PAD_LEFT = 48
_TOLERANCE = 1.5  # sub-pixel rounding only; the bug was 9.5-10.3px

_PAGE = """
<style>
  body { margin:0; }
  #doc-editor-wrap { position:relative; width:600px; height:340px;
                     font-family: monospace; }
  #doc-editor-textarea {
    position:absolute; top:0; left:0; right:0; bottom:0;
    width:100%; height:100%; background:transparent;
    border:none; outline:none; resize:none; font-family:inherit;
    font-size:11px; line-height:1.45; padding:10px 12px 10px 48px;
    overflow-y:scroll; scrollbar-gutter:stable;
    tab-size:4; white-space:pre-wrap; word-wrap:break-word;
    box-sizing:border-box;
  }
  .doc-selection-overlay, .doc-find-rect { position:absolute; }
</style>
<div id="doc-editor-wrap"><textarea id="doc-editor-textarea"></textarea></div>
<select id="doc-language-select">
  <option value="python">python</option>
  <option value="markdown">markdown</option>
</select>
"""

# Short, unwrapped lines so line N's position is exactly predictable.
_LINES = ["line%02d aaaa" % i for i in range(1, 16)]
_TEXT = "\n".join(_LINES)


def _slice_source(start_marker: str, end_fn: str) -> str:
    """Lift real functions out of document.js by brace matching."""
    src = _DOC_JS.read_text()
    i = src.index(start_marker)
    j = src.index("function %s(" % end_fn, i)
    k = src.index("{", j)
    depth = 0
    for n in range(k, len(src)):
        if src[n] == "{":
            depth += 1
        elif src[n] == "}":
            depth -= 1
            if depth == 0:
                return src[i:n + 1]
    raise AssertionError("unbalanced braces slicing %s" % end_fn)


def _line_start(n: int) -> int:
    idx = 0
    for i in range(n - 1):
        idx += len(_LINES[i]) + 1
    return idx


def _expected_top(line_no: int) -> float:
    return _PAD_TOP + (line_no - 1) * _LINE_HEIGHT


@pytest.fixture(scope="module")
def page():
    try:
        with playwright_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # browser not downloaded
                pytest.skip("chromium unavailable: %s" % exc)
            pg = browser.new_page(viewport={"width": 700, "height": 400})
            pg.set_content(_PAGE)
            yield pg
            browser.close()
    except Exception as exc:
        pytest.skip("playwright unusable: %s" % exc)


def _render_selection(page, lang, start_line, end_line):
    code = _slice_source("  function _isCodeDoc() {",
                         "renderAllSelectionHighlights")
    assert "scrollHeight" in code and "addRect" in code, "slice missed a path"
    page.evaluate("() => { document.querySelectorAll("
                  "'.doc-selection-overlay,.doc-find-rect')"
                  ".forEach(e => e.remove()); }")
    page.add_script_tag(content="var _selections = [];\n" + code)
    start = _line_start(start_line)
    end = _line_start(end_line) + len(_LINES[end_line - 1])
    page.evaluate(
        """([text, lang, start, end]) => {
            document.getElementById('doc-editor-textarea').value = text;
            document.getElementById('doc-language-select').value = lang;
            _selections = [{ text: text.substring(start, end),
                             start, end, startLine: 0, endLine: 0 }];
            renderAllSelectionHighlights();
        }""",
        [_TEXT, lang, start, end],
    )
    return page.evaluate(
        """() => [...document.querySelectorAll('.doc-selection-overlay')]
             .map(o => { const r = o.getBoundingClientRect();
                         const w = document.getElementById('doc-editor-wrap')
                                    .getBoundingClientRect();
                         return { top: r.top - w.top, left: r.left - w.left,
                                  height: r.height }; })"""
    )


@pytest.mark.parametrize("start_line,end_line", [(7, 8), (3, 3), (1, 2)])
def test_code_doc_highlight_sits_on_its_lines(page, start_line, end_line):
    rects = _render_selection(page, "python", start_line, end_line)
    assert rects, "no overlay drawn"
    top = min(r["top"] for r in rects)
    assert abs(top - _expected_top(start_line)) < _TOLERANCE, (
        "L%d-%d band starts at %.2f, line %d is at %.2f"
        % (start_line, end_line, top, start_line, _expected_top(start_line))
    )
    height = max(r["top"] + r["height"] for r in rects) - top
    spanned = end_line - start_line + 1
    assert abs(height - spanned * _LINE_HEIGHT) < _TOLERANCE, (
        "band is %.2fpx, expected %d lines (%.2fpx)"
        % (height, spanned, spanned * _LINE_HEIGHT)
    )


@pytest.mark.parametrize("start_line,end_line", [(7, 8), (4, 4)])
def test_prose_doc_highlight_sits_on_its_lines(page, start_line, end_line):
    rects = _render_selection(page, "markdown", start_line, end_line)
    assert rects, "no overlay drawn"
    top = min(r["top"] for r in rects)
    assert abs(top - _expected_top(start_line)) < _TOLERANCE, (
        "L%d-%d band starts at %.2f, line %d is at %.2f"
        % (start_line, end_line, top, start_line, _expected_top(start_line))
    )


def test_prose_highlight_starts_at_the_text_not_past_the_gutter(page):
    """A whole-line prose selection must begin at the text's left edge.

    paddingLeft was added to an x that already contained it, putting
    every rect one 48px line-number gutter to the right.
    """
    rects = _render_selection(page, "markdown", 4, 4)
    left = min(r["left"] for r in rects)
    assert abs(left - _PAD_LEFT) < _TOLERANCE, (
        "prose highlight starts at x=%.1f, the text starts at x=%d"
        % (left, _PAD_LEFT)
    )


def test_find_rects_use_the_same_corrected_measurement():
    """renderFindRects shares the scrollHeight trap; it must subtract both."""
    src = _slice_source("  function renderFindRects(", "renderFindRects")
    bad = [ln.strip() for ln in src.splitlines()
           if "scrollHeight - paddingTop" in ln
           and "paddingBottom" not in ln]
    assert not bad, "find rects still ignore padding-bottom: %s" % bad
