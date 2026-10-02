"""The agent's google_meet tool: make a Meet in the user's Google Calendar
(now, or later with invites), join one, or say what is set up.

    {"action": "start", "attendees": ["bob@x.com"], "title": "Quick sync"}
    {"action": "schedule", "title": "Planning", "start": "tomorrow at 3pm",
     "minutes": 30, "attendees": "bob@x.com, amy@y.com"}
    {"action": "join", "url": "https://meet.google.com/abc-defg-hij"}
    {"action": "status"}

The same code paths as the Google Meet card (routes/meet_routes.py), so a
chat, a voice call or an SMS turn gets the same answers and the same checks.
"""

import json
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _args(content: str) -> Dict:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    # A bare link means join it; anything else is a status question.
    from src.meet import links
    return {"action": "join", "url": raw} if links.meet_url(raw) else {"action": "status"}


def _status(owner: Optional[str]) -> str:
    from src.meet import config as meet_config, google_calendar
    g = google_calendar.status(owner)
    cfg = meet_config.view(meet_config.get_config(owner))
    lines = []
    if not g["configured"]:
        lines.append("No Google OAuth client is set up on this server, so Odysseus cannot make meetings yet. "
                     "The Google Meet card in Settings > Calls & Meetings says what to do.")
    elif g["connected"]:
        lines.append(f"Google Calendar is connected as {g['email'] or 'a Google account'}: "
                     "Odysseus can make meetings and send invites.")
    else:
        lines.append("Google Calendar is not connected. Use Connect Google Calendar in the Google Meet card "
                     "in Settings > Calls & Meetings.")
    lines.append("Joining meetings is on." if cfg["enabled"] else
                 "Joining meetings is off (the Join meetings switch at the top of the Google Meet card), so "
                 "Odysseus can make a meeting but not join it.")
    return " ".join(lines)


async def run_tool(content: str, owner: Optional[str] = None, is_admin: bool = False) -> Dict:
    from fastapi import HTTPException
    from routes import meet_routes
    args = _args(content)
    action = str(args.get("action") or "").lower()
    if not action:
        action = "join" if args.get("url") else ("schedule" if args.get("start") not in (None, "", "now") else "start")
    try:
        if action == "status":
            return {"output": _status(owner), "exit_code": 0}
        if action == "join":
            m = await meet_routes.start_join(owner, is_admin, {
                "url": args.get("url") or "", "mode": args.get("mode") or "", "via": args.get("via") or "",
                "title": args.get("title") or "", "dial_in": args.get("dial_in") or "", "pin": args.get("pin") or ""})
            if m.via == "phone":
                output = (f"Join initiated: Odysseus (AI) is placing a phone call to join {m.dial_in.get('number', 'the meeting')}. "
                          "It has NOT joined yet (call in progress). Do NOT claim it has already joined.")
            else:
                output = (f"Join initiated: Odysseus (AI) is opening {m.url} via browser. It has NOT joined yet. "
                          "Do NOT claim it has joined; if joining as guest, someone in the meeting may need to admit it from the lobby.")
            return {"output": output, "meeting": m.public(), "exit_code": 0}
        if action in ("start", "now", "create", "schedule"):
            body = {
                "title": args.get("title") or "",
                "start": "now" if action in ("start", "now") else (args.get("start") or ""),
                "minutes": args.get("minutes") or args.get("duration") or 0,
                "attendees": args.get("attendees") or [],
                "description": args.get("description") or "",
                "mode": args.get("mode") or "",
            }
            if action == "schedule" and not body["start"]:
                return {"error": "When should it start? Pass start, like \"tomorrow at 3pm\".", "exit_code": 1}
            if "join" in args:
                body["join"] = bool(args["join"])
            made = await meet_routes.create_meeting(owner, is_admin, body)
            out = {k: made.get(k) for k in ("url", "title", "start", "end", "attendees", "html_link",
                                            "open_access", "join_error")}
            return {"output": made["message"], "meeting": out, "exit_code": 0}
        return {"error": f"Unknown action {action!r}: use start, schedule, join or status.", "exit_code": 1}
    except HTTPException as e:
        return {"error": str(e.detail), "exit_code": 1}
