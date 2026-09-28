"""References and side threads inside a chat.

Asked for on 2026-09-28: "I should be able to prune the messages, or like
basically fork it but keep it in same chat or like a thread ... a side chat
and then I can press merge to go back to main chat, or use as reference in
main chat", and "if I prune the chat, I can scroll up and still see it, but
then I can press use this for reference on the latest message".

- A reference is an earlier message of this chat (pruned or not), or a side
  thread, attached to the next message the user sends. It is read for that
  turn only: the text goes into this turn's copy of the user's message, and
  the saved message records only what it referenced.
- A side thread is a child session (parent_session_id / thread_anchor_id)
  seeded with the exchange it was started from, so it stays small. Merging
  posts its conversation into the parent as one message.
"""
import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from core.database import SessionLocal, ChatMessage as DbChatMessage

logger = logging.getLogger(__name__)

MAX_REFERENCE_CHARS = 12000      # per referenced message or thread
MAX_REFERENCES = 8
REFERENCE_HEADER = "[Referenced from earlier in this chat]"
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _meta(m) -> dict:
    meta = m.metadata if hasattr(m, "metadata") else (m.get("metadata") if isinstance(m, dict) else None)
    return meta if isinstance(meta, dict) else {}


def _role(m) -> str:
    return m.role if hasattr(m, "role") else (m.get("role") or "")


def _content(m) -> str:
    return (m.content if hasattr(m, "content") else m.get("content")) or ""


def parse_refs(raw) -> List[Dict]:
    """The page sends [{"kind": "message"|"thread", "id": ...}, ...]."""
    if not raw:
        return []
    try:
        refs = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    out = []
    for r in refs if isinstance(refs, list) else []:
        if isinstance(r, dict) and r.get("kind") in ("message", "thread") and r.get("id"):
            out.append({"kind": r["kind"], "id": str(r["id"])[:80]})
    return out[:MAX_REFERENCES]


def thread_transcript(thread_sess, *, skip_seed: bool = True) -> str:
    """A thread's conversation as plain text, without the messages it was
    seeded with (those are already in the main chat)."""
    lines = []
    for m in thread_sess.history:
        meta = _meta(m)
        if _role(m) == "system" or (skip_seed and meta.get("thread_seed")):
            continue
        who = "User" if _role(m) == "user" else "Assistant"
        text = _THINK_RE.sub("", _content(m)).strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n\n".join(lines).strip()


def _label(text: str, n: int = 60) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def resolve_references(sess, refs: List[Dict], session_manager=None) -> Tuple[str, List[Dict]]:
    """The reference text for the model, and labels for the saved message."""
    by_id = {}
    for m in sess.history:
        mid = _meta(m).get("_db_id")
        if mid:
            by_id[mid] = m
    blocks, labels = [], []
    for r in refs:
        if r["kind"] == "message":
            m = by_id.get(r["id"])
            if not m:
                continue
            who = "User" if _role(m) == "user" else "Assistant"
            text = _content(m).strip()[:MAX_REFERENCE_CHARS]
            blocks.append(f"<reference from=\"{who}\">\n{text}\n</reference>")
            labels.append({"kind": "message", "id": r["id"], "label": f"{who}: {_label(text)}"})
        else:
            child = None
            if session_manager:
                try:
                    child = session_manager.get_session(r["id"])
                except KeyError:
                    child = None
            if not child or getattr(child, "parent_session_id", None) != sess.id:
                continue
            text = thread_transcript(child)[:MAX_REFERENCE_CHARS]
            if not text:
                continue
            blocks.append(f"<reference from=\"side thread: {child.name}\">\n{text}\n</reference>")
            labels.append({"kind": "thread", "id": r["id"], "label": f"Side thread: {_label(child.name, 40)}"})
    if not blocks:
        return "", []
    return REFERENCE_HEADER + "\n" + "\n\n".join(blocks), labels


def apply_references(sess, messages: List[Dict], refs_raw, session_manager=None) -> List[Dict]:
    """Add the referenced text to this turn's last user message, and record
    what was referenced on the saved message. Returns the labels."""
    refs = parse_refs(refs_raw)
    if not refs:
        return []
    text, labels = resolve_references(sess, refs, session_manager)
    if not text:
        return []
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = content + "\n\n" + text
            elif isinstance(content, list):      # multimodal parts
                m["content"] = content + [{"type": "text", "text": text}]
            break
    # The saved user message: the latest one in history.
    for h in reversed(sess.history):
        if _role(h) == "user":
            meta = _meta(h)
            if hasattr(h, "metadata") and h.metadata is None:
                h.metadata = meta = {}
            meta["references"] = labels
            _persist_meta(sess.id, meta)
            break
    logger.info("[references] %d attached to a turn in %s", len(labels), sess.id[:8])
    return labels


def _persist_meta(session_id: str, meta: dict) -> None:
    """Write a message's metadata back to its row (by _db_id)."""
    mid = meta.get("_db_id")
    if not mid:
        return
    try:
        db = SessionLocal()
        try:
            row = db.query(DbChatMessage).filter(
                DbChatMessage.id == mid, DbChatMessage.session_id == session_id).first()
            if row:
                stored = {}
                if row.meta_data:
                    try:
                        stored = json.loads(row.meta_data)
                    except (TypeError, ValueError):
                        stored = {}
                stored.update({k: v for k, v in meta.items() if k not in ("_db_id", "timestamp")})
                row.meta_data = json.dumps(stored)
                db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("Could not save message metadata in %s: %s", session_id[:8], e)
