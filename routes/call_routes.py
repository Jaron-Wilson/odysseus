"""Moving a voice call to another device: /api/call/*.

The pages beat (presence), list where the call can go (targets), offer it
there, and follow the offer; the other page shows "Continue call here" and
answers. State lives in src/call_handoff.py. Browser sessions only, each
owner sees only their own pages, offers and push subscriptions.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src import call_handoff as ch
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

_LABEL_TTL_S = 60.0
_labels: Dict[str, Any] = {}          # ip -> (at, name, kind)
_PHONE_WORDS = ("phone", "pixel", "android", "iphone", "galaxy", "mobile")
_tasks: set = set()                    # pushes in flight (kept from the GC)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _verify_session(request: Request, session_id: str) -> None:
    from routes.session_routes import _verify_session_owner
    _verify_session_owner(request, session_id)


def _tailnet_name(ip: str):
    """(name, kind) of the machine at `ip` from the tailnet and Settings >
    Devices, or (None, None). Cached: pages beat every few seconds."""
    hit = _labels.get(ip)
    if hit and time.time() - hit[0] < _LABEL_TTL_S:
        return hit[1], hit[2]
    name = kind = None
    try:
        from src import machines
        info = machines.client_device(ip)
        if info:
            peer, dev = info["peer"], info.get("device")
            name = (dev or {}).get("name") or peer.get("name")
            os_name = (peer.get("os") or "").lower()
            kind = "phone" if os_name in ("android", "ios", "ipados") or (dev or {}).get("kind") == "phone" else "desktop"
    except Exception as e:
        logger.debug("call presence: no tailnet name for %s: %s", ip, e)
    _labels[ip] = (time.time(), name, kind)
    return name, kind


def _owner_subscriptions(owner: str) -> List[Dict]:
    """The owner's web push subscriptions, named as Settings > Devices names them."""
    from src import webpush
    out = []
    try:
        from src import devices
    except Exception:
        devices = None
    for s in webpush.load_subscriptions():
        if (s.get("owner") or "") != (owner or ""):
            continue
        raw = (s.get("device") or "").strip()
        name = raw
        kind = None
        if devices and raw:
            try:
                linked = devices.owner_of_alias(raw)
                if linked:
                    name = linked
                    rec = devices.get(linked) or {}
                    kind = rec.get("kind")
            except Exception:
                pass
        if not kind:
            kind = "phone" if any(w in (name or "").lower() for w in _PHONE_WORDS) else "desktop"
        out.append({"endpoint": s.get("endpoint") or "", "name": name or "A browser",
                    "kind": "phone" if kind == "phone" else "desktop"})
    return out


async def _body(request: Request) -> Dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _cid(v: Any) -> str:
    s = str(v or "").strip()
    if not s or len(s) > 64 or not all(c.isalnum() or c in "-_" for c in s):
        raise HTTPException(400, "client_id is required")
    return s


