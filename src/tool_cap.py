"""Ordering and a hard cap for the function tools sent in one request.

OpenAI caps function tools at 128 per request, and the ChatGPT plan's
Responses backend behaves the same way: past the cap the request fails or
the tools at the end of the list never reach the model. A Company run once
sent 252 tools (desktop MCP tools first) and the model said its Company
tools were missing. Everything that builds a tool list for those backends
goes through cap_tools so the most relevant tools are the ones kept, with a
warning in the log whenever any are dropped.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# OpenAI's documented limit, and the cap kept under it.
OPENAI_MAX_TOOLS = 128
SAFE_TOOL_CAP = 120

# Hosts whose function-tool limit is OpenAI's.
_CAPPED_HOSTS = ("api.openai.com", "chatgpt.com")


def tool_name(schema: Any) -> str:
    """The name of a chat-completions or Responses style tool schema."""
    if not isinstance(schema, dict):
        return ""
    fn = schema.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(schema.get("name") or "")


def needs_tool_cap(url: Optional[str]) -> bool:
    """True for endpoints that enforce OpenAI's function tool limit."""
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    return host in _CAPPED_HOSTS or host.endswith(".openai.azure.com")


def order_tools(schemas: Iterable[Dict[str, Any]], first: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The schemas with the ones named in `first` moved to the front, in
    `first`'s order; everything else keeps its order."""
    schemas = list(schemas or [])
    first = [n for n in first or []]
    if not first:
        return schemas
    by_name: Dict[str, Dict[str, Any]] = {}
    for s in schemas:
        by_name.setdefault(tool_name(s), s)
    head = []
    seen = set()
    for n in first:
        if n in by_name and n not in seen:
            head.append(by_name[n])
            seen.add(n)
    head_ids = {id(s) for s in head}
    return head + [s for s in schemas if id(s) not in head_ids]


def cap_tools(schemas: Iterable[Dict[str, Any]], limit: int = SAFE_TOOL_CAP, *,
              priority: Iterable[str] = (), relevant: Optional[Iterable[str]] = None,
              label: str = "", log: bool = True) -> List[Dict[str, Any]]:
    """At most `limit` schemas, keeping the most relevant: the names in
    `priority` first, then those in `relevant`, then the other built-in
    tools, then MCP tools. Order within each group is kept. Logs a warning
    when it drops any."""
    schemas = list(schemas or [])
    if limit <= 0 or len(schemas) <= limit:
        return schemas
    prio = list(priority or [])
    prio_set = set(prio)
    rel = set(relevant or [])

    def rank(s: Dict[str, Any]) -> int:
        n = tool_name(s)
        if n in prio_set:
            return 0
        if n in rel:
            return 1
        if not n.startswith("mcp__"):
            return 2
        return 3

    ranked = sorted(enumerate(schemas), key=lambda p: (rank(p[1]), p[0]))
    keep_idx = sorted(i for i, _ in ranked[:limit])
    keep_set = set(keep_idx)
    kept = [schemas[i] for i in keep_idx]
    dropped = [tool_name(s) for i, s in enumerate(schemas) if i not in keep_set]
    if log:
        logger.warning("[tool-cap] %s%d tools exceed the cap of %d; dropped %d: %s",
                       f"{label}: " if label else "", len(schemas), limit, len(dropped), dropped[:20])
    return order_tools(kept, prio)
