"""Sending a text to the owner's phone, when no Phone & Messages mod is installed.

Texting the agent from your phone (the SMS gateway: commands, conversations
with a chat, the inbound webhook) moved to the Phone & Messages mod
(Jaron-Wilson/odysseus-phone), with phone calls, the SIP line and Google Meet.
What stays here is the core's own way to send a text: the `messaging.sms`
capability's built-in provider (src/mods/capabilities.py `_sms_builtin` sends
with deliver() and split_reply()), and mod notifications at or above a level
(src/mods/notify.py). A Phone & Messages mod that is installed and enabled
provides the capability itself, with its own settings, and takes both over.

Owner side (logged in, Settings > Calls & Meetings > Text messages):
    GET  /api/sms/config     numbers and reply URL (and whether the old
                             gateway had a forward secret)
    PUT  /api/sms/config     save them
    POST /api/sms/test       send a test text through the reply URL

A text goes to the phone's local send endpoint (SMS Gate for Android,
`http://user:password@<phone tailscale ip>:8080/message`), or as a web push
to the owner's own browsers when no reply URL is set.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException, Request

from routes import prefs_routes
from src.auth_helpers import _auth_disabled, require_user

logger = logging.getLogger(__name__)

PREF_KEY = "sms_gateway"
MAX_NUMBERS = 5
REPLY_TIMEOUT = 5.0
REPLY_CHARS = 450
MAX_PARTS = 6
CONTINUED = "...continued in the app"
MOD_HINT = ("Texting the agent from your phone, phone calls and Google Meet are the Phone & Messages mod "
            "(Settings > Mods > Browse, search for phone).")


def normalize_number(raw: Any) -> str:
    """E.164-ish: "+1 (555) 010-2000", "5550102000" and "15550102000" are one
    number. A leading 00 is the international prefix. Short codes stay as
    digits. Empty for anything with no digits."""
    s = str(raw or "").strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if plus:
        return "+" + digits
    if digits.startswith("00") and len(digits) > 4:
        return "+" + digits[2:]
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) > 11:
        return "+" + digits
    return digits


def get_config(user: Optional[str]) -> Dict:
    cfg = prefs_routes._load_for_user(user).get(PREF_KEY)
    return dict(cfg) if isinstance(cfg, dict) else {}


def save_config(user: Optional[str], cfg: Dict) -> None:
    prefs = prefs_routes._load_for_user(user)
    prefs[PREF_KEY] = cfg
    prefs_routes._save_for_user(user, prefs)


def _cut(text: str, limit: int) -> int:
    """Where to end a part of at most `limit` characters: the last line
    break or sentence end in its second half, else the last space."""
    head = text[: limit + 1]
    best = head.rfind("\n", 0, limit)
    for m in re.finditer(r"[.!?][\"')\]]*\s", head):
        if m.end() - 1 <= limit:
            best = max(best, m.end() - 1)
    if best >= limit // 2:
        return best
    space = head.rfind(" ", 0, limit)
    return space if space >= limit // 3 else limit


def split_reply(text: str, size: int = REPLY_CHARS, max_parts: int = MAX_PARTS) -> List[str]:
    """A text as parts of at most `size` characters. One that fits is sent
    as is; a longer one becomes "(1/3) ...", "(2/3) ..." parts, and past
    `max_parts` the last part ends with CONTINUED."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text]
    budget = size - len(f"({max_parts}/{max_parts}) ")
    parts: List[str] = []
    rest = text
    while rest and len(parts) < max_parts:
        if len(rest) <= budget:
            parts.append(rest)
            rest = ""
            break
        at = _cut(rest, budget)
        parts.append(rest[:at].rstrip())
        rest = rest[at:].lstrip()
    if rest:
        room = budget - len(CONTINUED) - 1
        last = parts[-1]
        if len(last) > room:
            last = last[:_cut(last, room)].rstrip()
        parts[-1] = f"{last} {CONTINUED}"
    return [f"({i}/{len(parts)}) {p}" for i, p in enumerate(parts, 1)]


def reply_body(to: str, text: str) -> Dict:
    """What the phone's send endpoint gets: the body SMS Gate for Android's
    local server takes on POST /message (docs.sms-gate.app, local server)."""
    return {"textMessage": {"text": text}, "phoneNumbers": [to]}


