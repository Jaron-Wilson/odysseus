"""PDF pages as images, for the full-screen PDF viewer on phones.

Phone browsers (Chrome on Android) do not render a PDF inside a frame, so the
viewer (static/js/pdfViewer.js) asks for pages as images instead. The PDF is
fetched from this same app, in-process and with the caller's own cookies, so
this can only render what the user could already open: `src` must be a path
on this server (e.g. /api/claude_code/plan/<id>/pdf), never another host.
"""

import hashlib
import time
from typing import Dict, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

_CACHE: Dict[str, Tuple[float, bytes]] = {}
_TTL_S = 120
_MAX_CACHED = 6


def _valid_src(src: str) -> str:
    src = (src or "").strip()
    if not src.startswith("/api/") or "//" in src or "\\" in src or ".." in src:
        raise HTTPException(400, "src must be a PDF path on this server")
    return src


async def _pdf_bytes(request: Request, src: str) -> bytes:
    key = hashlib.sha1((src + "|" + request.headers.get("cookie", "")).encode()).hexdigest()
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL_S:
        return hit[1]
    transport = httpx.ASGITransport(app=request.app)
    headers = {k: v for k, v in request.headers.items()
               if k.lower() in ("cookie", "authorization", "x-forwarded-for", "x-forwarded-proto")}
    async with httpx.AsyncClient(transport=transport, base_url="http://odysseus.internal") as c:
        r = await c.get(src, headers=headers)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/pdf"):
        raise HTTPException(404 if r.status_code == 200 else r.status_code, "Not a PDF")
    if len(_CACHE) >= _MAX_CACHED:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    _CACHE[key] = (time.time(), r.content)
    return r.content


def setup_pdf_view_routes() -> APIRouter:
    router = APIRouter(tags=["pdf-view"])

    @router.get("/api/pdf-view/info")
    async def info(request: Request, src: str):
        import pymupdf as fitz
        data = await _pdf_bytes(request, _valid_src(src))
        with fitz.open(stream=data, filetype="pdf") as doc:
            return {"pages": doc.page_count, "title": (doc.metadata or {}).get("title") or ""}

    @router.get("/api/pdf-view/page")
    async def page(request: Request, src: str, n: int = 1, w: int = 1200):
        import pymupdf as fitz
        data = await _pdf_bytes(request, _valid_src(src))
        w = max(300, min(int(w or 1200), 2400))
        with fitz.open(stream=data, filetype="pdf") as doc:
            if not 1 <= n <= doc.page_count:
                raise HTTPException(404, "No such page")
            pg = doc[n - 1]
            zoom = w / max(pg.rect.width, 1)
            png = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    return router
