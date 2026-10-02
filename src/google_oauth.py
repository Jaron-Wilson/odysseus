"""The Google OAuth client this server uses, shared by Sign in with Google
(routes/auth_routes.py) and Google Calendar for Meet (src/meet/google_calendar.py).

One client ID and secret, from settings first then the environment
(GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET), and one redirect URI,
/api/auth/google/callback, so a Google Cloud project set up for one also
works for the other. The callback tells the two apart by the state it
carries (a "purpose" in the remembered payload).
"""

import os
import secrets
import time
import urllib.parse
from typing import Dict, Optional

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALLBACK_PATH = "/api/auth/google/callback"
STATE_TTL_S = 600

_states: Dict[str, Dict] = {}


def _setting(key: str) -> str:
    try:
        from src.settings import get_setting
        return (get_setting(key, "") or "").strip()
    except Exception:
        return ""


def client_config() -> Dict:
    """Client credentials, from settings first then environment."""
    cid = _setting("google_oauth_client_id") or os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = _setting("google_oauth_client_secret") or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    return {"client_id": cid, "client_secret": csec, "configured": bool(cid and csec)}


def save_client(client_id: str, client_secret: str) -> None:
    """Save the client in settings. An empty secret keeps the saved one, so
    the form can change the ID without asking for the secret again."""
    from src.settings import load_settings, save_settings
    current = load_settings()
    current["google_oauth_client_id"] = (client_id or "").strip()
    if (client_secret or "").strip():
        current["google_oauth_client_secret"] = client_secret.strip()
    save_settings(current)


def redirect_uri(request) -> str:
    """The callback URL, which must match Google's registered value exactly.

    Derived from the request by default so it is correct behind Tailscale
    Serve with no extra configuration, but overridable because a proxy can
    rewrite the host and Google compares the string, not the intent.
    """
    override = _setting("google_oauth_redirect_uri") or os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if override:
        return override
    base = str(request.base_url).rstrip("/")
    # base_url reports http behind a TLS-terminating proxy such as
    # Tailscale Serve. The registered URI is https, and Google rejects the
    # mismatch with an error that never mentions the scheme.
    if request.headers.get("x-forwarded-proto", "") == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base + CALLBACK_PATH


def remember_state(state: str, payload: Dict) -> None:
    now = time.time()
    for key, val in list(_states.items()):
        if now - val.get("at", 0) > STATE_TTL_S:
            _states.pop(key, None)
    payload["at"] = now
    _states[state] = payload


def pop_state(state: str) -> Optional[Dict]:
    entry = _states.pop(state or "", None)
    if entry and time.time() - entry.get("at", 0) > STATE_TTL_S:
        return None
    return entry


def authorize_url(request, payload: Dict, **params) -> str:
    """The Google consent URL for a new state carrying `payload`."""
    cfg = client_config()
    state = secrets.token_urlsafe(24)
    uri = redirect_uri(request)
    remember_state(state, {**payload, "redirect_uri": uri})
    q = {"client_id": cfg["client_id"], "redirect_uri": uri, "response_type": "code", "state": state}
    q.update(params)
    return AUTH_URL + "?" + urllib.parse.urlencode(q)
