"""Render markdown to a PDF in the jaronwilson.dev house style.

Thin wrapper over tools/build-doc-pdf.mjs, which drives Chromium through
Playwright for real pagination. Kept out of the request path's way: callers
treat a failure here as cosmetic, because a plan is still perfectly readable
as markdown when the renderer is missing.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

from src.constants import BASE_DIR, DATA_DIR

logger = logging.getLogger(__name__)

BUILDER = os.path.join(BASE_DIR, "tools", "build-doc-pdf.mjs")
PDF_DIR = os.path.join(DATA_DIR, "generated_pdfs")
RENDER_TIMEOUT_S = 120


async def render_markdown_pdf(
    markdown: str,
    out_name: str,
    *,
    running_title: str = "jaronwilson.dev",
) -> Tuple[Optional[str], Optional[str]]:
    """Render `markdown` to PDF_DIR/<out_name>.pdf.

    Returns (path, None) on success or (None, reason) on failure. Never raises:
    every caller so far has something useful to show without the PDF.
    """
    node = shutil.which("node")
    if not node:
        return None, "node is not on PATH"
    if not os.path.isfile(BUILDER):
        return None, f"builder not found at {BUILDER}"

    os.makedirs(PDF_DIR, exist_ok=True)
    safe = "".join(c for c in out_name if c.isalnum() or c in "-_")[:80] or "document"
    md_path = os.path.join(PDF_DIR, f"{safe}.md")
    pdf_path = os.path.join(PDF_DIR, f"{safe}.pdf")
    Path(md_path).write_text(markdown, encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        node, BUILDER, md_path, pdf_path, running_title,
        cwd=BASE_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return None, f"pdf render timed out after {RENDER_TIMEOUT_S}s"

    if proc.returncode != 0 or not os.path.isfile(pdf_path):
        detail = err.decode("utf-8", "replace")[:300] if err else "no output"
        logger.warning("PDF render failed for %s: %s", safe, detail)
        return None, f"pdf render failed: {detail}"
    return pdf_path, None
