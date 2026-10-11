"""Google Docs: read a Doc as Markdown-ish text, create one, append, insert,
find and replace. Headings ('# '), bullets ('- '), numbered items ('1. ')
and **bold** in the text written become real Docs formatting through one
documents.batchUpdate.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

from src.google_workspace import connection
from src.google_workspace.common import file_id, link_for, utf16_len, window
from src.google_workspace.connection import GoogleError

logger = logging.getLogger(__name__)

API = "https://docs.googleapis.com/v1/documents"
MAX_WRITE = 200_000

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+•]\s+(.*)$")
_NUMBER = re.compile(r"^\s*\d{1,3}[.)]\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


# ── reading ──

def _runs(elements: List[Dict]) -> str:
    out = []
    for el in elements or []:
        tr = el.get("textRun")
        if tr:
            out.append(tr.get("content") or "")
    return "".join(out)


def _paragraph(p: Dict, lists: Dict) -> str:
    text = _runs(p.get("elements")).rstrip("\n").replace("\u000b", "\n")
    style = (p.get("paragraphStyle") or {}).get("namedStyleType") or ""
    if style == "TITLE":
        return f"# {text}" if text else ""
    if style.startswith("HEADING_"):
        n = int(style.rsplit("_", 1)[1] or 1)
        return f"{'#' * min(6, n)} {text}" if text else ""
    b = p.get("bullet")
    if b:
        level = int(b.get("nestingLevel") or 0)
        levels = (((lists.get(b.get("listId") or "") or {}).get("listProperties") or {}).get("nestingLevels") or [])
        glyph = levels[level] if level < len(levels) else {}
        numbered = bool(glyph.get("glyphType")) and glyph.get("glyphType") not in ("GLYPH_TYPE_UNSPECIFIED", "NONE")
        return f"{'  ' * level}{'1.' if numbered else '-'} {text}"
    return text


def _table(t: Dict, lists: Dict) -> str:
    rows = []
    for i, row in enumerate(t.get("tableRows") or []):
        cells = []
        for cell in row.get("tableCells") or []:
            parts = [_paragraph(c["paragraph"], lists) for c in cell.get("content") or [] if "paragraph" in c]
            cells.append(" ".join(x for x in parts if x).replace("|", "\\|"))
        rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            rows.append("|" + " --- |" * len(cells))
    return "\n".join(rows)


def to_markdown(doc: Dict) -> str:
    body = (doc.get("body") or {}).get("content") or []
    lists = doc.get("lists") or {}
    out: List[str] = []
    for el in body:
        if "paragraph" in el:
            out.append(_paragraph(el["paragraph"], lists))
        elif "table" in el:
            out.append(_table(el["table"], lists))
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def _get(user: Optional[str], did: str) -> Dict:
    async with connection.client() as c:
        r = await connection.call(c, user, "GET", f"{API}/{did}")
    if r.status_code != 200:
        raise connection.explain(r, "docs", "document")
    return r.json()


async def read(user: Optional[str], raw_id: str, offset=0, max_chars=None) -> Dict:
    did = file_id(raw_id, "document")
    doc = await _get(user, did)
    text = to_markdown(doc)
    part, note = window(text, offset, max_chars, "google_doc_read")
    head = f"# {doc.get('title') or 'Untitled'}  (id {did}, {link_for('doc', did)})\n\n"
    return {"output": head + (part or "(The document is empty.)") + (("\n" + note) if note else ""),
            "exit_code": 0}


# ── writing ──

def parse_lines(text: str) -> List[Tuple[str, str, List[Tuple[int, int]]]]:
    """(plain text, style, bold spans) per line. style is NORMAL_TEXT,
    HEADING_1..6, BULLET or NUMBER. Bold spans are UTF-16 offsets."""
    out = []
    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        style = "NORMAL_TEXT"
        m = _HEADING.match(raw)
        if m:
            style, raw = f"HEADING_{len(m.group(1))}", m.group(2)
        else:
            m = _BULLET.match(raw)
            if m:
                style, raw = "BULLET", m.group(1)
            else:
                m = _NUMBER.match(raw)
                if m:
                    style, raw = "NUMBER", m.group(1)
        spans, plain, pos = [], "", 0
        for bm in _BOLD.finditer(raw):
            plain += raw[pos:bm.start()]
            start = utf16_len(plain)
            plain += bm.group(1)
            spans.append((start, utf16_len(plain)))
            pos = bm.end()
        plain += raw[pos:]
        out.append((plain, style, spans))
    while out and not out[-1][0] and out[-1][1] == "NORMAL_TEXT":
        out.pop()
    return out


def build_requests(text: str, at: int, lead_newline: bool = False) -> List[Dict]:
    """The batchUpdate requests that insert `text` at index `at` with its
    headings, lists and bold applied. lead_newline starts a new paragraph
    first (appending after the last one)."""
    lines = parse_lines(text)
    if not lines:
        return []
    body = "\n".join(t for t, _, _ in lines)
    full = ("\n" + body) if lead_newline else (body + "\n")
    reqs: List[Dict] = [{"insertText": {"location": {"index": at}, "text": full}}]
    pos = at + (1 if lead_newline else 0)
    spans = []                      # (start, end, style)
    bolds = []
    for t, style, b in lines:
        n = utf16_len(t)
        spans.append((pos, pos + n + 1, style))
        bolds.extend((pos + s, pos + e) for s, e in b)
        pos += n + 1
    whole = {"startIndex": spans[0][0], "endIndex": spans[-1][1]}
    # Paragraph styles, merged across neighbors with the same one. Lists are
    # normal text with bullets on top.
    i = 0
    while i < len(spans):
        named = spans[i][2] if spans[i][2].startswith("HEADING_") else "NORMAL_TEXT"
        j = i
        while j + 1 < len(spans) and (spans[j + 1][2] if spans[j + 1][2].startswith("HEADING_") else "NORMAL_TEXT") == named:
            j += 1
        reqs.append({"updateParagraphStyle": {"range": {"startIndex": spans[i][0], "endIndex": spans[j][1]},
                                              "paragraphStyle": {"namedStyleType": named},
                                              "fields": "namedStyleType"}})
        i = j + 1
    # New paragraphs inherit a list from the one they split; clear, then set.
    reqs.append({"deleteParagraphBullets": {"range": whole}})
    i = 0
    while i < len(spans):
        kind = spans[i][2]
        if kind in ("BULLET", "NUMBER"):
            j = i
            while j + 1 < len(spans) and spans[j + 1][2] == kind:
                j += 1
            reqs.append({"createParagraphBullets": {
                "range": {"startIndex": spans[i][0], "endIndex": spans[j][1]},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE" if kind == "BULLET" else "NUMBERED_DECIMAL_ALPHA_ROMAN"}})
            i = j + 1
        else:
            i += 1
    if bolds:
        # Inserted text takes the style around it; plain unless marked bold.
        reqs.append({"updateTextStyle": {"range": whole, "textStyle": {"bold": False}, "fields": "bold"}})
        for s, e in bolds:
            if e > s:
                reqs.append({"updateTextStyle": {"range": {"startIndex": s, "endIndex": e},
                                                 "textStyle": {"bold": True}, "fields": "bold"}})
    return reqs


async def _batch(user: Optional[str], did: str, reqs: List[Dict], what: str) -> Dict:
    async with connection.client() as c:
        r = await connection.call(c, user, "POST", f"{API}/{did}:batchUpdate", json={"requests": reqs})
    if r.status_code != 200:
        raise connection.explain(r, "docs", what)
    return r.json()


def _check_text(text: str) -> str:
    text = str(text or "")
    if len(text) > MAX_WRITE:
        raise GoogleError(f"That is too much text for one edit (at most {MAX_WRITE} characters).")
    return text


async def create(user: Optional[str], title: str, content: str = "") -> Dict:
    title = " ".join(str(title or "").split())[:300] or "Untitled document"
    content = _check_text(content)
    async with connection.client() as c:
        r = await connection.call(c, user, "POST", API, json={"title": title})
    if r.status_code not in (200, 201):
        raise connection.explain(r, "docs", "create a document")
    did = r.json()["documentId"]
    reqs = build_requests(content, 1)
    if reqs:
        await _batch(user, did, reqs, "write the document")
    link = link_for("doc", did)
    logger.info("[google] made a Doc for %s", user or "-")
    return {"output": f"Created the Google Doc \"{title}\", id {did}: {link}",
            "google_link": {"name": title, "url": link, "verb": "New Google Doc"},
            "file": {"id": did, "name": title, "url": link}, "exit_code": 0}


def _end_index(doc: Dict) -> int:
    content = (doc.get("body") or {}).get("content") or []
    return int(content[-1].get("endIndex") or 2) if content else 2


async def edit(user: Optional[str], raw_id: str, action: str, text: str = "", index=None,
               find: str = "", replace: str = "", match_case: bool = True) -> Dict:
    did = file_id(raw_id, "document")
    action = str(action or "").lower()
    title = "the edited Doc"
    if action == "replace":
        if not find:
            raise GoogleError("What text should be replaced? Pass find.")
        res = await _batch(user, did, [{"replaceAllText": {
            "containsText": {"text": str(find), "matchCase": bool(match_case)},
            "replaceText": _check_text(replace)}}], "edit the document")
        n = int(((res.get("replies") or [{}])[0].get("replaceAllText") or {}).get("occurrencesChanged") or 0)
        msg = f"Replaced {n} occurrence{'s' if n != 1 else ''}." if n else "Found nothing to replace; the Doc is unchanged."
    elif action in ("append", "insert"):
        text = _check_text(text)
        if not text.strip():
            raise GoogleError("What text should be added? Pass text.")
        doc = await _get(user, did)
        title = doc.get("title") or title
        if action == "append":
            end = _end_index(doc)
            empty = end <= 2
            reqs = build_requests(text, max(1, end - 1), lead_newline=not empty)
        else:
            at = 1
            if index is not None:
                try:
                    at = max(1, min(int(index), _end_index(doc) - 1))
                except (TypeError, ValueError):
                    at = 1
            reqs = build_requests(text, at)
        await _batch(user, did, reqs, "edit the document")
        msg = "Added the text at the end." if action == "append" else "Inserted the text."
    else:
        raise GoogleError("action must be append, insert or replace.")
    link = link_for("doc", did)
    return {"output": f"{msg} {link}", "google_link": {"name": title, "url": link, "verb": "Edited Google Doc"},
            "exit_code": 0}
