"""Queued messages and "tell me when it's done", kept on the server.

The composer queue used to live in the browser, so leaving the page lost it:
the reply carried on (agent_runs detaches it) but the next queued message was
never sent. Queues now live here, per chat, in the data dir, and every
finished run looks at its chat's queue:

- If a page has the chat open, it claims the next message within a second
  and sends it the normal way, with that page's toggles and attachments.
- If nothing claims it within CLAIM_GRACE_S, the server sends it itself: the
  message is added to the chat and the agent runs as a detached run, just like
  a resumed screen-control turn. Opening the chat later shows it, and an open
  chat attaches to it live.

The bell by the composer asks for a push notification when the chat has
nothing left to do: the reply and everything queued behind it. One
notification per batch, with the start of the answer and a link to the chat,
like deep research.

A run ended by Stop does neither. Stop clears the queue and the request.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from typing import Dict, List, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

QUEUE_FILE = os.path.join(DATA_DIR, "chat_queue.json")

# How long after a run finishes an open page has to claim the next message
# before the server sends it itself. The page claims at ~0.7 s.
CLAIM_GRACE_S = 4.0
# A page that claimed a message has this long to start sending it before the
# claim is treated as abandoned (tab closed mid-claim) and the server moves on.
CLAIM_HOLD_S = 20.0
MAX_ITEMS = 50
MAX_TEXT = 20000

_lock = threading.Lock()
_pending: Dict[str, asyncio.Task] = {}      # session_id -> after-run task
_claims: Dict[str, tuple] = {}              # session_id -> (when, item) a page claimed


# ── storage ──────────────────────────────────────────────────────────────

def _load() -> Dict[str, Dict]:
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: Dict[str, Dict]) -> None:
    # {} is a real notify target (all devices), so test for None, not falsiness.
    data = {k: v for k, v in data.items()
            if v.get("items") or v.get("notify") is not None}
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".chat_queue_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, QUEUE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _entry(data: Dict, session_id: str) -> Dict:
    return data.setdefault(session_id, {"items": [], "notify": None})


def _public(entry: Optional[Dict]) -> Dict:
    entry = entry or {}
    return {
        "items": [{"id": i["id"], "text": i["text"]} for i in entry.get("items") or []],
        "notify": entry.get("notify"),
    }


def get(session_id: str) -> Dict:
    with _lock:
        return _public(_load().get(session_id))


def add(session_id: str, text: str, *, client_device: Optional[Dict] = None,
        notify: Optional[Dict] = None, base: str = "") -> Dict:
    text = (text or "").strip()[:MAX_TEXT]
    if not session_id or not text:
        raise ValueError("a chat and some text are needed")
    with _lock:
        data = _load()
        e = _entry(data, session_id)
        if len(e["items"]) >= MAX_ITEMS:
            raise ValueError(f"the queue holds at most {MAX_ITEMS} messages")
        e["items"].append({"id": uuid.uuid4().hex[:12], "text": text,
                           "client_device": client_device, "added": time.time()})
        if notify is not None:
            e["notify"] = _with_base(clean_notify(notify), base)
        _save(data)
        return _public(e)


def remove(session_id: str, item_id: str) -> Dict:
    with _lock:
        data = _load()
        e = data.get(session_id)
        if e:
            e["items"] = [i for i in e["items"] if i["id"] != item_id]
            _save(data)
        return _public(data.get(session_id))


def clear(session_id: str) -> None:
    """Drop the queue and any notify request (Stop, or Clear)."""
    with _lock:
        data = _load()
        if data.pop(session_id, None) is not None:
            _save(data)


def claim(session_id: str) -> Optional[Dict]:
    """Take the next message for an open page to send. Atomic with the
    server's own drain, so a message is never sent twice."""
    with _lock:
        data = _load()
        e = data.get(session_id)
        if not e or not e["items"]:
            return None
        item = e["items"].pop(0)
        _claims[session_id] = (time.time(), item)
        _save(data)
        return {"id": item["id"], "text": item["text"]}


def clean_notify(notify) -> Optional[Dict]:
    """{"device": name} for one device (or its linked subscriptions),
    {"endpoint": ...} for "this browser", {} for all devices, None for off."""
    if notify is None or notify is False:
        return None
    if isinstance(notify, str):
        try:
            notify = json.loads(notify)
        except ValueError:
            notify = {"device": notify}
    if not isinstance(notify, dict) or notify.get("off"):
        return None
    out = {}
    if isinstance(notify.get("device"), str) and notify["device"].strip():
        out["device"] = notify["device"].strip()[:80]
    elif isinstance(notify.get("endpoint"), str) and notify["endpoint"].startswith("https://"):
        out["endpoint"] = notify["endpoint"][:1000]
    if isinstance(notify.get("label"), str):
        out["label"] = notify["label"].strip()[:80]
    return out


