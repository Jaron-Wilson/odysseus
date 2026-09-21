"""The plan link has to work, and it must not cost you the chat you're in.

Two faults met on one link.

The id allowlist was `^[a-zA-Z0-9-]{1,128}$`, but the agent SDKs hand
back ids like "ses_f3ba77bb0ffespZ8YzTJn2Ku8G". No underscore meant
every real id was rejected, so `/api/claude_code/plan/<id>/pdf` answered
400 -- and so did `/approve/<id>`, which is why the Approve button could
not approve anything and typing "i approve" could not stand in for it.

And the link was rendered as an ordinary external link, so clicking it
navigated away from the app. With the 400 above, that meant landing on a
page of raw JSON, with the chat gone and only the back button to return
by. A link that yields a file should yield a file and leave the page
alone.
"""
import re
from pathlib import Path

import pytest

from routes.claude_code_routes import _SESSION_ID_RE

_REPO = Path(__file__).resolve().parent.parent
_JS = _REPO / "static" / "js"


# --------------------------------------------------------------------------
# 1. real session ids are accepted; path tricks still are not
# --------------------------------------------------------------------------

@pytest.mark.parametrize("session_id", [
    "ses_f3ba77bb0ffespZ8YzTJn2Ku8G",   # the one from the report
    "ses_0123456789abcdef",
    "plain-uuid-style-1234",
    "A_B-c9",
])
def test_real_session_ids_are_accepted(session_id):
    assert _SESSION_ID_RE.fullmatch(session_id), (
        "%r is a live SDK session id and was rejected" % session_id
    )


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "a/b",
    "a.b",            # the id is interpolated into a filename
    "a\\b",
    "a b",
    "",
    "x" * 129,
])
def test_path_tricks_are_still_refused(bad):
    assert not _SESSION_ID_RE.fullmatch(bad), "%r should not pass" % bad


def test_the_id_cannot_escape_the_pdf_directory():
    """The validated id is interpolated straight into plan-<id>.pdf."""
    import os
    for candidate in ["ses_f3ba77bb0ffespZ8YzTJn2Ku8G", "A_B-c9"]:
        assert _SESSION_ID_RE.fullmatch(candidate)
        joined = os.path.join("/pdfs", f"plan-{candidate}.pdf")
        assert os.path.normpath(joined).startswith("/pdfs" + os.sep)


# --------------------------------------------------------------------------
# 2. an API link downloads in place instead of navigating
# --------------------------------------------------------------------------

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
)


def _slice_fn(name):
    src = (_JS / "chatRenderer.js").read_text()
    i = src.index("async function %s(" % name)
    k = src.index("{", i)
    depth = 0
    for n in range(k, len(src)):
        if src[n] == "{":
            depth += 1
        elif src[n] == "}":
            depth -= 1
            if depth == 0:
                return src[i:n + 1]
    raise AssertionError("unbalanced braces slicing %s" % name)


@pytest.fixture(scope="module")
def page():
    try:
        with playwright_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                pytest.skip("chromium unavailable: %s" % exc)
            pg = browser.new_page()
            pg.set_content("<a id='lnk' href='/api/x'>plan PDF</a>")
            pg.add_script_tag(content=_slice_fn("downloadWithoutLeaving"))
            yield pg
            browser.close()
    except Exception as exc:
        pytest.skip("playwright unusable: %s" % exc)


def test_a_failed_download_reports_on_the_link_not_a_blank_page(page):
    """This is the case that used to replace the chat with raw JSON."""
    detail = page.evaluate(
        """async () => {
            window.fetch = async () => ({
              ok: false, status: 400,
              json: async () => ({ detail: 'Invalid session ID format' }),
            });
            const a = document.getElementById('lnk');
            await downloadWithoutLeaving(a, 'https://x/api/y');
            return a.textContent;
        }"""
    )
    assert "Invalid session ID format" in detail, detail
    # Still on the same document: nothing navigated.
    assert page.evaluate("() => !!document.getElementById('lnk')")


def test_a_successful_download_restores_the_label_and_saves_a_file(page):
    result = page.evaluate(
        """async () => {
            const clicked = [];
            window.fetch = async () => ({
              ok: true,
              headers: { get: () => 'attachment; filename="plan-abc.pdf"' },
              blob: async () => new Blob(['%PDF-'], { type: 'application/pdf' }),
            });
            const realCreate = document.createElement.bind(document);
            document.createElement = (tag) => {
              const el = realCreate(tag);
              if (tag === 'a') el.click = () => clicked.push(el.download);
              return el;
            };
            const a = document.getElementById('lnk');
            a.textContent = 'plan PDF';
            await downloadWithoutLeaving(a, 'https://x/api/y');
            document.createElement = realCreate;
            return { label: a.textContent, clicked };
        }"""
    )
    assert result["label"] == "plan PDF", "the link was left mid-progress"
    assert result["clicked"] == ["plan-abc.pdf"], (
        "the server's filename was not used: %r" % (result["clicked"],)
    )


def test_click_handler_intercepts_api_links():
    """Source-level: the delegate must not fall through to navigation."""
    src = (_JS / "chatRenderer.js").read_text()
    after = src[src.index("document.addEventListener('click', function(e) {"):]
    assert "u.pathname.startsWith('/api/')" in after, (
        "same-origin API links still navigate away from the chat"
    )
    assert "downloadWithoutLeaving(a, u.href)" in after
