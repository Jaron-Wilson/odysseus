"""More of the mod API: what a mod needs to reach the agent the way a built-in
feature does. Bearer ODYSSEUS_MOD_TOKEN like the rest of /api/mods/v1
(src/mods/routes.py), each call behind its own permission.

    chats             the user's chats: list them, start one, switch its model,
                      send a message and stream the agent's answer, stop it,
                      add a note; the models the user may pick; what is
                      running for them
    speech            the server's speech engines (Settings > AI Defaults >
                      Voice call): WAV to text, text to WAV, whether they can
                      run without a browser
    browser           the cloud browser on the server: its CDP address and the
                      X11 display for real input
    google.calendar   make an event with a Google Meet in the user's own
                      Google Calendar; whether it is connected
    calendar.read     the user's calendar events that have a Google Meet link
    users             whether a user is an admin

Phone & Messages (Jaron-Wilson/odysseus-phone) is the first mod that uses
them: a phone call, a text conversation and a Google Meet are each a chat
with the agent, heard and spoken through the server's speech engines.

Every call names the user it is for (`user`, from the identity headers the
mod was given). With sign-in turned off there is one user and `local` (or
nothing) is fine; otherwise it must be a user who may use the mod. Unlike
notify, there is no fallback to the primary admin: a chat is someone's.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from src.mods import access, runtime

logger = logging.getLogger(__name__)

MAX_MESSAGE = 20_000
MAX_NOTE = 20_000
MAX_AUDIO = 25 * 1024 * 1024
MAX_SPEAK = 4000
TURN_WAIT_S = 600.0
STOP_WAIT_S = 10.0
LIST_MAX = 50
# Per mod: (calls, seconds). A phone call makes one turn every few seconds and
# one synthesis per sentence, so these are well above a busy call.
RATE_LIMITS = {"chats.turn": (60, 60), "speech": (600, 60), "google.calendar": (20, 60)}

# The permissions these need are described in src/mods/manifest.py (PERMISSIONS).


# ── who is asking ─────────────────────────────────────────────────────────

def _mod(request: Request, permission: Optional[str]) -> Dict[str, Any]:
    from src.mods import routes as mod_routes
    entry = mod_routes._mod(request, permission)
    if permission in RATE_LIMITS:
        _rate(entry["id"], permission)
    return entry


def _rate(mod_id: str, key: str) -> None:
    from src.mods import routes as mod_routes
    n, per = RATE_LIMITS[key]
    mod_routes.RATE_LIMITS.setdefault("host:" + key, (n, per))
    mod_routes._rate(mod_id, "host:" + key)


def _user(entry: Dict[str, Any], raw: Any) -> Tuple[Optional[str], str]:
    """(chat owner scope, user name) for the user a mod names. Sign-in off:
    one user, chats carry no owner. Otherwise a known user who may use the
    mod, or 403."""
    name = str(raw or "").strip()
    if access.auth_disabled():
        return None, name or "local"
    if not name or not access.known_user(name) or not access.may_use(entry, name):
        raise HTTPException(403, "Name a user who may use this mod (the X-Odysseus-User it was given)")
    return name, name


def _is_admin(owner: Optional[str]) -> bool:
    return access.is_admin_name(owner)


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Expected a JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a JSON object")
    return data


# ── chats ────────────────────────────────────────────────────────────────

def _sm():
    from src.ai_interaction import get_session_manager
    sm = get_session_manager()
    if sm is None:
        raise HTTPException(503, "Chats are not available yet")
    return sm


def _busy(sid: str) -> bool:
    try:
        from src import agent_runs
        return agent_runs.is_active(sid)
    except Exception:
        return False


def _stamp(ts) -> Optional[float]:
    if not ts:
        return None
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    except Exception:
        return None


def _chat_view(sid: str, sess, last=None) -> Dict[str, Any]:
    return {"id": sid, "name": getattr(sess, "name", "") or "Untitled", "model": getattr(sess, "model", "") or "",
            "last_activity": _stamp(last), "busy": _busy(sid), "archived": bool(getattr(sess, "archived", False))}


def recent_chats(scope: Optional[str]) -> List[Tuple[str, Any, Any]]:
    """(sid, session, last activity) for the owner's chats, newest first,
    archived ones left out."""
    sm = _sm()
    sessions = sm.get_sessions_for_user(scope)
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
        logger.debug("mod chats: session timestamps unavailable: %s", e)
    rows = [(sid, s, stamps.get(sid)) for sid, s in sessions.items()
            if not getattr(s, "archived", False) and (scope is None or getattr(s, "owner", None) == scope)]
    rows.sort(key=lambda r: (r[2] is not None, r[2].replace(tzinfo=None) if r[2] else datetime.min), reverse=True)
    return rows


def owned_chat(scope: Optional[str], sid: str):
    """The session `sid`, only if it exists and is the owner's."""
    try:
        sess = _sm().get_session(sid) if sid else None
    except Exception:
        return None
    if not sess or (scope is not None and getattr(sess, "owner", None) != scope):
        return None
    return sess


