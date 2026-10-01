"""Phone call settings, per user, in the prefs store under "phone_calls".

The provider's auth token is encrypted with src/secret_storage.py (the same
Fernet key the mail passwords use) and never leaves the server: public()
reports only whether one is saved. The PIN is kept as a salted hash. The
generic /api/prefs routes neither show nor write this key
(prefs_routes.PRIVATE_KEYS).
"""

import hashlib
import hmac
import re
import secrets
from typing import Dict, List, Optional, Tuple

from routes import prefs_routes

PREF_KEY = "phone_calls"
MAX_NUMBERS = 5
MAX_GREETING = 300
ENGINES = ("odysseus", "relay")
UNKNOWN = ("reject", "message")
DEFAULT_GREETING = "Hi, it's Odysseus. What can I do for you?"
SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")
PIN_RE = re.compile(r"^\d{4,8}$")


def normalize_number(raw) -> str:
    # One definition of "the same number" for texts and calls.
    from routes.sms_routes import normalize_number as _n
    return _n(raw)


def _owners() -> List[Tuple[Optional[str], Dict]]:
    raw = prefs_routes._load()
    if "_users" in raw and isinstance(raw["_users"], dict):
        items = list(raw["_users"].items())
    else:
        items = [(None, raw)]
    out = []
    for user, prefs in items:
        cfg = prefs.get(PREF_KEY) if isinstance(prefs, dict) else None
        if isinstance(cfg, dict):
            out.append((user, cfg))
    return out


def get_config(user: Optional[str]) -> Dict:
    cfg = prefs_routes._load_for_user(user).get(PREF_KEY)
    return dict(cfg) if isinstance(cfg, dict) else {}


def save_config(user: Optional[str], cfg: Dict) -> None:
    prefs = prefs_routes._load_for_user(user)
    prefs[PREF_KEY] = cfg
    prefs_routes._save_for_user(user, prefs)


def auth_token(cfg: Dict) -> str:
    from src import secret_storage
    return secret_storage.decrypt(str(cfg.get("auth_token") or ""))


def set_auth_token(cfg: Dict, token: str) -> None:
    from src import secret_storage
    cfg["auth_token"] = secret_storage.encrypt(token) if token else ""


def allowed_numbers(user: Optional[str], cfg: Dict) -> List[str]:
    """Who may talk to the agent. Unset means the numbers saved for the SMS
    gateway, so by default only your own phones get through. Never empty
    by accident into "everyone": an empty list lets no one in."""
    nums = cfg.get("numbers")
    if not isinstance(nums, list) or not nums:
        try:
            from routes.sms_routes import get_config as sms_config
            nums = sms_config(user).get("numbers") or []
        except Exception:
            nums = []
    out = []
    for n in nums:
        norm = normalize_number(n)
        if norm and norm not in out:
            out.append(norm)
    return out


def _pin_hash(pin: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{pin}".encode("utf-8")).hexdigest()


def set_pin(cfg: Dict, pin: str) -> None:
    if not pin:
        cfg.pop("pin_hash", None)
        cfg.pop("pin_salt", None)
        cfg.pop("pin_len", None)
        return
    salt = secrets.token_hex(8)
    cfg.update({"pin_salt": salt, "pin_hash": _pin_hash(pin, salt), "pin_len": len(pin)})


def check_pin(cfg: Dict, pin: str) -> bool:
    stored = str(cfg.get("pin_hash") or "")
    if not stored:
        return True
    return hmac.compare_digest(stored, _pin_hash(str(pin or ""), str(cfg.get("pin_salt") or "")))


def owners_for_number(number: str) -> List[Tuple[Optional[str], Dict]]:
    """Every user whose agent number this is (normally one)."""
    want = normalize_number(number)
    if not want:
        return []
    return [(u, c) for u, c in _owners() if normalize_number(c.get("phone_number")) == want]


def public(user: Optional[str], cfg: Dict) -> Dict:
    """What the Settings card sees. No token, no PIN."""
    explicit = cfg.get("numbers") if isinstance(cfg.get("numbers"), list) else []
    return {
        "enabled": bool(cfg.get("enabled")),
        "provider": str(cfg.get("provider") or "twilio"),
        "account_sid": str(cfg.get("account_sid") or ""),
        "has_auth_token": bool(cfg.get("auth_token")),
        "phone_number": str(cfg.get("phone_number") or ""),
        "numbers": list(explicit),
        "allowed": allowed_numbers(user, cfg),
        "numbers_from_sms": not explicit,
        "greeting": str(cfg.get("greeting") or ""),
        "default_greeting": DEFAULT_GREETING,
        "model": str(cfg.get("model") or ""),
        "endpoint_id": str(cfg.get("endpoint_id") or ""),
        "engine": cfg.get("engine") if cfg.get("engine") in ENGINES else "odysseus",
        "relay_voice": str(cfg.get("relay_voice") or ""),
        "unknown": cfg.get("unknown") if cfg.get("unknown") in UNKNOWN else "reject",
        "has_pin": bool(cfg.get("pin_hash")),
        "public_url": str(cfg.get("public_url") or ""),
    }
