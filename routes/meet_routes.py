"""Google Meet: the voice agent joins a meeting (src/meet/).

Asked for: "could we also do google meets? I have unlimited 4k unlimited
time on meets." docs/google-meet.md has the research (what each path needs,
what a Google One or Workspace plan unlocks) and the setup.

Owner side, logged in (Settings > Devices > Google Meet, and /meet):
    GET  /api/meet/config              settings, readiness, meetings
    PUT  /api/meet/config              save them
    GET  /api/meet/upcoming            calendar events with a Meet link, soon
    POST /api/meet/join                join one: {url, mode, via, title, dial_in, pin}
    GET  /api/meet/meetings            the meetings it is in (and just left)
    POST /api/meet/meetings/{id}/leave leave now

Joining by phone goes through the phone call line's Twilio webhook
(POST /api/telephony/twilio/meet in routes/telephony_routes.py).
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user
from src.meet import config as meet_config, links, session as meet_session

logger = logging.getLogger(__name__)


def _is_admin(request: Request, owner: Optional[str]) -> bool:
    from routes.telephony_routes import _is_admin as tel_is_admin
    return tel_is_admin(request.app, owner)


def _readiness(user: Optional[str], cfg: Dict) -> Dict:
    out: Dict = {}
    try:
        from src.telephony import call as call_mod
        out["engines"] = call_mod.engines_ready()
    except Exception:
        out["engines"] = ["The speech engines could not be checked."]
    try:
        from src import cloud_browser
        out["browser"] = bool(cloud_browser.enabled() and cloud_browser.chromium_path())
    except Exception:
        out["browser"] = False
    try:
        from src.meet import dialin
        out["phone"] = [p for p in dialin.ready(user) if p not in out["engines"]]
    except Exception:
        out["phone"] = ["Phone calls are not set up."]
    return out


def _view(request: Request, user: Optional[str], cfg: Dict) -> Dict:
    out = meet_config.view(cfg)
    out["owner_name"] = str(cfg.get("owner_name") or "")
    out["ready"] = _readiness(user, cfg)
    out["meetings"] = [m.public() for m in meet_session.for_owner(user)]
    try:
        from routes.sms_routes import available_models
        out["models"] = [{"model": m["model"], "name": m["name"], "endpoint_id": m["endpoint_id"],
                          "endpoint_name": m["endpoint_name"]}
                         for m in available_models(user, _is_admin(request, user))]
    except Exception:
        out["models"] = []
    return out


def _upcoming(owner: str, hours: int) -> List[Dict]:
    """Calendar events from an hour ago to `hours` ahead that have a Meet link."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from routes.calendar_routes import _expand_rrule
    from sqlalchemy import and_, or_
    start = datetime.utcnow() - timedelta(hours=1)
    end = datetime.utcnow() + timedelta(hours=hours)
    db = SessionLocal()
    try:
        q = db.query(CalendarEvent).join(CalendarCal).filter(
            CalendarEvent.status != "cancelled",
            CalendarCal.owner == owner,
            or_(and_(or_(CalendarEvent.rrule == "", CalendarEvent.rrule.is_(None)),
                     CalendarEvent.dtstart < end, CalendarEvent.dtend > start),
                and_(CalendarEvent.rrule.isnot(None), CalendarEvent.rrule != "", CalendarEvent.dtstart < end)))
        out = []
        for ev in q.order_by(CalendarEvent.dtstart).limit(500).all():
            for occ in _expand_rrule(ev, start, end):
                m = links.meeting_from_event(occ)
                if m:
                    out.append(m)
        out.sort(key=lambda m: m["start"])
        return out[:20]
    finally:
        db.close()