def available_models(owner: Optional[str], is_admin: bool) -> List[Dict[str, str]]:
    """Every chat model the owner can pick, in the web picker's order (the
    owner-scoped list GET /api/models serves). Offline and image endpoints
    are left out."""
    from routes.model_routes import fetch_models
    out: List[Dict[str, str]] = []
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


def activity(scope: Optional[str]) -> List[str]:
    """What is running for the owner: coding agents, background jobs, replies."""
    sm = _sm()
    mine = set(sm.get_sessions_for_user(scope).keys())
    import time as _time
    lines: List[str] = []
    try:
        from src import claude_code_jobs
        for j in claude_code_jobs.list_jobs(scope or ""):
            if j.status == "running" and (scope is None or j.owner == scope):
                lines.append(f"Agent {j.engine or 'coding'}: {j.prompt[:60] or j.action} "
                             f"({int((_time.time() - j.started) / 60)}m)")
    except Exception as e:
        logger.debug("mod activity: coding agents unavailable: %s", e)
    try:
        from src import bg_jobs
        for rec in bg_jobs.refresh().values():
            if rec.get("status") == "running" and rec.get("session_id") in mine:
                lines.append(f"Job: {str(rec.get('command') or '')[:60]}")
    except Exception as e:
        logger.debug("mod activity: background jobs unavailable: %s", e)
    try:
        from src import agent_runs
        for sid in agent_runs.active_sessions():
            if sid in mine:
                s = sm.get_session(sid)
                lines.append(f"Replying: {(getattr(s, 'name', '') or sid)[:40]}")
    except Exception as e:
        logger.debug("mod activity: replies unavailable: %s", e)
    return lines


async def _wait_idle(sid: str) -> None:
    """A reply still running from the turn before is stopped first (it keeps
    what it said), the way a new message in the web app cuts the old one off."""
    from src import agent_runs
    if not agent_runs.is_active(sid):
        return
    agent_runs.stop(sid)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STOP_WAIT_S
    while agent_runs.is_active(sid) and loop.time() < deadline:
        await asyncio.sleep(0.05)


def _event(ev: str) -> Optional[Dict]:
    for line in str(ev).splitlines():
        if line.startswith("data: "):
            body = line[6:].strip()
            if not body or body == "[DONE]":
                return None
            try:
                d = json.loads(body)
            except ValueError:
                return None
            return d if isinstance(d, dict) else None
    return None


async def turn_events(sid: str, text: str, *, source: str, voice_call: bool, note: str,
                      interrupt: bool) -> AsyncIterator[Dict[str, Any]]:
    """Send `text` into chat `sid` as a detached run (chat_queue.run_headless,
    the path a queued message takes with no page open) and yield the answer
    as it streams: {"delta"} pieces (reasoning left out), {"error"} if it
    failed, then {"done", "text"}."""
    from src import agent_runs, chat_queue
    if interrupt:
        await _wait_idle(sid)
    elif agent_runs.is_active(sid):
        yield {"error": "busy", "message": "That chat is busy with a reply right now."}
        return
    if not await chat_queue.run_headless(sid, text, source=source, voice_call=voice_call, note=note):
        yield {"error": "refused", "message": "The chat could not take the message."}
        return
    parts: List[str] = []
    failed = ""
    async for ev in agent_runs.subscribe(sid):
        d = _event(ev)
        if not d:
            continue
        if isinstance(d.get("delta"), str) and not d.get("thinking"):
            parts.append(d["delta"])
            yield {"delta": d["delta"]}
        elif d.get("error") and ("event: error" in ev or d.get("type") == "error"):
            failed = str(d.get("error"))[:200]
            yield {"error": "failed", "message": failed}
    yield {"done": True, "text": "".join(parts).strip(), "ok": not failed}


def _source(raw: Any) -> str:
    s = "".join(c for c in str(raw or "mod") if c.isalnum() or c in "_-")[:32]
    return s or "mod"


# ── speech ───────────────────────────────────────────────────────────────

