"""One Google Meet with the agent in it.

The meeting runs the phone call's turn loop (src/telephony/call.py) over
one of two transports: the Meet tab in the cloud browser (browser.py, 16 kHz
PCM) or the Meet dial-in line over Twilio (dialin.py, 8 kHz mu-law). Speech
to text, text to speech, endpointing, barge-in, and each answer through the
meeting's own chat as a detached agent run (src/telephony/agent.py) are all
shared with the in-app call and the phone line.

Two modes:
    talk        "talk with me": every utterance is a turn, as on a call.
    assistant   "meeting assistant": every utterance goes into the live
                transcript (a line in the meeting's chat, which the model
                does not read as conversation), and only an utterance that
                calls it by a wake word ("Odysseus, ...") is answered, with
                what was said since it last spoke as context. When the
                meeting is over it posts a summary with action items.

It says out loud that it is an AI and that the meeting is transcribed when
it joins (and posts the same in the meeting chat from the browser), shows
the display name "Odysseus (AI)", and leaves when told to ("Odysseus, leave
the meeting"), when the meeting ends, when it is left alone, after a quiet
spell, after the time limit, or from the Leave button in Odysseus.
"""

import asyncio
import logging
import re
import secrets
import time
from collections import deque
from datetime import datetime
from typing import Callable, Deque, Dict, List, Optional, Tuple

from src.meet import config as meet_config
from src.telephony import agent, call as call_mod, codec

logger = logging.getLogger(__name__)

SOURCE = "google_meet"
MAX_ACTIVE = 3
POLL_S = 1.0
LOAD_S = 60.0
ALONE_S = 120.0
ARMED_S = 10.0
DIAL_CONNECT_S = 90.0
PHONE_ANNOUNCE_S = 15.0
MAX_CONTEXT_CHARS = 4000
MAX_SUMMARY_CHARS = 60000
GUEST_DENIED_S = 60.0

GUEST_DENIED = (
    "Google would not let a signed-out guest join from the server's browser (\"You can't join this "
    "video call\"). Google refuses automated signed-out browsers even in open meetings. The fix: sign the "
    "cloud browser (Browser in the sidebar, accounts.google.com) into a separate Google account for the "
    "bot, not your own, and set Join as to \"Signed in\" in the Google Meet settings. Also check the "
    "meeting's host controls: Meeting access set to Open, not Trusted or Restricted."
)

UNSAFE_BROWSER_DENIED = (
    "Google blocked the cloud browser as an unverified or automated browser (\"This browser or app may "
    "not be secure\"). Joining by browser is blocked by Google. To attend this meeting, join by phone "
    "instead using the dial-in number and PIN from the calendar invite."
)

MEET_NOTE = (
    "You are in a Google Meet video meeting as \"{name}\", an AI assistant there for {owner}. "
    "Other people are in the meeting and hear everything you say; they were told an AI is "
    "listening and transcribing. What people say reaches you through speech recognition, without "
    "speaker names, so expect small mistakes. You were just addressed: answer that, briefly, in a "
    "few spoken sentences that suit everyone in the meeting. Do not read out private things about "
    "{owner} (email, calendar, notes, memory) unless the question asks for them."
)
TALK_NOTE = (
    "You are talking with {owner} in a Google Meet video meeting, as \"{name}\". Others in the "
    "meeting may hear you; they were told an AI is listening and the meeting is transcribed."
)
SUMMARY_NOTE = (
    "The Google Meet you were in has ended. Write the meeting notes for {owner} from the transcript "
    "you are given. This is written, not spoken: markdown is fine."
)

LEAVE_RE = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|alright|thanks|thank you)[\s,]+)?(?:(?:you can|please|go ahead and|time to)\s+)?"
    r"(?:leave|exit|hang up|drop off|end the call|good ?bye|bye(?: bye)?|bye now)"
    r"(?:\s+(?:the|this)\s+(?:meeting|call))?(?:\s+now)?(?:[\s,]+(?:thanks|thank you))?\W*$", re.I)

