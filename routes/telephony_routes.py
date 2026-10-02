"""Phone calls: call a number and talk to the agent, like the in-app call.

Asked for: "set up calling on my phone ... i want it to work how the /call
feature works." Google Voice cannot do that (no API, no SIP for consumer
accounts, and declining sends the caller to voicemail), so a phone number
from a telephony provider answers instead and streams the call's audio
here. docs/phone-calls.md has the research and the setup.

Twilio side (public, through Tailscale Funnel or a tunnel; each request
proves itself, so these are exempt from login in app.py):
    POST /api/telephony/twilio/voice       a call came in (or "call me" was answered)
    POST /api/telephony/twilio/pin         the digits the caller entered
    POST /api/telephony/twilio/done        after an unknown caller's message
    POST /api/telephony/twilio/recording   that message is ready
    POST /api/telephony/twilio/meet        the agent's call into a Google Meet was
                                           answered (src/meet/dialin.py): key in the PIN
    WS   /api/telephony/twilio/stream      Media Streams: the call's audio both ways
    WS   /api/telephony/twilio/relay       ConversationRelay: the call's words both ways
    GET  /api/telephony/twilio/health      for the public URL check
Owner side (logged in, Settings > Calls & Meetings > Phone calls):
    GET  /api/telephony/config             settings (never the token or PIN)
    PUT  /api/telephony/config             save them
    POST /api/telephony/test               check credentials, number, public URL, engines
    POST /api/telephony/call-me            the agent calls your phone

Every webhook must carry a valid X-Twilio-Signature made with the auth token
of the user whose agent number was called; anything else gets the same 404
as a path that does not exist. The WebSockets carry no cookie, so the TwiML
that connects a call hands Twilio a one-time token (as a stream parameter),
good for one stream, for STREAM_TOKEN_TTL seconds, for that call only.
"""

import asyncio
import base64
import json
import logging
import re
import secrets
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from src.auth_helpers import require_user
from src.telephony import agent, call as call_mod, codec, config, speech, twilio

logger = logging.getLogger(__name__)

PREFIX = "/api/telephony/twilio/"
STREAM_TOKEN_TTL = 120.0
PIN_TRIES = 3
MAX_DELAY_S = 25

# One-time stream tokens: token -> {owner, sid, call_sid, caller, engine, at}.
_PENDING: Dict[str, Dict] = {}
# Unknown callers leaving a message, by call SID, until the recording is in.
_MESSAGES: Dict[str, Dict] = {}
# Calls on the line now, by call SID (for the Settings card and tests).
ACTIVE: Dict[str, Dict] = {}
# What the agent opens with on a call it started (the call_me tool), by call SID.
_OUTBOUND_GREETINGS: Dict[str, Tuple[str, float]] = {}
# Calls into a Google Meet placed and not answered yet: key -> {meet, owner, at}.
_MEET_DIALS: Dict[str, Dict] = {}
MEET_DIAL_TTL = 300.0
_TASKS: set = set()


def _xml(body: str) -> Response:
    return Response(body, media_type="text/xml")


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _bg(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t


def _is_admin(app, owner: Optional[str]) -> bool:
    try:
        from src.auth_helpers import _auth_disabled
        if _auth_disabled():
            return True
        mgr = getattr(app.state, "auth_manager", None)
        return bool(owner and mgr is not None and mgr.is_admin(owner))
    except Exception:
        return False


def _public_base(cfg: Dict) -> str:
    return str(cfg.get("public_url") or "").rstrip("/")


def _signed_url(cfg: Dict, request: Request) -> str:
    """The URL Twilio called, as Twilio saw it: the public address, not the
    loopback one the proxy forwarded to."""
    q = request.url.query
    return _public_base(cfg) + request.url.path + (f"?{q}" if q else "")


def _ws_url(cfg: Dict, name: str) -> str:
    base = _public_base(cfg)
    p = urlsplit(base)
    scheme = "wss" if p.scheme == "https" else "ws"
    return f"{scheme}://{p.netloc}{p.path}{PREFIX}{name}"


def _sweep() -> None:
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v["at"] > STREAM_TOKEN_TTL]:
        _PENDING.pop(k, None)
    for k in [k for k, v in _MESSAGES.items() if now - v["at"] > 3600]:
        _MESSAGES.pop(k, None)
    for k in [k for k, v in _OUTBOUND_GREETINGS.items() if now - v[1] > 600]:
        _OUTBOUND_GREETINGS.pop(k, None)
    for k in [k for k, v in _MEET_DIALS.items() if now - v["at"] > MEET_DIAL_TTL]:
        _MEET_DIALS.pop(k, None)