def speech_problems() -> List[str]:
    """Why this server cannot hear or speak without a browser, as sentences
    (empty when it can): a phone line or a meeting has no browser engines."""
    problems = []
    try:
        from services.stt import get_stt_service
        stats = get_stt_service().get_stats()
        p = str(stats.get("provider") or "disabled")
        if p in ("disabled", "browser") or not stats.get("available"):
            problems.append("Speech to text: pick a local or API engine for \"Hears with\" "
                            "(Settings > AI Defaults > Voice call). The browser engine cannot hear a phone line or a meeting.")
    except Exception:
        problems.append("Speech to text is not available.")
    try:
        from services.tts import get_tts_service
        stats = get_tts_service().get_stats()
        p = str(stats.get("provider") or "disabled")
        if p in ("disabled", "browser") or not stats.get("available"):
            problems.append("Text to speech: pick Kokoro or an API engine for \"Speaks with\" "
                            "(Settings > AI Defaults > Voice call). The browser voice cannot speak on a phone line or in a meeting.")
        elif p == "elevenlabs" and stats.get("effective_provider") == "browser":
            problems.append("Text to speech: " + (stats.get("fallback_reason") or "ElevenLabs is paused")
                            + ", and Kokoro is not ready to take over. Download the Kokoro voice model or "
                            "lower the credit reserve (Settings > AI Defaults > Voice call).")
    except Exception:
        problems.append("Text to speech is not available.")
    return problems


# ── calendar ─────────────────────────────────────────────────────────────

def upcoming_meet_events(owner: str, hours: int) -> List[Dict[str, Any]]:
    """Calendar events (each occurrence) from an hour ago to `hours` ahead
    whose location or description has a Google Meet link."""
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
                text = f"{occ.get('location') or ''}\n{occ.get('description') or ''}"
                if "meet.google.com/" not in text.lower():
                    continue
                out.append({k: occ.get(k, "") for k in ("uid", "summary", "dtstart", "dtend", "location",
                                                          "description")})
        out.sort(key=lambda m: str(m.get("dtstart") or ""))
        return out[:40]
    finally:
        db.close()


# ── routes ───────────────────────────────────────────────────────────────

