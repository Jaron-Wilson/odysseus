"""Long chats stay smooth.

Reported on 2026-09-28: "website gets a little glitchy after getting lots of
stuff on it, ie: over 200 messages it's getting a little slow and skipping".
Profiled with the real 117-message chat (~230 items on screen) while a reply
streamed: 1.36 s of forced layout in 30 s, almost all from the scroll-to-
bottom button's script measuring the page on every DOM change.
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _script():
    html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    i = html.index('const bottomBtn = document.getElementById(\'scroll-bottom-btn\');')
    return html[i:html.index("</script>", i)]


def test_the_scroll_button_does_not_measure_on_every_change():
    js = _script()
    assert "new MutationObserver(update)" not in js                      # the old per-change layout
    assert "new MutationObserver(scheduleUpdateSoon)" in js
    assert "setTimeout(() => { _updTimer = 0; scheduleUpdate(); }, 200)" in js
    assert "addEventListener('scroll', scheduleUpdate" in js
    update = js[js.index("function update() {"):js.index("let _scrollRaf")]
    assert "reposition();" not in update                                  # positioned on resize only


def test_the_button_reaches_the_real_bottom():
    js = _script()
    assert "const bottom = () => container.scrollHeight - container.clientHeight;" in js


def test_offscreen_messages_skip_layout():
    css = open(os.path.join(HERE, "static", "style.css"), encoding="utf-8").read()
    assert "#chat-history > .msg.cv-skip { content-visibility: auto; }" in css
    # A fixed estimate made the page jump by thousands of px on load
    # ("randomly refuses to let me be at bottom"); placeholders are measured.
    assert "contain-intrinsic-size: auto 350px" not in css


def _js(name):
    return open(os.path.join(HERE, "static", "js", name), encoding="utf-8").read()


def test_placeholders_are_each_messages_measured_height():
    js = _js("chatSkipOffscreen.js")
    assert "getBoundingClientRect().height" in js
    assert "containIntrinsicSize = `auto ${Math.round(heights[i])}px`" in js
    assert "const KEEP_LAST = 2;" in js
    assert "import './chatSkipOffscreen.js';" in _js("chat.js")


def test_thinking_dots_leave_their_space_behind():
    # On a slow model the dots come and go between tokens; each removal
    # shrank the page and pulled a reader at the bottom up by ~48 px.
    js = _js("chat.js")
    i = js.index("      _removeThinkingSpinner = () => {")
    body = js[i:js.index("};", i)]
    assert "prev.style.minHeight" in body and "el.remove()" in body
    assert js.count("_releaseHeldSpace();") >= 3   # dots shown again, done, error


def test_scroll_follow_keeps_the_last_call_in_the_throttle_window():
    js = _js("ui.js")
    i = js.index("export function scrollHistory()")
    body = js[i:js.index("\n}\n", i)]
    assert "_scrollPending = true; return;" in body
    assert "if (_scrollPending) { _scrollPending = false; scrollHistory(); }" in body
