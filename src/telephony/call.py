"""One phone call with the agent, whatever carries the audio.

A transport (twilio.MediaStreamTransport, or the simulator's) hands this
the caller's audio as 8 kHz mu-law and plays what it is given. In between,
the same turn loop the in-app call runs:

    listening   the caller talks; a pause of SILENCE_MS ends the turn
    thinking    the words go to speech to text, then into the call's chat
    speaking    the reply is read out sentence by sentence while it streams

Talking over the agent (barge-in) stops its voice at once, both what is
queued here and what the provider has buffered ("clear"); the reply itself
carries on into the chat, as in the browser. The next thing said is a new
turn, which stops that reply if it is still going.

Speech to text and text to speech are whatever Settings > AI Defaults > Voice
call picked ("Hears with", "Speaks with"), called through the same services
the in-app call's /api/stt and /api/tts use. The browser engines cannot run
on a phone line; the webhook checks for that before a call is connected.
"""

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable, List, Optional, Protocol

import numpy as np

from src.telephony import codec, speech

logger = logging.getLogger(__name__)

MAX_CALL_S = 30 * 60
GOODBYE = "Okay, bye."
_BYE = re.compile(r"^\W*(?:(?:ok(?:ay)?|alright|thanks|thank you)[\s,]+)?(?:good ?bye|bye(?: bye)?|bye now|hang up|end (?:the )?call)\W*$", re.I)


class Transport(Protocol):
    async def play(self, ulaw: bytes, mark: str) -> None: ...
    async def clear(self) -> None: ...
    async def hangup(self) -> None: ...


SttFn = Callable[[bytes], Optional[str]]
TtsFn = Callable[[str], Optional[bytes]]


def default_stt(wav: bytes) -> Optional[str]:
    from services.stt import get_stt_service
    return get_stt_service().transcribe(wav)


def default_tts(text: str) -> Optional[bytes]:
    from services.tts import get_tts_service
    return get_tts_service().synthesize(text, response_format="wav")


def engines_ready() -> List[str]:
    """Why this server cannot hold a call with its own speech engines, as
    sentences (empty when it can)."""
    problems = []
    try:
        from services.stt import get_stt_service
        stats = get_stt_service().get_stats()
        p = str(stats.get("provider") or "disabled")
        if p in ("disabled", "browser") or not stats.get("available"):
            problems.append("Speech to text: pick a local or API engine for \"Hears with\" "
                            "(Settings > AI Defaults > Voice call). The browser engine cannot hear a phone line.")
    except Exception:
        problems.append("Speech to text is not available.")
    try:
        from services.tts import get_tts_service
        stats = get_tts_service().get_stats()
        p = str(stats.get("provider") or "disabled")
        if p in ("disabled", "browser") or not stats.get("available"):
            problems.append("Text to speech: pick Kokoro or an API engine for \"Speaks with\" "
                            "(Settings > AI Defaults > Voice call). The browser voice cannot speak on a phone line.")
    except Exception:
        problems.append("Text to speech is not available.")
    return problems


