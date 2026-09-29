"""Tidy a full chat: write its notes, then prune what they cover.

Asked for on 2026-09-29: "when i get the note that the conversation is over
the token count, could i enable a sub agent to go through the chat, make
necessary notes, then purge or prune the chat?"

Compaction (context_compactor) replaces the older half with a hidden summary.
Tidy keeps everything visible instead: a model reads the older messages and
writes what is worth keeping as "Needs to know" notes (chat_memory, read on
every turn), and only then are those messages left out of context, the same
way the Prune button does. Nothing is deleted and any message can be put
back. If no notes come back, nothing is pruned.

Run by hand (Tidy in the context popup, POST /api/session/{id}/tidy) or after
each reply once the chat is TIDY_AT full, when the chat has "tidy" switched on
(chat_prefs). Compaction stays as the safety net at its own threshold.
"""
import asyncio
import json
import logging
import re
from typing import Dict, List, Optional

from core.models import ChatMessage

logger = logging.getLogger(__name__)

TIDY_AT = 0.70              # of the context window, after a reply
KEEP_RECENT = 6             # newest messages always stay in context
MIN_TO_TIDY = 4
MAX_NOTES = 12
NOTE_CHARS = 300
TRANSCRIPT_CHARS = 60000

TIDY_PROMPT = (
    "You are tidying a long chat so it fits the model's memory. Below are its OLDER messages, which are "
    "about to be left out of context, and the notes the chat already keeps. Write the notes someone "
    "continuing the chat will need from these older messages: decisions made, facts found (names, file "
    "paths, URLs, versions, numbers, commands that worked), what the user asked for and how they like "
    "things done, and anything still open or promised. Skip small talk and anything the existing notes "
    "already say. Never write passwords, API keys, tokens or other secrets, even if they appear.\n\n"
    'Reply with JSON only, no other text: {"notes": ["...", "..."]}. At most {max_notes} notes, each '
    "one self-contained sentence or two under {chars} characters."
)

_running: set = set()


def _meta(m) -> Dict:
    md = m.metadata if isinstance(m, ChatMessage) else m.get("metadata")
    return md if isinstance(md, dict) else {}


def _role(m) -> str:
    return m.role if isinstance(m, ChatMessage) else str(m.get("role") or "")


def _text(m) -> str:
    c = m.content if isinstance(m, ChatMessage) else m.get("content")
    if isinstance(c, list):
        c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return str(c or "")


def candidates(history: List, keep_recent: int = KEEP_RECENT) -> List:
    """Older messages still in context: what a tidy would prune."""
    live = []
    for m in history:
        md = _meta(m)
        if _role(m) == "system" or md.get("excluded") or md.get("hidden") or not md.get("_db_id"):
            continue
        if md.get("source") in ("slash", "chat_tidy"):
            continue
        live.append(m)
    return live[:-keep_recent] if len(live) > keep_recent else []