_BASE_RE = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]]+$")


def _with_base(n: Optional[Dict], base: str) -> Optional[Dict]:
    """Remember the address the request came in on (Tailscale Serve's https
    name), so a notification sent later, with no request, can link back."""
    if n is not None and base and _BASE_RE.match(base.rstrip("/")):
        n["base"] = base.rstrip("/")
    return n


def chat_link(session_id: str, notify: Optional[Dict]) -> str:
    """The chat's full address, for a phone to open, or "" when unknown."""
    base = (notify or {}).get("base") or os.environ.get("ODYSSEUS_PUBLIC_URL", "").rstrip("/")
    return f"{base}/#{session_id}" if base else ""


def set_notify(session_id: str, notify, *, base: str = "") -> None:
    n = _with_base(clean_notify(notify), base)
    with _lock:
        data = _load()
        if n is None:
            e = data.get(session_id)
            if not e or e.get("notify") is None:
                return
            e["notify"] = None
        else:
            _entry(data, session_id)["notify"] = n
        _save(data)


# ── after each run ───────────────────────────────────────────────────────

def on_run_started(session_id: str) -> None:
    """agent_runs calls this when a run starts: a claimed message is now sending."""
    _claims.pop(session_id, None)


def on_run_finished(session_id: str, status: str) -> None:
    """agent_runs calls this from its drain task when a run ends."""
    if status == "stopped":
        # Stopped by the user or replaced by a newer message. Stop clears the
        # queue itself; a replacing message has its own run to finish.
        return
    with _lock:
        if session_id not in _load():
            return              # nothing queued, no notify asked for
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    old = _pending.get(session_id)
    if old and not old.done():
        old.cancel()
    _pending[session_id] = loop.create_task(_after_run(session_id, status))


async def _after_run(session_id: str, status: str) -> None:
    from src import agent_runs

    await asyncio.sleep(CLAIM_GRACE_S)
    while True:
        if agent_runs.is_active(session_id):
            return              # a page sent the next one; its end calls us again
        claimed = _claims.get(session_id)
        held = time.time() - claimed[0] if claimed else CLAIM_HOLD_S
        if held < CLAIM_HOLD_S:
            # A page claimed a message but has not started it yet.
            await asyncio.sleep(min(CLAIM_HOLD_S - held, 5))
            continue
        _claims.pop(session_id, None)
        with _lock:
            data = _load()
            if claimed:
                # The page never sent what it claimed (closed mid-claim):
                # it goes back to the front rather than being lost.
                _entry(data, session_id)["items"].insert(0, claimed[1])
            e = data.get(session_id)
            if not e:
                return
            item = e["items"].pop(0) if e["items"] else None
            notify = None
            if item is None:
                notify, e["notify"] = e.get("notify"), None
            _save(data)
        break
    if item is not None:
        if not await run_headless(session_id, item["text"], item.get("client_device")):
            logger.warning("Queued message for %s could not be sent; left for the page", session_id)
            with _lock:
                data = _load()
                _entry(data, session_id)["items"].insert(0, item)
                _save(data)
        return
    if notify is not None:
        await send_done_notification(session_id, notify, failed=(status == "error"))


async def _prepare(sess, session_id: str, context: List[Dict]):
    """The setup an ordinary send does before calling the model: auth
    headers, a model name if the chat lost it, and the context sized to the
    model. Each step is best-effort; the run goes ahead without it."""
    owner = getattr(sess, "owner", None)
    try:
        from routes.chat_helpers import resolve_session_auth
        resolve_session_auth(sess, session_id, owner=owner)
    except Exception as e:
        logger.debug("queued send: auth setup skipped: %s", e)
    try:
        from routes.chat_routes import _recover_empty_session_model
        _recover_empty_session_model(sess, session_id, owner=owner)
    except Exception as e:
        logger.debug("queued send: model recovery skipped: %s", e)
    try:
        from routes.chat_helpers import _normalize_model_id_from_cache
        norm = _normalize_model_id_from_cache(sess)
        if norm:
            sess.model = norm
    except Exception:
        pass
    ctx_len = getattr(sess, "context_length", 0) or 0
    try:
        from src.context_compactor import maybe_compact, trim_for_context
        context, ctx_len, _ = await maybe_compact(
            sess, sess.endpoint_url, sess.model, context,
            getattr(sess, "headers", None), owner=owner)
        context = trim_for_context(context, ctx_len)
    except Exception as e:
        logger.debug("queued send: context sizing skipped: %s", e)
    return context, ctx_len


