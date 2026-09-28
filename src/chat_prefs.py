"""Per-chat switches the user sets for one chat only.

For now one switch: whether the agent may use Claude Code (the claude_code
tool, either engine) in this chat. Asked for: "let me disable and enable
claude code per chat please, this one keeps using it to do stuff when I said
don't". Off is enforced twice: the tool is left out of the agent's tool list
for the chat (src/agent_loop.py), and the tool itself refuses when called
from it (claude_code_tool), so no path around the list reaches it.
"""

import json
import os
import tempfile
from typing import Dict

from src.constants import DATA_DIR

PREFS_FILE = os.path.join(DATA_DIR, "chat_prefs.json")
DEFAULTS = {"claude_code": True}


def _load() -> Dict[str, dict]:
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _save(data: Dict[str, dict]) -> None:
    d = os.path.dirname(PREFS_FILE) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".chat_prefs_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, PREFS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(session_id: str) -> dict:
    return {**DEFAULTS, **(_load().get(session_id or "") or {})}


def set_pref(session_id: str, key: str, value) -> dict:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r}")
    data = _load()
    entry = data.get(session_id) or {}
    entry[key] = type(DEFAULTS[key])(value)
    if entry == {k: v for k, v in DEFAULTS.items() if k in entry}:
        data.pop(session_id, None)              # back to the defaults: nothing to keep
    else:
        data[session_id] = entry
    _save(data)
    return get(session_id)


def claude_code_allowed(session_id: str) -> bool:
    return not session_id or bool(get(session_id).get("claude_code", True))
