"""Any document can be viewed as the PDF it renders to.

The page viewer refused anything that was not a filled-in copy of an
uploaded PDF -- "Document is not linked to a source PDF" -- so a
document the assistant wrote and called a PDF opened as plain text.

Both halves already existed: /export-pdf typesets a document's markdown,
and the viewer rasterises a PDF into page images. They were not
connected.
"""
import inspect
import re
from pathlib import Path

from routes import document_routes

_REPO = Path(__file__).resolve().parent.parent
_SRC = (_REPO / "routes" / "document_routes.py").read_text()
_JS = (_REPO / "static" / "js" / "document.js").read_text()


def test_a_shared_resolver_exists_for_both_view_routes():
    assert hasattr(document_routes, "_pdf_for_viewing")
    assert inspect.iscoroutinefunction(document_routes._pdf_for_viewing)


def test_neither_view_route_refuses_a_plain_document():
    for marker in ("async def render_pages(", "async def render_page_png("):
        i = _SRC.index(marker)
        body = _SRC[i:i + 1600]
        assert "Document is not linked to a source PDF" not in body, (
            "%s still refuses a document that has no uploaded PDF" % marker
        )
        assert "_pdf_for_viewing(" in body, "%s does not use the resolver" % marker


def test_the_render_is_cached_against_the_content():
    """The viewer fetches one PNG per page.

    Without a cache each page would relaunch Chromium and re-render the
    whole document, and pages could come from different versions of the
    text if it changed mid-view.
    """
    src = inspect.getsource(document_routes._pdf_for_viewing)
    assert "sha256" in src, "the render is not keyed to the content"
    assert "os.path.isfile(cached)" in src, "a cached render is never reused"


def test_fields_are_only_read_for_a_real_form():
    """A typeset document has no field sidecar; asking would fail the view."""
    i = _SRC.index("async def render_pages(")
    body = _SRC[i:i + 1600]
    assert "if _is_form else []" in body, (
        "the viewer looks for form fields on a document that has none"
    )


def test_the_picker_shows_the_render_for_any_document():
    i = _JS.index("_setPdfViewActive(val === 'pdf');")
    block = _JS[max(0, i - 700):i + 200]
    assert "val === 'pdf' ||" in block, "the pdf option is still form-only"
    assert "_isFormBackedDoc(live) && (val === 'pdf'" not in block, (
        "the old form-backed-only gate survives"
    )


def test_an_ordinary_document_still_opens_as_source():
    """Only a form-backed doc should default into the rendered view."""
    i = _JS.index("const active = explicit === true")
    line = _JS[i:i + 200]
    assert "explicit === true || (isForm && explicit !== false)" in line
