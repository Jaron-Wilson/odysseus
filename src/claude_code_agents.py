"""Which Claude Code agent (CLI session) belongs to which chat.

Each chat keeps its own agent per project folder: the next claude_code call
in that chat resumes it, so it still has everything it already read. Another
chat can find these agents (action "agents") and carry one on by naming the
chat it came from (`from_chat`), by id or by part of its name.

The CLI keeps sessions per project directory, so an agent is only resumed in
the folder it was started in.
"""

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

from src.constants import DATA_DIR

AGENTS_FILE = os.path.join(DATA_DIR, "claude_code_agents.json")
MAX_PER_CHAT = 10
MAX_AGE_S = 30 * 86400

_lock = threading.Lock()


def _load() -> Dict[str, List[Dict]]:
    try:
        with open(AGENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: Dict[str, List[Dict]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".cc_agents_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, AGENTS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _norm(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path or ""))


def record(chat_id: str, *, session_id: str, cwd: str, engine: str, model: str,
           action: str, prompt: str, summary: str = "") -> None:
    """Remember (or refresh) this chat's agent for this folder."""
    if not chat_id or not session_id:
        return
    now = time.time()
    with _lock:
        data = _load()
        agents = [a for a in data.get(chat_id, [])
                  if not (a.get("session_id") == session_id
                          or (_norm(a.get("cwd")) == _norm(cwd) and a.get("engine") == engine))]
        agents.insert(0, {
            "session_id": session_id, "cwd": _norm(cwd), "engine": engine, "model": model,
            "last_action": action, "last_prompt": (prompt or "")[:200],
            "summary": (summary or "")[:300], "last_used": now,
        })
        data[chat_id] = agents[:MAX_PER_CHAT]
        data = {k: [a for a in v if now - a.get("last_used", 0) < MAX_AGE_S]
                for k, v in data.items()}
        _save({k: v for k, v in data.items() if v})


def for_chat(chat_id: str, *, cwd: str = "", engine: str = "claude") -> Optional[Dict]:
    """This chat's agent for the folder (or its latest one, with no folder)."""
    agents = _load().get(chat_id or "", [])
    for a in agents:
        if a.get("engine", "claude") != engine:
            continue
        if not cwd or _norm(a.get("cwd")) == _norm(cwd):
            return dict(a, chat_id=chat_id)
    return None


def _chat_name(chat_id: str) -> str:
    try:
        from src.ai_interaction import get_session_manager
        sess = get_session_manager().get_session(chat_id)
        return (getattr(sess, "name", "") or "").strip()
    except Exception:
        return ""


def list_all() -> List[Dict]:
    """Every chat's agents, most recently used first, with the chat's name."""
    out = []
    for chat_id, agents in _load().items():
        name = _chat_name(chat_id)
        for a in agents:
            out.append(dict(a, chat_id=chat_id, chat_name=name))
    return sorted(out, key=lambda a: a.get("last_used", 0), reverse=True)


def find_chat(ref: str) -> List[str]:
    """Chats with agents matching a reference: an id, an id prefix, or part
    of the chat's name (case-insensitive)."""
    ref = (ref or "").strip().lstrip("#")
    if not ref:
        return []
    data = _load()
    if ref in data:
        return [ref]
    by_id = [c for c in data if c.startswith(ref)]
    if by_id:
        return by_id
    low = ref.lower()
    return [c for c in data if low in _chat_name(c).lower()]
