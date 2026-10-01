"""Moving a voice call between the user's open Odysseus pages.

Asked for 2026-10-01: "just like the music player allow to go between the
different devices, ie: change over to my phone cause im heading out and
leaving the computer".

Two in-memory tables, both per owner and gone on a restart (a call is a
live thing; nothing here is worth keeping):

  presence   every open Odysseus page beats every few seconds (callHandoff.js):
             which browser it is, whether it is on screen, its push
             subscription, and the call it has running, if any.
  offers     "continue this call there": pending until the target taps
             Continue (accepted), then connected once its mic is live, or
             failed; declined, cancelled or expired otherwise. The source
             watches its offer and only hangs up on connected.

A target that is not open (a phone in a pocket) is reached through its web
push subscription (src/webpush.py), the one the page reported or any of the
owner's subscriptions, and the notification opens the page at the offer.
"""

import secrets
import time
from typing import Dict, List, Optional

LIVE_S = 15.0           # a page that beat this recently is open
FORGET_S = 180.0        # and one silent this long is dropped
OFFER_TTL_S = 45.0      # time to tap Continue on the other device
CONNECT_TTL_S = 25.0    # then time for its mic and engines to come up
KEEP_DONE_S = 120.0     # finished offers stay readable this long

_presence: Dict[str, Dict[str, Dict]] = {}     # owner -> client_id -> record
_offers: Dict[str, Dict] = {}                  # offer id -> offer


def _now() -> float:
    return time.time()


def _prune(now: Optional[float] = None) -> None:
    now = now or _now()
    for owner in list(_presence):
        clients = _presence[owner]
        for cid in [c for c, r in clients.items() if now - r["last"] > FORGET_S]:
            del clients[cid]
        if not clients:
            del _presence[owner]
    for oid in list(_offers):
        o = _offers[oid]
        _expire(o, now)
        if o["status"] not in ("pending", "accepted") and now - o["updated"] > KEEP_DONE_S:
            del _offers[oid]


def _expire(o: Dict, now: float) -> None:
    if o["status"] == "pending" and now > o["expires"]:
        _set(o, "expired", now)
    elif o["status"] == "accepted" and now - o["updated"] > CONNECT_TTL_S:
        _set(o, "failed", now, reason="The other device did not start the call in time.")


def _set(o: Dict, status: str, now: Optional[float] = None, **extra) -> None:
    o["status"] = status
    o["updated"] = now or _now()
    o.update(extra)


# ── Presence ────────────────────────────────────────────────────────────────

def beat(owner: str, client_id: str, *, browser_id: str = "", name: str = "",
         kind: str = "desktop", visible: bool = False, push_endpoint: str = "",
         stt_ok: Optional[bool] = None, call: Optional[Dict] = None) -> Dict:
    """Record one page's heartbeat. Returns its record."""
    now = _now()
    _prune(now)
    rec = {
        "client_id": client_id, "browser_id": browser_id or client_id,
        "name": name or "A browser", "kind": "phone" if kind == "phone" else "desktop",
        "visible": bool(visible), "push_endpoint": push_endpoint or "",
        "stt_ok": stt_ok, "call": call if isinstance(call, dict) and call.get("session_id") else None,
        "last": now,
    }
    _presence.setdefault(owner, {})[client_id] = rec
    return rec


def leave(owner: str, client_id: str) -> None:
    _presence.get(owner, {}).pop(client_id, None)


def client(owner: str, client_id: str) -> Optional[Dict]:
    rec = _presence.get(owner, {}).get(client_id)
    if rec and _now() - rec["last"] <= LIVE_S:
        return rec
    return None


def live_clients(owner: str) -> List[Dict]:
    now = _now()
    return [r for r in _presence.get(owner, {}).values() if now - r["last"] <= LIVE_S]


def targets(owner: str, me: str, subscriptions: List[Dict]) -> List[Dict]:
    """Where the call on page `me` can go: the owner's other open browsers
    (one entry per browser, its most recently seen tab), then push-only
    devices, phones first. `subscriptions` are the owner's web push records,
    each with a display name already worked out ("name", "kind")."""
    mine = _presence.get(owner, {}).get(me) or {}
    my_browser = mine.get("browser_id") or me
    my_push = mine.get("push_endpoint") or ""
    best: Dict[str, Dict] = {}
    for r in live_clients(owner):
        if r["client_id"] == me or r["browser_id"] == my_browser:
            continue
        cur = best.get(r["browser_id"])
        if cur is None or (r["visible"], r["last"]) > (cur["visible"], cur["last"]):
            best[r["browser_id"]] = r
    out = [{
        "id": r["client_id"], "name": r["name"], "kind": r["kind"], "live": True,
        "visible": r["visible"], "stt_ok": r["stt_ok"], "push": bool(r["push_endpoint"]),
        "busy": bool(r["call"]),
    } for r in best.values()]
    reached = {r["push_endpoint"] for r in best.values() if r["push_endpoint"]}
    for s in subscriptions:
        ep = s.get("endpoint") or ""
        if not ep or ep in reached or ep == my_push:
            continue
        reached.add(ep)
        out.append({"id": "push:" + push_key(ep), "name": s.get("name") or "A browser",
                    "kind": s.get("kind") or "desktop", "live": False, "visible": False,
                    "stt_ok": None, "push": True, "busy": False})
    out.sort(key=lambda t: (t["kind"] != "phone", not t["live"], t["name"].lower()))
    return out


