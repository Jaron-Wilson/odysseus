"""Reloading mid-answer must show the answer, and the approval must come back.

Reloading during a generation already reattached to the detached run,
but the replay renderer was not the live one: it called mdToHtml on the
raw round text. Thinking is not a separate event -- it arrives as
<think> tags inside the delta -- so the live path checks
hasUnclosedThinkTag and shows a "Thinking" bar, while the resume path
knew nothing about it. Reload while the model was thinking and you saw
either nothing or raw reasoning, never "still thinking".

The resume loop also dropped `ui_event`, which is what raises the
screen-control prompt. Reloading while something waited on approval lost
the prompt: the request stayed pending and answerable on disk, but
nothing on screen said so, so the run looked hung. A pending request
outlives the run's replay buffer entirely, so the page also has to be
able to ask.
"""
import re
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_JS = _REPO / "static" / "js"


# --------------------------------------------------------------------------
# resume renders what the live stream renders
# --------------------------------------------------------------------------

def _resume_source():
    src = (_JS / "chat.js").read_text()
    i = src.index("export async function resumeStream(")
    return src[i:i + 9000]


def test_resume_shows_that_the_model_is_still_thinking():
    body = _resume_source()
    assert "hasUnclosedThinkTag" in body, (
        "resume cannot tell thinking from reply, so a reload mid-thought "
        "shows nothing that says it is still thinking"
    )
    assert "thinking-section" in body, "no Thinking indicator on resume"


def test_resume_collapses_finished_thinking_like_the_live_path():
    body = _resume_source()
    assert "processWithThinking" in body, (
        "resume renders closed <think> blocks with the plain renderer"
    )


def test_the_thinking_helpers_are_actually_reachable():
    """Both are called through the default import, so both must be on it.

    A missing name here fails silently -- the branch runs, the helper is
    undefined, and the reader just sees the unhelpful version.
    """
    md = (_JS / "markdown.js").read_text()
    default_obj = md[md.index("const markdownModule = {"):]
    for name in ("processWithThinking", "hasUnclosedThinkTag"):
        assert re.search(r"^\s*%s,\s*$" % name, default_obj, re.M), (
            "markdown.js default export is missing %s" % name
        )


def test_resume_restores_the_approval_prompt():
    body = _resume_source()
    assert "screen_control_request" in body, (
        "resume drops the ui_event, so a reload loses the approval prompt"
    )
    assert "showScreenControlModal" in body


# --------------------------------------------------------------------------
# the prompt survives even when the run's buffer does not
# --------------------------------------------------------------------------

def test_the_page_asks_on_load_whether_an_approval_is_waiting():
    src = (_JS / "sessions.js").read_text()
    assert "/api/screen_control/pending" in src, (
        "nothing asks the server for a waiting approval, so one that "
        "outlived its stream can never be answered"
    )
    assert "showScreenControlModal" in src


def test_approving_inline_opens_the_modal_rather_than_acting():
    """Handing over the mouse and keyboard should be confirmed, not fired."""
    src = (_JS / "chatRenderer.js").read_text()
    i = src.index("if (kind === 'screencontrol') {")
    branch = src[i:i + 1800]
    assert "showScreenControlModal(reqId" in branch, (
        "the inline Approve link still acts on the click"
    )
    # Denying needs no confirmation, and must still work from the link.
    assert "screen_control/deny/" in branch


def test_the_modal_states_what_approving_grants():
    """The bound is the whole point after a run reached round 24."""
    src = (_JS / "chatRenderer.js").read_text()
    i = src.index("export function showScreenControlModal")
    body = src[i:i + 1600]
    assert "25 actions" in body and "15 minutes" in body, (
        "the modal does not say what approving actually buys"
    )
    assert "Stop" in body, "the modal does not mention how to end it"


# --------------------------------------------------------------------------
# the endpoint behind it
# --------------------------------------------------------------------------

@pytest.fixture
def approvals(tmp_path, monkeypatch):
    import src.screen_control_approvals as mod
    monkeypatch.setattr(mod, "APPROVALS_FILE", str(tmp_path / "grants.json"))
    monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
    return mod


def test_pending_lists_only_unanswered_requests(approvals):
    a = approvals.request_grant("srv-1", "Windows PC", "jaron", "click")
    b = approvals.request_grant("srv-2", "Laptop", "jaron", "screenshot")
    approvals.set_status(b["id"], "approved", owner="jaron")

    ids = [r["id"] for r in approvals.pending_for("jaron")]
    assert ids == [a["id"]], "an answered request is still being offered"


def test_pending_is_owner_scoped(approvals):
    approvals.request_grant("srv-1", "Windows PC", "someone-else", "click")
    assert approvals.pending_for("jaron") == [], (
        "another person's approval prompt was offered to this user"
    )


def test_pending_drops_requests_too_old_to_answer(approvals):
    rec = approvals.request_grant("srv-1", "Windows PC", "jaron", "click")
    data = approvals._load()
    data[rec["id"]]["created"] = time.time() - approvals.REQUEST_TTL_S - 5
    approvals._save(data)
    assert approvals.pending_for("jaron") == [], (
        "a stale request is still clickable; answering it would grant "
        "control for a reason nobody remembers"
    )


def test_the_route_is_registered_and_scoped():
    src = (_REPO / "routes" / "screen_control_routes.py").read_text()
    assert '@router.get("/pending")' in src
    i = src.index('@router.get("/pending")')
    body = src[i:i + 700]
    assert "_require_user(request)" in body, "the pending list is unauthenticated"
    assert "pending_for(user)" in body, "the pending list is not owner-scoped"
