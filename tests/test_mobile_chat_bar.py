"""The chat bar fits on a phone.

Reported on 2026-09-28: "mobile message bar looks so jumbled". At 412px the
bottom row's right group needed 383px of 342: the send button hung off the
bar and the + menu was squeezed to nothing; at 360px the + and screen-share
buttons sat on top of the music button.
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(HERE, *p), encoding="utf-8").read()


def test_narrow_bar_uses_slimmer_buttons_that_are_still_tall_enough_to_tap():
    css = _read("static", "style.css")
    i = css.index("@container chatbar (max-width: 420px) {")
    block = css[i:css.index("\n}\n", i)]
    assert ".chat-input-bottom .input-icon-btn { min-width: 34px; width: 34px; padding: 10px 4px; }" in block
    assert "min-height" not in block            # the 44px touch height from the mobile rules stays


def test_screen_share_folds_into_the_plus_menu_first():
    js = _read("static", "app.js")
    assert "const collapsibleIds = ['screenshare-toggle-btn', 'bash-toggle-btn', 'web-toggle-btn'];" in js
    # Re-measured when the right group changes: its buttons appear after load.
    assert "}).observe(_rightGroup);" in js