def register_meet_dial(meet_id: str, owner: Optional[str]) -> str:
    """A one-time key for the webhook of an outbound call into a meeting."""
    _sweep()
    key = secrets.token_urlsafe(18)
    _MEET_DIALS[key] = {"meet": meet_id, "owner": owner, "at": time.time()}
    return key


async def _verified(request: Request) -> Optional[Tuple[Optional[str], Dict, Dict[str, str]]]:
    """(owner, config, form params) for a webhook whose signature checks out
    against the auth token of the user the call is for, else None."""
    form = await request.form()
    params = [(k, str(v)) for k, v in form.multi_items()]
    p = dict(params)
    header = request.headers.get("x-twilio-signature", "")
    outbound = p.get("Direction", "").startswith("outbound")
    agent_number = p.get("From") if outbound else p.get("To")
    for owner, cfg in config.owners_for_number(agent_number or ""):
        if str(cfg.get("account_sid") or "") != p.get("AccountSid", ""):
            continue
        token = config.auth_token(cfg)
        if twilio.valid_signature(_signed_url(cfg, request), params, token, header):
            return owner, cfg, p
    return None


async def _verified_any(request: Request) -> Optional[Tuple[Optional[str], Dict, Dict[str, str]]]:
    """Like _verified, for callbacks that do not say which number was called
    (recording status): matched by account SID, then signature."""
    form = await request.form()
    params = [(k, str(v)) for k, v in form.multi_items()]
    p = dict(params)
    header = request.headers.get("x-twilio-signature", "")
    for owner, cfg in config._owners():
        if not cfg.get("account_sid") or str(cfg.get("account_sid")) != p.get("AccountSid", ""):
            continue
        if twilio.valid_signature(_signed_url(cfg, request), params, config.auth_token(cfg), header):
            return owner, cfg, p
    return None


def _caller(p: Dict[str, str]) -> str:
    """The person on the other end: who called, or who "call me" rang."""
    outbound = p.get("Direction", "").startswith("outbound")
    return config.normalize_number(p.get("To") if outbound else p.get("From"))


def _connect(request: Request, owner: Optional[str], cfg: Dict, p: Dict[str, str]) -> Response:
    """The call is allowed: make its chat and hand Twilio the stream."""
    caller = _caller(p)
    engine = cfg.get("engine") if cfg.get("engine") in config.ENGINES else "odysseus"
    if engine == "odysseus":
        problems = call_mod.engines_ready()
        if problems:
            logger.warning("[phone] call from %s: speech engines not ready", caller)
            return _xml(twilio.say_and_hang_up(
                "Odysseus cannot take calls yet: its speech engines are not set up. "
                "Pick them in Settings, A I Defaults, Voice call. Goodbye."))
    direction = "outbound" if p.get("Direction", "").startswith("outbound") else "inbound"
    try:
        sid, _sess = agent.new_call_chat(owner, cfg, caller, direction, _is_admin(request.app, owner))
    except Exception as e:
        logger.warning("[phone] no chat for the call: %s", e)
        return _xml(twilio.say_and_hang_up("Odysseus has no model to answer with. "
                                           "Pick one in Settings, Calls and Meetings, Phone calls. Goodbye."))
    _sweep()
    token = secrets.token_urlsafe(24)
    opener = _OUTBOUND_GREETINGS.pop(p.get("CallSid", ""), ("", 0))[0] if direction == "outbound" else ""
    greeting = opener or str(cfg.get("greeting") or "").strip() or config.DEFAULT_GREETING
    _PENDING[token] = {"owner": owner, "sid": sid, "call_sid": p.get("CallSid", ""), "caller": caller,
                       "engine": engine, "at": time.time(), "greeting": greeting}
    logger.info("[phone] call %s from %s connected (%s) into chat %s",
                p.get("CallSid", "")[:12], caller, engine, sid)
    if engine == "relay":
        return _xml(twilio.connect_relay(_ws_url(cfg, "relay"), {"token": token}, greeting,
                                         str(cfg.get("relay_voice") or "")))
    return _xml(twilio.connect_stream(_ws_url(cfg, "stream"), {"token": token}))


