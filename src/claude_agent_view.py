"""Each chat's Claude Code agent, parked in Claude's own agent view.

Asked for 2026-09-30: "for the claude code, i was hoping for agents, not just
local dir .claude stuff, so a chat can keep going cause it just opens that
chats agents and finds it there", with both "a chat keeps its agent in any
folder" and "real agents in `claude agents`", and yes to marking the folders
it runs in as trusted.

A turn still runs as `claude -p --resume <session>` (streamed, exact cost,
read-only or write per turn). Between turns the session is parked as a
background agent, `claude --bg --resume <session>` with no prompt: it shows
in `claude agents` under the chat's name, `claude attach <id>` opens it in a
terminal, and no turn runs. The next turn stops it first, then parks it again.

Found by trying it (claude 2.1.285):
- A parked agent is a live process holding the session, and resuming a
  session whose agent is alive starts a *copy* under a new id; so it is
  stopped (`claude stop`, which keeps the conversation) before each turn.
- Parking with any flag also starts a copy; flag-less keeps the same id.
- `--bg` refuses a folder Claude has not been told to trust (the prompt an
  interactive first run shows), so the folder is marked trusted first.
- The chat's name (`-n` on each turn) is the session's title, shown by
  `claude --resume`; `claude agents` lists a flag-less park by its id, and
  naming the park would start a copy, so the agent is known by its id there.
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("ODYSSEUS_CLAUDE_AGENTS", "1").strip() not in ("0", "false", "no", "")
CLAUDE_JSON = os.path.join(os.path.expanduser("~"), ".claude.json")
_LIST_TTL_S = 3.0
_cache: Dict[str, object] = {"at": 0.0, "rows": []}
_trust_lock = threading.Lock()


def _cli() -> Optional[str]:
    import shutil
    return shutil.which("claude")


def _env() -> dict:
    from src.agent_tools.claude_code_tool import _cli_env
    return _cli_env()


def list_agents(fresh: bool = False) -> List[Dict]:
    """`claude agents --json --all`: every agent Claude knows, [] on failure
    or when switched off."""
    if not ENABLED:
        return []
    if not fresh and time.time() - float(_cache["at"]) < _LIST_TTL_S:
        return list(_cache["rows"])
    rows: List[Dict] = []
    cli = _cli()
    if cli:
        try:
            out = subprocess.run([cli, "agents", "--json", "--all"], capture_output=True, text=True,
                                 timeout=15, stdin=subprocess.DEVNULL, env=_env()).stdout
            data = json.loads(out or "[]")
            rows = [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []
        except Exception as e:
            logger.debug("claude agents failed: %s", e)
    _cache.update(at=time.time(), rows=rows)
    return rows


def find(session_id: str) -> Optional[Dict]:
    """The agent for a session, by its full session id."""
    if not session_id:
        return None
    for a in list_agents(fresh=True):
        if a.get("sessionId") == session_id:
            return a
    return None


def _run(args: List[str], cwd: str = "", timeout: int = 30) -> str:
    cli = _cli()
    if not cli:
        return ""
    try:
        r = subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=_env(), cwd=cwd or None,
                           start_new_session=True)
        from src.agent_tools.claude_code_tool import _strip_ansi
        return _strip_ansi((r.stdout or "") + (r.stderr or ""))
    except Exception as e:
        logger.debug("claude %s failed: %s", args[:1], e)
        return ""


def unpark(session_id: str) -> str:
    """Stop the parked agent for this session so a turn can run on it.
    Returns "" when the session is free, else why it is not (the user has it
    open and busy in a terminal)."""
    if not ENABLED or not session_id:
        return ""
    agent = find(session_id)
    if not agent or not agent.get("pid"):
        return ""                                   # not parked, or already stopped
    if str(agent.get("status") or "") == "busy":
        return (f"this chat's Claude Code agent ({agent.get('id')}) is busy: someone is using it in a "
                f"terminal (claude attach {agent.get('id')}). Wait for it, or pass new_agent:true.")
    _run(["stop", str(agent.get("id"))], timeout=20)
    for _ in range(20):                             # up to ~5s for the process to go
        again = find(session_id)
        if not again or not again.get("pid"):
            return ""
        time.sleep(0.25)
    return f"could not stop this chat's parked Claude Code agent ({agent.get('id')}); try again shortly."


def park(session_id: str, cwd: str) -> str:
    """Park the session as a background agent (no turn runs). Returns its
    agent id ("" if it could not be parked)."""
    if not ENABLED or not session_id or not cwd:
        return ""
    ensure_trusted(cwd)
    out = _run(["--bg", "--resume", session_id], cwd=cwd, timeout=40)
    for word in out.split():
        if len(word) == 8 and session_id.startswith(word):
            return word
    agent = find(session_id)
    if agent:
        return str(agent.get("id") or "")
    logger.info("Parking Claude session %s failed: %s", session_id[:8], out.strip()[:200])
    return ""


def is_trusted(path: str) -> bool:
    try:
        with open(CLAUDE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(((data.get("projects") or {}).get(os.path.realpath(path)) or {})
                    .get("hasTrustDialogAccepted"))
    except (OSError, ValueError):
        return False


def ensure_trusted(path: str) -> bool:
    """Mark a folder trusted in ~/.claude.json, as accepting Claude's trust
    prompt does. Only that folder's flag changes; the file is read again just
    before the atomic write, since running Claude processes rewrite it too."""
    path = os.path.realpath(path)
    if is_trusted(path):
        return True
    with _trust_lock:
        try:
            with open(CLAUDE_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            projects = data.setdefault("projects", {})
            entry = projects.setdefault(path, {})
            if entry.get("hasTrustDialogAccepted"):
                return True
            entry["hasTrustDialogAccepted"] = True
            mode = os.stat(CLAUDE_JSON).st_mode & 0o777
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CLAUDE_JSON), prefix=".claude.json.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.chmod(tmp, mode)
                os.replace(tmp, CLAUDE_JSON)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            logger.info("Marked %s trusted for Claude Code agents", path)
            return True
        except (OSError, ValueError) as e:
            logger.warning("Could not mark %s trusted: %s", path, e)
            return False


def agent_name(chat_id: str) -> str:
    """The name the chat's agent carries in `claude agents`."""
    try:
        from src.claude_code_agents import _chat_name
        name = _chat_name(chat_id)
    except Exception:
        name = ""
    return (f"Odysseus: {name}" if name else f"Odysseus chat {chat_id[:8]}")[:60]