def setup_call_routes() -> APIRouter:
    router = APIRouter(tags=["call"])

    @router.post("/api/call/presence")
    async def presence(request: Request) -> Dict[str, Any]:
        """A page's heartbeat. Answers with what it should act on: offers
        waiting for it, its own offers' progress, and calls running on the
        owner's other pages (to pick up here)."""
        owner = require_user(request)
        b = await _body(request)
        cid = _cid(b.get("client_id"))
        name, kind = await asyncio.to_thread(_tailnet_name, _client_ip(request))
        label = str(b.get("label") or "")[:80]
        call = b.get("call") if isinstance(b.get("call"), dict) else None
        rec = ch.beat(owner, cid, browser_id=str(b.get("browser_id") or "")[:64],
                      name=name or label, kind=kind or str(b.get("kind") or "desktop"),
                      visible=bool(b.get("visible")), push_endpoint=str(b.get("push_endpoint") or "")[:1000],
                      stt_ok=b.get("stt_ok") if isinstance(b.get("stt_ok"), bool) else None, call=call)
        calls = [{"client_id": r["client_id"], "name": r["name"], "kind": r["kind"],
                  "session_id": r["call"]["session_id"], "chat_name": str(r["call"].get("chat_name") or ""),
                  "model": str(r["call"].get("model") or ""),
                  "prefs": r["call"].get("prefs") if isinstance(r["call"].get("prefs"), dict) else {}}
                 for r in ch.live_clients(owner)
                 if r["call"] and r["browser_id"] != rec["browser_id"]]
        return {"name": rec["name"], "kind": rec["kind"],
                "offers": [ch.public(o) for o in ch.offers_for(owner, cid)],
                "outgoing": [ch.public(o) for o in ch.outgoing(owner, cid)],
                "calls": calls}

    @router.post("/api/call/presence/leave")
    async def presence_leave(request: Request) -> Dict[str, Any]:
        owner = require_user(request)
        b = await _body(request)
        ch.leave(owner, _cid(b.get("client_id")))
        return {"ok": True}

    @router.get("/api/call/targets")
    async def call_targets(request: Request, client_id: str) -> Dict[str, Any]:
        """Where this page's call can go: the owner's other open pages, then
        devices reachable by notification. Phones first."""
        owner = require_user(request)
        cid = _cid(client_id)
        return {"targets": ch.targets(owner, cid, _owner_subscriptions(owner))}

    @router.post("/api/call/offer")
    async def make_offer(request: Request) -> Dict[str, Any]:
        """Offer this page's call to `to` (a target id from /api/call/targets).
        A target not on screen also gets a notification."""
        owner = require_user(request)
        b = await _body(request)
        cid = _cid(b.get("client_id"))
        call = b.get("call") if isinstance(b.get("call"), dict) else {}
        sid = str(call.get("session_id") or "").strip()
        if not sid:
            raise HTTPException(400, "call.session_id is required")
        _verify_session(request, sid)
        to = str(b.get("to") or "").strip()
        me = ch.client(owner, cid) or {}
        from_name = me.get("name") or "your other device"
        push_ep = ""
        to_client = to_browser = to_name = ""
        visible = False
        if to.startswith("push:"):
            for s in _owner_subscriptions(owner):
                if ch.push_key(s["endpoint"]) == to[5:]:
                    push_ep, to_name = s["endpoint"], s["name"]
                    break
            if not push_ep:
                raise HTTPException(404, "That device is not reachable any more")
        else:
            rec = ch.client(owner, _cid(to))
            if not rec:
                raise HTTPException(404, "That page is not open any more")
            to_client, to_browser, to_name = rec["client_id"], rec["browser_id"], rec["name"]
            push_ep, visible = rec["push_endpoint"], rec["visible"]
            # Only the owner's own subscription is ever pushed to.
            if push_ep and push_ep not in {s["endpoint"] for s in _owner_subscriptions(owner)}:
                push_ep = ""
        o = ch.create_offer(owner, from_client=cid, from_name=from_name, call=call,
                            to_client=to_client, to_browser=to_browser, to_push=push_ep, to_name=to_name)
        pushed = False
        if push_ep and not visible:
            pushed = True
            # In the background: a push service can take seconds to answer.
            task = asyncio.create_task(_push_offer(o, push_ep))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
        return {**ch.public(o), "pushed": pushed}

    async def _push_offer(o: Dict, endpoint: str) -> None:
        from src import webpush
        who = o["model"] or "the agent"
        try:
            await webpush.send(f"Continue your call with {who}",
                               f"From {o['from_name']}" + (f" · {o['chat_name']}" if o["chat_name"] else "")
                               + ". Tap to take it here.",
                               endpoint=endpoint, url=f"/?call_offer={o['id']}", tag="odysseus-call")
        except Exception as e:
            logger.warning("call handoff push failed: %s", e)

    @router.get("/api/call/offer/{oid}")
    async def read_offer(request: Request, oid: str) -> Dict[str, Any]:
        owner = require_user(request)
        o = ch.get_offer(owner, oid)
        if not o:
            raise HTTPException(404, "No such call offer (it may have expired)")
        return ch.public(o)

    @router.post("/api/call/offer/{oid}/{verb}")
    async def answer_offer(request: Request, oid: str, verb: str) -> Dict[str, Any]:
        """accept / decline (the target), cancel (the source), connected /
        failed (the target, once its call is up or could not start)."""
        owner = require_user(request)
        b = await _body(request)
        if verb not in ("accept", "decline", "cancel", "connected", "failed"):
            raise HTTPException(400, "Unknown answer")
        try:
            o = ch.answer(owner, oid, verb, client_id=str(b.get("client_id") or "")[:64],
                          reason=str(b.get("reason") or ""))
        except KeyError:
            raise HTTPException(404, "No such call offer (it may have expired)")
        except ch.OfferError as e:
            raise HTTPException(409, str(e))
        return ch.public(o)

    @router.post("/api/call/pickup")
    async def pickup(request: Request) -> Dict[str, Any]:
        """Take the call running on another of the owner's pages: an offer
        that is accepted from the start. That page goes quiet now and hangs
        up when this one reports connected."""
        owner = require_user(request)
        b = await _body(request)
        cid = _cid(b.get("client_id"))
        src = ch.client(owner, _cid(b.get("from")))
        if not src or not src["call"]:
            raise HTTPException(404, "That call has ended")
        _verify_session(request, src["call"]["session_id"])
        me = ch.client(owner, cid) or {}
        o = ch.create_offer(owner, from_client=src["client_id"], from_name=src["name"], call=src["call"],
                            to_client=cid, to_browser=me.get("browser_id", ""), to_name=me.get("name", ""),
                            status="pending")
        ch.answer(owner, o["id"], "accept", client_id=cid)
        return ch.public(o)

    return router
