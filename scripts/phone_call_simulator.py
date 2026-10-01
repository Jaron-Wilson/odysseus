#!/usr/bin/env python3
"""Call a running Odysseus the way Twilio would, with no Twilio account.

Posts a signed "a call came in" webhook, opens the media stream the answer
names, says something (a WAV file, or a test tone), and saves what the agent
said back as a WAV you can play.

    python scripts/phone_call_simulator.py --base http://127.0.0.1:7080 \\
        --account-sid AC0123... --auth-token <token> \\
        --to +15550109999 --from +15550102000 \\
        --say question.wav --out reply.wav

--to is the agent's number and --from your number, as saved in Settings >
Devices > Phone calls; the account SID and auth token must match what is
saved there too (any made-up pair works, since nothing is sent to Twilio).
The public URL saved there is what the webhook is signed with; --base is
where the requests actually go.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from src.telephony import codec, simulator  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="where the server listens, e.g. http://127.0.0.1:7080")
    ap.add_argument("--public", default="", help="the public URL saved in Settings (default: --base)")
    ap.add_argument("--account-sid", required=True)
    ap.add_argument("--auth-token", required=True)
    ap.add_argument("--to", required=True, help="the agent's number")
    ap.add_argument("--from", dest="caller", required=True, help="the number calling")
    ap.add_argument("--say", default="", help="a WAV file to say (default: a two second test tone)")
    ap.add_argument("--pin", default="", help="digits to enter if a PIN is asked for")
    ap.add_argument("--out", default="reply.wav", help="where to save what the agent said")
    ap.add_argument("--wait", type=float, default=60.0, help="seconds to wait for each reply")
    a = ap.parse_args()

    if a.say:
        with open(a.say, "rb") as f:
            samples, rate = codec.decode_audio(f.read())
        speech = codec.resample(samples, rate, codec.RATE)
    else:
        speech = simulator.tone(2.0)

    tw = simulator.FakeTwilio(a.base, a.account_sid, a.auth_token, a.to, a.public)
    with httpx.Client(timeout=20) as client:
        call_sid, r = tw.incoming(client, a.caller)
        print(f"webhook: HTTP {r.status_code}")
        if r.status_code != 200:
            print(r.text[:300])
            return 1
        tm = simulator.parse_twiml(r.text)
        if tm.gather_action:
            print("asked for a PIN")
            r = tw.webhook(client, tm.gather_action, {"CallSid": call_sid, "From": a.caller, "To": a.to,
                                                      "Direction": "inbound", "Digits": a.pin})
            tm = simulator.parse_twiml(r.text)
        print("answer:", " ".join(tm.verbs), "|", " ".join(tm.say))
        if not tm.stream_url:
            print("the call was not connected to a media stream")
            return 1
    script = [("sleep", 0.2), ("wait_reply", a.wait), ("sleep", 0.5),
              ("audio", simulator.silence(0.3)), ("audio", speech), ("audio", simulator.silence(1.2)),
              ("wait_reply", a.wait)]
    res = asyncio.run(simulator.media_call(tw._to_local(tm.stream_url), tm.params.get("token", ""),
                                           call_sid, script, pace=1.0, timeout=a.wait))
    pcm = codec.ulaw_to_pcm16(bytes(res.audio))
    with open(a.out, "wb") as f:
        f.write(codec.wav_bytes(pcm, codec.RATE))
    print(f"heard {len(res.audio) / codec.RATE:.1f}s of speech in {len(res.clips)} sentence(s), "
          f"{res.clears} clear(s); saved {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
