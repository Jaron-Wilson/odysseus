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
    assert "#chat-history > .msg:not(:last-child):not(:nth-last-child(2))" in css
    assert "content-visibility: auto;" in css and "contain-intrinsic-size: auto 350px;" in css