def _take(token: str, call_sid: str) -> Optional[Dict]:
    """A stream token, once, only for the call it was made for."""
    _sweep()
    entry = _PENDING.get(token or "")
    if not entry or (entry["call_sid"] and call_sid and entry["call_sid"] != call_sid):
        return None
    _PENDING.pop(token, None)
    return entry


def _owner_cfg(owner: Optional[str]) -> Dict:
    return config.get_config(owner)


async def _unknown_caller(owner: Optional[str], cfg: Dict, p: Dict[str, str], is_admin: bool) -> Response:
    caller = config.normalize_number(p.get("From")) or "an unknown number"
    if cfg.get("unknown") == "message":
        _MESSAGES[p.get("CallSid", "")] = {"owner": owner, "from": caller, "at": time.time(), "admin": is_admin}
        base = _public_base(cfg) + PREFIX
        logger.info("[phone] call from %s: not an allowed number, taking a message", caller)
        return _xml(twilio.take_message(
            "Sorry, this assistant only talks with its owner. Leave a short message after the tone, and it will be passed on.",
            base + "done", base + "recording"))
    logger.info("[phone] call from %s: not an allowed number, turned away", caller)
    return _xml(twilio.say_and_hang_up("Sorry, this number only takes calls from its owner. Goodbye."))


async def _deliver_message(owner: Optional[str], cfg: Dict, caller: str, rec_url: str, rec_sid: str,
                           duration: str, is_admin: bool = False) -> None:
    """Transcribe an unknown caller's message into a "Phone messages" chat
    and send a notification. The recording is deleted from Twilio after."""
    sid_acct, token = str(cfg.get("account_sid") or ""), config.auth_token(cfg)
    text = ""
    try:
        wav = await twilio.fetch_recording(sid_acct, token, rec_url)
        samples, rate = codec.read_wav(wav)
        audio = codec.wav_bytes(codec.resample(samples, rate, 16000), 16000)
        text = (await asyncio.to_thread(call_mod.default_stt, audio)) or ""
    except Exception as e:
        logger.warning("[phone] message from %s could not be transcribed: %s", caller, type(e).__name__)
    finally:
        try:
            await twilio.delete_recording(sid_acct, token, rec_sid)
        except Exception:
            pass
    body = f"Voice message from {caller} ({duration}s): " + (text.strip() or "(could not be transcribed)")
    try:
        sid = str(cfg.get("messages_sid") or "")
        from routes.sms_routes import _owned
        if not sid or not _owned(agent._session_manager(), owner, sid):
            sid, _ = agent.new_call_chat(owner, cfg, "messages", is_admin=is_admin, name="Phone messages")
            fresh = config.get_config(owner)
            fresh["messages_sid"] = sid
            config.save_config(owner, fresh)
        agent.note(sid, body)
    except Exception as e:
        logger.warning("[phone] message could not be saved: %s", type(e).__name__)
    try:
        from routes.sms_routes import _push_owner
        await _push_owner(owner, body[:300])
    except Exception:
        pass


