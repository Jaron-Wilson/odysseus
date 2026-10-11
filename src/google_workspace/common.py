"""Small helpers the Google product modules share: IDs from links, cutting
long results down with a pointer to the rest, and Docs' UTF-16 indexes."""

import re
import urllib.parse
from typing import Dict, Optional, Tuple

from src.google_workspace.connection import GoogleError

DEFAULT_CHARS = 12000
MAX_CHARS = 40000

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_PATH_ID_RE = re.compile(r"/(?:d|folders)/([A-Za-z0-9_-]{10,200})")


def file_id(raw, what: str = "file") -> str:
    """The ID in a Google link (docs/sheets/slides/drive) or a bare ID.
    Raises GoogleError for anything else, so a value never reaches a URL
    path unchecked."""
    s = str(raw or "").strip()
    if not s:
        raise GoogleError(f"Which {what}? Pass its ID or link.")
    if _ID_RE.match(s):
        return s
    if s.startswith(("http://", "https://")):
        u = urllib.parse.urlsplit(s)
        m = _PATH_ID_RE.search(u.path)
        if m:
            return m.group(1)
        q = urllib.parse.parse_qs(u.query).get("id")
        if q and _ID_RE.match(q[0]):
            return q[0]
    raise GoogleError(f"That does not look like a Google {what} ID or link: {s[:80]}")


def clamp(value, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def window(text: str, offset=0, max_chars=None, tool: str = "") -> Tuple[str, str]:
    """A slice of `text` and, when there is more, a note saying how to get it."""
    total = len(text)
    start = clamp(offset, 0, 0, max(0, total))
    size = clamp(max_chars, DEFAULT_CHARS, 200, MAX_CHARS)
    part = text[start:start + size]
    end = start + len(part)
    note = ""
    if end < total:
        note = (f"[Showing characters {start}-{end} of {total}. "
                f"Call {tool or 'this tool'} again with offset={end} for more.]")
    elif start:
        note = f"[Showing characters {start}-{end} of {total}: the end.]"
    return part, note


def utf16_len(s: str) -> int:
    """Docs indexes count UTF-16 code units, not Python characters."""
    return len(s.encode("utf-16-le")) // 2


def link_for(kind: str, fid: str) -> str:
    return {
        "doc": f"https://docs.google.com/document/d/{fid}/edit",
        "sheet": f"https://docs.google.com/spreadsheets/d/{fid}/edit",
        "slides": f"https://docs.google.com/presentation/d/{fid}/edit",
        "folder": f"https://drive.google.com/drive/folders/{fid}",
    }.get(kind, f"https://drive.google.com/file/d/{fid}/view")


_SAFE_LINK = re.compile(r"^https://(?:docs|drive|calendar|tasks|contacts)\.google\.com/[^\s<>()\[\]]*$")


def safe_link(url: Optional[str]) -> str:
    """Only plain Google links go into a chat reply."""
    u = str(url or "").strip()
    return u if _SAFE_LINK.match(u) else ""


def link_line(link: Dict) -> str:
    """The Markdown line the chat gets after a create or edit, so the user
    always has the link, whatever the model writes."""
    url = safe_link((link or {}).get("url"))
    if not url:
        return ""
    name = re.sub(r"[\[\]\r\n]", " ", str((link or {}).get("name") or "Open in Google")).strip()[:120]
    verb = str((link or {}).get("verb") or "Open")
    return f"\n\n[{verb}: {name}]({url})\n"
