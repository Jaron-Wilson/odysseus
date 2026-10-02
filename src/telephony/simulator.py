"""Twilio's side of a phone call, for testing without an account.

Speaks exactly what Twilio would: a signed form POST to the voice webhook
(X-Twilio-Signature made with the auth token), then the Media Streams
WebSocket protocol named in the TwiML (connected, start with the stream's
custom parameters, 20 ms media frames of 8 kHz mu-law, marks echoed back
once "played", dtmf, stop), and it records what the server sends back
(media, mark, clear).

Used by tests/test_phone_calls.py against a real server on a local port,
and by scripts/phone_call_simulator.py by hand:

    python scripts/phone_call_simulator.py --base http://127.0.0.1:7080 \\
        --account-sid AC... --auth-token ... --to +1555... --from +1555... \\
        --say hello.wav --out reply.wav
"""

import asyncio
import base64
import json
import secrets
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from src.telephony import codec, twilio


@dataclass
class Twiml:
    verbs: List[str]
    stream_url: str = ""
    relay_url: str = ""
    params: Dict[str, str] = field(default_factory=dict)
    say: List[str] = field(default_factory=list)
    gather_action: str = ""
    redirect: str = ""
    digits: str = ""
    raw: str = ""


def parse_twiml(xml: str) -> Twiml:
    root = ET.fromstring(xml)
    out = Twiml(verbs=[], raw=xml)
    for el in root.iter():
        if el is root:
            continue
        out.verbs.append(el.tag)
        if el.tag == "Stream":
            out.stream_url = el.get("url", "")
        elif el.tag == "ConversationRelay":
            out.relay_url = el.get("url", "")
        elif el.tag == "Parameter":
            out.params[el.get("name", "")] = el.get("value", "")
        elif el.tag == "Say":
            out.say.append(el.text or "")
        elif el.tag == "Gather":
            out.gather_action = el.get("action", "")
        elif el.tag == "Redirect":
            out.redirect = (el.text or "").strip()
        elif el.tag == "Play" and el.get("digits"):
            out.digits += el.get("digits", "")
    return out


def tone(seconds: float, freq: float = 300.0, level: float = 0.3, rate: int = codec.RATE) -> np.ndarray:
    """Something speech-like enough for the endpointer: a voiced buzz with
    a syllable-rate wobble."""
    t = np.arange(int(seconds * rate)) / rate
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)
    x = level * env * (np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(2 * np.pi * 2 * freq * t))
    return np.clip(x * 32767, -32768, 32767).astype(np.int16)


def silence(seconds: float, rate: int = codec.RATE) -> np.ndarray:
    # A little line noise, like a real call's comfort noise.
    rng = np.random.default_rng(7)
    return (rng.normal(0, 30, int(seconds * rate))).astype(np.int16)


class FakeTwilio:
    """One simulated account with one agent number."""

    def __init__(self, base_url: str, account_sid: str, auth_token: str, agent_number: str,
                 public_url: str = ""):
        self.base = base_url.rstrip("/")
        self.public = (public_url or base_url).rstrip("/")
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.agent_number = agent_number

    def _to_local(self, url: str) -> str:
        """A public URL from the TwiML, aimed at the local server instead
        (the public host is the Funnel; here we talk to the port)."""
        p, b = urlsplit(url), urlsplit(self.base)
        scheme = p.scheme
        if scheme in ("wss", "ws"):
            scheme = "wss" if b.scheme == "https" else "ws"
        else:
            scheme = b.scheme
        return urlunsplit((scheme, b.netloc, p.path, p.query, ""))

    def webhook(self, client, path_or_url: str, params: Dict[str, str], *, sign_with: str = "",
                bad_signature: bool = False):
        """POST a signed webhook with any client that has .post(url, data=,
        headers=) (httpx, or Starlette's TestClient). Returns the response."""
        public = path_or_url if path_or_url.startswith("http") else self.public + path_or_url
        params = {"AccountSid": self.account_sid, **params}
        sig = twilio.signature(public, params.items(), sign_with or self.auth_token)
        if bad_signature:
            sig = sig[:-4] + "AAA="
        return client.post(self._to_local(public), data=params, headers={"X-Twilio-Signature": sig})

    def incoming(self, client, caller: str, call_sid: str = "", path: str = "/api/telephony/twilio/voice",
                 **kw):
        call_sid = call_sid or "CA" + secrets.token_hex(16)
        params = {"CallSid": call_sid, "From": caller, "To": self.agent_number, "Direction": "inbound",
                  "CallStatus": "ringing", "ApiVersion": "2010-04-01"}
        r = self.webhook(client, path, params, **kw)
        return call_sid, r

    def answered(self, client, url: str, to: str, call_sid: str = "", **kw):
        """An outbound call the agent placed (the REST call's Url) was
        answered by `to`: Twilio asks that URL what to do."""
        call_sid = call_sid or "CA" + secrets.token_hex(16)
        params = {"CallSid": call_sid, "From": self.agent_number, "To": to, "Direction": "outbound-api",
                  "CallStatus": "in-progress", "ApiVersion": "2010-04-01"}
        return call_sid, self.webhook(client, url, params, **kw)


@dataclass
class StreamResult:
    audio: bytearray = field(default_factory=bytearray)   # everything the server played
    clips: List[int] = field(default_factory=list)       # bytes per mark (one per sentence)
    clears: int = 0
    marks: List[str] = field(default_factory=list)
    closed_by_server: bool = False
    events: List[Tuple[float, str]] = field(default_factory=list)