class PhoneCall:
    def __init__(self, transport: Transport, sid: str, *, greeting: str = "",
                 stt: Optional[SttFn] = None, tts: Optional[TtsFn] = None,
                 reply: Optional[Callable] = None, barge_in: bool = True,
                 silence_ms: int = speech.SILENCE_MS, max_call_s: float = MAX_CALL_S,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        from src.telephony import agent
        self.t = transport
        self.sid = sid
        self.greeting = greeting
        self.stt = stt or default_stt
        self.tts = tts or default_tts
        self.reply = reply or agent.reply
        self.barge_in = barge_in
        self.max_call_s = max_call_s
        self.on_event = on_event
        self.vad = speech.Vad(silence_ms=silence_ms)
        self.state = "listening"
        self.ended = False
        self.started = time.monotonic()
        self.turns = 0
        self._gen = 0                      # bumps on every interrupt
        self._turn_task: Optional[asyncio.Task] = None
        self._speak_q: "asyncio.Queue" = asyncio.Queue()
        self._speaker: Optional[asyncio.Task] = None
        self._marks: set = set()           # sent to the provider, not played yet
        self._mark_n = 0
        self._preroll: List[np.ndarray] = []
        self._preroll_ms = 0.0
        self._capturing = False
        self._frames: List[np.ndarray] = []
        self._capture_ms = 0.0
        self._quiet_until = 0.0
        self._reply_done = True
        self._hangup_after = False

    # ── events, for logs and the simulator ──

    def _emit(self, name: str, **data) -> None:
        if self.on_event:
            try:
                self.on_event(name, data)
            except Exception:
                pass

    def _set(self, state: str) -> None:
        if self.state != state:
            self.state = state
            self._emit("state", state=state)

    # ── lifecycle ──

    async def start(self) -> None:
        self._speaker = asyncio.create_task(self._speak_loop())
        if self.greeting:
            self._reply_done = False
            self._say(self.greeting, self._gen)
            self._reply_done = True

    async def end(self, reason: str = "hangup") -> None:
        if self.ended:
            return
        self.ended = True
        self._gen += 1
        for task in (self._turn_task, self._speaker):
            if task and not task.done():
                task.cancel()
        self._emit("ended", reason=reason, turns=self.turns, seconds=round(time.monotonic() - self.started))

    # ── audio in ──

    def _hearing(self) -> bool:
        if self.ended:
            return False
        if self.state == "listening":
            return True
        return self.barge_in

    def feed(self, ulaw: bytes) -> None:
        """Caller audio, any length; handled in 20 ms frames."""
        if self.ended:
            return
        if time.monotonic() - self.started > self.max_call_s and not self._hangup_after:
            self._hangup_after = True
            self.interrupt("time limit")
            self._reply_done = False
            self._say("This call has reached its time limit. Goodbye.", self._gen)
            self._reply_done = True
            return
        for frame in codec.frames(ulaw):
            self._frame(codec.ulaw_to_pcm16(frame))

    def _frame(self, pcm: np.ndarray) -> None:
        dt = len(pcm) * 1000.0 / codec.RATE
        level = codec.rms(pcm)
        self._preroll.append(pcm)
        self._preroll_ms += dt
        while self._preroll_ms - dt > speech.PREROLL_MS and len(self._preroll) > 1:
            self._preroll.pop(0)
            self._preroll_ms -= dt
        if self._capturing:
            self._frames.append(pcm)
            self._capture_ms += dt
        vad = self.vad
        if not self._hearing() or self._hangup_after or time.monotonic() < self._quiet_until:
            if vad.speaking or vad._above:
                vad.reset(False)
            if not vad.calibrated:
                vad.push(level, dt)
            return
        barging = self.state != "listening"
        vad.onset_ms = 250 if barging else 120
        vad.ratio = 4 if barging else 3
        ev = vad.push(level, dt)
        if ev == "calibrated":
            self._emit("calibrated", noise=round(vad.noise, 4))
        elif ev == "start":
            if barging:
                self.interrupt("barge-in")
            self._capturing = True
            self._frames = list(self._preroll)
            self._capture_ms = self._preroll_ms
            self._emit("speech-start")
        elif ev == "end" or (self._capturing and self._capture_ms > speech.MAX_UTTERANCE_MS):
            if ev != "end":
                vad.reset(False)
            frames, speech_ms = self._frames, vad.speech_ms
            self._capturing = False
            self._frames = []
            self._emit("speech-end", ms=round(speech_ms))
            if speech_ms >= speech.MIN_SPEECH_MS and frames:
                self._start_turn(np.concatenate(frames))

    def dtmf(self, digit: str) -> None:
        """* stops the agent talking, like the in-app Interrupt button."""
        if digit == "*":
            self.interrupt("keypad")

    # ── playback marks ──

    def played(self, mark: str) -> None:
        """The provider reached a mark: everything before it was heard."""
        self._marks.discard(mark)
        self._maybe_listen()

    def _maybe_listen(self) -> None:
        if self.ended or self._marks or not self._speak_q.empty() or not self._reply_done:
            return
        if self._hangup_after:
            asyncio.create_task(self._hang_up())
            return
        if self.state != "listening":
            self._quiet_until = time.monotonic() + speech.ECHO_TAIL_MS / 1000
            self.vad.reset(False)
            self._set("listening")

    async def _hang_up(self) -> None:
        try:
            await self.t.hangup()
        finally:
            await self.end("goodbye")

    # ── interrupting ──

    def interrupt(self, reason: str) -> None:
        """Stop the voice now. The reply keeps going into the chat."""
        if self.ended:
            return
        self._gen += 1
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        while not self._speak_q.empty():
            try:
                self._speak_q.get_nowait()
            except asyncio.QueueEmpty:
                break
        had_audio = bool(self._marks)
        self._marks.clear()
        self._reply_done = True
        if had_audio or self.state == "speaking":
            asyncio.create_task(self._safe(self.t.clear()))
        self._emit("interrupt", reason=reason)
        self._set("listening")

    @staticmethod
    async def _safe(coro: Awaitable) -> None:
        try:
            await coro
        except Exception as e:
            logger.debug("[phone] transport call failed: %s", type(e).__name__)

    # ── a turn ──

    def _start_turn(self, pcm: np.ndarray) -> None:
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        gen = self._gen
        self._turn_task = asyncio.create_task(self._turn(pcm, gen))

    async def _turn(self, pcm: np.ndarray, gen: int) -> None:
        self._set("thinking")
        self._reply_done = False
        try:
            wav = codec.for_stt(pcm)
            try:
                text = await asyncio.to_thread(self.stt, wav)
            except Exception as e:
                logger.warning("[phone] speech to text failed: %s", type(e).__name__)
                text = None
            if gen != self._gen or self.ended:
                return
            text = (text or "").strip()
            if not text:
                self._reply_done = True
                self._emit("heard", text="")
                self._maybe_listen()
                return
            self.turns += 1
            self._emit("heard", text=text)
            if _BYE.match(text):
                self._hangup_after = True
                self._say(GOODBYE, gen)
                self._reply_done = True
                self._maybe_listen()
                return
            raw = ""
            idx = 0
            failed = ""
            stream = self.reply(self.sid, text)
            try:
                async for kind, piece in stream:
                    if gen != self._gen or self.ended:
                        return
                    if kind == "delta":
                        raw += piece
                        sentences, idx = speech.take_sentences(speech.speakable_text(raw), idx)
                        for s in sentences:
                            self._say(s, gen)
                    elif kind == "error":
                        failed = piece
            finally:
                # Stop listening to the run (it carries on into the chat).
                await stream.aclose()
            if gen != self._gen or self.ended:
                return
            sentences, idx = speech.take_sentences(speech.speakable_text(raw), idx, final=True)
            for s in sentences:
                self._say(s, gen)
            if not raw.strip():
                self._say("Sorry, the agent did not answer. Check the chat for details."
                          if failed else "The reply has no spoken text. It is in the chat.", gen)
            self._emit("replied", chars=len(raw))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("[phone] turn failed: %s", type(e).__name__)
            if gen == self._gen and not self.ended:
                self._say("Sorry, something went wrong on the server.", gen)
        finally:
            if gen == self._gen:
                self._reply_done = True
                self._maybe_listen()

    # ── speaking ──

    def _say(self, text: str, gen: int) -> None:
        self._speak_q.put_nowait((gen, text))

    async def _speak_loop(self) -> None:
        while not self.ended:
            gen, text = await self._speak_q.get()
            if gen != self._gen:
                continue
            try:
                audio = await asyncio.to_thread(self.tts, text)
                ulaw = codec.to_phone(audio) if audio else b""
            except Exception as e:
                logger.warning("[phone] text to speech failed: %s", e)
                ulaw = b""
            if gen != self._gen or self.ended:
                continue
            if not ulaw:
                self._maybe_listen()
                continue
            self._mark_n += 1
            mark = f"s{self._mark_n}"
            self._marks.add(mark)
            self._set("speaking")
            self._emit("speak", text=text, bytes=len(ulaw))
            try:
                await self.t.play(ulaw, mark)
            except Exception as e:
                logger.info("[phone] playback failed: %s", type(e).__name__)
                self._marks.discard(mark)
                self._maybe_listen()
