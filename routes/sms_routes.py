"""SMS gateway: text Odysseus from your phone.

Asked for: "your Pixel's SMS-Forwarder posts each incoming text to a new
POST /api/sms/inbound ... commands are list, say <n> <text>, status, help;
replies go back over the phone's send endpoint, or web-push if that's unset".
Then: "what if i want to just chat with an agent, can i select a model for
that text messaging? so then i can talk back and forth between it". So each
sender has a current conversation (new, chat <n>, end): any text that is not
a command is a turn in it, and models / model <n|name> pick its model.

Phone side (the secret in the path is the credential, like the task webhooks):
    POST /api/sms/inbound/{secret}      one incoming text from the forwarder app
Owner side (logged in, Settings > Calls & Meetings > Phone SMS):
    GET  /api/sms/config                numbers, reply URL, whether a secret exists
    PUT  /api/sms/config                save numbers and reply URL
    POST /api/sms/secret                mint a new secret (shown once), drops the old one
    DELETE /api/sms/secret              turn the gateway off
    POST /api/sms/test                  send a test reply through the reply channel

The inbound path is exempt from login in app.py. A text is acted on only when
the secret matches one owner's stored hash AND the sender is one of that
owner's allowed numbers; anything else gets the same 404 as a path that does
not exist, so a caller cannot tell which check failed. The secret itself is
never stored (only its SHA-256) and never logged; uvicorn's access log line is
redacted too, since the path carries it.

A "say" is a model turn and can take a while. The forwarder apps give up on a
slow webhook and retry, which would send the message twice, so the answer is
waited for only SAY_INLINE_WAIT seconds. After that the response is an
acknowledgement and the answer goes out through the reply channel when ready.
Every reply goes through the reply channel either way, since the forwarder
apps do not show the HTTP response to anyone. A long answer goes out as up to
MAX_PARTS numbered texts, "(1/3) ...", in order.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from routes import prefs_routes
from src.auth_helpers import _auth_disabled, require_user

logger = logging.getLogger(__name__)

PREF_KEY = "sms_gateway"
INBOUND_PREFIX = "/api/sms/inbound/"
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
MAX_NUMBERS = 5
MAX_TEXT = 2000
REPLY_CHARS = 450
MAX_PARTS = 6
CONTINUED = "...continued in the app"
LIST_LIMIT = 10
MODELS_LIMIT = 10
SAY_INLINE_WAIT = 5.0
REPLY_TIMEOUT = 5.0

# The last "list" each owner was sent, so "say 2" means the chat that was
# number 2 in that text even after a reply has moved it to the top.
_LAST_LIST: Dict[str, Tuple[float, List[str]]] = {}
LIST_MEMORY_S = 6 * 3600
# Likewise the last "models", so "model 3" is the model that was number 3.
_LAST_MODELS: Dict[str, Tuple[float, List[Dict]]] = {}
# Replies and long "say" turns run past the request; keep them referenced.
_PENDING: set = set()

HELP = ("Odysseus commands:\n"
        "new [model] - start a chat and talk to it\n"
        "chat <n> - talk to chat n from the last list\n"
        "end - stop talking to it\n"
        "models - models you can use, numbered\n"
        "model <n|name> - switch this chat's model\n"
        "list - your recent chats, numbered\n"
        "say <n> <text> - one text to chat n\n"
        "status - running agents and jobs\n"
        "call - a link that opens a voice call with this chat\n"
        "help - this message\n"
        "Anything else goes to the chat you are talking to.")
HINT = ("You are not talking to a chat. Text new to start one, or list and then "
        "chat <n> to pick one. help lists the commands.")


# ── Phone numbers ──────────────────────────────────────────────────────────

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


# ── Config (in the per-user prefs store) ────────────────────────────────────

def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _owners() -> List[Tuple[Optional[str], Dict]]:
    """Every (owner, sms config) in the prefs store. A flat (auth off) store
    has one config with no owner."""
    raw = prefs_routes._load()
    if "_users" in raw and isinstance(raw["_users"], dict):
        items = list(raw["_users"].items())
    else:
        items = [(None, raw)]
    out = []
    for user, prefs in items:
        cfg = prefs.get(PREF_KEY) if isinstance(prefs, dict) else None
        if isinstance(cfg, dict):
            out.append((user, cfg))
    return out


def get_config(user: Optional[str]) -> Dict:
    cfg = prefs_routes._load_for_user(user).get(PREF_KEY)
    return dict(cfg) if isinstance(cfg, dict) else {}


def save_config(user: Optional[str], cfg: Dict) -> None:
    prefs = prefs_routes._load_for_user(user)
    prefs[PREF_KEY] = cfg
    prefs_routes._save_for_user(user, prefs)


def get_conversation(owner: Optional[str], sender: str) -> str:
    """The chat this sender is talking to, or "". Kept in the owner's sms
    config, keyed by normalized number, so it survives a restart."""
    conv = get_config(owner).get("conversations")
    entry = conv.get(sender) if isinstance(conv, dict) else None
    return str(entry.get("sid") or "") if isinstance(entry, dict) else ""


def set_conversation(owner: Optional[str], sender: str, sid: Optional[str]) -> None:
    """Make `sid` the sender's current chat (None clears it). Re-reads the
    config first so a save from Settings in the meantime is kept."""
    cfg = get_config(owner)
    conv = dict(cfg.get("conversations") or {})
    if sid:
        conv[sender] = {"sid": sid, "since": time.time()}
    else:
        conv.pop(sender, None)
    cfg["conversations"] = conv
    save_config(owner, cfg)


def match_secret(secret: str) -> Optional[Tuple[Optional[str], Dict]]:
    """The owner whose stored hash this secret has. Every config is compared
    (no early exit), in constant time, so timing says nothing either."""
    if not SECRET_RE.match(secret or ""):
        return None
    want = _hash(secret)
    found = None
    for user, cfg in _owners():
        stored = str(cfg.get("secret_hash") or "")
        if stored and hmac.compare_digest(stored, want) and found is None:
            found = (user, cfg)
    return found


# ── Inbound body: each forwarder app names its fields its own way ──────────

_FROM_KEYS = ("from", "sender", "phoneNumber", "phone_number", "number", "msisdn")
_TEXT_KEYS = ("text", "message", "body", "msg", "content")


def parse_inbound(body: Dict) -> Tuple[str, str, str]:
    """(from, text, event) from any of the shapes docs/sms-gateway.md covers:
    {from, text} from SMS Forwarder's default JSON template, and SMS Gateway
    for Android's {"event": "sms:received", "payload": {"sender", "message"}}
    (older builds send payload.phoneNumber instead of sender)."""
    event = str(body.get("event") or "")
    src = body.get("payload") if isinstance(body.get("payload"), dict) else body

    def first(keys):
        for k in keys:
            v = src.get(k)
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return str(v)
        return ""

    return first(_FROM_KEYS), first(_TEXT_KEYS), event


async def _read_body(request: Request) -> Dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        import json
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    try:
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items() if v}
    except Exception:
        return {}


# ── Sessions ───────────────────────────────────────────────────────────────

def _session_owner_scope(owner: Optional[str]) -> Optional[str]:
    """Whose chats a text may reach. With auth off there is one user and
    chats carry no owner, so every chat is theirs."""
    return None if _auth_disabled() else owner


def _ago(ts) -> str:
    if not ts:
        return "never"
    try:
        now = datetime.now(timezone.utc) if ts.tzinfo else datetime.utcnow()
        s = (now - ts).total_seconds()
    except Exception:
        return "?"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s / 60)}m ago"
    if s < 86400:
        return f"{int(s / 3600)}h ago"
    return f"{int(s / 86400)}d ago"


def _recent_sessions(session_manager, owner: Optional[str]) -> List[Tuple[str, Any, Any]]:
    """(sid, session, last activity) for the owner's chats, newest first,
    archived ones left out."""
    scope = _session_owner_scope(owner)
    sessions = session_manager.get_sessions_for_user(scope)
    stamps: Dict[str, Any] = {}
    try:
        from core.database import SessionLocal, Session as DbSession
        db = SessionLocal()
        try:
            for r in db.query(DbSession).filter(DbSession.id.in_(list(sessions.keys()))).all():
                stamps[r.id] = (getattr(r, "last_message_at", None) or getattr(r, "last_accessed", None)
                                or getattr(r, "updated_at", None))
        finally:
            db.close()
    except Exception as e:
        logger.debug("[sms] session timestamps unavailable: %s", e)
    rows = [(sid, s, stamps.get(sid)) for sid, s in sessions.items()
            if not getattr(s, "archived", False)
            and (scope is None or getattr(s, "owner", None) == scope)]
    rows.sort(key=lambda r: (r[2] is not None, r[2].replace(tzinfo=None) if r[2] else datetime.min),
              reverse=True)
    return rows


def _owner_key(owner: Optional[str]) -> str:
    return owner or ""


def cmd_list(session_manager, owner: Optional[str]) -> str:
    rows = _recent_sessions(session_manager, owner)[:LIST_LIMIT]
    _LAST_LIST[_owner_key(owner)] = (time.time(), [sid for sid, _, _ in rows])
    if not rows:
        return "You have no chats yet."
    lines = [f"{i}. {(s.name or 'Untitled')[:40]} ({getattr(s, 'model', '') or 'no model'}, {_ago(ts)})"
             for i, (sid, s, ts) in enumerate(rows, 1)]
    return "\n".join(lines) + "\nReply: chat <n> to talk to one, or say <n> <text>"


def _resolve_n(session_manager, owner: Optional[str], n: int):
    """Chat n from the owner's last list (or today's order if there was none),
    only if it is still theirs."""
    remembered = _LAST_LIST.get(_owner_key(owner))
    if remembered and time.time() - remembered[0] < LIST_MEMORY_S:
        ids = remembered[1]
    else:
        ids = [sid for sid, _, _ in _recent_sessions(session_manager, owner)[:LIST_LIMIT]]
    if n < 1 or n > len(ids):
        return None
    sid = ids[n - 1]
    try:
        sess = session_manager.get_session(sid)
    except Exception:
        return None
    scope = _session_owner_scope(owner)
    if not sess or (scope is not None and getattr(sess, "owner", None) != scope):
        return None
    return sid, sess


def _owned(session_manager, owner: Optional[str], sid: str):
    """The session `sid`, only if it still exists and is the owner's."""
    try:
        sess = session_manager.get_session(sid) if sid else None
    except Exception:
        return None
    scope = _session_owner_scope(owner)
    if not sess or (scope is not None and getattr(sess, "owner", None) != scope):
        return None
    return sess


