"""Regenerate must stop the thread it is about to rewrite.

regenerateFrom truncated the session and sent a fresh message without
stopping whatever was already running on that thread. Two things go
wrong, and both are worse than a wasted generation.

The old run keeps its tools. It is detached server-side, so it carries
on calling MCP -- still clicking and typing on a real machine -- for a
turn the user has already abandoned.

And it still finishes. The wrapped generator saves the assistant message
on completion, so a run that lands after the truncate writes its answer
into history that was rewritten underneath it: the reply just
regenerated away reappears, out of order, sometimes after the new one.

Stopping is therefore not a courtesy, and neither is waiting for it --
posting the stop and truncating immediately loses the race to a run
already inside its save.
"""
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHAT = _REPO / "static" / "js" / "chat.js"


def _fn(name):
    """Body of a function in chat.js, by brace matching."""
    src = _CHAT.read_text()
    m = re.search(r"(?:async )?function %s\s*\(" % re.escape(name), src)
    assert m, "%s not found in chat.js" % name
    k = src.index("{", m.end() - 1)
    depth = 0
    for n in range(k, len(src)):
        if src[n] == "{":
            depth += 1
        elif src[n] == "}":
            depth -= 1
            if depth == 0:
                return src[k:n + 1]
    raise AssertionError("unbalanced braces in %s" % name)


def test_regenerate_stops_the_run_before_rewriting():
    body = _fn("regenerateFrom")
    assert "_stopThreadForRegen" in body, (
        "regenerate rewrites the thread without stopping the run on it"
    )
    # Before the truncate, or the race is still open.
    stop_at = body.index("_stopThreadForRegen")
    trunc_at = body.index("/truncate")
    assert stop_at < trunc_at, (
        "the thread is truncated before it is stopped, so a run finishing "
        "in between saves its answer over the rewrite"
    )


def test_regenerate_snapshots_the_old_answer_after_stopping():
    """Read a moving target and the kept variant is a torn partial.

    Stopping first is also what lets a regenerate pressed mid-answer keep
    what had arrived, instead of discarding it.
    """
    body = _fn("regenerateFrom")
    stop_at = body.index("_stopThreadForRegen")
    read_at = body.index("aiMsgElement.dataset.raw")
    assert stop_at < read_at, (
        "the previous answer is captured while it is still streaming"
    )


def test_the_stop_is_awaited():
    body = _fn("regenerateFrom")
    assert re.search(r"await\s+_stopThreadForRegen", body), (
        "the stop is fired and not awaited, so the truncate races it"
    )


def test_stopping_waits_for_the_run_to_actually_go():
    body = _fn("_stopThreadForRegen")
    assert "/api/chat/stop/" in body, "no server-side stop"
    assert "stream_status" in body, (
        "nothing confirms the run stopped; the POST only requests it"
    )
    assert "setTimeout" in body, "no wait between checks -- this would spin"


def test_stopping_also_cancels_research_on_that_thread():
    """Deep research is a separate server-side job on the same thread."""
    body = _fn("_stopThreadForRegen")
    assert "/api/research/cancel/" in body, (
        "research keeps running for a question that no longer exists"
    )
    assert "_researchingStreamIds" in body, (
        "research is cancelled unconditionally rather than when running"
    )


def test_stopping_reports_when_it_took_the_machine_back():
    """Regenerate goes through the same Stop, so it releases screen control.

    Silently is how the last one went wrong: a release nobody can see is
    indistinguishable from no release.
    """
    body = _fn("_stopThreadForRegen")
    assert "screen_control_revoked" in body
    assert "uiModule.showToast" in body, "the release is not surfaced"
    assert "window.uiModule" not in body, (
        "goes through the global nothing assigns, so it never shows"
    )


def test_the_fork_still_keeps_previous_answers():
    """The variant pager is the fork; regenerate must keep feeding it."""
    body = _fn("regenerateFrom")
    assert "_pendingVariants" in body, "the previous answer is not kept"
    src = _CHAT.read_text()
    assert "update-last-meta" in src, (
        "variants are never persisted, so the fork dies on reload"
    )
