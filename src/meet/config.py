"""Google Meet settings, per user, in the prefs store under "google_meet".

Nothing secret lives here: joining through the browser uses whatever Google
account (if any) is signed in to the cloud browser, and joining by phone
uses the Twilio account saved for phone calls (src/telephony/config.py,
whose auth token stays encrypted there).
"""

import re
from typing import Dict, List, Optional

from routes import prefs_routes

PREF_KEY = "google_meet"
MODES = ("assistant", "talk")
JOIN_AS = ("guest", "signed_in")
VIAS = ("browser", "phone")
DEFAULT_NAME = "Odysseus (AI)"
DEFAULT_WAKE = ["odysseus"]
DEFAULT_ANNOUNCE = ("Hi, I'm Odysseus, an AI assistant. I'm listening and keeping a transcript "
                    "of this meeting for {owner}. Say \"Odysseus\" if you want me to answer.")
DEFAULT_ANNOUNCE_TALK = ("Hi, I'm Odysseus, an AI assistant, here to talk with {owner}. "
                         "This meeting is being transcribed.")
MAX_NAME = 60
MAX_ANNOUNCE = 400
MAX_WAKE = 5
DEFAULT_MAX_MIN = 120
MAX_MAX_MIN = 24 * 60          # Meet's own limit on paid plans
DEFAULT_IDLE_MIN = 15
DEFAULT_LOBBY_MIN = 10
DEFAULT_DIAL_WAIT = 4
_WAKE_RE = re.compile(r"^[\w' -]{2,30}$", re.U)


def get_config(user: Optional[str]) -> Dict:
    cfg = prefs_routes._load_for_user(user).get(PREF_KEY)
    return dict(cfg) if isinstance(cfg, dict) else {}


def save_config(user: Optional[str], cfg: Dict) -> None:
    prefs = prefs_routes._load_for_user(user)
    prefs[PREF_KEY] = cfg
    prefs_routes._save_for_user(user, prefs)


def _int(cfg: Dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(cfg.get(key) if cfg.get(key) not in (None, "") else default)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def wake_words(cfg: Dict) -> List[str]:
    words = cfg.get("wake_words")
    if not isinstance(words, list) or not words:
        return list(DEFAULT_WAKE)
    out = []
    for w in words:
        w = str(w or "").strip().lower()
        if w and w not in out:
            out.append(w)
    return out or list(DEFAULT_WAKE)


def clean_wake_words(raw) -> List[str]:
    """Validated wake words from the card (a list or a comma separated
    string). Raises ValueError with a sentence for the card."""
    items = raw if isinstance(raw, list) else re.split(r"[,;\n]", str(raw or ""))
    out: List[str] = []
    for w in items:
        w = re.sub(r"\s+", " ", str(w or "")).strip().lower()
        if not w:
            continue
        if not _WAKE_RE.match(w):
            raise ValueError(f"A wake word is a name or short phrase: {w[:30]}")
        if w not in out:
            out.append(w)
    if len(out) > MAX_WAKE:
        raise ValueError(f"At most {MAX_WAKE} wake words.")
    return out


def view(cfg: Dict) -> Dict:
    """Every setting with its default filled in."""
    return {
        "enabled": bool(cfg.get("enabled")),
        "display_name": str(cfg.get("display_name") or "").strip() or DEFAULT_NAME,
        "mode": cfg.get("mode") if cfg.get("mode") in MODES else "assistant",
        "join_as": cfg.get("join_as") if cfg.get("join_as") in JOIN_AS else "guest",
        "via": cfg.get("via") if cfg.get("via") in VIAS else "browser",
        "wake_words": wake_words(cfg),
        "announce": cfg.get("announce", True) is not False,
        "announcement": str(cfg.get("announcement") or ""),
        "default_announcement": DEFAULT_ANNOUNCE,
        "chat_notice": cfg.get("chat_notice", True) is not False,
        "summary": cfg.get("summary", True) is not False,
        "max_minutes": _int(cfg, "max_minutes", DEFAULT_MAX_MIN, 5, MAX_MAX_MIN),
        "idle_minutes": _int(cfg, "idle_minutes", DEFAULT_IDLE_MIN, 2, 120),
        "lobby_minutes": _int(cfg, "lobby_minutes", DEFAULT_LOBBY_MIN, 1, 60),
        "dial_wait": _int(cfg, "dial_wait", DEFAULT_DIAL_WAIT, 0, 15),
        "model": str(cfg.get("model") or ""),
        "endpoint_id": str(cfg.get("endpoint_id") or ""),
    }


def announcement(cfg: Dict, mode: str, owner: Optional[str]) -> str:
    """What it says when it joins. Always says it is an AI and that the
    meeting is transcribed, even with a custom text: people in the meeting
    have to know."""
    v = view(cfg)
    who = (owner or "").strip() or "my owner"
    text = v["announcement"].strip() or (DEFAULT_ANNOUNCE if mode == "assistant" else DEFAULT_ANNOUNCE_TALK)
    text = text.replace("{owner}", who)
    low = text.lower()
    if " ai" not in f" {low}" or not any(k in low for k in ("transcri", "record", "notes")):
        text = text.rstrip(". ") + ". I'm an AI assistant and this meeting is being transcribed."
    return text[:MAX_ANNOUNCE + 80]
