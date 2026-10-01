"""The Twilio adapter: what Twilio sends, what it wants back.

Webhooks (Twilio -> us, form-encoded POSTs):
    every one carries X-Twilio-Signature: base64(HMAC-SHA1(auth token,
    the full URL Twilio called + each POST parameter's name and value,
    sorted by name)). valid_signature() checks it; a request that fails is
    treated like a path that does not exist.

TwiML (our answer to a webhook) to connect a call:
    <Connect><Stream url="wss://.../stream">     raw audio both ways (Media Streams)
    <Connect><ConversationRelay url="wss://...">  Twilio does the speech, we send text

Media Streams messages (JSON text frames on the WebSocket):
    in:  connected, start {streamSid, callSid, customParameters, mediaFormat},
         media {payload: base64 8 kHz mu-law}, mark {name}, dtmf {digit}, stop
    out: media {payload}, mark {name} (echoed back once played), clear

ConversationRelay messages:
    in:  setup {callSid, from, to, customParameters}, prompt {voicePrompt, last},
         interrupt {utteranceUntilInterrupt}, dtmf {digit}, error {description}
    out: text {token, last}, end {handoffData}

REST (us -> Twilio, HTTP basic auth with the account SID and auth token):
    POST Accounts/{sid}/Calls.json                    "call me"
    GET  Accounts/{sid}/IncomingPhoneNumbers.json     the Test button
    GET  a recording's URL + .wav, then DELETE it     an unknown caller's message

src/telephony/simulator.py speaks the Twilio side of all of this, so a call
can be run end to end without an account.
"""

import base64
import hashlib
import hmac
import json
import logging
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape, quoteattr

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.twilio.com/2010-04-01"
TIMEOUT = 10.0
CHUNK_BYTES = 8000          # one second of mu-law per media message


# ── Signatures ─────────────────────────────────────────────────────────────

def signature(url: str, params: Iterable[Tuple[str, str]], auth_token: str) -> str:
    """X-Twilio-Signature for a form POST to `url` with `params`."""
    data = url + "".join(f"{k}{v}" for k, v in sorted(params, key=lambda kv: (kv[0], kv[1])))
    mac = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def _url_variants(url: str) -> List[str]:
    """The URL as given, and with the default HTTPS port added or removed:
    Twilio's own validators try both, since which one it signs depends on
    how the URL was written in the console."""
    out = [url]
    p = urlsplit(url)
    if p.scheme == "https":
        host = p.hostname or ""
        if p.port == 443:
            out.append(urlunsplit((p.scheme, host, p.path, p.query, p.fragment)))
        elif p.port is None:
            out.append(urlunsplit((p.scheme, f"{host}:443", p.path, p.query, p.fragment)))
    return out


def valid_signature(url: str, params: Iterable[Tuple[str, str]], auth_token: str, header: str) -> bool:
    if not (auth_token and header):
        return False
    params = list(params)
    ok = False
    for u in _url_variants(url):
        # No early exit: the comparisons take the same time either way.
        ok = hmac.compare_digest(signature(u, params, auth_token), header) or ok
    return ok


# ── TwiML ──────────────────────────────────────────────────────────────────

def _say(text: str) -> str:
    return f"<Say>{escape(text)}</Say>"


def twiml(*verbs: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"


def say_and_hang_up(text: str) -> str:
    return twiml(_say(text), "<Hangup/>")


def gather_pin(action: str, digits: int, prompt: str = "Enter your PIN.") -> str:
    return twiml(
        f'<Gather input="dtmf" numDigits="{int(digits)}" timeout="8" finishOnKey="#" '
        f'action={quoteattr(action)} method="POST">{_say(prompt)}</Gather>',
        _say("No PIN entered. Goodbye."), "<Hangup/>",
    )


def connect_stream(ws_url: str, params: Dict[str, str]) -> str:
    """Bidirectional Media Stream: only <Connect> lets us send audio back."""
    ps = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in params.items())
    return twiml(f"<Connect><Stream url={quoteattr(ws_url)}>{ps}</Stream></Connect>")


def connect_relay(ws_url: str, params: Dict[str, str], greeting: str = "", voice: str = "",
                  language: str = "en-US") -> str:
    """ConversationRelay: Twilio hears and speaks, we exchange text."""
    attrs = [f"url={quoteattr(ws_url)}", f"language={quoteattr(language)}",
             'interruptible="any"', 'dtmfDetection="true"']
    if greeting:
        attrs.append(f"welcomeGreeting={quoteattr(greeting)}")
    if voice:
        attrs.append(f"voice={quoteattr(voice)}")
    ps = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in params.items())
    return twiml(f"<Connect><ConversationRelay {' '.join(attrs)}>{ps}</ConversationRelay></Connect>")