def setup_host_routes(router: APIRouter) -> None:
    P = "/api/mods/v1"

    @router.get(P + "/users/{name}")
    async def v1_user(request: Request, name: str):
        e = _mod(request, "users")
        scope, user = _user(e, name)
        return {"user": user, "admin": _is_admin(scope)}

    @router.get(P + "/chats")
    async def v1_chats(request: Request, user: str = "", q: str = "", limit: int = 10):
        e = _mod(request, "chats")
        scope, _ = _user(e, user)
        needle = q.strip().lower()
        rows = [r for r in recent_chats(scope) if not needle or needle in (getattr(r[1], "name", "") or "").lower()]
        return {"chats": [_chat_view(sid, s, ts) for sid, s, ts in rows[:max(1, min(LIST_MAX, int(limit or 10)))]]}

    @router.get(P + "/chats/{sid}")
    async def v1_chat(request: Request, sid: str, user: str = ""):
        e = _mod(request, "chats")
        scope, _ = _user(e, user)
        sess = owned_chat(scope, sid)
        if not sess:
            raise HTTPException(404, "No such chat")
        return _chat_view(sid, sess)

    @router.post(P + "/chats")
    async def v1_chat_new(request: Request):
        e = _mod(request, "chats")
        body = await _body(request)
        scope, user = _user(e, body.get("user"))
        model, endpoint_id = str(body.get("model") or ""), str(body.get("endpoint_id") or "")
        if not (model and endpoint_id):
            from routes.model_routes import resolve_default_chat
            dc = resolve_default_chat(scope or "", _is_admin(scope))
            model, endpoint_id = str(dc.get("model") or ""), str(dc.get("endpoint_id") or "")
            if not (model and endpoint_id):
                raise HTTPException(409, "No default model is set (Settings > AI Defaults)")
        name = " ".join(str(body.get("name") or "").split())[:120] or f"{model.split('/')[-1]} chat"
        from routes.session_routes import create_direct_chat
        try:
            sid, sess = create_direct_chat(_sm(), scope, model, endpoint_id, name=name)
        except HTTPException:
            raise HTTPException(409, "That model's endpoint is not available any more")
        runtime.log_line(e["id"], f"[odysseus] new chat {sid} for {user}")
        return _chat_view(sid, sess)

    @router.post(P + "/chats/{sid}/model")
    async def v1_chat_model(request: Request, sid: str):
        e = _mod(request, "chats")
        body = await _body(request)
        scope, _ = _user(e, body.get("user"))
        sess = owned_chat(scope, sid)
        if not sess:
            raise HTTPException(404, "No such chat")
        model, endpoint_id = str(body.get("model") or ""), str(body.get("endpoint_id") or "")
        pick = next((m for m in available_models(scope, _is_admin(scope))
                     if m["model"] == model and (not endpoint_id or m["endpoint_id"] == endpoint_id)), None)
        if not pick:
            raise HTTPException(404, "That model is not one this user can pick")
        from routes.session_routes import switch_session_model
        try:
            switch_session_model(sess, sid, pick["model"], pick["url"], pick["endpoint_id"], scope)
        except HTTPException:
            raise HTTPException(409, "That model's endpoint is not available any more")
        return _chat_view(sid, sess)

    @router.post(P + "/chats/{sid}/turn")
    async def v1_chat_turn(request: Request, sid: str):
        """{user, message, source, voice_call, note, interrupt, stream}. With
        stream, server-sent events of {delta} / {error} / {done, text};
        without, the whole answer as {ok, text, error}."""
        e = _mod(request, "chats")
        _rate(e["id"], "chats.turn")
        body = await _body(request)
        scope, user = _user(e, body.get("user"))
        if not owned_chat(scope, sid):
            raise HTTPException(404, "No such chat")
        message = str(body.get("message") or "").strip()
        if not message or len(message) > MAX_MESSAGE:
            raise HTTPException(400, f"message must be 1 to {MAX_MESSAGE} characters")
        kw = dict(source=_source(body.get("source")), voice_call=bool(body.get("voice_call")),
                  note=str(body.get("note") or "")[:4000], interrupt=body.get("interrupt", True) is not False)
        runtime.log_line(e["id"], f"[odysseus] chat turn in {sid} for {user} ({kw['source']})")
        if body.get("stream"):
            async def gen():
                try:
                    async for ev in turn_events(sid, message, **kw):
                        yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    yield f"data: {json.dumps({'error': 'failed', 'message': type(ex).__name__})}\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        out: Dict[str, Any] = {"ok": False, "text": "", "error": ""}

        async def collect():
            async for ev in turn_events(sid, message, **kw):
                if ev.get("error"):
                    out["error"] = ev.get("message") or ev["error"]
                    if ev["error"] in ("busy", "refused"):
                        out["code"] = ev["error"]
                if ev.get("done"):
                    out["text"], out["ok"] = ev["text"], bool(ev["ok"])
        try:
            await asyncio.wait_for(collect(), TURN_WAIT_S)
        except asyncio.TimeoutError:
            out["error"] = "The answer took too long."
        return out

    @router.post(P + "/chats/{sid}/stop")
    async def v1_chat_stop(request: Request, sid: str):
        e = _mod(request, "chats")
        body = await _body(request)
        scope, _ = _user(e, body.get("user"))
        if not owned_chat(scope, sid):
            raise HTTPException(404, "No such chat")
        from src import agent_runs
        return {"ok": True, "stopped": bool(agent_runs.stop(sid))}

    @router.post(P + "/chats/{sid}/note")
    async def v1_chat_note(request: Request, sid: str):
        """A line in the chat that is not a turn, saved as an assistant note
        (`meta.excluded`: shown, not read by the model)."""
        e = _mod(request, "chats")
        body = await _body(request)
        scope, _ = _user(e, body.get("user"))
        if not owned_chat(scope, sid):
            raise HTTPException(404, "No such chat")
        text = str(body.get("text") or "")
        if not text.strip() or len(text) > MAX_NOTE:
            raise HTTPException(400, f"text must be 1 to {MAX_NOTE} characters")
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        meta = {str(k)[:40]: v for k, v in list(meta.items())[:10]
                if isinstance(v, (str, int, float, bool)) and str(k) not in ("source", "note")}
        from core.models import ChatMessage
        sm = _sm()
        sm.add_message(sid, ChatMessage("assistant", text, metadata={"source": _source(body.get("source")),
                                                                      "note": True, **meta}))
        sm.save_sessions()
        return {"ok": True}

    @router.get(P + "/models")
    async def v1_models(request: Request, user: str = ""):
        e = _mod(request, "chats")
        scope, _ = _user(e, user)
        from routes.model_routes import resolve_default_chat
        dc = resolve_default_chat(scope or "", _is_admin(scope))
        return {"models": available_models(scope, _is_admin(scope)),
                "default": {"model": str(dc.get("model") or ""), "endpoint_id": str(dc.get("endpoint_id") or "")}}

    @router.get(P + "/activity")
    async def v1_activity(request: Request, user: str = ""):
        e = _mod(request, "chats")
        scope, _ = _user(e, user)
        return {"lines": activity(scope)}

    @router.get(P + "/speech/status")
    async def v1_speech_status(request: Request):
        _mod(request, "speech")
        return {"problems": await asyncio.to_thread(speech_problems)}

    @router.post(P + "/speech/transcribe")
    async def v1_transcribe(request: Request):
        """Body: a WAV file. Answers {text} ("" when nothing was heard)."""
        _mod(request, "speech")
        audio = await request.body()
        if not audio or len(audio) > MAX_AUDIO:
            raise HTTPException(400, "Send a WAV file of up to 25 MB as the body")
        from services.stt import get_stt_service
        try:
            text = await asyncio.to_thread(get_stt_service().transcribe, audio)
        except Exception as ex:
            raise HTTPException(502, f"Speech to text failed: {type(ex).__name__}")
        return {"text": text or ""}

    @router.post(P + "/speech/synthesize")
    async def v1_synthesize(request: Request):
        """{text}: the speech as audio/wav, or 502."""
        _mod(request, "speech")
        body = await _body(request)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > MAX_SPEAK:
            raise HTTPException(400, f"text must be 1 to {MAX_SPEAK} characters")
        from services.tts import get_tts_service
        try:
            wav = await asyncio.to_thread(get_tts_service().synthesize, text, True, "wav")
        except Exception as ex:
            raise HTTPException(502, f"Text to speech failed: {type(ex).__name__}")
        if not wav:
            raise HTTPException(502, "Text to speech gave nothing")
        return Response(wav, media_type="audio/wav")

    @router.get(P + "/browser")
    async def v1_browser(request: Request):
        _mod(request, "browser")
        from src import cloud_browser
        return {"available": bool(cloud_browser.enabled() and cloud_browser.chromium_path()),
                "running": bool(cloud_browser.running())}

    @router.post(P + "/browser/session")
    async def v1_browser_session(request: Request):
        """Start the cloud browser if needed: {cdp_url, x11_display}. The mod
        connects over CDP itself (loopback) and opens its own tabs."""
        e = _mod(request, "browser")
        from src import cloud_browser
        if not (cloud_browser.enabled() and cloud_browser.chromium_path()):
            raise HTTPException(409, "The cloud browser is not available on this server (no Chromium, or it is switched off)")
        try:
            endpoint = await asyncio.to_thread(cloud_browser.ensure)
        except Exception as ex:
            raise HTTPException(502, f"The cloud browser did not start: {type(ex).__name__}")
        if not endpoint:
            raise HTTPException(502, "The cloud browser did not start")
        runtime.log_line(e["id"], "[odysseus] cloud browser session")
        return {"cdp_url": endpoint, "x11_display": cloud_browser.x11_display_for_input() or ""}

    @router.get(P + "/google/calendar")
    async def v1_google_status(request: Request, user: str = ""):
        e = _mod(request, "google.calendar")
        scope, _ = _user(e, user)
        from src.google_workspace import meet as gmeet
        return gmeet.status(scope)

    @router.post(P + "/google/meet")
    async def v1_google_meet(request: Request):
        """{user, title, start ("now", ISO or words), minutes, attendees,
        description, open_access}: a Calendar event with a new Meet in the
        user's primary calendar. Answers the link and the event."""
        e = _mod(request, "google.calendar")
        body = await _body(request)
        scope, user = _user(e, body.get("user"))
        from src.google_workspace import meet as gmeet
        try:
            start = gmeet.parse_start(str(body.get("start") or "now"))
            attendees = gmeet.clean_attendees(body.get("attendees") or [])
            if not gmeet.status(scope)["connected"]:
                raise gmeet.GoogleError("Google Calendar is not connected. Connect it in Settings > Google.")
            made = await gmeet.create_meeting(
                scope, title=str(body.get("title") or ""), start=start,
                minutes=body.get("minutes") or gmeet.DEFAULT_MINUTES, attendees=attendees,
                description=str(body.get("description") or ""), open_access=body.get("open_access", True) is not False)
        except gmeet.GoogleError as ex:
            return JSONResponse({"detail": str(ex)}, status_code=400)
        made["access_hint"] = gmeet.access_hint(made)
        made["starts_now"] = start <= datetime.now(start.tzinfo) + timedelta(minutes=5)
        runtime.log_line(e["id"], f"[odysseus] google meet made for {user} ({len(attendees)} invited)")
        return made

    @router.get(P + "/calendar/meetings")
    async def v1_calendar_meetings(request: Request, user: str = "", hours: int = 24):
        e = _mod(request, "calendar.read")
        scope, user_name = _user(e, user)
        owner = scope or user_name
        try:
            return {"events": upcoming_meet_events(owner, max(1, min(24 * 7, int(hours))))}
        except Exception as ex:
            logger.warning("mod calendar meetings: %s", type(ex).__name__)
            return {"events": []}
