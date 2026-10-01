"""Joining a Google Meet by phone: the Twilio line from src/telephony/ calls
the meeting's dial-in number and keys in its PIN.

    1. dial() asks Twilio for an outbound call from the agent's number to
       the dial-in number, answered by POST /api/telephony/twilio/meet?m=KEY
       (a one-time key for this meeting).
    2. That webhook (routes/telephony_routes.py, signature checked like the
       others) answers with twiml(): wait for Meet's prompt, key in
       "PIN#", then the same bidirectional Media Stream a phone call uses.
    3. The stream's handler hands the line to the meeting
       (session.Meeting.attach_phone), which runs the call loop on 8 kHz
       mu-law.

Meet shows a phone participant as a (partly hidden) number, not a name,
so the announcement when it joins is what tells people an AI is there.
Meet only has dial-in numbers when the organizer's plan includes them (see
docs/google-meet.md).
"""

from typing import Dict, List
from urllib.parse import urlencode

from src.telephony import call as call_mod, config as tconfig, twilio


def ready(owner) -> List[str]:
    """Why this user cannot join by phone, as sentences (empty when ready)."""
    cfg = tconfig.get_config(owner)
    out = []
    if not (cfg.get("account_sid") and cfg.get("auth_token") and cfg.get("phone_number")):
        out.append("Joining by phone uses the Twilio number from Settings > Devices > Phone calls: "
                   "save the account SID, auth token and the agent's number there.")
    if not cfg.get("public_url"):
        out.append("Save the public URL in Settings > Devices > Phone calls, so Twilio can reach this server.")
    out.extend(call_mod.engines_ready())
    return out


async def dial(meeting) -> str:
    """Place the call. Returns Twilio's call SID."""
    from routes import telephony_routes as tr
    problems = ready(meeting.owner)
    if problems:
        raise RuntimeError(problems[0])
    cfg = tconfig.get_config(meeting.owner)
    to = meeting.dial_in.get("number", "")
    if not to or not meeting.dial_in.get("pin"):
        raise RuntimeError("The meeting's dial-in number and PIN are needed to join by phone.")
    key = tr.register_meet_dial(meeting.id, meeting.owner)
    url = str(cfg.get("public_url") or "").rstrip("/") + tr.PREFIX + "meet?" + urlencode({"m": key})
    r = await twilio.create_call(str(cfg["account_sid"]), tconfig.auth_token(cfg), str(cfg["phone_number"]), to, url)
    return r.get("call_sid", "")


def twiml(meeting, ws_url: str, stream_token: str) -> str:
    """Wait for Meet's "enter the meeting PIN", key it in, then stream."""
    wait = "W" * int(meeting.cfg.get("dial_wait") or 0)
    pin = meeting.dial_in.get("pin", "")
    return twilio.twiml(twilio.play_digits(f"{wait}{pin}#"), twilio.stream_verb(ws_url, {"token": stream_token}))


def describe(dial_in: Dict[str, str]) -> str:
    """For logs and notes: the number, never the PIN."""
    return dial_in.get("number", "") if dial_in else ""