async def media_call(ws_url: str, token: str, call_sid: str, script: List[Tuple[str, object]],
                     *, pace: float = 0.25, timeout: float = 20.0) -> StreamResult:
    """Run a Media Streams call over a real WebSocket. `script` is a list of
    steps: ("audio", int16 array at 8 kHz) streams it in 20 ms frames,
    ("wait_reply", seconds) waits until the server has played something and
    gone quiet, ("barge_in", int16 array) talks while the reply plays,
    ("dtmf", "*"), ("sleep", seconds). `pace` is how fast frames go out
    relative to real time (0.25: four times as fast)."""
    import websockets

    res = StreamResult()
    stream_sid = "MZ" + secrets.token_hex(16)
    t0 = time.monotonic()
    pending: List[Tuple[str, int]] = []      # marks not echoed yet, with their clip size
    clip = 0
    last_audio = [0.0]
    hold_marks = [False]

    async with websockets.connect(ws_url, open_timeout=timeout) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({
            "event": "start", "sequenceNumber": "1", "streamSid": stream_sid,
            "start": {"accountSid": "AC" + "0" * 32, "streamSid": stream_sid, "callSid": call_sid,
                      "tracks": ["inbound"], "customParameters": {"token": token},
                      "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}},
        }))

        async def receiver():
            nonlocal clip
            try:
                async for raw in ws:
                    m = json.loads(raw)
                    ev = m.get("event")
                    res.events.append((round(time.monotonic() - t0, 3), ev))
                    if ev == "media":
                        b = base64.b64decode(m["media"]["payload"])
                        res.audio.extend(b)
                        clip += len(b)
                        last_audio[0] = time.monotonic()
                    elif ev == "mark":
                        name = m["mark"]["name"]
                        res.clips.append(clip)
                        clip = 0
                        pending.append((name, res.clips[-1]))
                    elif ev == "clear":
                        res.clears += 1
                        pending.clear()
            except websockets.ConnectionClosed:
                pass
            res.closed_by_server = True

        async def player():
            # Twilio sends a mark back once the audio before it has played.
            while True:
                await asyncio.sleep(0.02)
                if pending and not hold_marks[0]:
                    name, size = pending.pop(0)
                    await asyncio.sleep(size / codec.RATE * pace)
                    if hold_marks[0]:
                        pending.insert(0, (name, size))
                        continue
                    try:
                        await ws.send(json.dumps({"event": "mark", "streamSid": stream_sid,
                                                  "mark": {"name": name}}))
                    except websockets.ConnectionClosed:
                        return
                    res.marks.append(name)

        async def send_audio(samples: np.ndarray):
            ulaw = codec.pcm16_to_ulaw(samples)
            for i, fr in enumerate(codec.frames(ulaw)):
                await ws.send(json.dumps({"event": "media", "streamSid": stream_sid,
                                          "media": {"track": "inbound", "chunk": str(i),
                                                    "timestamp": str(i * 20),
                                                    "payload": base64.b64encode(fr).decode()}}))
                await asyncio.sleep(0.02 * pace)

        rx = asyncio.create_task(receiver())
        pl = asyncio.create_task(player())
        try:
            for kind, arg in script:
                if res.closed_by_server:
                    break
                if kind == "audio":
                    await send_audio(arg)
                elif kind == "sleep":
                    await asyncio.sleep(float(arg))
                elif kind == "dtmf":
                    await ws.send(json.dumps({"event": "dtmf", "streamSid": stream_sid,
                                              "dtmf": {"track": "inbound_track", "digit": str(arg)}}))
                elif kind == "wait_reply":
                    deadline = time.monotonic() + float(arg)
                    seen = len(res.audio)
                    while time.monotonic() < deadline and not res.closed_by_server:
                        await asyncio.sleep(0.05)
                        if len(res.audio) > seen and not pending and time.monotonic() - last_audio[0] > 0.5:
                            break
                elif kind == "barge_in":
                    # Wait for the reply to start, then talk over it while
                    # its audio is still "playing" (marks held back).
                    hold_marks[0] = True
                    deadline = time.monotonic() + timeout
                    seen = len(res.audio)
                    while len(res.audio) == seen and time.monotonic() < deadline:
                        await asyncio.sleep(0.02)
                    await send_audio(arg)
                    hold_marks[0] = False
            if not res.closed_by_server:
                await ws.send(json.dumps({"event": "stop", "streamSid": stream_sid,
                                          "stop": {"callSid": call_sid}}))
                await asyncio.sleep(0.1)
        finally:
            pl.cancel()
            try:
                await asyncio.wait_for(rx, 2.0)
            except (asyncio.TimeoutError, Exception):
                rx.cancel()
    return res


async def relay_call(ws_url: str, token: str, call_sid: str, prompts: List[str],
                     timeout: float = 20.0) -> List[str]:
    """A ConversationRelay call: setup, then each prompt as Twilio's speech
    recognition would send it. Returns the text the server spoke, per turn."""
    import websockets
    spoken: List[str] = []
    async with websockets.connect(ws_url, open_timeout=timeout) as ws:
        await ws.send(json.dumps({"type": "setup", "sessionId": "VX" + secrets.token_hex(16),
                                  "callSid": call_sid, "from": "+15550102000", "to": "+15550109999",
                                  "customParameters": {"token": token}}))
        for prompt in prompts:
            await ws.send(json.dumps({"type": "prompt", "voicePrompt": prompt, "lang": "en-US", "last": True}))
            text = ""
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if m.get("type") == "text":
                    text += m.get("token", "")
                    if m.get("last"):
                        break
                elif m.get("type") == "end":
                    break
            spoken.append(text.strip())
    return spoken