async def run_headless(session_id: str, text: str, client_device: Optional[Dict] = None) -> bool:
    """Send a queued message with no page open, as a detached run."""
    try:
        from src.ai_interaction import get_session_manager
        from src.screen_control_resume import _resume_stream
        from core.models import ChatMessage
        from src import agent_runs

        sm = get_session_manager()
        if not sm:
            return False
        try:
            sess = sm.get_session(session_id)
        except KeyError:
            return False
        if not sess or agent_runs.is_active(session_id):
            return False
        sm.add_message(session_id, ChatMessage("user", text, metadata={"source": "queued"}))
        sm.save_sessions()
        context = sess.get_context_messages()
        if not context or context[-1].get("content") != text:
            context.append({"role": "user", "content": text})
        context, ctx_len = await _prepare(sess, session_id, context)
        if agent_runs.is_active(session_id):
            return True         # a page got in first; the message is in the chat either way
        agent_runs.start(session_id, _resume_stream(
            sess, sm, context, source="queued", client_device=client_device,
            context_length=ctx_len))
        logger.info("Sent a queued message in %s with no page open", session_id)
        return True
    except Exception as e:
        logger.warning("Could not send queued message in %s: %s", session_id, e)
        return False


def _last_reply(session_id: str) -> str:
    try:
        from src.ai_interaction import get_session_manager
        sess = get_session_manager().get_session(session_id)
        msgs = getattr(sess, "history", None) or getattr(sess, "messages", None) or []
        for m in reversed(msgs):
            role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
            if role == "assistant":
                content = getattr(m, "content", None)
                if content is None and isinstance(m, dict):
                    content = m.get("content")
                return content if isinstance(content, str) else ""
    except Exception:
        pass
    return ""


def _session_title(session_id: str) -> str:
    try:
        from src.ai_interaction import get_session_manager
        sess = get_session_manager().get_session(session_id)
        return (getattr(sess, "name", "") or getattr(sess, "title", "") or "").strip()
    except Exception:
        return ""


def _preview(text: str, limit: int = 160) -> str:
    import re
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    # The agent's own "_Note: ... context budget ..._" line is not the answer.
    t = re.sub(r"(?m)^\s*_Note:.*?_\s*$", "", t)
    t = re.sub(r"[`*_#>\[\]]", "", t)
    t = " ".join(t.split())
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def _listener_targets(notify: Dict) -> List[Dict]:
    """Registered devices whose Modes listener should show the notification:
    the chosen device, or every device with a listener for "all devices".
    "This browser" is a browser subscription, so no listener."""
    if notify.get("endpoint"):
        return []
    try:
        from src import devices as _devices
        if notify.get("device"):
            # The menu can name a push subscription ("android-phone")
            # rather than the device it's linked to ("pixel-8a").
            d = _devices.resolve(notify["device"])
            if d is None:
                owner = _devices.owner_of_alias(notify["device"])
                d = _devices.get(owner) if owner else None
            found = [d] if d else []
        else:
            found = _devices.list_devices()
        return [d for d in found
                if (d.get("endpoint") or "").strip() and _devices.supports(d, "notify")]
    except Exception as e:
        logger.debug("listener lookup failed: %s", e)
        return []


async def send_done_notification(session_id: str, notify: Dict, *, failed: bool = False) -> Dict:
    """Push to the chosen browsers, and also show it through each chosen
    device's Modes listener. Seen live: the push was accepted for the phone
    but never shown with the site closed, while the listener was up."""
    from src import webpush

    title = _session_title(session_id)
    head = "Reply failed" if failed else "Reply ready"
    body = _preview(_last_reply(session_id)) or (
        "The run stopped with an error." if failed else "Your reply is ready.")
    heading = f"{head}: {title}" if title else head

    async def _push():
        try:
            return await webpush.send(
                heading, body,
                device=notify.get("device", ""), endpoint=notify.get("endpoint", ""),
                url=f"/#{session_id}", tag=f"odysseus-done-{session_id}")
        except Exception as e:
            logger.warning("Done push for %s failed: %s", session_id, e)
            return {"sent": 0, "failed": 1, "errors": [str(e)]}

    async def _listener(device):
        from src import devices as _devices
        # Modes 0.1.53+ shows the title; older builds show only the text.
        params = {"title": "Odysseus", "text": f"{heading}. {body}"}
        link = chat_link(session_id, notify)
        if link:
            params["url"] = link          # tapping it opens the chat (Modes 1.x+)
        r = await _devices.send_command(device, "notify", params)
        return device.get("name"), r

    targets = _listener_targets(notify)
    results = await asyncio.gather(_push(), *[_listener(d) for d in targets],
                                   return_exceptions=True)
    push = results[0] if isinstance(results[0], dict) else {"sent": 0, "failed": 1}
    listeners = {}
    for r in results[1:]:
        if isinstance(r, Exception):
            listeners["?"] = str(r)
            continue
        name, out = r
        listeners[name] = "shown" if out.get("ok") else out.get("error", "failed")
    result = {**push, "listeners": listeners}
    logger.info("Done notification for %s: %s", session_id, result)
    return result