def setup_meet_routes() -> APIRouter:
    router = APIRouter(prefix="/api/meet", tags=["meet"])

    @router.get("/config")
    async def read_config(request: Request):
        user = require_user(request) or None
        return _view(request, user, meet_config.get_config(user))

    @router.put("/config")
    async def write_config(request: Request):
        user = require_user(request) or None
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object.")
        cfg = meet_config.get_config(user)
        if "enabled" in body:
            cfg["enabled"] = bool(body["enabled"])
        if "display_name" in body:
            name = " ".join(str(body["display_name"] or "").split())[: meet_config.MAX_NAME]
            if name and "ai" not in name.lower().replace("(", " ").replace(")", " ").split():
                raise HTTPException(400, "Keep \"AI\" in the display name, like \"Odysseus (AI)\", "
                                         "so people in the meeting can tell.")
            cfg["display_name"] = name
        if "owner_name" in body:
            cfg["owner_name"] = " ".join(str(body["owner_name"] or "").split())[:60]
        for key, allowed in (("mode", meet_config.MODES), ("join_as", meet_config.JOIN_AS),
                             ("via", meet_config.VIAS)):
            if key in body:
                if body[key] not in allowed:
                    raise HTTPException(400, f"Unknown {key.replace('_', ' ')}.")
                cfg[key] = body[key]
        if "wake_words" in body:
            try:
                cfg["wake_words"] = meet_config.clean_wake_words(body["wake_words"])
            except ValueError as e:
                raise HTTPException(400, str(e))
        for key in ("announce", "chat_notice", "summary"):
            if key in body:
                cfg[key] = bool(body[key])
        if "announcement" in body:
            cfg["announcement"] = str(body["announcement"] or "").strip()[: meet_config.MAX_ANNOUNCE]
        for key in ("max_minutes", "idle_minutes", "lobby_minutes", "dial_wait"):
            if key in body:
                try:
                    cfg[key] = int(body[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key.replace('_', ' ').capitalize()} is a number.")
        if "model" in body or "endpoint_id" in body:
            cfg["model"] = str(body.get("model") or "")
            cfg["endpoint_id"] = str(body.get("endpoint_id") or "")
        # Store the clamped values.
        v = meet_config.view(cfg)
        for key in ("max_minutes", "idle_minutes", "lobby_minutes", "dial_wait"):
            if key in cfg:
                cfg[key] = v[key]
        meet_config.save_config(user, cfg)
        logger.info("[meet] settings saved for %s (enabled=%s)", user or "-", bool(cfg.get("enabled")))
        return _view(request, user, cfg)

    @router.get("/upcoming")
    async def upcoming(request: Request, hours: int = 24):
        from routes.calendar_routes import _require_user as cal_user
        owner = cal_user(request)
        try:
            return {"meetings": _upcoming(owner, max(1, min(24 * 7, int(hours))))}
        except Exception as e:
            logger.warning("[meet] upcoming meetings: %s", type(e).__name__)
            return {"meetings": []}

    @router.get("/meetings")
    async def meetings(request: Request):
        user = require_user(request) or None
        return {"meetings": [m.public() for m in meet_session.for_owner(user)]}

    @router.post("/meetings/{meeting_id}/leave")
    async def leave(request: Request, meeting_id: str):
        user = require_user(request) or None
        m = meet_session.get(meeting_id)
        if not m or m.owner != user:
            raise HTTPException(404, "No such meeting.")
        await m.leave("left from Odysseus")
        return {"ok": True}

    @router.post("/join")
    async def join(request: Request):
        user = require_user(request) or None
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object.")
        raw_cfg = meet_config.get_config(user)
        cfg = meet_config.view(raw_cfg)
        if not cfg["enabled"]:
            raise HTTPException(400, "Turn Google Meet on in Settings > Devices first.")
        mode = body.get("mode") or cfg["mode"]
        via = body.get("via") or cfg["via"]
        if mode not in meet_config.MODES or via not in meet_config.VIAS:
            raise HTTPException(400, "Unknown mode or way to join.")
        url = links.meet_url(str(body.get("url") or ""))
        dial_in = None
        if via == "browser":
            if not url:
                raise HTTPException(400, "That is not a Google Meet link (https://meet.google.com/abc-defg-hij).")
        else:
            number = links.phone_number(str(body.get("dial_in") or ""))
            pin = links.clean_pin(str(body.get("pin") or ""))
            if not (number and pin):
                raise HTTPException(400, "Joining by phone needs the meeting's US dial-in number and PIN "
                                         "(in the invite under \"Join by phone\").")
            dial_in = {"number": number, "pin": pin}
        why = meet_session.can_start(user)
        if why:
            raise HTTPException(409, why)
        ready = _readiness(user, raw_cfg)
        if ready["engines"]:
            raise HTTPException(400, " ".join(ready["engines"]))
        if via == "browser" and not ready["browser"]:
            raise HTTPException(400, "The cloud browser is not available on this server (no Chromium, or it is "
                                     "switched off), so it cannot join through a browser.")
        if via == "phone" and ready["phone"]:
            raise HTTPException(400, " ".join(ready["phone"]))
        title = " ".join(str(body.get("title") or "").split())[:100]
        m = meet_session.Meeting(user, raw_cfg, url, mode=mode, via=via, title=title, dial_in=dial_in)
        try:
            await m.start(_is_admin(request, user))
        except Exception as e:
            raise HTTPException(400, str(e)[:200])
        logger.info("[meet] %s joining %s (%s, %s) for %s", m.id, url or "by phone", mode, via, user or "-")
        return m.public()

    return router
