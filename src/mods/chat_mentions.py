"""@-mentions of mods in chats (permission `chat.mention`, manifest `chat`).

A mod that declares

    "permissions": ["chat.mention"],
    "chat": {"mention": {"label": "Trove", "description": "Your collection", "icon": "box"},
             "mention_items": "/mod/chat/items",       optional: sub-items for the picker
             "mention_context": "/mod/chat/context"}   optional: a short note for the turn

appears in the chat composer's @ picker for every user who may use the mod.
Picking it writes `@<handle>` (or `@<handle>/<ref>` for a sub-item) into the
message. For that turn:

  * the mod's agent tools are always offered, first, and the system context
    says the user addressed the mod (src/agent_loop.py), whatever the words;
  * the mod's `mention_context` endpoint, if any, may return a short note
    about what was pointed at; it is added to the message (routes/chat_routes.py);
  * the mention shows as a chip in the sent message and in history
    (static/js/modMentions.js).

Several mods may be mentioned in one message (at most MAX_MENTIONS). The text
is the only record: this module parses it again wherever it is needed, so a
mention in an old message is the same mention.

Endpoints the mod serves (called by the core as the signed-in user):

    GET  <mention_items>?q=<text>       -> {"items": [{"ref": "ACME-1", "label": "Fix the login", "help": "open"}]}
    POST <mention_context> {"text", "handle", "items": ["ACME-1"]} -> {"context": "short text"}

`ref` is a token without spaces ([A-Za-z0-9_.:-], 60 characters at most).
Context is capped (CONTEXT_PER_MOD, CONTEXT_TOTAL characters) and a mod that
fails or is slow (CALL_TIMEOUT_S) is skipped: the turn goes on without it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.mods import access, registry, signing
from src.mods import tools as mod_tools

logger = logging.getLogger(__name__)

PERMISSION = "chat.mention"
MAX_MENTIONS = 5
MAX_ITEMS = 20
CONTEXT_PER_MOD = 2000
CONTEXT_TOTAL = 6000
CALL_TIMEOUT_S = 5.0
_MENTION_RE = re.compile(r"(?<![\w.@/-])@([A-Za-z][A-Za-z0-9-]{1,30})(?:/([A-Za-z0-9_.:-]{1,60}))?(?![\w])")
_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,60}$")


def _chat(entry: Dict[str, Any]) -> Dict[str, Any]:
    return (entry.get("manifest") or {}).get("chat") or {}


def usable(owner: Optional[str]) -> List[Dict[str, Any]]:
    """Mods this user may @-mention: enabled, may use, declared and granted."""
    out = []
    for entry in sorted(registry.list_mods(), key=lambda m: m["id"]):
        if not entry.get("enabled") or not _chat(entry).get("mention"):
            continue
        if PERMISSION not in (entry.get("granted_permissions") or []):
            continue
        if not access.may_use(entry, owner):
            continue
        out.append(entry)
    return out


def by_handle(owner: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """handle (lowercase) -> mod entry. When two mods ask for one handle, the first by id keeps it."""
    out: Dict[str, Dict[str, Any]] = {}
    for entry in usable(owner):
        out.setdefault(_chat(entry)["mention"]["handle"].lower(), entry)
    return out


def catalog(owner: Optional[str]) -> List[Dict[str, Any]]:
    """What the picker lists (GET /api/mods/chat/mentions)."""
    taken = by_handle(owner)
    rows = []
    for handle, entry in taken.items():
        c = _chat(entry)
        m = c["mention"]
        rows.append({"id": entry["id"], "handle": m["handle"], "label": m["label"], "description": m["description"],
                     "icon": m.get("icon") or "", "name": entry.get("name") or entry["id"],
                     "items": bool(c.get("mention_items")), "context": bool(c.get("mention_context"))})
    return rows


def parse(text: Any, owner: Optional[str]) -> List[Dict[str, Any]]:
    """The mods a message addresses, in order: [{mod_id, handle, label, item}], one row per
    (mod, item), at most MAX_MENTIONS mods. An @ that is not a mod's handle is ignored."""
    if not isinstance(text, str) or "@" not in text:
        return []
    handles = by_handle(owner)
    if not handles:
        return []
    found: List[Dict[str, Any]] = []
    mods = set()
    for m in _MENTION_RE.finditer(text):
        entry = handles.get(m.group(1).lower())
        if not entry:
            continue
        item = m.group(2) or None
        row = {"mod_id": entry["id"], "handle": m.group(1).lower(), "label": _chat(entry)["mention"]["label"],
               "item": item}
        if any(r["mod_id"] == row["mod_id"] and r["item"] == item for r in found):
            continue
        if entry["id"] not in mods and len(mods) >= MAX_MENTIONS:
            continue
        mods.add(entry["id"])
        found.append(row)
    return found