# Meet's own phone prompts, heard on the dial-in line: not the meeting.
IVR_RE = re.compile(
    r"meeting pin|pound (?:key|sign)|followed by pound|star six|press star|let you in|"
    r"you(?:'re| are) the first|joined the meeting|welcome to google meet|"
    r"this meeting has ended|no one (?:else )?is here|to mute or unmute", re.I)

# What speech to text makes of "Odysseus".
_VARIANTS = {"odysseus": ["odysseus", "odyssey", "odysseys", "odysseas", "odyseus", "odessius", "odisseus"]}


def wake_patterns(words: List[str]) -> re.Pattern:
    alts: List[str] = []
    for w in words:
        for v in _VARIANTS.get(w, [w]):
            v = re.escape(v).replace(r"\ ", r"\s+")
            if v not in alts:
                alts.append(v)
    return re.compile(r"\b(?P<name>" + "|".join(alts) + r")\b", re.I)


_LEAD = re.compile(r"\b(?:hey|hi|hello|ok|okay|so|and|um|uh)\b", re.I)


def find_wake(text: str, pattern: re.Pattern) -> Optional[str]:
    """None if the utterance does not call the agent. Otherwise what it was
    asked: the words after the wake word when it starts the sentence, the
    rest when it is said to it at the end or between commas ("what do you
    think, Odysseus?"), or "" when it was only called. A name in the middle
    of a sentence ("we read the Odyssey") is talk about it, not to it."""
    text = text or ""
    for m in pattern.finditer(text):
        before, after = text[: m.start("name")], text[m.end("name"):]
        if not re.search(r"\w", _LEAD.sub("", before)):
            rest = after.strip(" ,.!?:;-")
            return after.lstrip(" ,.:;-").strip() if rest else ""
        at_end = not re.search(r"\w", after)
        commas = before.rstrip().endswith(",") and after.lstrip().startswith(",")
        if at_end or commas:
            tail = after.strip() if at_end else " " + after.lstrip(" ,").strip()
            rest = (before.rstrip(" ,") + tail).strip()
            return rest if re.search(r"\w", rest) else ""
    return None


def _fmt_clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


ACTIVE: Dict[str, "Meeting"] = {}
RECENT: Deque["Meeting"] = deque(maxlen=10)
_TASKS: set = set()