async def twilio_call_me(user: Optional[str], to: str = "", greeting: str = "") -> Dict:
    """Have Twilio ring one of the user's allowed numbers (the first unless
    `to` names another). `greeting`, if given, is what the agent opens with
    instead of the usual greeting. ValueError: not set up; RuntimeError:
    Twilio refused."""
    cfg = config.get_config(user)
    allowed = config.allowed_numbers(user, cfg)
    to = config.normalize_number(to or (allowed[0] if allowed else ""))
    if not cfg.get("enabled"):
        raise ValueError("Turn phone calls on first.")
    if not to or to not in allowed:
        raise ValueError("The agent only calls your allowed numbers.")
    sid, token, number = str(cfg.get("account_sid") or ""), config.auth_token(cfg), str(cfg.get("phone_number") or "")
    if not (sid and token and number and _public_base(cfg)):
        raise ValueError("Save the Twilio account, the agent's number and the public URL first.")
    try:
        r = await twilio.create_call(sid, token, number, to, _public_base(cfg) + PREFIX + "voice")
    except Exception as e:
        raise RuntimeError(str(e)[:200])
    if greeting and r.get("sid"):
        _OUTBOUND_GREETINGS[str(r["sid"])] = (greeting, time.time())
    logger.info("[phone] calling %s for %s", to, user or "-")
    return {"ok": True, "to": to, "status": r.get("status", "")}


