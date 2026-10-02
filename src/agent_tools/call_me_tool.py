"""Ring the user and talk: the agent's way to start a phone call.

"Call me when the build is done" ends here. The call goes to the softphone
registered on the free SIP line (src/telephony/sip_line.py) when there is
one, else through Twilio to the user's first allowed number
(routes/telephony_routes.py), the same plumbing as the Call me buttons in
Settings > Calls & Meetings > Phone calls. Either way the call is a new chat and the
agent opens with `message`, then listens.
"""

import json
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class CallMeTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        raw = (content or "").strip()
        args: Dict = {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, TypeError):
                args = {}
        elif raw:
            args = {"message": raw}
        message = str(args.get("message") or "").strip()
        via = str(args.get("via") or "auto").strip().lower()
        owner = (ctx or {}).get("owner") or None
        from src.telephony import config, sip_line
        message = message[: config.MAX_GREETING]
        errors = []
        if via in ("auto", "sip"):
            try:
                r = await sip_line.LINE.call_me(owner, message)
                return {"output": f"Ringing the softphone on the SIP line ({r['device']}). "
                                  "When they answer, the call opens with your message and is its own chat.",
                        "exit_code": 0}
            except ValueError as e:
                errors.append(f"SIP line: {e}")
        if via in ("auto", "phone", "twilio"):
            try:
                from routes.telephony_routes import twilio_call_me
                r = await twilio_call_me(owner, greeting=message)
                return {"output": f"Calling {r['to']} through Twilio. When they answer, the call opens "
                                  "with your message and is its own chat.", "exit_code": 0}
            except ValueError as e:
                errors.append(f"Phone number: {e}")
        return {"error": "Could not call: " + " ".join(errors or ["unknown way to call"]), "exit_code": 1}
