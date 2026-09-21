"""A stopped turn has to say it was stopped.

Reported: asked for a PDF, "before refresh it jus said thinking, after
refresh nothing".

The log showed the run start at 12:06:30, the model produce nothing
usable, and the browser's own six-minute agent timeout post
/api/chat/stop at 12:12:28. The cancellation save path was guarded by
`if full_response:`, so a run that wrote nothing saved nothing, and the
turn was empty forever.

Empty is the problem. It is indistinguishable from the app being
broken, and it is what a reader is left with precisely when they
refresh -- the browser does print a timeout note, but only into the
DOM, so refreshing takes away the one explanation there was.
"""
import re
from pathlib import Path

from routes.chat_helpers import (
    STOPPED_NO_OUTPUT,
    stopped_response_for_save,
)

_REPO = Path(__file__).resolve().parent.parent
_ROUTES = _REPO / "routes" / "chat_routes.py"


def test_a_run_that_wrote_nothing_still_records_something():
    content, md = stopped_response_for_save("")
    assert content.strip(), "an empty turn is indistinguishable from a broken app"
    assert content == STOPPED_NO_OUTPUT
    assert md.get("stopped_empty") is True, "nothing marks this turn as empty"


def test_none_is_handled_like_empty():
    """The generator can be cancelled before full_response is ever assigned."""
    content, _ = stopped_response_for_save(None)
    assert content.strip()


def test_a_partial_answer_is_kept_verbatim():
    """Whatever did arrive is the valuable part; it must not be replaced."""
    content, md = stopped_response_for_save("Here is the start of the ans")
    assert content == "Here is the start of the ans"
    assert "stopped_empty" not in md


def test_reasoning_only_output_stays_visible():
    """Pins existing behaviour: reasoning with no reply is kept as content.

    chat_helpers deliberately does not move thinking into metadata when
    no reply follows, because that would save blank content and produce
    exactly the empty bubble this whole change is about.
    """
    content, _ = stopped_response_for_save("<think>weighing the pdf</think>")
    assert "weighing the pdf" in content


def test_metadata_passed_in_is_preserved():
    content, md = stopped_response_for_save("", {"stopped": True, "model": "qwen3.8-27b"})
    assert md["stopped"] is True
    assert md["model"] == "qwen3.8-27b"
    assert content.strip()


# --------------------------------------------------------------------------
# both cancellation paths in chat_routes
# --------------------------------------------------------------------------

def _cancel_blocks():
    src = _ROUTES.read_text()
    return [
        src[m.start():m.start() + 1400]
        for m in re.finditer(r"except \(asyncio\.CancelledError, GeneratorExit\):", src)
    ]


def test_no_cancel_path_still_gates_on_a_nonempty_response():
    for block in _cancel_blocks():
        assert not re.search(r"if full_response:", block), (
            "a cancelled run with no output still saves nothing, leaving "
            "an empty turn:\n" + block[:300]
        )


def test_every_cancel_path_records_the_stop():
    blocks = _cancel_blocks()
    assert len(blocks) >= 2, "expected chat-mode and agent-mode handlers"
    for block in blocks:
        assert "stopped_response_for_save(" in block, (
            "a cancellation path does not record what happened"
        )


def test_compare_panes_are_left_alone():
    """A compare pane is a throwaway session; a note there is noise."""
    for block in _cancel_blocks():
        assert "if not compare_mode:" in block, (
            "compare panes would accumulate stop notes nobody returns to"
        )


def test_the_helper_is_imported():
    """Otherwise it is a NameError at the exact moment a run is cancelled."""
    src = _ROUTES.read_text()
    assert re.search(r"^\s*stopped_response_for_save,\s*$", src, re.M), (
        "stopped_response_for_save is used but never imported"
    )


def test_recovery_does_not_quote_the_stop_marker_back():
    """The marker is the server saying there was no output, not output.

    Observed live once the marker landed: the stall watchdog picked it up
    as partial text and sent "It ended with: _Stopped before any answer
    was written._ ... continue where you left off", asking the model to
    carry on from a sentence it never wrote. Recording the stop must not
    manufacture a new prompt out of the recording.
    """
    chat = (_REPO / "static" / "js" / "chat.js").read_text()
    i = chat.index("function _tryAutoRecover(")
    body = chat[i:i + 2600]
    assert "STOPPED_MARKERS" in body, (
        "auto-recovery still treats the stopped marker as partial output"
    )
    assert STOPPED_NO_OUTPUT in body, (
        "the marker the server writes and the one the client strips have "
        "drifted apart"
    )