def setup_telephony_routes() -> APIRouter:
    router = APIRouter(tags=["telephony"])

    # ── Twilio webhooks ──

    @router.post(PREFIX + "voice")
    async def voice(request: Request):
        found = await _verified(request)
        if not found:
            logger.info("[phone] voice webhook: signature or number did not match")
            return _not_found()
        owner, cfg, p = found
        if not cfg.get("enabled"):
            return _xml(twilio.say_and_hang_up("Phone calls to Odysseus are turned off. Goodbye."))
        caller = _caller(p)
        if not caller or caller not in config.allowed_numbers(owner, cfg):
            if p.get("Direction", "").startswith("outbound"):
                return _xml(twilio.say_and_hang_up("Goodbye."))
            return await _unknown_caller(owner, cfg, p, _is_admin(request.app, owner))
        outbound = p.get("Direction", "").startswith("outbound")
        delay = int(cfg.get("answer_delay") or 0)
        if delay and not outbound and request.query_params.get("rung") != "1":
            # Let the call ring first (your own phones get the first chance
            # when Google Voice rings this number with them); Twilio picks up
            # only after the pause, then asks again.
            nxt = _public_base(cfg) + PREFIX + "voice?rung=1"
            return _xml(twilio.twiml(f'<Pause length="{min(delay, MAX_DELAY_S)}"/>',
                                     f'<Redirect method="POST">{nxt}</Redirect>'))
        if cfg.get("pin_hash") and not outbound:
            action = _public_base(cfg) + PREFIX + "pin?" + urlencode({"try": 1})
            return _xml(twilio.gather_pin(action, int(cfg.get("pin_len") or 4)))
        return _connect(request, owner, cfg, p)

    @router.post(PREFIX + "pin")
    async def pin(request: Request):
        found = await _verified(request)
        if not found:
            return _not_found()
        owner, cfg, p = found
        caller = _caller(p)
        if not cfg.get("enabled") or caller not in config.allowed_numbers(owner, cfg):
            return _xml(twilio.say_and_hang_up("Goodbye."))
        if config.check_pin(cfg, p.get("Digits", "")):
            return _connect(request, owner, cfg, p)
        try:
            tries = int(request.query_params.get("try") or 1)
        except ValueError:
            tries = PIN_TRIES
        logger.info("[phone] wrong PIN from %s (try %d)", caller, tries)
        if tries >= PIN_TRIES:
            return _xml(twilio.say_and_hang_up("That PIN is not right. Goodbye."))
        action = _public_base(cfg) + PREFIX + "pin?" + urlencode({"try": tries + 1})
        return _xml(twilio.gather_pin(action, int(cfg.get("pin_len") or 4), "That is not right. Try again."))

    @router.post(PREFIX + "done")
    async def done(request: Request):
        if not await _verified(request):
            return _not_found()
        return _xml(twilio.say_and_hang_up("Thanks, your message was passed on. Goodbye."))

    @router.post(PREFIX + "recording")
    async def recording(request: Request):
        found = await _verified_any(request)
        if not found:
            return _not_found()
        owner, cfg, p = found
        entry = _MESSAGES.pop(p.get("CallSid", ""), None)
        if not entry or entry["owner"] != owner or p.get("RecordingStatus", "completed") != "completed":
            return {"ok": True}
        _bg(_deliver_message(owner, cfg, entry["from"], p.get("RecordingUrl", ""),
                             p.get("RecordingSid", ""), p.get("RecordingDuration", "?"), entry.get("admin", False)))
        return {"ok": True}

    @router.post(PREFIX + "meet")
    async def meet_dial(request: Request):
        """The agent's call into a Google Meet's dial-in number was answered."""
        found = await _verified(request)
        if not found:
            return _not_found()
        owner, cfg, p = found
        _sweep()
        entry = _MEET_DIALS.pop(request.query_params.get("m") or "", None)
        from src.meet import dialin, session as meet_session
        meeting = meet_session.get(entry["meet"]) if entry else None
        if (not entry or entry["owner"] != owner or not meeting
                or not p.get("Direction", "").startswith("outbound")):
            return _xml(twilio.say_and_hang_up("Goodbye."))
        token = secrets.token_urlsafe(24)
        _PENDING[token] = {"owner": owner, "sid": meeting.sid, "call_sid": p.get("CallSid", ""),
                           "caller": dialin.describe(meeting.dial_in), "engine": "odysseus",
                           "meet": meeting.id, "at": time.time()}
        logger.info("[phone] call %s into a Google Meet answered", p.get("CallSid", "")[:12])
        return _xml(dialin.twiml(meeting, _ws_url(cfg, "stream"), token))

    @router.get(PREFIX + "health")
    async def health():
        return {"ok": True, "service": "odysseus-telephony"}

    # ── Media Streams: our own speech engines ──

    @router.websocket(PREFIX + "stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        call: Optional[call_mod.PhoneCall] = None
        entry: Optional[Dict] = None
        call_sid = ""

        async def close():
            try:
                await ws.close()
            except Exception:
                pass

        try:
            while True:
                msg = json.loads(await ws.receive_text())
                ev = msg.get("event")
                if ev == "start":
                    start = msg.get("start") or {}
                    call_sid = str(start.get("callSid") or "")
                    entry = _take(str((start.get("customParameters") or {}).get("token") or ""), call_sid)
                    if not entry or entry["engine"] != "odysseus":
                        logger.info("[phone] media stream with no valid token: closed")
                        await close()
                        return
                    cfg = _owner_cfg(entry["owner"])
                    transport = twilio.MediaStreamTransport(ws.send_text, str(msg.get("streamSid") or start.get("streamSid") or ""), close)
                    ACTIVE[call_sid] = {"owner": entry["owner"], "sid": entry["sid"], "caller": entry["caller"],
                                        "since": time.time(), "engine": "meet" if entry.get("meet") else "odysseus"}
                    if entry.get("meet"):
                        # A Google Meet's dial-in line: the meeting runs the call.
                        from src.meet import session as meet_session
                        meeting = meet_session.get(entry["meet"])
                        if not meeting:
                            await close()
                            return
                        call = await meeting.attach_phone(transport)
                    else:
                        greeting = entry.get("greeting") or str(cfg.get("greeting") or "").strip() or config.DEFAULT_GREETING
                        call = call_mod.PhoneCall(transport, entry["sid"], greeting=greeting)
                        await call.start()
                elif call is None:
                    continue            # "connected" comes before "start"
                elif ev == "media":
                    media = msg.get("media") or {}
                    if media.get("track", "inbound") in ("inbound", "inbound_track"):
                        call.feed(base64.b64decode(media.get("payload") or ""))
                elif ev == "mark":
                    call.played(str((msg.get("mark") or {}).get("name") or ""))
                elif ev == "dtmf":
                    call.dtmf(str((msg.get("dtmf") or {}).get("digit") or ""))
                elif ev == "stop":
                    break
                if call is not None and call.ended:
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        except ValueError:
            logger.info("[phone] media stream sent something that is not JSON: closed")
        finally:
            ACTIVE.pop(call_sid, None)
            if call is not None:
                await call.end("stream closed")
                logger.info("[phone] call %s ended after %d turn(s)", call_sid[:12], call.turns)
            if entry and entry.get("meet"):
                from src.meet import session as meet_session
                meeting = meet_session.get(entry["meet"])
                if meeting:
                    meeting.phone_closed()
            await close()

    # ── ConversationRelay: Twilio's speech engines, our agent ──

    @router.websocket(PREFIX + "relay")
    async def relay(ws: WebSocket):
        await ws.accept()
        entry: Optional[Dict] = None
        call_sid = ""
        turn: Optional[asyncio.Task] = None
        turns = 0

        async def answer(text: str):
            nonlocal turns
            turns += 1
            if call_mod._BYE.match(text):
                await ws.send_text(twilio.relay_text(call_mod.GOODBYE, True))
                await asyncio.sleep(1.5)
                await ws.send_text(twilio.relay_end("goodbye"))
                return
            raw, idx, sent = "", 0, False
            stream = agent.reply(entry["sid"], text)
            try:
                async for kind, piece in stream:
                    if kind != "delta":
                        continue
                    raw += piece
                    sentences, idx = speech.take_sentences(speech.speakable_text(raw), idx)
                    for s in sentences:
                        await ws.send_text(twilio.relay_text(s + " ", False))
                        sent = True
            finally:
                await stream.aclose()
            sentences, idx = speech.take_sentences(speech.speakable_text(raw), idx, final=True)
            for s in sentences:
                await ws.send_text(twilio.relay_text(s + " ", False))
                sent = True
            if not sent:
                await ws.send_text(twilio.relay_text("The reply has no spoken text. It is in the chat.", False))
            await ws.send_text(twilio.relay_text("", True))

        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "setup":
                    call_sid = str(msg.get("callSid") or "")
                    entry = _take(str((msg.get("customParameters") or {}).get("token") or ""), call_sid)
                    if not entry or entry["engine"] != "relay":
                        logger.info("[phone] relay with no valid token: closed")
                        return
                    ACTIVE[call_sid] = {"owner": entry["owner"], "sid": entry["sid"], "caller": entry["caller"],
                                        "since": time.time(), "engine": "relay"}
                elif entry is None:
                    continue
                elif time.time() - ACTIVE.get(call_sid, {}).get("since", time.time()) > call_mod.MAX_CALL_S:
                    await ws.send_text(twilio.relay_text("This call has reached its time limit. Goodbye.", True))
                    await asyncio.sleep(3)
                    await ws.send_text(twilio.relay_end("time limit"))
                    return
                elif kind == "prompt" and msg.get("last", True):
                    text = str(msg.get("voicePrompt") or "").strip()
                    if text:
                        if turn and not turn.done():
                            turn.cancel()
                        turn = asyncio.create_task(answer(text))
                elif kind == "interrupt":
                    if turn and not turn.done():
                        turn.cancel()
                elif kind == "error":
                    logger.info("[phone] relay error from Twilio: %s", str(msg.get("description") or "")[:200])
        except (WebSocketDisconnect, RuntimeError):
            pass
        except ValueError:
            logger.info("[phone] relay sent something that is not JSON: closed")
        finally:
            if turn and not turn.done():
                turn.cancel()
            ACTIVE.pop(call_sid, None)
            if entry:
                logger.info("[phone] relay call %s ended after %d turn(s)", call_sid[:12], turns)
            try:
                await ws.close()
            except Exception:
                pass

    # ── Settings ──

    def _view(request: Request, user: Optional[str], cfg: Dict) -> Dict:
        out = config.public(user, cfg)
        base = _public_base(cfg)
        out["webhook_url"] = (base + PREFIX + "voice") if base else ""
        out["engines"] = call_mod.engines_ready() if out["engine"] == "odysseus" else []
        out["active_calls"] = sum(1 for c in ACTIVE.values() if c["owner"] == user)
        out["answer_delay"] = int(cfg.get("answer_delay") or 0)
        try:
            from routes.sms_routes import available_models
            out["models"] = [{"model": m["model"], "name": m["name"], "endpoint_id": m["endpoint_id"],
                              "endpoint_name": m["endpoint_name"]}
                             for m in available_models(user, _is_admin(request.app, user))]
        except Exception:
            out["models"] = []
        return out

    @router.get("/api/telephony/config")
    async def read_config(request: Request):
        user = require_user(request) or None
        return _view(request, user, config.get_config(user))

    @router.put("/api/telephony/config")
    async def write_config(request: Request):
        user = require_user(request) or None
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object.")
        cfg = config.get_config(user)
        if "enabled" in body:
            cfg["enabled"] = bool(body["enabled"])
        if "provider" in body:
            if body["provider"] != "twilio":
                raise HTTPException(400, "Twilio is the only provider so far.")
            cfg["provider"] = "twilio"
        if "account_sid" in body:
            sid = str(body["account_sid"] or "").strip()
            if sid and not config.SID_RE.match(sid):
                raise HTTPException(400, "A Twilio account SID is AC followed by 32 letters and digits.")
            cfg["account_sid"] = sid
        if body.get("auth_token"):
            tok = str(body["auth_token"]).strip()
            if not re.fullmatch(r"[A-Za-z0-9]{16,64}", tok):
                raise HTTPException(400, "That does not look like a Twilio auth token.")
            config.set_auth_token(cfg, tok)
        if body.get("clear_auth_token"):
            config.set_auth_token(cfg, "")
        if "phone_number" in body:
            num = config.normalize_number(body["phone_number"])
            if body["phone_number"] and len(num.lstrip("+")) < 10:
                raise HTTPException(400, "The agent's number should be a full number, like +15550102000.")
            cfg["phone_number"] = num
        if "numbers" in body:
            nums: List[str] = []
            for n in body.get("numbers") or []:
                norm = config.normalize_number(n)
                if len(norm.lstrip("+")) < 5:
                    raise HTTPException(400, f"Not a phone number: {str(n)[:30]}")
                if norm not in nums:
                    nums.append(norm)
            if len(nums) > config.MAX_NUMBERS:
                raise HTTPException(400, f"At most {config.MAX_NUMBERS} numbers.")
            cfg["numbers"] = nums
        if "greeting" in body:
            cfg["greeting"] = str(body["greeting"] or "").strip()[: config.MAX_GREETING]
        if "model" in body or "endpoint_id" in body:
            cfg["model"] = str(body.get("model") or "")
            cfg["endpoint_id"] = str(body.get("endpoint_id") or "")
        if "engine" in body:
            if body["engine"] not in config.ENGINES:
                raise HTTPException(400, "Unknown speech option.")
            cfg["engine"] = body["engine"]
        if "relay_voice" in body:
            cfg["relay_voice"] = str(body["relay_voice"] or "").strip()[:80]
        if "unknown" in body:
            if body["unknown"] not in config.UNKNOWN:
                raise HTTPException(400, "Unknown option for other callers.")
            cfg["unknown"] = body["unknown"]
        if "answer_delay" in body:
            try:
                d = int(body["answer_delay"] or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "The answer delay is a number of seconds.")
            cfg["answer_delay"] = max(0, min(MAX_DELAY_S, d))
        if "pin" in body and body["pin"] not in (None, ""):
            pin_s = str(body["pin"]).strip()
            if not config.PIN_RE.match(pin_s):
                raise HTTPException(400, "The PIN is 4 to 8 digits.")
            config.set_pin(cfg, pin_s)
        if body.get("clear_pin"):
            config.set_pin(cfg, "")
        if "public_url" in body:
            url = str(body["public_url"] or "").strip().rstrip("/")
            if url:
                p = urlsplit(url)
                if p.scheme != "https" or not p.hostname or p.path not in ("",) or p.query:
                    raise HTTPException(400, "The public URL is the https:// address with no path, "
                                             "like https://server.tailnet.ts.net:8443")
            cfg["public_url"] = url
        config.save_config(user, cfg)
        logger.info("[phone] settings saved for %s (enabled=%s)", user or "-", bool(cfg.get("enabled")))
        return _view(request, user, cfg)

    @router.post("/api/telephony/test")
    async def test(request: Request):
        user = require_user(request) or None
        cfg = config.get_config(user)
        checks = []

        def add(name, ok, detail=""):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        sid, token, number = str(cfg.get("account_sid") or ""), config.auth_token(cfg), str(cfg.get("phone_number") or "")
        base = _public_base(cfg)
        add("Turned on", cfg.get("enabled"), "" if cfg.get("enabled") else "Calls are refused until you turn this on.")
        allowed = config.allowed_numbers(user, cfg)
        add("Allowed numbers", bool(allowed), ", ".join(allowed) if allowed else "Add your number, or no one gets through.")
        if not (sid and token and number):
            add("Twilio account", False, "Save the account SID, auth token and the agent's number first.")
        else:
            try:
                rec = await twilio.find_number(sid, token, number)
                if not rec:
                    add("Twilio account", False, f"{number} is not a number on this Twilio account.")
                else:
                    add("Twilio account", True, f"{number} is on the account.")
                    want = base + PREFIX + "voice" if base else ""
                    have = str(rec.get("voice_url") or "")
                    add("Number points here", bool(want) and have == want,
                        "Yes." if have == want and want else
                        f"In Twilio, set the number's \"A call comes in\" webhook to {want or '(save the public URL first)'} (HTTP POST).")
            except Exception as e:
                add("Twilio account", False, str(e)[:200])
        if not base:
            add("Public URL", False, "Save the public URL (Tailscale Funnel or a tunnel) first.")
        else:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=6.0) as client:
                    r = await client.get(base + PREFIX + "health")
                ok = r.status_code == 200 and r.json().get("service") == "odysseus-telephony"
                add("Public URL", ok, "Reachable." if ok else f"HTTP {r.status_code} from {base}.")
            except Exception as e:
                add("Public URL", False, f"Could not reach {base}: {type(e).__name__}.")
        if (cfg.get("engine") or "odysseus") == "odysseus":
            problems = call_mod.engines_ready()
            add("Speech engines", not problems, " ".join(problems) or "Ready.")
        else:
            add("Speech engines", True, "Twilio hears and speaks (ConversationRelay).")
        try:
            model, ep = agent.resolve_model(user, cfg, _is_admin(request.app, user))
            add("Model", bool(model and ep), model or "No model picked and no default model.")
        except Exception:
            add("Model", False, "No model picked and no default model.")
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    @router.post("/api/telephony/call-me")
    async def call_me(request: Request):
        user = require_user(request) or None
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return await twilio_call_me(user, to=str((body or {}).get("to") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e)[:200])

    return router
