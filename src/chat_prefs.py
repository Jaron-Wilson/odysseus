"""Per-chat switches the user sets for one chat only.

For now, which coding agents the agent may use in this chat: Claude Code and
OpenCode, separately (both engines of the claude_code tool). Asked for: "let
me disable and enable claude code per chat", then "disabling claude code
should be separate from opencode, but disabling coding agents should disable
both". With both off the tool is left out of the agent's tool list for the
chat (src/agent_loop.py); the tool itself refuses a switched-off engine
(claude_code_tool), and bash refuses its CLI (tool_execution).
"""

import json
import os
import tempfile
from typing import Dict

from src.constants import DATA_DIR

PREFS_FILE = os.path.join(DATA_DIR, "chat_prefs.json")
# "tidy": when the chat gets full, write its notes and prune (chat_tidy.py).
DEFAULTS = {"claude": True, "opencode": True, "tidy": False}


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
    stored = dict(_load().get(session_id or "") or {})
    # Before the two were separate, one "claude_code" switch covered both.
    legacy = stored.pop("claude_code", None)
    out = {**DEFAULTS, **({"claude": False, "opencode": False} if legacy is False else {}), **stored}
    return {k: out[k] for k in DEFAULTS}


def set_pref(session_id: str, key: str, value) -> dict:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r}")
    data = _load()
    entry = get(session_id)                    # folds in the old single switch
    entry[key] = type(DEFAULTS[key])(value)
    if entry == DEFAULTS:
        data.pop(session_id, None)              # back to the defaults: nothing to keep
    else:
        data[session_id] = entry
    _save(data)
    return get(session_id)


def engine_allowed(session_id: str, engine: str) -> bool:
    """Whether this chat allows the coding agent `engine` ("claude" or
    "opencode")."""
    if not session_id:
        return True
    return bool(get(session_id).get("claude" if engine == "claude" else "opencode", True))


def claude_code_allowed(session_id: str) -> bool:
    """Whether any coding agent is allowed in this chat (the tool at all)."""
    return engine_allowed(session_id, "claude") or engine_allowed(session_id, "opencode")
