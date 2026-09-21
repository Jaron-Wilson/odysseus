"""Asking for a PDF has to produce a PDF.

There was no path from a document to a rendered PDF at all. The two
routes that emit one fill an *uploaded* PDF's form fields and refused
anything else with "Document is not linked to a source PDF"; the real
markdown renderer (src/doc_pdf.render_markdown_pdf) had a single caller,
Claude Code plans. Meanwhile create_document validated `language`
against a set with no "pdf" in it, so `language="pdf"` was dropped,
re-sniffed as markdown, and stored without a word to anyone.

The result was a document labelled pdf, carrying a PDF icon, containing
markdown -- a capability advertised and never delivered. These tests pin
the three places that has to hold: the renderer works, the route reaches
it, and the tool answers the request instead of discarding it.
"""
import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from routes.document_routes import _sanitize_doc_language

_REPO = Path(__file__).resolve().parent.parent

_PLAIN = SimpleNamespace(language="markdown", current_content="# Notes\n\nhi\n")
_FORM = SimpleNamespace(
    language="markdown",
    current_content=(
        '<!-- pdf_form_source upload_id="0123456789abcdef0123456789abcdef.pdf" fields="3" -->'
        "\n\n# Intake\n"
    ),
)


# --------------------------------------------------------------------------
# 1. "pdf" must not be stored as an ordinary document's language
# --------------------------------------------------------------------------

def test_pdf_language_is_refused_on_a_plain_document():
    """Storing it is what made markdown wear a PDF icon and a .pdf name."""
    assert _sanitize_doc_language("pdf", _PLAIN) == "markdown"


def test_pdf_language_survives_on_a_form_backed_document():
    """There it is a view toggle between the PDF and its markdown source."""
    assert _sanitize_doc_language("pdf", _FORM) == "pdf"


def test_other_languages_pass_through_untouched():
    assert _sanitize_doc_language("python", _PLAIN) == "python"
    assert _sanitize_doc_language("markdown", _PLAIN) == "markdown"
    assert _sanitize_doc_language(None, _PLAIN) is None


# --------------------------------------------------------------------------
# 2. the route reaches the renderer instead of refusing
# --------------------------------------------------------------------------

def _export_pdf_endpoint():
    from routes import document_routes

    router = document_routes.setup_document_routes(SimpleNamespace())
    for route in router.routes:
        if getattr(route, "path", "") == "/api/document/{doc_id}/export-pdf":
            return route.endpoint
    raise AssertionError("export-pdf route not registered")


class _Query:
    def __init__(self, doc):
        self._doc = doc

    def filter(self, *_):
        return self

    def first(self):
        return self._doc


class _Db:
    def __init__(self, doc):
        self._doc = doc
        self.closed = False

    def query(self, *_):
        return _Query(self._doc)

    def close(self):
        self.closed = True


def test_plain_document_export_renders_its_markdown(monkeypatch, tmp_path):
    """The old branch raised 400 here; a document is not a failure case."""
    from routes import document_routes
    import src.doc_pdf as doc_pdf

    doc = SimpleNamespace(
        id="doc-1", title="Quarterly Notes",
        current_content="# Quarterly Notes\n\nbody text\n",
    )
    out = tmp_path / "rendered.pdf"
    out.write_bytes(b"%PDF-1.7\nstub\n")
    seen = {}

    async def _fake_render(markdown, out_name, running_title="x"):
        seen["markdown"] = markdown
        seen["out_name"] = out_name
        seen["running_title"] = running_title
        return str(out), None

    monkeypatch.setattr(doc_pdf, "render_markdown_pdf", _fake_render)
    monkeypatch.setattr(document_routes, "SessionLocal", lambda: _Db(doc))
    monkeypatch.setattr(document_routes, "get_current_user", lambda r: "jaron")
    monkeypatch.setattr(document_routes, "_verify_doc_owner", lambda *a, **k: None)

    resp = asyncio.run(_export_pdf_endpoint()("doc-1", SimpleNamespace()))

    assert resp.media_type == "application/pdf"
    assert resp.filename.endswith(".pdf")
    # The document's own text, not a placeholder or the stored title alone.
    assert seen["markdown"] == doc.current_content
    assert seen["running_title"] == "Quarterly Notes"


