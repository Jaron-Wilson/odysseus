"""Google Slides: read a deck's text, and make a simple deck from an outline
(a title slide, then a title-and-body slide per item)."""

import logging
import uuid
from typing import Dict, List, Optional

from src.google_workspace import connection
from src.google_workspace.common import file_id, link_for, window
from src.google_workspace.connection import GoogleError

logger = logging.getLogger(__name__)

API = "https://slides.googleapis.com/v1/presentations"
MAX_SLIDES = 60


def _shape_text(el: Dict) -> str:
    shape = el.get("shape") or {}
    parts = []
    for te in ((shape.get("text") or {}).get("textElements") or []):
        tr = te.get("textRun")
        if tr:
            parts.append(tr.get("content") or "")
    text = "".join(parts).strip()
    if text:
        return text
    table = el.get("table") or {}
    rows = []
    for row in table.get("tableRows") or []:
        cells = []
        for cell in row.get("tableCells") or []:
            cells.append("".join((te.get("textRun") or {}).get("content", "")
                                 for te in ((cell.get("text") or {}).get("textElements") or [])).strip())
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def to_text(deck: Dict) -> str:
    out = []
    for i, slide in enumerate(deck.get("slides") or [], 1):
        texts = [t for t in (_shape_text(el) for el in slide.get("pageElements") or []) if t]
        out.append(f"## Slide {i}\n" + ("\n".join(texts) if texts else "(no text)"))
        notes = ((slide.get("slideProperties") or {}).get("notesPage") or {}).get("pageElements") or []
        ntext = [t for t in (_shape_text(el) for el in notes) if t]
        if ntext:
            out.append("Notes: " + " ".join(ntext))
    return "\n\n".join(out)


async def read(user: Optional[str], raw_id: str, offset=0, max_chars=None) -> Dict:
    pid = file_id(raw_id, "presentation")
    async with connection.client() as c:
        r = await connection.call(c, user, "GET", f"{API}/{pid}")
    if r.status_code != 200:
        raise connection.explain(r, "slides", "presentation")
    deck = r.json()
    part, note = window(to_text(deck), offset, max_chars, "google_slides_read")
    head = f"# {deck.get('title') or 'Untitled'}  ({len(deck.get('slides') or [])} slides, id {pid}, {link_for('slides', pid)})\n\n"
    return {"output": head + part + (("\n" + note) if note else ""), "exit_code": 0}


def _oid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def outline_requests(title: str, subtitle: str, slides: List[Dict]) -> List[Dict]:
    """createSlide + insertText requests for the title slide and the outline."""
    reqs: List[Dict] = []
    sid, t_id, s_id = _oid("s"), _oid("t"), _oid("st")
    reqs.append({"createSlide": {"objectId": sid, "insertionIndex": 0,
                                 "slideLayoutReference": {"predefinedLayout": "TITLE"},
                                 "placeholderIdMappings": [
                                     {"layoutPlaceholder": {"type": "CENTERED_TITLE", "index": 0}, "objectId": t_id},
                                     {"layoutPlaceholder": {"type": "SUBTITLE", "index": 0}, "objectId": s_id}]}})
    reqs.append({"insertText": {"objectId": t_id, "text": title, "insertionIndex": 0}})
    if subtitle:
        reqs.append({"insertText": {"objectId": s_id, "text": subtitle, "insertionIndex": 0}})
    for n, item in enumerate(slides, 1):
        item = item if isinstance(item, dict) else {"title": str(item)}
        sid, t_id, b_id = _oid("s"), _oid("t"), _oid("b")
        reqs.append({"createSlide": {"objectId": sid, "insertionIndex": n,
                                     "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
                                     "placeholderIdMappings": [
                                         {"layoutPlaceholder": {"type": "TITLE", "index": 0}, "objectId": t_id},
                                         {"layoutPlaceholder": {"type": "BODY", "index": 0}, "objectId": b_id}]}})
        stitle = " ".join(str(item.get("title") or "").split())[:300]
        if stitle:
            reqs.append({"insertText": {"objectId": t_id, "text": stitle, "insertionIndex": 0}})
        bullets = [" ".join(str(b).split())[:500] for b in (item.get("bullets") or []) if str(b).strip()][:30]
        if bullets:
            reqs.append({"insertText": {"objectId": b_id, "text": "\n".join(bullets), "insertionIndex": 0}})
    return reqs


async def create(user: Optional[str], title: str, subtitle: str = "", slides=None) -> Dict:
    title = " ".join(str(title or "").split())[:300] or "Untitled presentation"
    if slides and not isinstance(slides, list):
        raise GoogleError("slides must be a list of {title, bullets}.")
    slides = [s for s in (slides or []) if s][:MAX_SLIDES]
    async with connection.client(60) as c:
        r = await connection.call(c, user, "POST", API, json={"title": title})
        if r.status_code not in (200, 201):
            raise connection.explain(r, "slides", "create a presentation")
        deck = r.json()
        pid = deck["presentationId"]
        reqs = outline_requests(title, " ".join(str(subtitle or "").split())[:300], slides)
        # The blank first slide Google makes goes; the outline replaces it.
        for s in deck.get("slides") or []:
            if s.get("objectId"):
                reqs.append({"deleteObject": {"objectId": s["objectId"]}})
        b = await connection.call(c, user, "POST", f"{API}/{pid}:batchUpdate", json={"requests": reqs})
        if b.status_code != 200:
            raise connection.explain(b, "slides", "fill in the slides")
    link = link_for("slides", pid)
    logger.info("[google] made a Slides deck for %s", user or "-")
    return {"output": f"Created the Google Slides deck \"{title}\" with {len(slides) + 1} slides, id {pid}: {link}",
            "google_link": {"name": title, "url": link, "verb": "New Google Slides deck"},
            "file": {"id": pid, "name": title, "url": link}, "exit_code": 0}