def _bg(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t


def for_owner(owner: Optional[str]) -> List["Meeting"]:
    live = [m for m in ACTIVE.values() if m.owner == owner]
    done = [m for m in RECENT if m.owner == owner and m.id not in ACTIVE]
    return live + done


def get(meeting_id: str) -> Optional["Meeting"]:
    return ACTIVE.get(meeting_id)


class Meeting:
    def __init__(self, owner: Optional[str], cfg: Dict, url: str, *, mode: str = "", via: str = "",
                 title: str = "", dial_in: Optional[Dict[str, str]] = None, owner_name: str = "",
                 stt=None, tts=None, reply=None, browser_factory: Optional[Callable] = None,
                 dialer: Optional[Callable] = None):
        self.id = secrets.token_hex(6)
        self.owner = owner
        self.cfg = meet_config.view(cfg)
        self.url = url
        self.mode = mode if mode in meet_config.MODES else self.cfg["mode"]
        self.via = via if via in meet_config.VIAS else self.cfg["via"]
        self.title = title
        self.dial_in = dial_in or {}
        self.owner_name = owner_name or str(cfg.get("owner_name") or "") or (owner or "")
        self.stt = stt
        self.tts = tts
        self._reply_impl = reply or agent.reply
        self._browser_factory = browser_factory
        self._dialer = dialer
        self.sid = ""
        self.state = "starting"
        self.error = ""
        self.error_code = ""
        self.reason = ""
        self.started = time.time()
        self.joined_at = 0.0
        self.ended_at = 0.0
        self.last_speech = time.monotonic()
        self.transcript: List[Tuple[float, str]] = []
        self._since_answer: List[Tuple[float, str]] = []
        self._armed_until = 0.0
        self._announced = False
        self.call: Optional[call_mod.PhoneCall] = None
        self.browser = None
        self.phone_transport = None
        self.call_sid = ""
        self.summary_posted = False
        self._stop = asyncio.Event()
        self._finished = False
        self._wake = wake_patterns(self.cfg["wake_words"])

    # ── for the UI ──

    def public(self) -> Dict:
        return {
            "id": self.id, "sid": self.sid, "url": self.url, "title": self.title, "mode": self.mode,
            "via": self.via, "state": self.state, "error": self.error, "error_code": self.error_code,
            "reason": self.reason,
            "started": self.started, "joined_at": self.joined_at, "ended_at": self.ended_at,
            "lines": len(self.transcript),
            "transcript_tail": [{"at": t, "text": x} for t, x in self.transcript[-8:]],
            "speaking": bool(self.call and self.call.state == "speaking"),
            "summary_posted": self.summary_posted,
        }

    def _set(self, state: str) -> None:
        if self.state != state:
            self.state = state
            logger.info("[meet] %s: %s", self.id, state)

    # ── words ──

    def announcement(self) -> str:
        return meet_config.announcement(self.cfg, self.mode, self.owner_name)

    def _note(self) -> str:
        tmpl = MEET_NOTE if self.mode == "assistant" else TALK_NOTE
        return tmpl.format(name=self.cfg["display_name"], owner=self.owner_name or "the user")

    def _reply(self, sid: str, text: str):
        return self._reply_impl(sid, text, source=SOURCE, note=self._note())

    def _line(self, text: str) -> None:
        now = time.time()
        self.transcript.append((now, text))
        self._since_answer.append((now, text))
        if self.mode == "assistant" and self.sid:
            agent.note(self.sid, f"{_fmt_clock(now)}  {text}", source=SOURCE, transcript=True)

    def _compose(self, question: str) -> str:
        before = self._since_answer[:-1]
        lines: List[str] = []
        size = 0
        for t, x in reversed(before):
            line = f"[{_fmt_clock(t)}] {x}"
            size += len(line) + 1
            if size > MAX_CONTEXT_CHARS:
                break
            lines.append(line)
        lines.reverse()
        head = "Google Meet" + (f" ({self.title})" if self.title else "")
        if lines:
            ctx = "\n".join(lines)
            return (f"[{head}. What was said since you last spoke, transcribed, speakers not named:]\n"
                    f"{ctx}\n\n[Then someone asked you:] {question}")
        return f"[{head}. Someone asked you:] {question}"

    def respond(self, text: str) -> Optional[str]:
        """Every transcribed utterance: the message to answer it with, or None."""
        text = (text or "").strip()
        if not text:
            return None
        if self.via == "phone":
            if IVR_RE.search(text):
                if re.search(r"joined the meeting|you(?:'re| are) the first", text, re.I):
                    self._announce()
                return None
            self._announce()
        self.last_speech = time.monotonic()
        self._line(text)
        if self.mode == "talk":
            self._since_answer = []
            return text
        q = find_wake(text, self._wake)
        now = time.monotonic()
        if q is None:
            if self._armed_until > now:
                self._armed_until = 0.0
                q = text
            else:
                return None
        elif q == "":
            self._armed_until = now + ARMED_S
            if self.call:
                self.call.say("Yes?")
            return None
        self._armed_until = 0.0
        if LEAVE_RE.match(q):
            return q
        msg = self._compose(q)
        self._since_answer = []
        return msg

    def _on_event(self, name: str, data: dict) -> None:
        if name == "speech-start":
            self.last_speech = time.monotonic()
        elif name == "ended" and not self._stop.is_set():
            self.reason = self.reason or "the call ended"
            self._stop.set()

    def _announce(self) -> None:
        if self._announced or not self.call:
            return
        self._announced = True
        if self.cfg["announce"]:
            self.call.say(self.announcement(), hold=True)

    # ── the shared call loop ──

    def _make_call(self, transport, fmt: codec.AudioFormat) -> call_mod.PhoneCall:
        kw = {}
        if self.stt:
            kw["stt"] = self.stt
        if self.tts:
            kw["tts"] = self.tts
        return call_mod.PhoneCall(
            transport, self.sid, reply=self._reply, barge_in=(self.mode == "talk"),
            max_call_s=self.cfg["max_minutes"] * 60, on_event=self._on_event, fmt=fmt,
            respond=self.respond, bye=LEAVE_RE, **kw)

    def _feed(self, pcm: bytes) -> None:
        if self.call and not self.call.ended:
            self.call.feed(pcm)

    def _mark(self, name: str) -> None:
        if self.call:
            self.call.played(name)

    # ── starting ──

    def chat_key(self) -> str:
        """What makes two joins the same meeting: the Meet code (or the rest
        of a lookup link), else the dial-in number and PIN."""
        from src.meet import links
        if self.url:
            return "meet:" + (links.meeting_code(self.url) or self.url.rsplit("/", 1)[-1])
        if self.dial_in.get("number"):
            return f"phone:{self.dial_in.get('number')}:{self.dial_in.get('pin', '')}"
        return ""

    def _earlier_chat(self, key: str) -> str:
        """The chat of the last join of this meeting, if it is still there."""
        sid = meet_config.chat_for(self.owner, key)
        if not sid:
            return ""
        try:
            sess = agent._session_manager().get_session(sid)
        except Exception:
            return ""
        if not sess or (self.owner and getattr(sess, "owner", None) not in (None, self.owner)):
            return ""
        return sid

    def _open_chat(self, is_admin: bool) -> None:
        code = self.url.rsplit("/", 1)[-1] if self.url else (self.dial_in.get("number") or "")
        key = self.chat_key()
        how = "by phone" if self.via == "phone" else f"as {self.cfg['display_name']}"
        mode = "meeting assistant: transcribing, answers when called by name" if self.mode == "assistant" \
            else "talk with me: answers every turn"
        where = self.url or self.dial_in.get("number", "")
        again = self._earlier_chat(key)
        if again:
            # The same meeting again (a retry, or back after a break): carry
            # on in its chat instead of starting a pile of new ones.
            self.sid = again
            agent.note(self.sid, f"Joining again: {where} {how} ({mode}).", source=SOURCE)
            return
        stamp = datetime.now().strftime("%H:%M")
        name = f"Meet: {self.title} {stamp}" if self.title else f"Google Meet {stamp} ({code})"
        self.sid, _ = agent.new_call_chat(self.owner, self.cfg, code, is_admin=is_admin, name=name[:120])
        try:
            meet_config.remember_chat(self.owner, key, self.sid)
        except Exception as e:
            logger.debug("[meet] could not remember the chat: %s", type(e).__name__)
        agent.note(self.sid, f"Joining {where} {how} ({mode}).", source=SOURCE)

    async def start(self, is_admin: bool = False) -> None:
        """Make the chat and start joining in the background."""
        self._open_chat(is_admin)
        ACTIVE[self.id] = self
        RECENT.append(self)
        if self.via == "phone":
            _bg(self._run_phone())
        else:
            _bg(self._run_browser())

    # ── by browser ──

    async def _run_browser(self) -> None:
        from src.meet import browser as browser_mod
        factory = self._browser_factory or (lambda on_audio, on_mark: browser_mod.MeetBrowser(on_audio, on_mark))
        reason = ""
        try:
            self._set("joining")
            self.browser = factory(self._feed, self._mark)
            await self.browser.open(self.url, self.cfg["display_name"], self.cfg["join_as"])
            opened = time.monotonic()
            clicked = 0.0
            lobby_since = 0.0
            alone_since = 0.0
            while not self._stop.is_set():
                st = (await self.browser.status()).get("state", "loading")
                now = time.monotonic()
                if st == "prejoin":
                    if not clicked or now - clicked > 15:
                        await self.browser.join(self.cfg["display_name"])
                        clicked = now
                elif st == "lobby":
                    self._set("lobby")
                    lobby_since = lobby_since or now
                    if now - lobby_since > self.cfg["lobby_minutes"] * 60:
                        reason = "nobody let it in"
                        break
                elif st in ("in", "alone"):
                    if not self.call:
                        await self._joined_browser()
                    if st == "alone":
                        alone_since = alone_since or now
                        if now - alone_since > ALONE_S:
                            reason = "everyone else left"
                            break
                    else:
                        alone_since = 0.0
                elif st == "unsafe_browser":
                    self.error_code = "unsafe_browser"
                    self.error = UNSAFE_BROWSER_DENIED
                    break
                elif st == "denied":
                    if self.cfg["join_as"] == "guest" and not lobby_since and now - opened < GUEST_DENIED_S:
                        # Turned away at once, before any lobby. Seen on
                        # 2026-10-01 even with the meeting open to anyone: at
                        # "Join now" Google refuses a signed-out browser it
                        # takes for automation.
                        self.error_code = "guest_denied"
                        self.error = GUEST_DENIED
                    else:
                        self.error = "Meet did not let it in (denied, removed, or the link is not valid)."
                    break
                elif st == "ended":
                    reason = "the meeting ended"
                    break
                elif st == "signin":
                    self.error = ("This meeting only lets in signed-in Google accounts. Sign the cloud "
                                  "browser in to a Google account and pick \"Signed in\" under Join as.")
                    break
                elif not clicked and now - opened > LOAD_S:
                    self.error = "Meet's page did not get to the join screen."
                    break
                if self.call and self.joined_at:
                    if time.monotonic() - self.last_speech > self.cfg["idle_minutes"] * 60:
                        reason = f"nobody spoke for {self.cfg['idle_minutes']} minutes"
                        break
                try:
                    await asyncio.wait_for(self._stop.wait(), POLL_S)
                except asyncio.TimeoutError:
                    pass
        except Exception as e:
            logger.warning("[meet] %s: %s", self.id, e)
            self.error = str(e)[:300] or type(e).__name__
        await self._finish(reason or self.reason or ("it failed" if self.error else "asked to leave"))

    async def _joined_browser(self) -> None:
        from src.meet.browser import BrowserTransport
        self._set("in")
        self.joined_at = time.time()
        self.last_speech = time.monotonic()
        self.call = self._make_call(BrowserTransport(self.browser, self._asked_to_leave), codec.PCM_16K)
        await self.call.start()
        self._announce()
        if self.cfg["chat_notice"]:
            await self.browser.send_chat(self.announcement())
        agent.note(self.sid, "In the meeting.", source=SOURCE)

    async def _asked_to_leave(self) -> None:
        self.reason = self.reason or "asked to leave"
        self._stop.set()

    # ── by phone ──

    async def _run_phone(self) -> None:
        from src.meet import dialin
        reason = ""
        try:
            self._set("dialing")
            dial = self._dialer or dialin.dial
            self.call_sid = await dial(self)
            deadline = time.monotonic() + DIAL_CONNECT_S
            while not self._stop.is_set():
                now = time.monotonic()
                if not self.call and now > deadline:
                    self.error = "The call to the meeting did not connect."
                    break
                if self.call and now - self.last_speech > self.cfg["idle_minutes"] * 60:
                    reason = f"nobody spoke for {self.cfg['idle_minutes']} minutes"
                    break
                if self.call and not self._announced and time.time() - self.joined_at > PHONE_ANNOUNCE_S:
                    # Nobody has said anything yet: say who is here anyway.
                    self._announce()
                try:
                    await asyncio.wait_for(self._stop.wait(), POLL_S)
                except asyncio.TimeoutError:
                    pass
        except Exception as e:
            logger.warning("[meet] %s: dial-in failed: %s", self.id, e)
            self.error = str(e)[:300] or type(e).__name__
        await self._finish(reason or self.reason or ("it failed" if self.error else "the call ended"))

    async def attach_phone(self, transport) -> call_mod.PhoneCall:
        """The dial-in call's media stream is up (routes/telephony_routes.py)."""
        self.phone_transport = transport
        self._set("in")
        self.joined_at = time.time()
        self.last_speech = time.monotonic()
        self.call = self._make_call(transport, codec.ULAW_8K)
        await self.call.start()
        return self.call

    def phone_closed(self) -> None:
        self.reason = self.reason or "the call ended"
        self._stop.set()

    # ── leaving ──

    async def leave(self, reason: str = "left from Odysseus") -> None:
        self.reason = self.reason or reason
        self._stop.set()

    async def _finish(self, reason: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop.set()
        self.reason = self.reason or reason
        self._set("leaving")
        if self.call and not self.call.ended:
            await self.call.end(self.reason)
        if self.browser:
            try:
                await self.browser.leave()
            finally:
                await self.browser.close()
        if self.phone_transport:
            try:
                await self.phone_transport.hangup()
            except Exception:
                pass
        self.ended_at = time.time()
        mins = round((self.ended_at - (self.joined_at or self.started)) / 60)
        if self.joined_at:
            msg = f"Left the meeting: {self.reason}. {mins} min, {len(self.transcript)} line(s) transcribed."
        else:
            msg = f"Did not get into the meeting: {self.error or self.reason}."
        agent.note(self.sid, msg, source=SOURCE)
        ACTIVE.pop(self.id, None)
        if self.mode == "assistant" and self.cfg["summary"] and self.transcript:
            self._set("summarizing")
            await self._summarize(mins)
        self._set("failed" if self.error and not self.joined_at else "ended")
        logger.info("[meet] %s ended: %s", self.id, self.reason)

    async def _summarize(self, mins: int) -> None:
        lines = [f"[{_fmt_clock(t)}] {x}" for t, x in self.transcript]
        text = "\n".join(lines)
        if len(text) > MAX_SUMMARY_CHARS:
            half = MAX_SUMMARY_CHARS // 2
            text = text[:half] + "\n[... the middle of the transcript is left out ...]\n" + text[-half:]
        head = "Google Meet" + (f" \"{self.title}\"" if self.title else "")
        prompt = (f"[{head} has ended after about {mins} minute(s). Its transcript, from speech recognition, "
                  f"speakers not named:]\n\n{text}\n\n"
                  "Write the meeting notes: a two or three sentence overview, the decisions made, and the "
                  "action items as a checklist (with who and by when, where that was said). If the "
                  "transcript is too thin to tell, say so.")
        note = SUMMARY_NOTE.format(owner=self.owner_name or "the user")
        try:
            stream = self._reply_impl(self.sid, prompt, source=SOURCE, note=note, voice_call=False)
            try:
                async for kind, _piece in stream:
                    if kind == "error":
                        logger.info("[meet] %s: the summary run failed", self.id)
            finally:
                await stream.aclose()
            self.summary_posted = True
        except Exception as e:
            logger.warning("[meet] %s: no summary: %s", self.id, type(e).__name__)
            return
        try:
            from routes.sms_routes import _push_owner
            await _push_owner(self.owner, f"Meeting notes are ready: {self.title or self.url}"[:300])
        except Exception:
            pass


def can_start(owner: Optional[str]) -> Optional[str]:
    """Why a new meeting cannot start now, or None."""
    if any(m.owner == owner for m in ACTIVE.values()):
        return "Odysseus is already in a meeting for you. Leave that one first."
    if len(ACTIVE) >= MAX_ACTIVE:
        return "Odysseus is in as many meetings as this server takes."
    return None