def transcript(msgs: List, budget: int = TRANSCRIPT_CHARS) -> str:
    each = max(300, budget // max(1, len(msgs)))
    lines = []
    for m in msgs:
        t = _text(m).strip()
        if len(t) > each:
            t = t[: each // 2] + " […] " + t[-each // 2:]
        lines.append(f"{_role(m).upper()}: {t}")
    return "\n\n".join(lines)[:budget + 2000]


def parse_notes(reply: str) -> List[str]:
    reply = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S)
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    notes = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(notes, list):
        return []
    out = []
    for n in notes[:MAX_NOTES]:
        n = " ".join(str(n or "").split())
        if len(n) < 8:
            continue
        # A model asked not to can still copy a key out of the chat.
        if re.search(r"\b(sk-[A-Za-z0-9]{12,}|ody_[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{12,})", n):
            continue
        out.append(n[:NOTE_CHARS])
    return out


def _exclude(session_id: str, msgs: List) -> List[str]:
    """Leave these out of context, in memory and in the DB (like Prune)."""
    from core.database import SessionLocal, ChatMessage as DbChatMessage
    changed = []
    db = SessionLocal()
    try:
        for m in msgs:
            md = _meta(m)
            if md.get("excluded") or not md.get("_db_id"):
                continue
            md["excluded"] = True
            md["tidied"] = True
            row = db.query(DbChatMessage).filter(
                DbChatMessage.id == md["_db_id"], DbChatMessage.session_id == session_id).first()
            if row:
                stored = {}
                if row.meta_data:
                    try:
                        stored = json.loads(row.meta_data)
                    except ValueError:
                        stored = {}
                stored["excluded"] = True
                stored["tidied"] = True
                row.meta_data = json.dumps(stored)
            changed.append(md["_db_id"])
        db.commit()
    finally:
        db.close()
    return changed


async def tidy(session, *, owner: str = "", keep_recent: int = KEEP_RECENT, reason: str = "by hand") -> Dict:
    from src import chat_memory
    from src.llm_core import llm_call_async
    from src.endpoint_resolver import resolve_endpoint

    sid = session.id
    if sid in _running:
        return {"tidied": False, "reason": "A tidy is already running for this chat."}
    older = candidates(session.history, keep_recent)
    if len(older) < MIN_TO_TIDY:
        return {"tidied": False, "reason": "Not enough older messages to tidy yet."}
    _running.add(sid)
    try:
        existing = [i["text"] for i in chat_memory.get(sid)["items"]]
        room = max(0, chat_memory.MAX_ITEMS - len(existing))
        util_url, util_model, util_headers = resolve_endpoint("utility", owner=owner or None)
        url = util_url or session.endpoint_url
        model = util_model or session.model
        headers = util_headers if util_url else getattr(session, "headers", None)
        prompt = TIDY_PROMPT.replace("{max_notes}", str(min(MAX_NOTES, room) or 1)).replace("{chars}", str(NOTE_CHARS))
        user = ("Existing notes:\n" + ("\n".join(f"- {t}" for t in existing) or "(none)")
                + f"\n\nOlder messages ({len(older)}):\n\n" + transcript(older))
        reply = await llm_call_async(url, model, [{"role": "system", "content": prompt},
                                                   {"role": "user", "content": user}],
                                     temperature=0.2, max_tokens=1500, headers=headers, timeout=120)
        notes = parse_notes(reply)
        if not notes:
            return {"tidied": False, "reason": "The model wrote no notes, so nothing was pruned."}
        saved, full = [], 0
        for n in notes:
            try:
                chat_memory.add(sid, n, by="tidy", owner=owner or "")
                saved.append(n)
            except ValueError:
                full += 1
        if not saved:
            return {"tidied": False, "reason": "Needs to know is full, so the notes could not be saved and "
                                               "nothing was pruned. Remove some notes first."}
        pruned = _exclude(sid, older)
        text = (f"**Chat tidied** ({reason}): saved {len(saved)} note{'s' if len(saved) != 1 else ''} to Needs to know "
                f"and left {len(pruned)} older message{'s' if len(pruned) != 1 else ''} out of context. They are "
                "still above; Put back brings any of them back.")
        if full:
            text += f" {full} more note{'s' if full != 1 else ''} did not fit: Needs to know is full."
        from src.ai_interaction import get_session_manager
        sm = get_session_manager()
        msg = ChatMessage("assistant", text, metadata={"source": "chat_tidy", "notes": saved,
                                                       "pruned": len(pruned)})
        if sm:
            sm.add_message(sid, msg)
        else:
            session.add_message(msg)
        logger.info("[tidy] %s: %d notes, %d pruned (%s)", sid[:8], len(saved), len(pruned), reason)
        return {"tidied": True, "notes": saved, "pruned": len(pruned), "not_saved": full}
    finally:
        _running.discard(sid)


def usage(session, last_metrics: Optional[Dict] = None) -> float:
    ctx = (last_metrics or {}).get("context_length")
    used = (last_metrics or {}).get("input_tokens")
    if not (ctx and used):
        from src.model_context import estimate_tokens, get_context_length
        ctx = get_context_length(session.endpoint_url, session.model)
        used = estimate_tokens(session.get_context_messages())
    return (used / ctx) if ctx else 0.0


def schedule_if_full(session, last_metrics: Optional[Dict] = None, *, owner: str = "") -> bool:
    """After a reply: tidy in the background if this chat asked for it and is full enough."""
    from src import chat_prefs
    if not chat_prefs.get(session.id).get("tidy") or session.id in _running:
        return False
    if usage(session, last_metrics) < TIDY_AT:
        return False

    async def _go():
        await asyncio.sleep(1)
        try:
            await tidy(session, owner=owner, reason=f"automatically, the chat was over {int(TIDY_AT * 100)}% full")
        except Exception as e:
            logger.warning("[tidy] %s failed: %s", session.id[:8], e)
    asyncio.create_task(_go())
    return True