def push_key(endpoint: str) -> str:
    import hashlib
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


# ── Offers ──────────────────────────────────────────────────────────────────

def create_offer(owner: str, *, from_client: str, from_name: str, call: Dict,
                 to_client: str = "", to_browser: str = "", to_push: str = "",
                 to_name: str = "", status: str = "pending") -> Dict:
    now = _now()
    _prune(now)
    # One live offer per call: a new one replaces what this page offered before.
    for o in _offers.values():
        if o["owner"] == owner and o["from_client"] == from_client and o["status"] == "pending":
            _set(o, "cancelled", now)
    oid = secrets.token_urlsafe(9)
    o = {
        "id": oid, "owner": owner, "status": status, "created": now, "updated": now,
        "expires": now + OFFER_TTL_S,
        "from_client": from_client, "from_name": from_name,
        "to_client": to_client, "to_browser": to_browser, "to_push": to_push, "to_name": to_name,
        "accepted_by": "", "reason": "",
        "session_id": str(call.get("session_id") or ""),
        "chat_name": str(call.get("chat_name") or "")[:200],
        "model": str(call.get("model") or "")[:200],
        "prefs": call.get("prefs") if isinstance(call.get("prefs"), dict) else {},
    }
    _offers[oid] = o
    return o


def get_offer(owner: str, oid: str) -> Optional[Dict]:
    o = _offers.get(oid)
    if not o or o["owner"] != owner:
        return None
    _expire(o, _now())
    return o


def offers_for(owner: str, client_id: str) -> List[Dict]:
    """Pending offers this page should show: sent to it, to its browser (a
    tab reopened from the notification is a new page), or to its push
    subscription."""
    rec = _presence.get(owner, {}).get(client_id) or {}
    browser = rec.get("browser_id") or ""
    push = rec.get("push_endpoint") or ""
    now = _now()
    out = []
    for o in _offers.values():
        if o["owner"] != owner:
            continue
        _expire(o, now)
        if o["status"] != "pending" or o["from_client"] == client_id:
            continue
        if (o["to_client"] == client_id or (browser and o["to_browser"] == browser)
                or (push and o["to_push"] == push)):
            out.append(o)
    return out


def outgoing(owner: str, client_id: str) -> List[Dict]:
    """This page's own offers, newest first, so it can follow them."""
    now = _now()
    out = []
    for o in _offers.values():
        if o["owner"] == owner and o["from_client"] == client_id:
            _expire(o, now)
            out.append(o)
    out.sort(key=lambda o: o["created"], reverse=True)
    return out[:3]


class OfferError(Exception):
    """The offer cannot move that way (taken, expired, not yours)."""


def answer(owner: str, oid: str, verb: str, client_id: str = "", reason: str = "") -> Dict:
    """accept / decline / cancel / connected / failed."""
    o = get_offer(owner, oid)
    if o is None:
        raise KeyError(oid)
    st = o["status"]
    if verb == "accept":
        if st != "pending":
            raise OfferError("This call was already answered, cancelled or timed out.")
        _set(o, "accepted", accepted_by=client_id)
    elif verb == "decline":
        if st != "pending":
            raise OfferError("This call is no longer waiting.")
        _set(o, "declined")
    elif verb == "cancel":
        if st not in ("pending", "accepted"):
            raise OfferError("This call is no longer waiting.")
        _set(o, "cancelled")
    elif verb in ("connected", "failed"):
        if st != "accepted" or (o["accepted_by"] and client_id and o["accepted_by"] != client_id):
            raise OfferError("This device did not take the call.")
        _set(o, verb, reason=(reason or "")[:300])
    else:
        raise OfferError("Unknown answer.")
    return o


def public(o: Dict) -> Dict:
    """An offer as pages see it."""
    left = max(0, int(o["expires"] - _now())) if o["status"] == "pending" else 0
    return {k: o[k] for k in ("id", "status", "from_client", "from_name", "to_name", "session_id",
                              "chat_name", "model", "prefs", "reason")} | {"expires_in": left}


def reset() -> None:
    """Tests."""
    _presence.clear()
    _offers.clear()
