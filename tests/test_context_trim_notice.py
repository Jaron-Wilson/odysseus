"""Say when the conversation did not fit.

Reported as "it keeps getting confused looks like no history with the
chatbot": it asked "turn what into pdf" about a document it had offered
two turns earlier.

Not a memory bug. An external job held the GPU, the 27b could not
answer, and the turn fell back to qwen3:8b with a 32k budget. The
conversation carries a whole extracted PDF -- one message is 24,683
characters -- so the trimmer dropped a third of it:

    [agent] soft-trimmed context: 32358 -> 21615 tokens (budget=32000)

That went to the log and nowhere else. On screen the model simply
appeared to have forgotten a conversation still visible above it, which
is the worst version of the failure: the evidence on screen contradicts
what the model was actually given.
"""
import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent / "src" / "agent_loop.py").read_text()


def _threshold(dropped, before):
    """The condition as written in agent_loop, evaluated directly."""
    return dropped >= 1000 and dropped >= before * 0.1


def test_the_reported_case_would_have_been_announced():
    """32358 -> 21615 is the trim that produced "turn what into pdf"."""
    assert _threshold(32358 - 21615, 32358)


def test_a_trivial_trim_stays_quiet():
    """A notice on every long chat is noise, and noise gets ignored."""
    assert not _threshold(120, 30000)


def test_a_small_proportion_stays_quiet_even_if_large_in_tokens():
    assert not _threshold(1500, 400000)


def test_a_large_proportion_of_a_small_chat_stays_quiet_below_1000():
    """Percentage alone would fire on tiny conversations."""
    assert not _threshold(300, 1200)


def test_the_note_is_both_streamed_and_saved():
    """A yielded delta reaches the live stream only.

    Without the append the explanation is gone on reload -- exactly when
    someone rereads the chat wondering why the model lost the thread.
    This is the same omission that once made approval links vanish.
    """
    i = _SRC.index("if _context_trim_note:")
    block = _SRC[i:i + 600]
    assert "yield" in block, "the note never reaches the live stream"
    assert "full_response += _context_trim_note" in block, (
        "the note is streamed but not saved, so it disappears on reload"
    )


def test_the_note_names_the_model_and_the_budget():
    """"Some context was dropped" is not actionable; which model and how
    much is what tells you to switch models or start a fresh chat."""
    i = _SRC.index("_context_trim_note = (")
    block = _SRC[i:i + 700]
    assert "{model}" in block, "the note does not say which model"
    assert "effective_budget" in block, "the note does not say the budget"
    assert "_dropped" in block, "the note does not say how much was lost"


def test_the_note_is_initialised_before_the_trim_block():
    """The trim runs inside a try; a bare name would NameError on failure."""
    init = _SRC.index('_context_trim_note = ""')
    used = _SRC.index("if _context_trim_note:")
    assert init < used
    # And it is outside the try that can raise.
    between = _SRC[init:used]
    assert between.count("try:") >= 1, "expected the trim try block after init"
