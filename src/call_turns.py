"""Spoken call turns (a phone call to the agent line, the in-app voice call): what makes them answer sooner.

A turn is in a call when its first system message carries the voice call note (routes/chat_routes.py,
VOICE_CALL_NOTE). Three things change for such a turn (src/agent_loop.py):

- **No reasoning pass** before the answer, unless Settings > AI Defaults > Voice call, "Think before
  answering", is on (``call_thinking``, off by default). On qwen3.8-27b (vLLM) the reasoning pass was most of
  the 6 s before the first spoken word.
- **One tool set for the whole call.** Tool retrieval picks different tools for every message, and the tools
  are rendered near the start of the prompt (Qwen's template puts them in the system block), so a local
  server's prefix cache (vLLM, llama.cpp) missed on every turn and read the whole prompt again: 2.8 s for 4k
  tokens, 8.2 s for 12k. The first turn of a call picks the tools; the later turns keep them (in the same
  order) and only add what the message clearly needs (its domain words, a mod's keywords). The choices that
  shape the system prompt (admin tools, the email style) are kept too.
- **No tool re-indexing inside the turn**: the MCP and mod tool index is refreshed in the background, and the
  turn goes on with the index it has.

Kept in memory per chat for ``IDLE_S`` after the last call turn; a turn in that chat outside a call forgets it.
"""

import logging
import threading
import time
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

IDLE_S = 15 * 60
SETTING = "call_thinking"

# Offered from a call's first turn, whatever it says: what calls most often need next (look something up, a
# note, a reminder, the calendar). Without them the first "what's the weather" or "remind me" later in the call
# added tools, which changed the prompt and cost the whole cached prefix (3 to 8 s on qwen3.8-27b).
CALL_TOOLS = frozenset({"web_search", "web_fetch", "manage_notes", "manage_calendar", "manage_tasks"})

_lock = threading.Lock()
_calls: Dict[str, Dict] = {}


def in_call(messages: List[Dict]) -> bool:
    """Is this turn spoken in a call? (The voice call note is on its first system message.)"""
    try:
        from src.opencode_chat import VOICE_CALL_MARK
    except Exception:
        return False
    first = messages[0] if messages else None
    if not isinstance(first, dict) or first.get("role") != "system":
        return False
    content = first.get("content")
    return isinstance(content, str) and VOICE_CALL_MARK in content


def think_on_calls() -> bool:
    """Settings > AI Defaults > Voice call, "Think before answering" (off by default)."""
    try:
        from src.settings import get_setting
        return bool(get_setting(SETTING, False))
    except Exception:
        return False


def frozen(session_id: str) -> Optional[Dict]:
    """The tool set this chat's call has been using, or None (first turn of a call, or idle too long)."""
    if not session_id:
        return None
    now = time.time()
    with _lock:
        for k in [k for k, v in _calls.items() if now - v["at"] > IDLE_S]:
            _calls.pop(k, None)
        st = _calls.get(session_id)
        if not st:
            return None
        st["at"] = now
        return {"tools": set(st["tools"]), "mod_tools": list(st["mod_tools"]),
                "needs_admin": st["needs_admin"]}


def keep(session_id: str, *, tools: Iterable[str], mod_tools: Iterable[str], needs_admin: bool) -> None:
    """Remember the tool set this call turn used, for the next ones."""
    if not session_id:
        return
    with _lock:
        _calls[session_id] = {"tools": set(tools or ()), "mod_tools": list(mod_tools or ()),
                              "needs_admin": bool(needs_admin), "at": time.time()}


def forget(session_id: str) -> None:
    with _lock:
        _calls.pop(session_id or "", None)


def merge_mod_tools(kept: List[str], picked: Iterable[str]) -> List[str]:
    """The call's mod tools, then this message's new ones (appended, so the kept ones never move)."""
    out = list(kept)
    for n in picked:
        if n not in out:
            out.append(n)
    return out


# ── Tool index refresh off the turn's path ──────────────────────────────

_index_lock = threading.Lock()
_index_busy = threading.Event()


def reindex_tools(tool_idx, mcp_mgr, mcp_disabled_map, mod_docs=None, mod_generation=None) -> None:
    """Bring the tool index up to date with the connected MCP servers and the installed mods. Each part is
    re-indexed only when its generation changed (src/tool_index.py). One at a time."""
    with _index_lock:
        if mcp_mgr:
            tool_idx.index_mcp_tools(mcp_mgr, mcp_disabled_map)
        if mod_docs is not None:
            tool_idx.index_extra_tools("mod", mod_docs() if callable(mod_docs) else mod_docs,
                                       mod_generation() if callable(mod_generation) else mod_generation)


def reindex_in_background(tool_idx, mcp_mgr, mcp_disabled_map, mod_docs=None, mod_generation=None) -> bool:
    """reindex_tools in a thread, so a call turn never waits for it. False when one is already running."""
    if _index_busy.is_set():
        return False
    _index_busy.set()

    def work():
        t = time.time()
        try:
            reindex_tools(tool_idx, mcp_mgr, mcp_disabled_map, mod_docs, mod_generation)
        except Exception as e:
            logger.debug("[call] background tool reindex failed: %s", e)
        finally:
            _index_busy.clear()
            if time.time() - t > 0.5:
                logger.info("[call] tool index refreshed in the background (%.1fs)", time.time() - t)

    threading.Thread(target=work, name="call-tool-reindex", daemon=True).start()
    return True