def take_message(prompt: str, action: str, recording_callback: str, max_s: int = 60) -> str:
    """Record a message after the tone. `action` is where Twilio goes after
    the recording (it must hang up: without one Twilio re-requests the
    voice webhook)."""
    return twiml(
        _say(prompt),
        f'<Record maxLength="{int(max_s)}" timeout="5" playBeep="true" finishOnKey="#" '
        f'action={quoteattr(action)} method="POST" '
        f'recordingStatusCallback={quoteattr(recording_callback)} '
        f'recordingStatusCallbackEvent="completed" recordingStatusCallbackMethod="POST"/>',
        _say("No message recorded. Goodbye."), "<Hangup/>",
    )


# ── Media Streams ──────────────────────────────────────────────────────────

class MediaStreamTransport:
    """call.Transport over a Twilio Media Streams WebSocket."""

    def __init__(self, send_text, stream_sid: str, close=None):
        self._send = send_text          # async (str) -> None
        self._close = close             # async () -> None
        self.stream_sid = stream_sid

    async def play(self, ulaw: bytes, mark: str) -> None:
        for i in range(0, len(ulaw), CHUNK_BYTES):
            await self._send(json.dumps({
                "event": "media", "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(ulaw[i:i + CHUNK_BYTES]).decode("ascii")},
            }))
        await self._send(json.dumps({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": mark}}))

    async def clear(self) -> None:
        await self._send(json.dumps({"event": "clear", "streamSid": self.stream_sid}))

    async def hangup(self) -> None:
        # Closing the stream ends <Connect>; nothing follows it in the TwiML,
        # so Twilio hangs up.
        if self._close:
            await self._close()


def relay_text(token: str, last: bool) -> str:
    return json.dumps({"type": "text", "token": token, "last": bool(last)})


def relay_end(reason: str = "") -> str:
    return json.dumps({"type": "end", "handoffData": json.dumps({"reason": reason})})


# ── REST ───────────────────────────────────────────────────────────────────

def _auth(account_sid: str, auth_token: str) -> Tuple[str, str]:
    return (account_sid, auth_token)


async def create_call(account_sid: str, auth_token: str, from_: str, to: str, url: str) -> Dict:
    """Place an outbound call; Twilio requests `url` when it is answered."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(f"{API_BASE}/Accounts/{account_sid}/Calls.json",
                              data={"To": to, "From": from_, "Url": url, "Method": "POST"},
                              auth=_auth(account_sid, auth_token))
    body = _json(r)
    if r.status_code >= 300:
        raise RuntimeError(_error(r.status_code, body))
    return {"call_sid": body.get("sid", ""), "status": body.get("status", "")}


async def find_number(account_sid: str, auth_token: str, number: str) -> Optional[Dict]:
    """The account's phone number record (voice URL and all), or None if
    the number is not on the account. Raises on bad credentials."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(f"{API_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json",
                             params={"PhoneNumber": number}, auth=_auth(account_sid, auth_token))
    body = _json(r)
    if r.status_code >= 300:
        raise RuntimeError(_error(r.status_code, body))
    nums = body.get("incoming_phone_numbers") or []
    return nums[0] if nums else None


async def fetch_recording(account_sid: str, auth_token: str, recording_url: str) -> bytes:
    """A recording as WAV. Only Twilio's own API host is fetched, with the
    credentials, so a forged callback cannot make us send them elsewhere."""
    p = urlsplit(recording_url)
    if p.scheme != "https" or p.hostname != urlsplit(API_BASE).hostname:
        raise RuntimeError("not a Twilio recording URL")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(recording_url + ".wav", auth=_auth(account_sid, auth_token))
    if r.status_code >= 300:
        raise RuntimeError(f"recording download failed: HTTP {r.status_code}")
    return r.content


async def delete_recording(account_sid: str, auth_token: str, recording_sid: str) -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await client.delete(f"{API_BASE}/Accounts/{account_sid}/Recordings/{recording_sid}.json",
                            auth=_auth(account_sid, auth_token))


def _json(r: httpx.Response) -> Dict:
    try:
        d = r.json()
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _error(status: int, body: Dict) -> str:
    if status == 401:
        return "Twilio did not accept the account SID and auth token."
    msg = str(body.get("message") or "")[:200]
    return f"Twilio said HTTP {status}" + (f": {msg}" if msg else "")
