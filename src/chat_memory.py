""""Needs to know": a memory for each chat, like Brain but per chat.

A list of short items (the task, decisions, what is waiting on whom) that
the model sees on every turn in that chat, so they survive compaction and
long gaps. The user adds, edits and removes items in the notes panel.

The model does not write them silently: it suggests an item and the user
accepts or declines it ("Add to Needs to know?"), the way Brain suggests
memories. It adds directly only when the user asked it to.

A new chat also sees the items of the user's recent chats. Seen live on
2026-09-27: after a page reload, "okay check if he did merge it" went into a
fresh, empty chat instead of the one about the PR, and the model had no idea
who "he" was. With the recent items in view it can connect the two, or ask.
"""

import json
import os
import tempfile
import threading
import time
import uuid
from typing import Dict, List, Optional

from src.constants import DATA_DIR

MEMORY_FILE = os.path.join(DATA_DIR, "chat_memory.json")
MAX_ITEMS = 30
MAX_ITEM_CHARS = 400
RECENT_HOURS = 12
RECENT_LIMIT = 3

_lock = threading.Lock()


def _load() -> Dict[str, Dict]:
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: Dict[str, Dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".chat_memory_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, MEMORY_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _items(rec: Optional[Dict]) -> List[Dict]:
    """A chat's items. Notes saved by the first version were one block of
    text; each line becomes an item."""
    rec = rec or {}
    if isinstance(rec.get("items"), list):
        return rec["items"]
    text = (rec.get("text") or "").strip()
    ts = rec.get("updated") or time.time()
    by = "you" if rec.get("by") == "user" else "ai"
    return [{"id": uuid.uuid4().hex[:8], "text": ln.strip()[:MAX_ITEM_CHARS], "by": by,
             "status": "active", "created": ts}
            for ln in text.splitlines() if ln.strip()]


def get(session_id: str) -> Dict:
    items = _items(_load().get(session_id or ""))
    return {"items": [i for i in items if i.get("status") == "active"],
            "suggested": [i for i in items if i.get("status") == "suggested"]}


def _mutate(session_id: str, fn, owner: str = ""):
    if not session_id:
        raise ValueError("no chat")
    with _lock:
        data = _load()
        rec = data.get(session_id) or {}
        items = _items(rec)
        out = fn(items)
        rec = {"items": items, "owner": owner or rec.get("owner", ""), "updated": time.time()}
        if items:
            data[session_id] = rec
        else:
            data.pop(session_id, None)
        _save(data)
    return out


def add(session_id: str, text: str, *, by: str = "you", status: str = "active",
        owner: str = "") -> Dict:
    """Add an item (status "active"), or propose one ("suggested")."""
    text = " ".join((text or "").split())[:MAX_ITEM_CHARS]
    if not text:
        raise ValueError("an item needs some text")

    def fn(items):
        for i in items:
            if i["text"].lower() == text.lower():
                if status == "active":
                    i["status"] = "active"
                return i
        live = [i for i in items if i.get("status") == "active"]
        if status == "active" and len(live) >= MAX_ITEMS:
            raise ValueError(f"Needs to know holds at most {MAX_ITEMS} items; remove one first")
        item = {"id": uuid.uuid4().hex[:8], "text": text, "by": by, "status": status,
                "created": time.time()}
        items.append(item)
        return item
    return dict(_mutate(session_id, fn, owner))


def update(session_id: str, item_id: str, *, text: Optional[str] = None,
           accept: bool = False) -> Optional[Dict]:
    """Edit an item's text, or accept a suggestion."""
    def fn(items):
        for i in items:
            if i["id"] == item_id:
                if text is not None and text.strip():
                    i["text"] = " ".join(text.split())[:MAX_ITEM_CHARS]
                if accept:
                    i["status"] = "active"
                return dict(i)
        return None
    return _mutate(session_id, fn)


def remove(session_id: str, item_id: str) -> bool:
    """Remove an item, or decline a suggestion."""
    def fn(items):
        before = len(items)
        items[:] = [i for i in items if i["id"] != item_id]
        return len(items) < before
    return _mutate(session_id, fn)


def _chat_name(session_id: str) -> str:
    try:
        from src.ai_interaction import get_session_manager
        return (getattr(get_session_manager().get_session(session_id), "name", "") or "").strip()
    except Exception:
        return ""


def recent(exclude: str = "", owner: str = "", *, hours: float = RECENT_HOURS,
           limit: int = RECENT_LIMIT) -> List[Dict]:
    """Other chats' active items, most recently updated first."""
    cutoff = time.time() - hours * 3600
    out = []
    for sid, rec in _load().items():
        if sid == exclude or (rec.get("updated") or 0) < cutoff:
            continue
        if owner and rec.get("owner") and rec["owner"] != owner:
            continue
        live = [i["text"] for i in _items(rec) if i.get("status") == "active"]
        if live:
            out.append({"session_id": sid, "name": _chat_name(sid), "items": live,
                        "updated": rec.get("updated")})
    out.sort(key=lambda r: r["updated"] or 0, reverse=True)
    return out[:limit]


def _ago(ts: Optional[float]) -> str:
    s = int(time.time() - (ts or time.time()))
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago"


def context_text(session_id: str, *, owner: str = "", new_chat: bool = False) -> str:
    """What the model is shown this turn, or "" for nothing."""
    parts = []
    mine = get(session_id)
    if mine["items"]:
        parts.append("## Needs to know (this chat)\n"
                     "Facts kept for this chat. Remove an item with chat_memory when it is done "
                     "or wrong; suggest new ones when something worth keeping comes up.\n"
                     + "\n".join(f"- [{i['id']}] {i['text']}" for i in mine["items"]))
    if new_chat:
        others = recent(exclude=session_id, owner=owner)
        if others:
            lines = ["## Recent chats",
                     "This chat is new. The user's other recent chats, in case their message "
                     "refers back to one (\"did he merge it?\", \"carry on\"). If it does, say "
                     "which chat you mean; if you are unsure which, ask. Ignore this otherwise."]
            for r in others:
                lines.append(f"- \"{r['name'] or 'Untitled'}\" (chat {r['session_id'][:8]}, "
                             f"{_ago(r['updated'])}): " + "; ".join(r["items"]))
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


def run_tool(content: str, *, session_id: Optional[str], owner: Optional[str] = None) -> Dict:
    """The chat_memory tool.

    suggest: propose an item; the user is asked to add it or not (default).
    add:     add straight away, only when the user asked for it.
    remove:  drop an item by id (done, or wrong).
    list:    read the items.
    """
    if not session_id:
        return {"error": "chat_memory only works inside a chat", "exit_code": 1}
    raw = (content or "").strip()
    try:
        args = json.loads(raw) if raw.startswith("{") else {"action": "suggest", "text": raw}
    except ValueError:
        args = {"action": "suggest", "text": raw}
    action = str(args.get("action") or ("suggest" if args.get("text") else "list")).lower()
    if action in ("set", "append"):
        action = "suggest"          # the first version's verbs
    text = str(args.get("text") or "")
    try:
        if action == "suggest":
            item = add(session_id, text, by="ai", status="suggested", owner=owner or "")
            if item.get("status") == "active":
                return {"output": f"Already in Needs to know: {item['text']}", "exit_code": 0}
            return {
                "output": (f"Suggested for Needs to know (id {item['id']}): {item['text']}\n"
                           "The user is asked whether to add it; it is kept only if they do. "
                           "Do not ask again in your reply."),
                "suggestion": {"id": item["id"], "text": item["text"]},
                "exit_code": 0,
            }
        if action == "add":
            item = add(session_id, text, by="ai", status="active", owner=owner or "")
            return {"output": f"Added to Needs to know (id {item['id']}): {item['text']}", "exit_code": 0}
        if action == "remove":
            ok = remove(session_id, str(args.get("id") or ""))
            return {"output": "Removed." if ok else "No item with that id.", "exit_code": 0 if ok else 1}
        if action == "list":
            items = get(session_id)["items"]
            return {"output": "\n".join(f"- [{i['id']}] {i['text']}" for i in items)
                    or "Needs to know is empty for this chat.", "exit_code": 0}
    except ValueError as e:
        return {"error": str(e), "exit_code": 1}
    return {"error": "action must be suggest, add, remove or list", "exit_code": 1}
