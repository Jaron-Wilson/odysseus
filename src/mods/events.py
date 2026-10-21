"""The mod event bus.

A mod with `events.emit` posts {type, data}; the core stamps it with an id,
the source mod and the time, and delivers it to every enabled mod whose
subscriptions match, by POSTing it to that mod's own `events.webhook` path
(default /mod/events) with signed identity headers (user "odysseus-core").
Webhooks are always paths on the subscriber's own server, never URLs, so a
subscription cannot point the core at another host.

Subscriptions come from the manifest (`events.subscribes`) and from
POST /api/mods/v1/events/subscriptions at run time (kept in memory until the
server restarts). A pattern is "<mod id>/<type glob>" or a bare type glob,
matched with fnmatch: "print-hub/print.*", "*/print.failed", "*.failed".

Each event also fires the core's own event bus (src/event_bus.py) as
"mod:<mod id>:<type>", so a scheduled task can be triggered by it.

A short history (the last HISTORY events) is kept for the Mods page.
"""
from __future__ import annotations

import asyncio
import collections
import fnmatch
import json
import logging
import time
import uuid
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.mods import manifest as mf
from src.mods import registry
from src.mods import signing

logger = logging.getLogger(__name__)

HISTORY = 200
MAX_DATA_BYTES = 64 * 1024
DELIVERY_TIMEOUT_S = 10

_history: Deque[Dict[str, Any]] = collections.deque(maxlen=HISTORY)
# subscriber mod id -> [(pattern, callback path)]
_runtime_subs: Dict[str, List[Tuple[str, str]]] = {}
_pending: set = set()


def matches(pattern: str, source: str, etype: str) -> bool:
    if "/" in pattern:
        src_pat, type_pat = pattern.split("/", 1)
        return fnmatch.fnmatchcase(source, src_pat) and fnmatch.fnmatchcase(etype, type_pat)
    return fnmatch.fnmatchcase(etype, pattern)


def add_subscription(mod_id: str, pattern: str, path: Optional[str] = None) -> Dict[str, str]:
    if not mf.EVENT_PATTERN_RE.match(pattern or ""):
        raise ValueError("pattern must look like print-hub/print.* or *.failed")
    if path is not None and (not mf.PATH_RE.match(path) or ".." in path.split("/")):
        raise ValueError("path must be a path on your own server, starting with /")
    subs = _runtime_subs.setdefault(mod_id, [])
    item = (pattern, path or "")
    if item not in subs:
        if len(subs) >= 64:
            raise ValueError("at most 64 subscriptions per mod")
        subs.append(item)
    return {"pattern": pattern, "path": path or ""}


def remove_subscription(mod_id: str, pattern: str) -> int:
    subs = _runtime_subs.get(mod_id, [])
    keep = [s for s in subs if s[0] != pattern]
    _runtime_subs[mod_id] = keep
    return len(subs) - len(keep)


def subscriptions(mod_id: str) -> List[Dict[str, str]]:
    entry = registry.get(mod_id) or {}
    man = entry.get("manifest") or {}
    hook = (man.get("events") or {}).get("webhook") or "/mod/events"
    out = [{"pattern": p, "path": hook, "from": "manifest"} for p in (man.get("events") or {}).get("subscribes") or []]
    out += [{"pattern": p, "path": path or hook, "from": "runtime"} for p, path in _runtime_subs.get(mod_id, [])]
    return out


def _targets(source: str, etype: str) -> List[Tuple[str, str]]:
    """(mod id, path) for every enabled subscriber with a matching pattern."""
    out = []
    for entry in registry.list_mods():
        mid = entry["id"]
        if not entry.get("enabled") or "events.subscribe" not in (entry.get("granted_permissions") or []):
            continue
        seen_paths = set()
        for sub in subscriptions(mid):
            if matches(sub["pattern"], source, etype) and sub["path"] not in seen_paths:
                seen_paths.add(sub["path"])
                out.append((mid, sub["path"]))
    return out


async def _deliver(mod_id: str, path: str, event: Dict[str, Any]) -> None:
    from src.mods import runtime
    port = await runtime.ensure_running(mod_id)
    if not port:
        runtime.log_line(mod_id, f"[odysseus] event {event['type']} not delivered: the mod is not running")
        return
    headers = signing.sign_identity(registry.token_of(mod_id), mod_id, "odysseus-core")
    headers["Content-Type"] = "application/json"
    headers["X-Odysseus-Event"] = event["type"]
    try:
        r = await runtime._client().post(f"http://127.0.0.1:{port}{path}", content=json.dumps(event),
                                         headers=headers, timeout=DELIVERY_TIMEOUT_S)
        if r.status_code >= 400:
            runtime.log_line(mod_id, f"[odysseus] event {event['type']} -> {path}: HTTP {r.status_code}")
    except Exception as e:
        runtime.log_line(mod_id, f"[odysseus] event {event['type']} -> {path} failed: {type(e).__name__}")


def publish(source: str, etype: str, data: Any, owner: Optional[str] = None) -> Dict[str, Any]:
    """Record and deliver an event. Returns it (with its id and who gets it)."""
    if not mf.EVENT_RE.match(etype or ""):
        raise ValueError("type must look like print.finished")
    raw = json.dumps(data if data is not None else {}, default=str)
    if len(raw) > MAX_DATA_BYTES:
        raise ValueError("event data is larger than 64 KB")
    event = {"id": uuid.uuid4().hex[:16], "source": source, "type": etype, "data": json.loads(raw),
             "ts": time.time()}
    targets = [t for t in _targets(source, etype) if t[0] != source]
    event_view = dict(event, delivered_to=[t[0] for t in targets])
    _history.append(event_view)
    for mid, path in targets:
        task = asyncio.get_running_loop().create_task(_deliver(mid, path, event))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    try:
        from src.event_bus import fire_event
        fire_event(f"mod:{source}:{etype}", owner)
    except Exception:
        logger.debug("core event bus fire failed", exc_info=True)
    return event_view


def recent(limit: int = 50, source: Optional[str] = None) -> List[Dict[str, Any]]:
    items = [e for e in _history if not source or e["source"] == source or source in e.get("delivered_to", [])]
    return list(reversed(items))[:limit]