def _current(session_manager, owner: Optional[str], sender: str):
    """(sid, session) the sender is talking to. A chat that was deleted or
    is no longer theirs is forgotten."""
    sid = get_conversation(owner, sender)
    if not sid:
        return None
    sess = _owned(session_manager, owner, sid)
    if not sess:
        set_conversation(owner, sender, None)
        return None
    return sid, sess


def _chat_label(sess) -> str:
    return f"{(getattr(sess, 'name', '') or 'Untitled')[:40]} ({getattr(sess, 'model', '') or 'no model'})"


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
    """A reply as texts of at most `size` characters. One that fits is sent
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


async def _say(sid: str, message: str, owner: Optional[str]) -> str:
    """One turn in chat `sid` through the send_to_session agent tool."""
    from src.ai_interaction import do_send_to_session
    r = await do_send_to_session(f"{sid}\n{message}", owner=_session_owner_scope(owner))
    if r.get("error"):
        logger.warning("[sms] say to %s failed: %s", sid, r["error"])
        return "Sorry, that chat could not answer right now."
    return (r.get("response") or "(empty answer)").strip()


# ── Models ─────────────────────────────────────────────────────────────────

def available_models(owner: Optional[str], is_admin: bool) -> List[Dict]:
    """Every chat model the owner can pick, in the web picker's order (the
    same owner-scoped list GET /api/models serves). Offline and image
    endpoints are left out."""
    from routes.model_routes import fetch_models
    out: List[Dict] = []
    for item in fetch_models(owner=owner or "", is_admin=is_admin).get("items") or []:
        if item.get("offline") or (item.get("model_type") or "llm") != "llm":
            continue
        ids = list(item.get("models") or []) + list(item.get("models_extra") or [])
        shown = list(item.get("models_display") or []) + list(item.get("models_extra_display") or [])
        for i, mid in enumerate(ids):
            out.append({"model": mid, "name": shown[i] if i < len(shown) else mid.split("/")[-1],
                        "endpoint_id": item.get("endpoint_id") or "", "url": item.get("url") or "",
                        "endpoint_name": item.get("endpoint_name") or ""})
    return out


def cmd_models(owner: Optional[str], is_admin: bool, current_model: str = "") -> str:
    models = available_models(owner, is_admin)
    shown = models[:MODELS_LIMIT]
    _LAST_MODELS[_owner_key(owner)] = (time.time(), shown)
    if not shown:
        return "No models are available. Add a model endpoint in Settings."
    names = [m["name"] for m in shown]
    lines = []
    for i, m in enumerate(shown, 1):
        # The endpoint only when two endpoints serve a model of that name.
        where = f" ({m['endpoint_name'][:20]})" if names.count(m["name"]) > 1 and m["endpoint_name"] else ""
        mark = " *" if current_model and m["model"] == current_model else ""
        lines.append(f"{i}. {m['name'][:40]}{where}{mark}")
    if len(models) > len(shown):
        lines.append(f"(+{len(models) - len(shown)} more, model <name> finds them)")
    return "\n".join(lines) + "\nReply: model <n> to switch this chat, new <n> for a new one"


def _squash(s: str) -> str:
    return re.sub(r"[\s._:/-]+", "", s.lower())


def resolve_model(owner: Optional[str], is_admin: bool, arg: str) -> Optional[Dict]:
    """A model by its number in the last "models" text, or by name: an exact
    name first, then one that starts with it, then one that contains it,
    ignoring case, spaces and punctuation ("gpt 5" finds gpt-5)."""
    arg = arg.strip()
    if re.fullmatch(r"\d+", arg):
        remembered = _LAST_MODELS.get(_owner_key(owner))
        if remembered and time.time() - remembered[0] < LIST_MEMORY_S:
            shown = remembered[1]
        else:
            shown = available_models(owner, is_admin)[:MODELS_LIMIT]
        n = int(arg)
        return shown[n - 1] if 1 <= n <= len(shown) else None
    want = _squash(arg)
    if not want:
        return None
    models = available_models(owner, is_admin)
    for test in (lambda k: k == want, lambda k: k.startswith(want), lambda k: want in k):
        for m in models:
            if test(_squash(m["name"])) or test(_squash(m["model"])):
                return m
    return None


def _no_such_model(arg: str) -> str:
    arg = arg.strip()
    if re.fullmatch(r"\d+", arg):
        return f"There is no model {arg}. Send models to see the numbers."
    return f"No model matches \"{arg[:40]}\". Send models to see them."


def cmd_status(session_manager, owner: Optional[str]) -> str:
    scope = _session_owner_scope(owner)
    mine = set(session_manager.get_sessions_for_user(scope).keys())
    lines: List[str] = []
    try:
        from src import claude_code_jobs
        for j in claude_code_jobs.list_jobs(scope or ""):
            if j.status == "running" and (scope is None or j.owner == scope):
                lines.append(f"Agent {j.engine or 'coding'}: {j.prompt[:60] or j.action} "
                             f"({int((time.time() - j.started) / 60)}m)")
    except Exception as e:
        logger.debug("[sms] coding-agent status unavailable: %s", e)
    try:
        from src import bg_jobs
        for rec in bg_jobs.refresh().values():
            if rec.get("status") == "running" and rec.get("session_id") in mine:
                lines.append(f"Job: {str(rec.get('command') or '')[:60]}")
    except Exception as e:
        logger.debug("[sms] background-job status unavailable: %s", e)
    try:
        from src import agent_runs
        for sid in agent_runs.active_sessions():
            if sid in mine:
                s = session_manager.get_session(sid)
                lines.append(f"Replying: {(getattr(s, 'name', '') or sid)[:40]}")
    except Exception as e:
        logger.debug("[sms] chat status unavailable: %s", e)
    return "\n".join(lines) if lines else "Nothing is running."


def _is_admin(request: Request, owner: Optional[str]) -> bool:
    """Admins see every endpoint in the model list, as in the web picker."""
    try:
        auth_mgr = getattr(request.app.state, "auth_manager", None)
        return bool(owner and auth_mgr is not None and auth_mgr.is_admin(owner))
    except Exception:
        return False


# ── Replies ────────────────────────────────────────────────────────────────

def reply_body(to: str, text: str) -> Dict:
    """What the phone's send endpoint gets: the body SMS Gateway for Android's
    local server takes on POST /message (docs.sms-gate.app, local server)."""
    return {"textMessage": {"text": text}, "phoneNumbers": [to]}


async def _post_reply(url: str, payload: Dict) -> int:
    """POST to the reply URL. Credentials in the URL (http://user:pass@host)
    become basic auth, which is what the phone's local server wants."""
    import httpx
    parts = urlsplit(url)
    auth = None
    if parts.username is not None:
        from urllib.parse import unquote
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
        r = await webpush.send("Odysseus (SMS)", text, endpoint=s["endpoint"],
                               url="/", tag="odysseus-sms")
        sent += int(r.get("sent") or 0)
    return {"sent": sent, "subscriptions": len(subs)}


async def deliver(owner: Optional[str], cfg: Dict, to: str, text: str) -> str:
    """Send `text` back to the phone. Returns which channel took it."""
    url = str(cfg.get("reply_url") or "").strip()
    if url:
        try:
            code = await _post_reply(url, reply_body(to, text))
            logger.info("[sms] reply to %s via reply URL: HTTP %s", to, code)
            if 200 <= code < 300:
                return "reply_url"
        except Exception as e:
            logger.warning("[sms] reply to %s via reply URL failed: %s", to, type(e).__name__)
        return "failed"
    try:
        r = await _push_owner(owner, text)
        logger.info("[sms] reply to %s via web push: %s sent", to, r["sent"])
        return "push" if r["sent"] else "failed"
    except Exception as e:
        logger.warning("[sms] web push reply failed: %s", type(e).__name__)
        return "failed"


async def deliver_parts(owner: Optional[str], cfg: Dict, to: str, parts: List[str]) -> None:
    """Send a split reply. Texts go one at a time, each after the last was
    taken, so they arrive in order. A web push is one notification (they
    share a tag, so a second would replace the first)."""
    if not str(cfg.get("reply_url") or "").strip():
        await deliver(owner, cfg, to, "\n\n".join(parts))
        return
    for part in parts:
        await deliver(owner, cfg, to, part)


def _background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


async def wait_pending(timeout: float = 10.0) -> None:
    """Let queued replies finish (tests, and shutdown if wanted)."""
    if _PENDING:
        await asyncio.wait(list(_PENDING), timeout=timeout)


# ── Access-log redaction ───────────────────────────────────────────────────

_SECRET_IN_PATH = re.compile(r"(/api/sms/inbound/)[^/?\s\"]+")


class _RedactInboundSecret(logging.Filter):
    """uvicorn logs every request path; this one carries the secret."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(_SECRET_IN_PATH.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                                    for a in record.args)
            if isinstance(record.msg, str) and INBOUND_PREFIX in record.msg:
                record.msg = _SECRET_IN_PATH.sub(r"\1<redacted>", record.msg)
        except Exception:
            pass
        return True


def _install_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactInboundSecret) for f in access.filters):
        access.addFilter(_RedactInboundSecret())


# ── Routes ─────────────────────────────────────────────────────────────────

def _not_found() -> JSONResponse:
    # The body FastAPI gives a path that does not exist.
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _public(cfg: Dict) -> Dict:
    return {
        "numbers": list(cfg.get("numbers") or []),
        "reply_url": str(cfg.get("reply_url") or ""),
        "has_secret": bool(cfg.get("secret_hash")),
        "secret_created": cfg.get("secret_created"),
        "inbound_path": INBOUND_PREFIX,
    }


def _link_base(request: Request) -> str:
    """The address the phone's forwarder reached us on (Tailscale Serve's
    https name), for links in replies. Empty for loopback, which a phone
    cannot open."""
    from routes.enroll_routes import base_url
    try:
        base = base_url(request)
    except Exception:
        return ""
    host = (urlsplit(base).hostname or "").lower()
    if not host or host in ("localhost", "::1") or host.startswith("127."):
        return ""
    if host.endswith(".ts.net") and base.startswith("http://"):
        # Serve may not say X-Forwarded-Proto; its ts.net names are https.
        base = "https://" + base[len("http://"):]
    return base


def setup_sms_routes(session_manager) -> APIRouter:
    router = APIRouter(tags=["sms"])
    _install_log_redaction()

    def _send(owner: Optional[str], cfg: Dict, sender: str, reply: str) -> Dict:
        parts = split_reply(reply)
        _background(deliver_parts(owner, cfg, sender, parts))
        return {"ok": True, "reply": "\n".join(parts)}

    async def _turn(owner: Optional[str], cfg: Dict, sender: str, sid: str, sess, message: str) -> Dict:
        """One model turn in `sid`, for say and for the current conversation:
        answered inline if it is quick, else acknowledged now and sent later."""
        try:
            from src import agent_runs
            busy = agent_runs.is_active(sid)
        except Exception:
            busy = False
        if busy:
            return _send(owner, cfg, sender, "That chat is busy with a reply right now. Try again in a minute.")
        logger.info("[sms] turn in %s for %s", sid, owner or "-")
        turn = _background(_say(sid, message, owner))
        try:
            answer = await asyncio.wait_for(asyncio.shield(turn), SAY_INLINE_WAIT)
        except asyncio.TimeoutError:
            async def _later():
                await deliver_parts(owner, cfg, sender, split_reply(await turn))
            _background(_later())
            name = (getattr(sess, "name", "") or "the chat")[:40]
            return {"ok": True, "reply": f"Sent to {name}. The answer will follow when it is ready."}
        return _send(owner, cfg, sender, answer)

    def _new_chat(owner: Optional[str], sender: str, is_admin: bool, arg: str) -> str:
        """`new [model]`: a chat on that model, or on the default model the
        web UI's new chat starts on."""
        from routes.session_routes import create_direct_chat
        scope = _session_owner_scope(owner)
        if arg:
            m = resolve_model(owner, is_admin, arg)
            if not m:
                return _no_such_model(arg)
            model, endpoint_id = m["model"], m["endpoint_id"]
        else:
            from routes.model_routes import resolve_default_chat
            dc = resolve_default_chat(owner or "", is_admin)
            model, endpoint_id = dc.get("model") or "", dc.get("endpoint_id") or ""
            if not (model and endpoint_id):
                return "No default model is set. Send models, then new <n>."
        name = f"{model.split('/')[-1]} {datetime.now().strftime('%H:%M')} (SMS)"
        try:
            sid, sess = create_direct_chat(session_manager, scope, model, endpoint_id, name=name)
        except HTTPException:
            return "That model's endpoint is not available any more. Send models to pick another."
        set_conversation(owner, sender, sid)
        logger.info("[sms] new chat %s for %s", sid, owner or "-")
        return f"New chat with {model.split('/')[-1]}. Text anything to talk to it, end to stop."

    def _switch_model(owner: Optional[str], sender: str, is_admin: bool, arg: str) -> str:
        current = _current(session_manager, owner, sender)
        if not current:
            return HINT
        sid, sess = current
        if not arg:
            return f"This chat uses {getattr(sess, 'model', '') or 'no model'}. Send models to see others."
        m = resolve_model(owner, is_admin, arg)
        if not m:
            return _no_such_model(arg)
        from routes.session_routes import switch_session_model
        try:
            switch_session_model(sess, sid, m["model"], m["url"], m["endpoint_id"], _session_owner_scope(owner))
        except HTTPException:
            return "That model's endpoint is not available any more. Send models to pick another."
        logger.info("[sms] chat %s switched model for %s", sid, owner or "-")
        return f"{(getattr(sess, 'name', '') or 'This chat')[:40]} now uses {m['name']}."

    async def _handle(owner: Optional[str], cfg: Dict, sender: str, text: str, is_admin: bool = False,
                      base: str = "") -> Dict:
        words = text.strip().split(None, 1)
        cmd = words[0].lower() if words else ""
        rest = words[1].strip() if len(words) > 1 else ""
        # A bare word is a command only on its own ("stop" ends the
        # conversation, "stop it from crashing" is a turn in it); say, new and
        # model always are.
        bare = not rest
        if cmd in ("help", "?", "commands") and bare:
            current = _current(session_manager, owner, sender)
            reply = HELP + (f"\nNow talking to: {_chat_label(current[1])}" if current else "")
        elif cmd == "list" and bare:
            reply = cmd_list(session_manager, owner)
        elif cmd == "status" and bare:
            current = _current(session_manager, owner, sender)
            reply = cmd_status(session_manager, owner)
            if current:
                reply += f"\nTalking to: {_chat_label(current[1])}"
        elif cmd == "say":
            m = re.match(r"^(\d+)\s+(.+)$", rest, re.S)
            if not m:
                reply = "Usage: say <n> <text>. Send list to see the numbers."
            else:
                found = _resolve_n(session_manager, owner, int(m.group(1)))
                if not found:
                    reply = f"There is no chat {m.group(1)} in your list. Send list to see the numbers."
                else:
                    return await _turn(owner, cfg, sender, found[0], found[1], m.group(2).strip())
        elif cmd == "chat" and re.fullmatch(r"\d+", rest):
            found = _resolve_n(session_manager, owner, int(rest))
            if not found:
                reply = f"There is no chat {rest} in your list. Send list to see the numbers."
            else:
                set_conversation(owner, sender, found[0])
                reply = f"Now talking to {_chat_label(found[1])}. Text anything to send it, end to stop."
        elif cmd == "new":
            reply = _new_chat(owner, sender, is_admin, rest)
        elif cmd in ("end", "stop") and bare:
            if get_conversation(owner, sender):
                set_conversation(owner, sender, None)
                reply = "Conversation ended. Text new or chat <n> to start another."
            else:
                reply = "You are not talking to a chat."
        elif cmd in ("call", "/call") and (bare or rest.lower() == "me"):
            # Nothing can ring the phone from here, so the reply is a link that
            # opens the call in the browser (callHandoff.js reads ?call=).
            current = _current(session_manager, owner, sender)
            if not base:
                reply = "Open Odysseus on your phone and type /call in the chat."
            elif current:
                sid = current[0]
                reply = (f"Tap to start a voice call with {_chat_label(current[1])}:\n"
                         f"{base}/?call={quote(sid)}#{quote(sid)}")
            else:
                reply = f"Tap to start a voice call in a new chat:\n{base}/?call=new"
        elif cmd == "models" and bare:
            current = _current(session_manager, owner, sender)
            reply = cmd_models(owner, is_admin, getattr(current[1], "model", "") if current else "")
        elif cmd == "model":
            reply = _switch_model(owner, sender, is_admin, rest)
        else:
            current = _current(session_manager, owner, sender)
            if not current or not text.strip():
                reply = HINT
            else:
                return await _turn(owner, cfg, sender, current[0], current[1], text.strip())
        return _send(owner, cfg, sender, reply)

    @router.post("/api/sms/inbound/{secret}")
    async def inbound(secret: str, request: Request):
        body = await _read_body(request)
        raw_from, text, event = parse_inbound(body)
        sender = normalize_number(raw_from)
        match = match_secret(secret)
        if not match:
            logger.info("[sms] inbound from %s: rejected", sender or "?")
            return _not_found()
        owner, cfg = match
        allowed = {normalize_number(n) for n in (cfg.get("numbers") or [])}
        allowed.discard("")
        if not sender or sender not in allowed:
            logger.info("[sms] inbound from %s: rejected", sender or "?")
            return _not_found()
        if event and event != "sms:received":
            # The phone app reports sent/delivered texts to the same webhook.
            return {"ok": True, "reply": ""}
        text = (text or "")[:MAX_TEXT]
        logger.info("[sms] inbound from %s for %s: accepted (%d chars)", sender or "?", owner or "-", len(text))
        logger.debug("[sms] inbound text: %r", text[:200])
        try:
            return await _handle(owner, cfg, sender, text, _is_admin(request, owner), _link_base(request))
        except Exception as e:
            logger.exception("[sms] command failed: %s", type(e).__name__)
            return {"ok": False, "reply": "Something went wrong on the server. Try again."}

    @router.get("/api/sms/config")
    async def read_config(request: Request):
        return _public(get_config(require_user(request) or None))

    @router.put("/api/sms/config")
    async def write_config(request: Request):
        user = require_user(request) or None
        body = await request.json()
        numbers = []
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

    @router.post("/api/sms/secret")
    async def new_secret(request: Request):
        user = require_user(request) or None
        secret = secrets.token_urlsafe(32)
        cfg = get_config(user)
        cfg.update({"secret_hash": _hash(secret), "secret_created": time.time()})
        save_config(user, cfg)
        logger.info("[sms] new secret for %s", user or "-")
        return {**_public(cfg), "secret": secret}

    @router.delete("/api/sms/secret")
    async def drop_secret(request: Request):
        user = require_user(request) or None
        cfg = get_config(user)
        cfg.pop("secret_hash", None)
        cfg.pop("secret_created", None)
        save_config(user, cfg)
        return _public(cfg)

    @router.post("/api/sms/test")
    async def test_reply(request: Request):
        user = require_user(request) or None
        cfg = get_config(user)
        numbers = cfg.get("numbers") or []
        if not numbers:
            raise HTTPException(400, "Add your number first.")
        # A reply URL sends a distinct outbound text per phone, so test each
        # one; a web push just reaches the owner's devices regardless of
        # "to", so testing more than one number would only duplicate it.
        to_test = numbers if str(cfg.get("reply_url") or "").strip() else numbers[:1]
        results = []
        for to in to_test:
            via = await deliver(user, cfg, to, "Odysseus: the SMS gateway can reach you.")
            results.append({"to": to, "ok": via != "failed", "via": via})
        return {"ok": all(r["ok"] for r in results), "results": results}

    return router
