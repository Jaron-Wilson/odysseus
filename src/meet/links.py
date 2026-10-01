"""Meet links and dial-in details: what counts as one, and finding them in
the text of a calendar event.

A Google Calendar event with Meet has, in the description its CalDAV feed
carries, something like:

    Join with Google Meet: https://meet.google.com/abc-defg-hij
    Or dial: (US) +1 650-555-0123 PIN: 123 456 789#
    More phone numbers: https://tel.meet/abc-defg-hij?pin=123456789

Only meet.google.com links are joined (the browser goes nowhere else), and
only US and Canada numbers are dialed, the ones Meet gives paid plans at no
charge and Twilio bills at its lowest rate.
"""

import re
from typing import Dict, List, Optional

MEET_HOST = "meet.google.com"
_CODE = r"[a-z]{3}-[a-z]{4}-[a-z]{3}"
_CODE_RE = re.compile(rf"^{_CODE}$")
_URL_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?meet\.google\.com/(?P<path>{_CODE}|lookup/[A-Za-z0-9_-]{{4,64}})(?![A-Za-z0-9-])",
    re.I)
_PHONE_RE = re.compile(r"\+1[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_PIN_RE = re.compile(r"PIN[:\s]*([\d\s]{4,24})#?", re.I)
_TEL_MEET_RE = re.compile(r"tel\.meet/" + _CODE + r"\?pin=(\d{4,15})", re.I)


def meet_url(text: str) -> str:
    """The canonical https://meet.google.com/... link for a pasted link or a
    bare meeting code, or "" if it is not a Meet link."""
    t = str(text or "").strip()
    if _CODE_RE.match(t.lower()):
        return f"https://{MEET_HOST}/{t.lower()}"
    m = _URL_RE.search(t)
    if not m:
        return ""
    # Nothing but the Meet host before the match: "evil.com/?meet.google.com/..." is not one.
    before = t[: m.start()]
    if before.strip():
        return ""
    path = m.group("path")
    return f"https://{MEET_HOST}/{path.lower() if _CODE_RE.match(path.lower()) else path}"


def meeting_code(url: str) -> str:
    m = re.search(_CODE, url or "")
    return m.group(0) if m else ""


def find_meet_links(text: str) -> List[str]:
    out: List[str] = []
    for m in _URL_RE.finditer(str(text or "")):
        path = m.group("path")
        url = f"https://{MEET_HOST}/{path.lower() if _CODE_RE.match(path.lower()) else path}"
        if url not in out:
            out.append(url)
    return out


def phone_number(raw: str) -> str:
    """A US or Canada number as +1XXXXXXXXXX, or ""."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1") or digits[1] in "01":
        return ""
    return "+" + digits


def clean_pin(raw: str) -> str:
    """A Meet PIN as digits (spaces and the closing # dropped), or ""."""
    d = re.sub(r"[\s#-]", "", str(raw or ""))
    return d if re.fullmatch(r"\d{4,15}", d) else ""


def find_dial_in(text: str) -> Optional[Dict[str, str]]:
    """The first US dial-in number and its PIN in an event's text."""
    t = str(text or "")
    num = ""
    for m in _PHONE_RE.finditer(t):
        num = phone_number(m.group(0))
        if num:
            break
    pin = ""
    m = _PIN_RE.search(t)
    if m:
        pin = clean_pin(m.group(1))
    if not pin:
        m = _TEL_MEET_RE.search(t)
        if m:
            pin = clean_pin(m.group(1))
    if num and pin:
        return {"number": num, "pin": pin}
    return None


def meeting_from_event(ev: Dict) -> Optional[Dict]:
    """A calendar event (routes/calendar_routes._event_to_dict) as a meeting
    that can be joined, or None if it has no Meet link."""
    text = "\n".join(str(ev.get(k) or "") for k in ("location", "description"))
    links = find_meet_links(text)
    if not links:
        return None
    out = {"uid": ev.get("uid", ""), "summary": ev.get("summary") or "(no title)",
           "start": ev.get("dtstart", ""), "end": ev.get("dtend", ""), "url": links[0]}
    dial = find_dial_in(text)
    if dial:
        out["dial_in"] = dial
    return out
