"""Open PDF: a full-screen viewer for a PDF link in the chat (not the documents
sidebar). The browser's own viewer on a computer; page images on a phone,
rendered from this app's own PDF (in-process, with the caller's cookies)."""
import os

import pymupdf
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pdf(pages=2) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 100), f"page {i + 1}")
    return doc.tobytes()


def _app():
    from routes import pdf_view_routes as pv
    pv._CACHE.clear()
    app = FastAPI()
    app.include_router(pv.setup_pdf_view_routes())

    @app.get("/api/doc.pdf")
    async def doc():
        return Response(_pdf(), media_type="application/pdf")

    @app.get("/api/not-pdf")
    async def not_pdf():
        return {"hello": "world"}
    return TestClient(app)


def test_pages_as_images():
    c = _app()
    assert c.get("/api/pdf-view/info", params={"src": "/api/doc.pdf"}).json()["pages"] == 2
    r = c.get("/api/pdf-view/page", params={"src": "/api/doc.pdf", "n": 2, "w": 600})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert c.get("/api/pdf-view/page", params={"src": "/api/doc.pdf", "n": 3}).status_code == 404


def test_only_pdfs_on_this_server():
    c = _app()
    for bad in ("https://example.com/x.pdf", "//evil/x.pdf", "/static/../app.db", "/login"):
        assert c.get("/api/pdf-view/info", params={"src": bad}).status_code == 400, bad
    assert c.get("/api/pdf-view/info", params={"src": "/api/not-pdf"}).status_code == 404


def test_wiring():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    mw = read("core", "middleware.py")
    assert 'response.headers.get("content-type", "").startswith("application/pdf"))' in mw
    assert 'request.query_params.get("inline") == "1"' in mw
    md = read("static", "js", "markdown.js")
    assert "btn.className = 'pdf-open-btn';" in md
    assert "import './pdfViewer.js';" in read("static", "js", "chat.js")
    # Approving an already-approved plan reads as its state, not a failure.
    rend = read("static", "js", "chatRenderer.js")
    assert "already (approved|used|denied)" in rend and "if (a.dataset.busy) return;" in rend
