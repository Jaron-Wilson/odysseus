"""The agent's question is a modal, and its arguments are not printed twice.

Two things were on screen at once. The question already rendered as a
card with clickable options; what looked like raw JSON was the generic
tool card beside it, printing ask_user's arguments the way it prints a
shell command. For every other tool that is worth seeing. For this one
the arguments ARE the question, so the transcript showed it twice, the
second time as machine output.

And a question that ends the turn should interrupt rather than join the
scroll: appended to the history it scrolled away with everything else
and could be missed entirely after a long answer.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CHAT = (_REPO / "static" / "js" / "chat.js").read_text()
_CSS = (_REPO / "static" / "style.css").read_text()


def test_ask_user_gets_no_generic_tool_card():
    """Its arguments are the question; the tool card repeats it as JSON."""
    i = _CHAT.index("} else if (json.type === 'tool_start') {")
    assert "'ask_user') continue;" in _CHAT[i:i + 700], (
        "the tool card still prints ask_user's arguments alongside the card"
    )


def test_ask_user_output_card_is_suppressed_too():
    i = _CHAT.index("} else if (json.type === 'tool_output') {")
    assert "'ask_user') continue;" in _CHAT[i:i + 700], (
        "the tool result card echoes the same payload back"
    )


def test_the_question_is_raised_as_an_overlay_not_appended_to_history():
    i = _CHAT.index("card.className = 'ask-user-card'")
    block = _CHAT[i:i + 12000]
    assert "ask-user-overlay" in block, "the question is not raised as a modal"
    assert "document.body.appendChild(askOverlay)" in block, (
        "the modal is still inside the scrolling history"
    )
    assert "chatBox.appendChild(card)" not in block, (
        "the card is still appended to the chat history"
    )


def test_dismissing_removes_the_backdrop_too():
    """A leftover backdrop covers the page and reads as a frozen app."""
    i = _CHAT.index("card.className = 'ask-user-card'")
    block = _CHAT[i:i + 12000]
    assert "card.closest('.ask-user-overlay')" in block, (
        "closing the card leaves its backdrop covering everything"
    )
    assert re.search(r"^\s+card\.remove\(\);\s*$", block, re.M) is None, (
        "a bare card.remove() survives and would strand the backdrop"
    )


def test_an_earlier_unanswered_question_is_cleared_wherever_it_lives():
    i = _CHAT.index("card.className = 'ask-user-card'")
    # The dedupe runs just before the card is built.
    block = _CHAT[max(0, i - 800):i + 200]
    assert ".ask-user-overlay, .ask-user-card" in block, (
        "dedupe only searches the chat box, so a previous modal would stack"
    )


def test_the_overlay_is_styled_as_a_centred_modal():
    i = _CSS.index(".ask-user-overlay {")
    rule = _CSS[i:i + 500]
    assert "position: fixed" in rule and "inset: 0" in rule
    assert "justify-content: center" in rule and "align-items: center" in rule
    assert "z-index" in rule, "without a stacking order it can render behind"


def test_a_long_option_description_stays_inside_its_button():
    """A <button> flex container does not grow to fit a wrapped flex line.

    With the description inline, anything long enough to wrap painted
    outside the button's border, and short ones stayed on line one, so
    options rendered inconsistently: it read as a rendering fault.
    """
    i = _CSS.index(".ask-user-option-desc {")
    rule = _CSS[i:i + 300]
    assert "flex: 1 0 100%" in rule, "the description can still share the line"
    i2 = _CSS.index(".ask-user-option {")
    box = _CSS[i2:i2 + 500]
    assert "height: auto" in box, (
        "the button cannot grow past min-height, so the text spills out"
    )


def test_reduced_motion_is_respected():
    assert "prefers-reduced-motion" in _CSS[_CSS.index(".ask-user-overlay {"):
                                            _CSS.index(".ask-user-overlay {") + 1200]
