"""A Claude Code session attached to a chat: `/claude attach 7238cfa3`.

Asked for: "I want to be able to in a chat do: claude attach 7238cfa3 and
then it can now use that chat." The id is the start of one of the user's
own Claude Code sessions (~/.claude/projects, src/claude_code_sessions.py).
Once attached, the chat's claude_code tool carries THAT session on, with
everything it already knows, in the folder it began in, instead of
starting a new agent. `/claude detach` lets it go again.

The attachment is kept here (DATA_DIR/claude_attached.json, one per chat)
and the session is also recorded as the chat's agent in
src/claude_code_agents.py, so `agents` lists it and its parking between
turns (src/claude_agent_view.py) works as for any chat agent.
"""

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

from src.constants import DATA_DIR
from src import claude_code_sessions as ccs

ATTACH_FILE = os.path.join(DATA_DIR, "claude_attached.json")

_lock = threading.Lock()


class AttachError(Exception):
    """Why a prefix can't be attached; `candidates` when it matched several."""

    def __init__(self, message: str, candidates: Optional[List[Dict]] = None, status: int = 400):
        super().__init__(message)
        self.candidates = candidates or []
        self.status = status


def _load() -> Dict[str, Dict]:
    try:
        with open(ATTACH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: Dict[str, Dict]) -> None:
    folder = os.path.dirname(ATTACH_FILE) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".cc_attached_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, ATTACH_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _brief(info: Dict) -> Dict:
    return {k: info.get(k) for k in ("id", "project", "title", "cwd", "git_branch",
                                     "last_activity", "live", "message_count")}


def resolve(prefix: str) -> Dict:
    """The one session whose id starts with `prefix`, as session_info gives
    it. Raises AttachError when the prefix is malformed, matches nothing,
    or matches more than one session."""
    prefix = (prefix or "").strip().lower()
    if not ccs.valid_prefix(prefix):
        raise AttachError(f"Give at least {ccs.MIN_PREFIX} characters of a Claude Code session id "
                          "(hex, as `claude agents` or the Claude sessions page shows it).")
    matches = ccs.resolve_prefix(prefix)
    if not matches:
        raise AttachError(f"No Claude Code session on this host starts with {prefix}.", status=404)
    infos = [i for i in (ccs.session_info(p, path) for p, path in matches) if i]
    if len(infos) > 1:
        raise AttachError(f"{prefix} matches {len(infos)} sessions. Give more of the id.",
                          candidates=[_brief(i) for i in infos[:10]], status=409)
    if not infos:
        raise AttachError(f"No Claude Code session on this host starts with {prefix}.", status=404)
    return infos[0]


def get(chat_id: str) -> Optional[Dict]:
    """This chat's attachment, or None. Dropped when the session's
    transcript is gone."""
    if not chat_id:
        return None
    a = _load().get(chat_id)
    if not isinstance(a, dict) or not ccs.valid_session_id(a.get("session_id", "")):
        return None
    if not ccs.transcript_path(a.get("project", ""), a["session_id"]):
        return None
    return dict(a)


def info(chat_id: str) -> Optional[Dict]:
    """The attachment with the session's current title, branch and activity."""
    a = get(chat_id)
    if not a:
        return None
    path = ccs.transcript_path(a["project"], a["session_id"])
    now = ccs.session_info(a["project"], path) if path else None
    out = dict(a)
    if now:
        out.update(_brief(now))
    out["id"] = a["session_id"]
    return out


def attach(chat_id: str, prefix: str, *, by: str = "") -> Dict:
    """Attach the session `prefix` names to the chat. Returns info()."""
    if not chat_id:
        raise AttachError("Open a chat first.")
    s = resolve(prefix)
    cwd = s.get("cwd") or ""
    rec = {"session_id": s["id"], "project": s["project"], "cwd": cwd,
           "attached_at": time.time(), "attached_by": by or ""}
    with _lock:
        data = _load()
        data[chat_id] = rec
        _save(data)
    try:
        from src import claude_code_agents
        claude_code_agents.record(chat_id, session_id=s["id"], cwd=cwd, engine="claude",
                                  model="", action="attach", prompt="",
                                  summary=f"Attached with /claude attach: {s.get('title') or ''}")
    except Exception:
        pass
    return info(chat_id) or dict(rec, id=s["id"])


def detach(chat_id: str) -> Optional[Dict]:
    """Let the chat's attached session go. Returns what was attached."""
    with _lock:
        data = _load()
        rec = data.pop(chat_id, None)
        if rec is not None:
            _save(data)
    if isinstance(rec, dict) and rec.get("session_id"):
        try:
            from src import claude_code_agents
            claude_code_agents.forget(chat_id, rec["session_id"])
        except Exception:
            pass
    return rec if isinstance(rec, dict) else None


def follow(chat_id: str, old_id: str, new_id: str) -> None:
    """The CLI carried the session on under a new id: keep attached to it."""
    if not chat_id or not new_id or old_id == new_id or not ccs.valid_session_id(new_id):
        return
    with _lock:
        data = _load()
        rec = data.get(chat_id)
        if isinstance(rec, dict) and rec.get("session_id") == old_id:
            rec["session_id"] = new_id
            _save(data)


def context_note(chat_id: str) -> str:
    """One short paragraph for the model's system prompt, or ""."""
    a = get(chat_id)
    if not a:
        return ""
    path = ccs.transcript_path(a["project"], a["session_id"])
    s = ccs.session_info(a["project"], path) if path else None
    branch = (s or {}).get("git_branch") or ""
    return (f"This chat has the user's Claude Code session {a['session_id'][:8]} attached "
            f"(/claude attach), working in {a.get('cwd') or '?'}"
            + (f" on branch {branch}" if branch and branch != "HEAD" else "") + ". "
            "When the user asks to continue, check on, or change that work, use the claude_code tool "
            "(ask, or plan then execute) and leave out session_id, engine and from_chat: the server "
            "resumes the attached session in its own folder on Claude Code, with its full context. "
            "Pass new_agent:true only if the user wants a fresh agent instead.")


def cwd_warning(cwd: str) -> str:
    """Why the chat may not be able to carry the session on from its
    folder, or "". The claude_code tool refuses the running Odysseus
    install as a working folder, and needs the folder to exist."""
    if not cwd:
        return "The session has no folder on record, so the chat cannot run it."
    path = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(path):
        return f"Its folder {cwd} is not on this host any more, so the chat cannot run it."
    root = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    if path == root or path.startswith(root + os.sep):
        return ("It works in the running Odysseus install, which the coding agent refuses as a "
                "folder, so the chat cannot run it there.")
    return ""
