"""Everything a conversation produced, in one place.

Files, documents and generated images each live in their own store, which
is right for how they are written and wrong for how they are looked for: a
person remembers "the PDF from that chat", not which subsystem happened to
own it. This gathers them by conversation and hands back one list.

Uploads carry a session_id from the moment they arrive. Documents and
research reports are matched where they record one. Nothing is moved or
copied — this is a view over the existing stores, so deleting a file still
happens in the place that owns it.
"""

import json
import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src.constants import DATA_DIR, DEEP_RESEARCH_DIR, UPLOAD_DIR

logger = logging.getLogger(__name__)

# Rough grouping for the UI. Extension first, mime second: a .md served as
# text/plain is still a document to the person who saved it.
_KIND_BY_EXT = {
    ".md": "document", ".markdown": "document", ".txt": "document",
    ".pdf": "pdf",
    ".html": "page", ".htm": "page",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".svg": "image",
    ".csv": "data", ".json": "data", ".xlsx": "data", ".tsv": "data",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".ogg": "audio",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video",
    ".zip": "archive", ".tar": "archive", ".gz": "archive",
}


def _kind(name: str, mime: str = "") -> str:
    ext = os.path.splitext(name or "")[1].lower()
    if ext in _KIND_BY_EXT:
        return _KIND_BY_EXT[ext]
    mime = (mime or "").lower()
    for prefix, kind in (("image/", "image"), ("audio/", "audio"),
                         ("video/", "video"), ("text/", "document")):
        if mime.startswith(prefix):
            return kind
    if "pdf" in mime:
        return "pdf"
    return "file"


def _sort_key(item: Dict[str, Any]) -> float:
    """Epoch seconds for sorting, whatever the store wrote.

    Uploads record an ISO string, research records an epoch float. Sorting
    those as text puts every epoch value before every ISO one regardless of
    when either happened, which is only invisible while one kind dominates.
    """
    raw = item.get("created")
    if raw in (None, ""):
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw)
    try:
        return float(text)
    except ValueError:
        pass
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26], fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def setup_chat_library_routes() -> APIRouter:
    router = APIRouter(prefix="/api/chat_library", tags=["chat-library"])

    def _require_user(request: Request) -> str:
        if os.getenv("AUTH_ENABLED", "true").lower() == "false":
            return ""
        user = getattr(request.state, "current_user", None)
        if not user:
            raise HTTPException(401, "Sign in first.")
        return user

    def _uploads_for(session_id: str, owner: str) -> List[Dict[str, Any]]:
        path = os.path.join(UPLOAD_DIR, "uploads.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            return []
        rows = data if isinstance(data, list) else list(data.values())
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            if owner and r.get("owner") and r.get("owner") != owner:
                continue
            if session_id and r.get("session_id") != session_id:
                continue
            name = r.get("name") or r.get("original_name") or r.get("id") or ""
            out.append({
                "id": r.get("id"),
                "name": name,
                "kind": _kind(name, r.get("mime", "")),
                "mime": r.get("mime", ""),
                "size": r.get("size"),
                "created": r.get("uploaded_at"),
                "source": "upload",
                "url": f"/api/upload/{r.get('id')}" if r.get("id") else None,
            })
        return out

    def _documents_for(session_id: str, owner: str) -> List[Dict[str, Any]]:
        try:
            from core.database import SessionLocal, Document
        except ImportError:
            return []
        db = SessionLocal()
        try:
            q = db.query(Document)
            # Documents only sometimes record a session; filtering on a
            # column that may not exist would drop them all.
            if session_id and hasattr(Document, "session_id"):
                q = q.filter(Document.session_id == session_id)
            elif session_id:
                return []
            if owner and hasattr(Document, "owner"):
                q = q.filter(Document.owner == owner)
            rows = q.limit(200).all()
            out = []
            for d in rows:
                title = getattr(d, "title", "") or "Untitled"
                lang = (getattr(d, "language", "") or "").lower()
                out.append({
                    "id": getattr(d, "id", None),
                    "name": title,
                    "kind": "page" if lang in ("html", "htm") else "document",
                    "mime": "text/html" if lang in ("html", "htm") else "text/markdown",
                    "size": len(getattr(d, "content", "") or ""),
                    "created": str(getattr(d, "created_at", "") or ""),
                    "source": "document",
                    "url": f"#document-{getattr(d, 'id', '')}",
                })
            return out
        except Exception as e:
            logger.debug("chat library: documents unavailable: %s", e)
            return []
        finally:
            db.close()

    def _research_for(session_id: str, owner: str) -> List[Dict[str, Any]]:
        out = []
        try:
            names = os.listdir(DEEP_RESEARCH_DIR)
        except OSError:
            return out
        for fn in names:
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(DEEP_RESEARCH_DIR, fn), "r", encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                continue
            if owner and rec.get("owner") and rec.get("owner") != owner:
                continue
            if session_id and rec.get("origin_session") != session_id:
                continue
            rid = fn[:-len(".json")]
            out.append({
                "id": rid,
                "name": rec.get("query") or "Research report",
                "kind": "research",
                "mime": "text/markdown",
                "size": len(rec.get("result") or ""),
                "created": rec.get("completed_at"),
                "source": "research",
                "url": f"#research-{rid}",
            })
        return out

    @router.get("/{session_id}")
    async def chat_library(session_id: str, request: Request):
        """Files, documents and reports belonging to one conversation."""
        user = _require_user(request)
        items = (_uploads_for(session_id, user)
                 + _documents_for(session_id, user)
                 + _research_for(session_id, user))
        items.sort(key=_sort_key, reverse=True)
        by_kind: Dict[str, int] = {}
        for i in items:
            by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
        return {"session_id": session_id, "count": len(items),
                "by_kind": by_kind, "items": items}

    @router.get("")
    async def all_library(request: Request, limit: int = 200):
        """Everything, for the times you remember the file but not the chat."""
        user = _require_user(request)
        items = (_uploads_for("", user) + _documents_for("", user)
                 + _research_for("", user))
        items.sort(key=_sort_key, reverse=True)
        return {"count": len(items), "items": items[:max(1, min(limit, 1000))]}

    return router