def test_render_failure_is_reported_not_swallowed(monkeypatch, tmp_path):
    """A silent empty download is the failure mode this whole area had."""
    from fastapi import HTTPException
    from routes import document_routes
    import src.doc_pdf as doc_pdf

    doc = SimpleNamespace(id="doc-2", title="Notes", current_content="# Notes\n")

    async def _fail(markdown, out_name, running_title="x"):
        return None, "node is not installed"

    monkeypatch.setattr(doc_pdf, "render_markdown_pdf", _fail)
    monkeypatch.setattr(document_routes, "SessionLocal", lambda: _Db(doc))
    monkeypatch.setattr(document_routes, "get_current_user", lambda r: "jaron")
    monkeypatch.setattr(document_routes, "_verify_doc_owner", lambda *a, **k: None)

    with pytest.raises(HTTPException) as err:
        asyncio.run(_export_pdf_endpoint()("doc-2", SimpleNamespace()))
    assert err.value.status_code == 500
    assert "node is not installed" in str(err.value.detail)


# --------------------------------------------------------------------------
# 3. the agent tool answers a PDF request instead of dropping it
# --------------------------------------------------------------------------

class _SessionModel:
    """Stands in for the chat-session ORM class the tool filters on."""
    id = "id"


class _ToolDb:
    def __init__(self, session_owner="jaron"):
        self.added = []
        self._owner = session_owner

    def query(self, *_):
        return _Query(SimpleNamespace(id="sess-1", owner=self._owner))

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _run_create(monkeypatch, raw):
    import src.database as database
    from src.agent_tools.document_tools import CreateDocumentTool

    db = _ToolDb()
    # conftest installs a minimal src.database stub (SessionLocal +
    # ModelEndpoint only), so the names the tool imports have to be put
    # there or the import inside execute() fails before any logic runs.
    monkeypatch.setattr(database, "SessionLocal", lambda: db, raising=False)
    monkeypatch.setattr(database, "Session", _SessionModel, raising=False)
    for name in ("Document", "DocumentVersion"):
        monkeypatch.setattr(
            database, name,
            lambda **kw: SimpleNamespace(**kw),
            raising=False,
        )
    result = asyncio.run(
        CreateDocumentTool().execute(raw, {"session_id": "sess-1", "owner": "jaron"})
    )
    return result, db


def test_requesting_a_pdf_returns_a_pdf_link(monkeypatch):
    """`language="pdf"` used to vanish with no signal to the model."""
    raw = "<title>Report</title><language>pdf</language><content># Report\n\nbody\n</content>"
    result, _ = _run_create(monkeypatch, raw)

    assert "error" not in result, result
    assert "pdf" in result, "a requested PDF produced no PDF link"
    assert "/export-pdf" in result["pdf"], result["pdf"]
    assert result["doc_id"] in result["pdf"]
    # Still an editable document underneath -- a PDF is not editable text.
    assert result["language"] != "pdf"


def test_a_pdf_titled_document_also_gets_a_link(monkeypatch):
    raw = "<title>invoice.pdf</title><content># Invoice\n\nbody\n</content>"
    result, _ = _run_create(monkeypatch, raw)
    assert "pdf" in result, "a .pdf title produced no PDF link"
    assert ".pdf]" not in result["pdf"], "link label kept the extension twice"


def test_an_ordinary_document_gets_no_pdf_link(monkeypatch):
    raw = "<title>Notes</title><language>markdown</language><content># Notes\n</content>"
    result, _ = _run_create(monkeypatch, raw)
    assert "pdf" not in result


# --------------------------------------------------------------------------
# 4. the renderer itself actually works here
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.skipif(
    not (_REPO / "node_modules" / "playwright").is_dir(),
    reason="node deps not installed",
)
def test_renderer_emits_a_real_pdf(tmp_path, monkeypatch):
    """Everything above is wiring; this is the part that must not be a stub."""
    import src.doc_pdf as doc_pdf

    monkeypatch.setattr(doc_pdf, "PDF_DIR", tmp_path)
    path, reason = asyncio.run(
        doc_pdf.render_markdown_pdf(
            "# Title\n\nSome **bold** text.\n\n- a\n- b\n",
            "pytest-export",
            running_title="test",
        )
    )
    assert path, f"renderer returned no file: {reason}"
    data = Path(path).read_bytes()
    assert data.startswith(b"%PDF-"), "output is not a PDF"
    assert len(data) > 1000, "PDF is suspiciously small"