def mentioned_tools(text: Any, owner: Optional[str]) -> Tuple[List[str], List[str]]:
    """(qualified tool names of every mentioned mod, in mention order; the mods' labels)."""
    names: List[str] = []
    labels: List[str] = []
    seen = set()
    for r in parse(text, owner):
        if r["mod_id"] in seen:
            continue
        seen.add(r["mod_id"])
        labels.append(r["label"])
        entry = registry.get(r["mod_id"])
        if not entry or not (entry.get("manifest") or {}).get("tools"):
            continue
        names += [mod_tools.qualified(entry["id"], t["name"]) for t in mod_tools.cached(entry["id"])]
    return names, labels


def system_note(labels: List[str]) -> str:
    who = ", ".join(f"@{x}" for x in labels)
    return (f"The user addressed {who} in this message; use {'its' if len(labels) == 1 else 'their'} tools to "
            f"answer, before anything else.")


# ── Calling the mod ──────────────────────────────────────────────────────

async def _call(entry: Dict[str, Any], method: str, path: str, owner: Optional[str], **kw) -> Optional[Dict[str, Any]]:
    from src.mods import runtime
    port = await runtime.ensure_running(entry["id"])
    if not port:
        return None
    headers = signing.sign_identity(registry.token_of(entry["id"]), entry["id"],
                                    signing.caller(owner, access.auth_disabled()))
    if method == "POST":
        headers["Content-Type"] = "application/json"
    try:
        r = await runtime._client().request(method, f"http://127.0.0.1:{port}{path}", headers=headers,
                                            timeout=CALL_TIMEOUT_S, **kw)
        if r.status_code >= 400:
            return None
        d = r.json()
    except Exception as e:
        logger.debug("[mods] %s chat mention call failed: %s", entry["id"], type(e).__name__)
        return None
    return d if isinstance(d, dict) else None


async def items(mod_id: str, owner: Optional[str], q: str = "") -> Optional[List[Dict[str, str]]]:
    """The picker's sub-items for a mod (None: no such mod for this user or no items endpoint)."""
    entry = next((e for e in usable(owner) if e["id"] == mod_id), None)
    path = _chat(entry).get("mention_items") if entry else None
    if not path:
        return None
    d = await _call(entry, "GET", path, owner, params={"q": (q or "")[:80]})
    out: List[Dict[str, str]] = []
    for it in ((d or {}).get("items") or [])[:MAX_ITEMS]:
        if isinstance(it, dict) and isinstance(it.get("ref"), str) and _REF_RE.match(it["ref"]):
            out.append({"ref": it["ref"], "label": str(it.get("label") or it["ref"])[:120],
                        "help": str(it.get("help") or "")[:160]})
    return out


async def context_block(text: Any, owner: Optional[str]) -> str:
    """The mods' short notes for this message, as one block to add to it ('' for none)."""
    parts: List[str] = []
    by_mod: Dict[str, List[Dict[str, Any]]] = {}
    for r in parse(text, owner):
        by_mod.setdefault(r["mod_id"], []).append(r)
    total = 0
    for mod_id, rows in by_mod.items():
        entry = registry.get(mod_id)
        path = _chat(entry).get("mention_context") if entry else None
        if not path:
            continue
        d = await _call(entry, "POST", path, owner, content=json.dumps({
            "text": str(text)[:8000], "handle": rows[0]["handle"], "items": [r["item"] for r in rows if r["item"]]}),
            )
        c = (d or {}).get("context")
        if not isinstance(c, str) or not c.strip():
            continue
        c = c.strip()[:min(CONTEXT_PER_MOD, CONTEXT_TOTAL - total)]
        if not c:
            break
        total += len(c)
        parts.append(f"[{rows[0]['label']}, for this message]\n{c}")
    return "\n\n".join(parts)


async def apply_context(messages: List[Dict[str, Any]], text: Any, owner: Optional[str]) -> int:
    """Add the notes to this turn's last user message (routes/chat_routes.py). Returns how many mods added one."""
    block = await context_block(text, owner)
    if not block:
        return 0
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = content + "\n\n" + block
            elif isinstance(content, list):
                m["content"] = content + [{"type": "text", "text": block}]
            break
    return block.count("for this message]")