async def _post_reply(url: str, payload: Dict) -> int:
    """POST to the reply URL. Credentials in the URL (http://user:pass@host)
    become basic auth, which is what the phone's local server wants."""
    import httpx
    parts = urlsplit(url)
    auth = None
    if parts.username is not None:
        auth = (unquote(parts.username), unquote(parts.password or ""))
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        url = parts._replace(netloc=host).geturl()
    async with httpx.AsyncClient(timeout=REPLY_TIMEOUT) as client:
        r = await client.post(url, json=payload, auth=auth)
        return r.status_code


async def _push_owner(owner: Optional[str], text: str) -> Dict:
    """Web push to the owner's own browsers only: webpush.send with no target
    would reach every user's subscriptions. With auth off there is one user."""
    from src import webpush
    subs = [s for s in webpush.load_subscriptions()
            if s.get("endpoint") and (_auth_disabled() or (s.get("owner") or "") == (owner or ""))]
    sent = 0
    for s in subs:
        r = await webpush.send("Odysseus (SMS)", text, endpoint=s["endpoint"], url="/", tag="odysseus-sms")
        sent += int(r.get("sent") or 0)
    return {"sent": sent, "subscriptions": len(subs)}


async def deliver(owner: Optional[str], cfg: Dict, to: str, text: str) -> str:
    """Send `text` to the phone. Returns which channel took it: reply_url,
    push or failed."""
    url = str(cfg.get("reply_url") or "").strip()
    if url:
        try:
            code = await _post_reply(url, reply_body(to, text))
            logger.info("[sms] text to %s via reply URL: HTTP %s", to, code)
            if 200 <= code < 300:
                return "reply_url"
        except Exception as e:
            logger.warning("[sms] text to %s via reply URL failed: %s", to, type(e).__name__)
        return "failed"
    try:
        r = await _push_owner(owner, text)
        logger.info("[sms] text to %s via web push: %s sent", to, r["sent"])
        return "push" if r["sent"] else "failed"
    except Exception as e:
        logger.warning("[sms] web push failed: %s", type(e).__name__)
        return "failed"


def _public(cfg: Dict) -> Dict:
    return {
        "numbers": list(cfg.get("numbers") or []),
        "reply_url": str(cfg.get("reply_url") or ""),
        # Left from the built-in gateway: the Phone & Messages mod's import
        # takes it over (docs/mods.md, "Pages that moved to mods").
        "had_gateway": bool(cfg.get("secret_hash")),
        "mod_hint": MOD_HINT,
    }


def setup_sms_routes() -> APIRouter:
    router = APIRouter(tags=["sms"])

    @router.get("/api/sms/config")
    async def read_config(request: Request):
        return _public(get_config(require_user(request) or None))

    @router.put("/api/sms/config")
    async def write_config(request: Request):
        user = require_user(request) or None
        body = await request.json()
        numbers: List[str] = []
        for n in body.get("numbers") or []:
            norm = normalize_number(n)
            if len(norm.lstrip("+")) < 5:
                raise HTTPException(400, f"Not a phone number: {str(n)[:30]}")
            if norm not in numbers:
                numbers.append(norm)
        if len(numbers) > MAX_NUMBERS:
            raise HTTPException(400, f"At most {MAX_NUMBERS} numbers.")
        reply_url = str(body.get("reply_url") or "").strip()
        if reply_url and urlsplit(reply_url).scheme not in ("http", "https"):
            raise HTTPException(400, "The reply URL must start with http:// or https://")
        cfg = get_config(user)
        cfg.update({"numbers": numbers, "reply_url": reply_url})
        save_config(user, cfg)
        logger.info("[sms] config saved for %s: %d number(s), reply %s",
                    user or "-", len(numbers), "URL" if reply_url else "web push")
        return _public(cfg)

    @router.post("/api/sms/test")
    async def test_reply(request: Request):
        user = require_user(request) or None
        cfg = get_config(user)
        numbers = cfg.get("numbers") or []
        if not numbers:
            raise HTTPException(400, "Add your number first.")
        to_test = numbers if str(cfg.get("reply_url") or "").strip() else numbers[:1]
        results = []
        for to in to_test:
            via = await deliver(user, cfg, to, "Odysseus: texts from the server can reach you.")
            results.append({"to": to, "ok": via != "failed", "via": via})
        return {"ok": all(r["ok"] for r in results), "results": results}

    return router
