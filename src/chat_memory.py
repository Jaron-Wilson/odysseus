""""Needs to know": a short note per chat that the model always sees.

The task in hand, decisions made, what is waiting on whom. The model keeps
it current with the chat_memory tool, and the user can read and edit it. It
rides along on every turn, so it survives compaction and long gaps.

A new chat also sees the notes of the user's recent chats. Seen live on
2026-09-27: after a page reload, "okay check if he did merge it" went into a
fresh, empty chat instead of the one about the PR, and the model had no idea
who "he" was. With the recent notes in view it can connect the two, or ask.
"""

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

from src.constants import DATA_DIR

MEMORY_FILE = os.path.join(DATA_DIR, "chat_memory.json")
MAX_CHARS = 2000
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


def get(session_id: str) -> Dict:
    rec = _load().get(session_id or "") or {}
    return {"text": rec.get("text", ""), "updated": rec.get("updated"), "by": rec.get("by", "")}


def set_text(session_id: str, text: str, *, by: str = "model", owner: str = "") -> Dict:
    if not session_id:
        raise ValueError("no chat")
    text = (text or "").strip()
    if len(text) > MAX_CHARS:
        # Keep the newest part: notes are appended to as a task moves on.
        text = "…" + text[-(MAX_CHARS - 1):]
    with _lock:
        data = _load()
        if text:
            data[session_id] = {"text": text, "updated": time.time(), "by": by,
                                "owner": owner or (data.get(session_id) or {}).get("owner", "")}
        else:
            data.pop(session_id, None)
        _save(data)
    return get(session_id)


def append(session_id: str, line: str, *, by: str = "model", owner: str = "") -> Dict:
    cur = get(session_id)["text"]
    line = (line or "").strip()
    if not line:
        return get(session_id)
    return set_text(session_id, (cur + "\n" + line).strip() if cur else line, by=by, owner=owner)


def _chat_name(session_id: str) -> str:
    try:
        from src.ai_interaction import get_session_manager
        return (getattr(get_session_manager().get_session(session_id), "name", "") or "").strip()
    except Exception:
        return ""


def recent(exclude: str = "", owner: str = "", *, hours: float = RECENT_HOURS,
           limit: int = RECENT_LIMIT) -> List[Dict]:
    """Other chats' notes, most recently updated first."""
    cutoff = time.time() - hours * 3600
    out = []
    for sid, rec in _load().items():
        if sid == exclude or not rec.get("text") or (rec.get("updated") or 0) < cutoff:
            continue
        if owner and rec.get("owner") and rec["owner"] != owner:
            continue
        out.append({"session_id": sid, "name": _chat_name(sid), "text": rec["text"],
                    "updated": rec.get("updated")})
    out.sort(key=lambda r: r["updated"] or 0, reverse=True)
    return out[:limit]


def _ago(ts: Optional[float]) -> str:
    s = int(time.time() - (ts or time.time()))
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago"


def context_text(session_id: str, *, owner: str = "", new_chat: bool = False) -> str:
    """What the model is shown this turn, or "" for nothing."""
    parts = []
    mine = get(session_id)["text"]
    if mine:
        parts.append("## Needs to know (this chat)\n"
                     "Your running notes for this chat. Keep them current with chat_memory "
                     "when the task, a decision or what is pending changes.\n" + mine)
    if new_chat:
        others = recent(exclude=session_id, owner=owner)
        if others:
            lines = ["## Recent chats",
                     "This chat is new. The user's other recent chats, in case their message "
                     "refers back to one (\"did he merge it?\", \"carry on\"). If it does, say "
                     "which chat you mean; if you are unsure which, ask. Ignore this otherwise."]
            for r in others:
                lines.append(f"- \"{r['name'] or 'Untitled'}\" (chat {r['session_id'][:8]}, "
                             f"{_ago(r['updated'])}): {r['text']}")
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


def run_tool(content: str, *, session_id: Optional[str], owner: Optional[str] = None) -> Dict:
    """The chat_memory tool: get, set or append this chat's notes."""
    if not session_id:
        return {"error": "chat_memory only works inside a chat", "exit_code": 1}
    raw = (content or "").strip()
    try:
        args = json.loads(raw) if raw.startswith("{") else {"action": "set", "text": raw}
    except ValueError:
        args = {"action": "set", "text": raw}
    action = str(args.get("action") or ("set" if args.get("text") else "get")).lower()
    text = str(args.get("text") or "")
    try:
        if action == "get":
            rec = get(session_id)
        elif action == "append":
            rec = append(session_id, text, by="model", owner=owner or "")
        elif action == "set":
            rec = set_text(session_id, text, by="model", owner=owner or "")
        else:
            return {"error": "action must be get, set or append", "exit_code": 1}
    except ValueError as e:
        return {"error": str(e), "exit_code": 1}
    return {"output": ("Notes for this chat:\n" + rec["text"]) if rec["text"]
            else "This chat has no notes.", "notes": rec["text"], "exit_code": 0}
