"""Sign in with ChatGPT: use a ChatGPT Plus/Pro plan for OpenAI models.

This is OpenAI's official "ChatGPT plan usage" path for open-source and
locally hosted apps (https://developers.openai.com/siwc/token-sharing-open-source).
It is separate from the older "ChatGPT Subscription" provider in
src/chatgpt_subscription.py, which borrows the Codex CLI's device flow.

Flow, per the docs:

* one ``ext_agent_host_id`` (``urn:uuid:<v4>``) per install, persisted;
* browser authorization at auth.openai.com with PKCE (S256), ``state`` and
  ``nonce``, a loopback ``redirect_uri`` and ``client_id=dynamic_agent_client``
  on the first sign-in. The callback carries the issued ``client_id``, which
  is saved and used for every later sign-in and refresh;
* code exchange and refresh at the token endpoint (no client secret). The
  refresh token rotates, so both tokens are stored after every refresh;
* inference only through ``POST /v1/responses`` with ``store: false`` and
  ``stream: true``; the model list comes from ``GET /v1/models``.

Odysseus usually runs on a server reached over Tailscale, so the loopback
redirect lands on whatever device the browser is on and fails to load. The
user copies that address-bar URL back into Settings (``complete_sign_in``).
When the browser is on the server itself, a short-lived listener on
127.0.0.1 catches the callback automatically (``_LoopbackListener``).

Credentials are per Odysseus user, in ``<data dir>/chatgpt_plan/users/`` as
0600 JSON files whose token values are additionally Fernet-encrypted with the
app key (src/secret_storage.py), the same at-rest protection API keys get.
Tokens are never logged; ``redact`` scrubs them from error text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

PROVIDER = "chatgpt-plan"
AUTH_ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{AUTH_ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
API_BASE = "https://api.openai.com/v1"
RESPONSES_URL = f"{API_BASE}/responses"
MODELS_URL = f"{API_BASE}/models"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "Odysseus"
USAGE_SETTINGS_URL = "https://chatgpt.com/settings/usage"

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = int(os.getenv("ODYSSEUS_CHATGPT_LOOPBACK_PORT", "1455") or 1455)
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}{CALLBACK_PATH}"

# Odysseus-internal endpoint marker. The real requests go to API_BASE; this
# path only tells the provider-by-URL routing (llm_core._detect_provider)
# that a ModelEndpoint row is the ChatGPT plan rather than an OpenAI API key.
ENDPOINT_BASE = f"{API_BASE}/chatgpt-plan"
ENDPOINT_NAME = "ChatGPT"
PROVIDER_AUTH_MARKER = "chatgpt-plan"

PENDING_TTL_SECONDS = 600
REFRESH_SKEW_SECONDS = 300
# Refresh-token errors that mean the token set is dead (errors-and-recovery).
_DEAD_REFRESH_CODES = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_reused"}

SIGN_IN_AGAIN = "Your ChatGPT sign-in expired. Sign in again in Settings > Add Models > Sign in with ChatGPT."
USAGE_LIMIT_MESSAGE = (
    "Your ChatGPT plan's usage limit was reached for now. "
    f"Check ChatGPT Settings > Usage ({USAGE_SETTINGS_URL}) or try again later."
)
_ERROR_MESSAGES = {
    "subscription_sharing_usage_limit_exceeded": USAGE_LIMIT_MESSAGE,
    "subscription_sharing_usage_unavailable": (
        "ChatGPT couldn't check your plan's usage right now. Try again in a moment."
    ),
    "subscription_sharing_user_not_eligible": (
        "This ChatGPT account or workspace isn't eligible to use its plan in other apps."
    ),
    "subscription_sharing_unsupported_capability": (
        "ChatGPT plan usage doesn't support a feature this request used."
    ),
    "subscription_sharing_invalid_user": SIGN_IN_AGAIN,
}


class ChatGPTPlanError(RuntimeError):
    """A ChatGPT plan failure whose message is safe to show the user."""

    status = 502


class NotSignedIn(ChatGPTPlanError):
    status = 401


class ReauthRequired(ChatGPTPlanError):
    status = 401


class SignInRejected(ChatGPTPlanError):
    status = 400


# ── Redaction ─────────────────────────────────────────────────────────────

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*)?")
_KV_RE = re.compile(
    r"(?i)((?:access_token|refresh_token|id_token|code_verifier|code|authorization)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)"
    r"([^\s\"'&,}]+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")


def redact(text: Any, *secrets_: Optional[str]) -> str:
    """Scrub tokens from text that might end up in a log or an error."""
    out = "" if text is None else str(text)
    for value in secrets_:
        if value and len(value) >= 6:
            out = out.replace(value, "[redacted]")
    out = _JWT_RE.sub("[redacted]", out)
    out = _BEARER_RE.sub(lambda m: m.group(1) + "[redacted]", out)
    out = _KV_RE.sub(lambda m: m.group(1) + "[redacted]", out)
    return out


# ── Storage ───────────────────────────────────────────────────────────────

def _default_store_dir() -> Path:
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / "chatgpt_plan"


STORE_DIR: Optional[Path] = None  # tests point this at a temp dir


def _store_dir() -> Path:
    return Path(STORE_DIR) if STORE_DIR else _default_store_dir()


def _user_key(owner: Optional[str]) -> str:
    return hashlib.sha256((owner or "").encode("utf-8")).hexdigest()[:32]


def _creds_path(owner: Optional[str]) -> Path:
    return _store_dir() / "users" / f"{_user_key(owner)}.json"


def _registration_path(owner: Optional[str]) -> Path:
    return _store_dir() / "users" / f"{_user_key(owner)}.client.json"


def _write_private(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write JSON readable only by the owner (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("ChatGPT plan: unreadable credential file %s", path.name)
        return {}
    return data if isinstance(data, dict) else {}


def host_id() -> str:
    """This install's ext_agent_host_id, created once and kept."""
    path = _store_dir() / "host.json"
    data = _read_json(path)
    value = data.get("ext_agent_host_id")
    if isinstance(value, str) and value.startswith("urn:uuid:"):
        return value
    value = f"urn:uuid:{uuid.uuid4()}"
    _write_private(path, {"ext_agent_host_id": value})
    return value


_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")


def _encrypt(value: str) -> str:
    from src.secret_storage import encrypt

    return encrypt(value) if value else ""


def _decrypt(value: str) -> str:
    from src.secret_storage import decrypt

    return decrypt(value) if value else ""


def load_credentials(owner: Optional[str]) -> Dict[str, Any]:
    raw = _read_json(_creds_path(owner))
    if not raw:
        return {}
    out = dict(raw)
    for field in _TOKEN_FIELDS:
        out[field] = _decrypt(raw.get(field) or "")
    return out


def save_credentials(owner: Optional[str], creds: Dict[str, Any]) -> None:
    stored = dict(creds)
    for field in _TOKEN_FIELDS:
        stored[field] = _encrypt(creds.get(field) or "")
    _write_private(_creds_path(owner), stored)


def load_registration(owner: Optional[str]) -> Dict[str, Any]:
    return _read_json(_registration_path(owner))


def save_registration(owner: Optional[str], **fields: Any) -> None:
    reg = load_registration(owner)
    reg.update({k: v for k, v in fields.items()})
    _write_private(_registration_path(owner), reg)


def delete_credentials(owner: Optional[str]) -> bool:
    try:
        _creds_path(owner).unlink()
        return True
    except FileNotFoundError:
        return False


# ── PKCE / authorize URL ──────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_code_verifier() -> str:
    return _b64url(secrets.token_bytes(48))  # 64 chars, within RFC 7636's 43..128


def code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def build_authorize_url(
    *,
    client_id: str,
    state: str,
    nonce: str,
    challenge: str,
    host: str,
    redirect_uri: str = REDIRECT_URI,
    login_hint: Optional[str] = None,
    id_token_hint: Optional[str] = None,
) -> str:
    params = {
        "client_id": client_id,
        "ext_agent_host_id": host,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if client_id == DYNAMIC_CLIENT_ID:
        params["agent_name_hint"] = AGENT_NAME
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    elif login_hint:
        params["login_hint"] = login_hint
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


_pending: Dict[str, Dict[str, Any]] = {}
_pending_lock = threading.Lock()


def _prune_pending(now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    for key in [k for k, v in _pending.items() if now - v["created"] > PENDING_TTL_SECONDS]:
        _pending.pop(key, None)


def start_sign_in(owner: Optional[str], *, listen: bool = True) -> Dict[str, Any]:
    """Begin a sign-in: returns the authorize URL to open in a new tab."""
    reg = load_registration(owner)
    client_id = reg.get("client_id") or DYNAMIC_CLIENT_ID
    verifier = new_code_verifier()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    creds = load_credentials(owner)
    url = build_authorize_url(
        client_id=client_id,
        state=state,
        nonce=nonce,
        challenge=code_challenge(verifier),
        host=host_id(),
        login_hint=reg.get("email") if client_id != DYNAMIC_CLIENT_ID else None,
        id_token_hint=(creds.get("id_token") or None) if client_id != DYNAMIC_CLIENT_ID else None,
    )
    with _pending_lock:
        _prune_pending()
        # One live attempt per user: a new click replaces the old one.
        for key in [k for k, v in _pending.items() if v["owner"] == (owner or "")]:
            _pending.pop(key, None)
        _pending[state] = {
            "owner": owner or "",
            "verifier": verifier,
            "nonce": nonce,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "created": time.time(),
        }
    listening = _LISTENER.ensure_running() if listen and _listener_enabled() else False
    return {
        "authorize_url": url,
        "redirect_uri": REDIRECT_URI,
        "expires_in": PENDING_TTL_SECONDS,
        "listening": listening,
        "first_sign_in": client_id == DYNAMIC_CLIENT_ID,
    }


def parse_callback(pasted: str) -> Dict[str, str]:
    """Pull code/state/client_id/scope/error out of a pasted redirect URL."""
    text = (pasted or "").strip().strip("<>\"'")
    if not text:
        raise SignInRejected("Paste the full address from the browser tab that failed to load.")
    query = urlparse(text).query if "://" in text else text.lstrip("?")
    if "#" in query:
        query = query.split("#", 1)[0]
    params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items() if v}
    if not params:
        raise SignInRejected("That doesn't look like the sign-in redirect URL. Copy the whole address bar.")
    return params


def complete_sign_in(pasted_or_params: Any, *, owner: Optional[str]) -> Dict[str, Any]:
    """Validate the callback's state, exchange the code and store the tokens."""
    params = pasted_or_params if isinstance(pasted_or_params, dict) else parse_callback(pasted_or_params)
    if params.get("error"):
        desc = params.get("error_description") or params["error"]
        if params["error"] == "access_denied":
            raise SignInRejected("Sign-in was cancelled or ChatGPT access was declined.")
        raise SignInRejected(f"ChatGPT sign-in failed: {redact(desc)}")
    state = params.get("state") or ""
    code = params.get("code") or ""
    if not state or not code:
        raise SignInRejected("The pasted URL is missing the code or state. Copy the whole address bar.")
    with _pending_lock:
        _prune_pending()
        pending = _pending.get(state)
        if pending is None or pending["owner"] != (owner or ""):
            raise SignInRejected(
                "This sign-in link doesn't match a pending sign-in (it may have expired). "
                "Click Sign in with ChatGPT again."
            )
        _pending.pop(state, None)
    # The callback carries the issued client_id on a first registration; on
    # re-authorization it may be omitted, so keep the pending request's.
    client_id = params.get("client_id") or pending["client_id"]
    if client_id == DYNAMIC_CLIENT_ID:
        raise SignInRejected("ChatGPT didn't return a client id for this app. Try signing in again.")
    tokens = exchange_code(
        client_id=client_id,
        code=code,
        verifier=pending["verifier"],
        redirect_uri=pending["redirect_uri"],
    )
    claims = _id_token_claims(tokens.get("id_token") or "")
    if claims.get("nonce") and claims.get("nonce") != pending["nonce"]:
        raise SignInRejected("ChatGPT sign-in failed a security check (nonce mismatch). Try again.")
    scope = tokens.get("scope") or params.get("scope") or ""
    email = claims.get("email") or ""
    prior = load_registration(owner)
    if prior.get("email") and email and prior["email"] != email and prior.get("client_id") == client_id:
        logger.info("ChatGPT plan: sign-in switched accounts for this Odysseus user")
    now = time.time()
    creds = _creds_from_token_response(tokens, now=now)
    creds.update({"client_id": client_id, "email": email, "scope": scope, "signed_in_at": int(now)})
    save_credentials(owner, creds)
    save_registration(owner, client_id=client_id, email=email, signed_out_reason="")
    return status(owner)


def _creds_from_token_response(tokens: Dict[str, Any], *, now: float, previous: Optional[Dict] = None) -> Dict[str, Any]:
    previous = previous or {}
    access = tokens.get("access_token") or ""
    if not access:
        raise ChatGPTPlanError("ChatGPT didn't return an access token.")
    expires_in = tokens.get("expires_in")
    try:
        expires_at = now + float(expires_in)
    except (TypeError, ValueError):
        expires_at = float(_jwt_claims(access).get("exp") or now + 3600)
    try:
        earliest = float(tokens.get("earliest_refresh_at") or 0)
    except (TypeError, ValueError):
        earliest = 0.0
    out = dict(previous)
    out.update({
        "access_token": access,
        "refresh_token": tokens.get("refresh_token") or previous.get("refresh_token") or "",
        "id_token": tokens.get("id_token") or previous.get("id_token") or "",
        "expires_at": int(expires_at),
        "earliest_refresh_at": int(earliest),
    })
    if tokens.get("scope"):
        out["scope"] = tokens["scope"]
    return out


def _jwt_claims(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _id_token_claims(id_token: str) -> Dict[str, Any]:
    # The ID token comes straight from the token endpoint over TLS (OIDC Core
    # 3.1.3.7), so its claims are read for identity (email) and the nonce.
    return _jwt_claims(id_token)


# ── Token endpoint ────────────────────────────────────────────────────────

def _oauth_error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or "")
    return str(err or payload.get("code") or "")


def exchange_code(*, client_id: str, code: str, verifier: str, redirect_uri: str, timeout: float = 20.0) -> Dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "resource": RESOURCE,
    }
    try:
        response = httpx.post(
            TOKEN_URL, data=form, timeout=timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise ChatGPTPlanError(f"Couldn't reach ChatGPT to finish sign-in: {redact(type(exc).__name__)}") from None
    if response.status_code != 200:
        code_ = _oauth_error_code(response)
        raise SignInRejected(
            f"ChatGPT rejected the sign-in code ({response.status_code}{', ' + redact(code_) if code_ else ''}). "
            "Click Sign in with ChatGPT and try again."
        )
    data = response.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ChatGPTPlanError("ChatGPT's token response was missing the access token.")
    return data


def _refresh_request(client_id: str, refresh_token: str, timeout: float = 20.0) -> Dict[str, Any]:
    form = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "resource": RESOURCE,
    }
    try:
        response = httpx.post(
            TOKEN_URL, data=form, timeout=timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        # Transient: keep the credentials (errors-and-recovery).
        raise ChatGPTPlanError(f"Couldn't reach ChatGPT to refresh the sign-in ({type(exc).__name__}).") from None
    if response.status_code == 200:
        data = response.json()
        if isinstance(data, dict) and data.get("access_token"):
            return data
        raise ChatGPTPlanError("ChatGPT's refresh response was missing the access token.")
    code_ = _oauth_error_code(response)
    if code_ in _DEAD_REFRESH_CODES or code_ == "invalid_client" or response.status_code in (400, 401):
        raise ReauthRequired(SIGN_IN_AGAIN)
    raise ChatGPTPlanError(f"ChatGPT couldn't refresh the sign-in right now (HTTP {response.status_code}).")


_refresh_locks: Dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(owner: Optional[str]) -> threading.Lock:
    key = _user_key(owner)
    with _refresh_locks_guard:
        lock = _refresh_locks.get(key)
        if lock is None:
            lock = _refresh_locks[key] = threading.Lock()
        return lock


def _needs_refresh(creds: Dict[str, Any], now: float) -> bool:
    expires_at = float(creds.get("expires_at") or 0)
    if now < expires_at - REFRESH_SKEW_SECONDS:
        return False
    earliest = float(creds.get("earliest_refresh_at") or 0)
    # Inside the skew window: refresh once OpenAI allows it, and always once
    # the access token is about to lapse.
    return now >= earliest or now >= expires_at - 30


def mark_signed_out(owner: Optional[str], reason: str) -> None:
    delete_credentials(owner)
    save_registration(owner, signed_out_reason=reason)
    try:
        remove_endpoint(owner)
    except Exception as exc:  # the endpoint row is a convenience, not state
        logger.debug("ChatGPT plan: endpoint removal after sign-out failed: %s", type(exc).__name__)


def get_access_token(owner: Optional[str], *, now: Optional[float] = None) -> str:
    """A current access token for this user, refreshing first when due."""
    creds = load_credentials(owner)
    if not creds.get("access_token"):
        raise NotSignedIn("Sign in with ChatGPT in Settings > Add Models to use ChatGPT models.")
    t = time.time() if now is None else now
    if not _needs_refresh(creds, t):
        return creds["access_token"]
    with _refresh_lock(owner):
        creds = load_credentials(owner)  # another request may have refreshed
        if not creds.get("access_token"):
            raise NotSignedIn(SIGN_IN_AGAIN)
        t = time.time() if now is None else now
        if not _needs_refresh(creds, t):
            return creds["access_token"]
        if not creds.get("refresh_token") or not creds.get("client_id"):
            mark_signed_out(owner, "missing_refresh_token")
            raise ReauthRequired(SIGN_IN_AGAIN)
        try:
            refreshed = _refresh_request(creds["client_id"], creds["refresh_token"])
        except ReauthRequired:
            logger.info("ChatGPT plan: refresh token rejected; user must sign in again")
            mark_signed_out(owner, "refresh_failed")
            raise
        creds = _creds_from_token_response(refreshed, now=t, previous=creds)
        creds["last_refresh"] = int(t)
        save_credentials(owner, creds)
        return creds["access_token"]


def status(owner: Optional[str]) -> Dict[str, Any]:
    creds = load_credentials(owner)
    reg = load_registration(owner)
    signed_in = bool(creds.get("access_token") and creds.get("refresh_token"))
    scope = creds.get("scope") or ""
    out: Dict[str, Any] = {
        "signed_in": signed_in,
        "email": (creds.get("email") or reg.get("email") or "") if signed_in else "",
        "plan_usage_enabled": signed_in and (not scope or REQUIRED_SCOPE in scope.split()),
        "redirect_uri": REDIRECT_URI,
        "usage_url": USAGE_SETTINGS_URL,
        "registered": bool(reg.get("client_id")),
        "needs_sign_in_again": (not signed_in) and bool(reg.get("signed_out_reason")),
    }
    if signed_in:
        out["expires_at"] = creds.get("expires_at")
    with _pending_lock:
        _prune_pending()
        out["pending"] = any(v["owner"] == (owner or "") for v in _pending.values())
    return out


def sign_out(owner: Optional[str]) -> Dict[str, Any]:
    """Delete this user's tokens and their ChatGPT endpoint row."""
    with _refresh_lock(owner):
        delete_credentials(owner)
        save_registration(owner, signed_out_reason="")
        with _pending_lock:
            for key in [k for k, v in _pending.items() if v["owner"] == (owner or "")]:
                _pending.pop(key, None)
    remove_endpoint(owner)
    return status(owner)


# ── Models ────────────────────────────────────────────────────────────────

def fetch_models(access_token: str, timeout: float = 15.0) -> List[Dict[str, str]]:
    """List models the plan offers: ``visibility == "list"`` entries only."""
    try:
        response = httpx.get(
            MODELS_URL, timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise ChatGPTPlanError(f"Couldn't reach OpenAI for the model list ({type(exc).__name__}).") from None
    if response.status_code in (401, 403):
        raise ReauthRequired(SIGN_IN_AGAIN if response.status_code == 401
                             else "ChatGPT plan usage isn't available for this account.")
    if response.status_code != 200:
        raise ChatGPTPlanError(f"OpenAI returned HTTP {response.status_code} for the model list.")
    try:
        data = response.json()
    except Exception:
        raise ChatGPTPlanError("OpenAI's model list wasn't valid JSON.") from None
    return filter_models(data)


def filter_models(data: Any) -> List[Dict[str, str]]:
    entries: Iterable = []
    if isinstance(data, dict):
        entries = data.get("models") or data.get("data") or []
    elif isinstance(data, list):
        entries = data
    out: List[Dict[str, str]] = []
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip() or slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug.strip(), "display_name": str(item.get("display_name") or slug).strip()})
    return out


# ── Endpoint row (so the models show in the picker) ───────────────────────

def _session_local():
    from core.database import SessionLocal

    return SessionLocal


def upsert_endpoint(owner: Optional[str], models: List[str]) -> Dict[str, Any]:
    from core.database import ModelEndpoint

    db = _session_local()()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.base_url == ENDPOINT_BASE)
        q = q.filter(ModelEndpoint.owner == owner) if owner else q.filter(ModelEndpoint.owner.is_(None))
        ep = q.first()
        if ep is None:
            ep = ModelEndpoint(id=str(uuid.uuid4())[:8], name=ENDPOINT_NAME, base_url=ENDPOINT_BASE,
                               owner=owner or None)
            db.add(ep)
        ep.name = ENDPOINT_NAME
        ep.api_key = None
        ep.provider_auth_id = PROVIDER_AUTH_MARKER
        ep.is_enabled = True
        ep.model_type = "llm"
        ep.endpoint_kind = "api"
        ep.model_refresh_mode = "manual"
        # The Responses API takes function tools (build_payload maps them), so
        # agent mode sends native tool schemas. The endpoint toggle can still
        # switch this off; only an unset value gets the default.
        if ep.supports_tools is None:
            ep.supports_tools = True
        ep.cached_models = json.dumps(models)
        db.commit()
        result = {"id": ep.id, "name": ep.name, "models": models}
    finally:
        db.close()
    _invalidate_models_cache()
    return result


_NATIVE_TOOLS_MARKER = "native_tools_default.json"


def enable_native_tools_once(session_local=None) -> int:
    """One-time upgrade for endpoint rows saved before native tools were the
    default: those got ``supports_tools=False``, which made agent mode send
    the plan models no tools. Flip them on once, then leave the toggle to
    the user. Returns the number of rows changed."""
    marker = _store_dir() / _NATIVE_TOOLS_MARKER
    if marker.exists():
        return 0
    from core.database import ModelEndpoint

    db = (session_local or _session_local())()
    try:
        rows = db.query(ModelEndpoint).filter(
            ModelEndpoint.base_url == ENDPOINT_BASE,
            ModelEndpoint.supports_tools == False,  # noqa: E712
        ).all()
        for ep in rows:
            ep.supports_tools = True
        db.commit()
        changed = len(rows)
    finally:
        db.close()
    _write_private(marker, {"done": True, "at": int(time.time())})
    if changed:
        logger.info("ChatGPT plan: turned on native tools for %d endpoint(s)", changed)
    return changed


def remove_endpoint(owner: Optional[str]) -> int:
    from core.database import ModelEndpoint

    db = _session_local()()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.base_url == ENDPOINT_BASE)
        q = q.filter(ModelEndpoint.owner == owner) if owner else q.filter(ModelEndpoint.owner.is_(None))
        n = 0
        for ep in q.all():
            db.delete(ep)
            n += 1
        db.commit()
    finally:
        db.close()
    if n:
        _invalidate_models_cache()
    return n


def _invalidate_models_cache() -> None:
    try:
        from routes.model_routes import _invalidate_models_cache as inv

        inv()
    except Exception:
        pass


def refresh_models(owner: Optional[str]) -> Dict[str, Any]:
    token = get_access_token(owner)
    models = fetch_models(token)
    slugs = [m["slug"] for m in models]
    endpoint = upsert_endpoint(owner, slugs)
    return {"models": models, "endpoint": endpoint}


# ── URL routing helpers ───────────────────────────────────────────────────

def is_chatgpt_plan_base(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "").rstrip("/")
    marker = urlparse(ENDPOINT_BASE).path
    return host == "api.openai.com" and (path == marker or path.startswith(marker + "/"))


def uses_request_scoped_bearer(url: str) -> bool:
    """True for providers whose short-lived bearer must never be persisted."""
    if is_chatgpt_plan_base(url):
        return True
    try:
        from src.chatgpt_subscription import is_chatgpt_subscription_base

        return is_chatgpt_subscription_base(url)
    except Exception:
        return False


def headers_for(access_token: Optional[str]) -> Dict[str, str]:
    h = {"Accept": "text/event-stream"}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


# ── Chat -> Responses translation ─────────────────────────────────────────

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _user_parts(content: Any) -> List[Dict[str, Any]]:
    if not isinstance(content, list):
        return [{"type": "input_text", "text": _text_of(content)}]
    parts: List[Dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text") and isinstance(part.get("text"), str):
            parts.append({"type": "input_text", "text": part["text"]})
        elif kind == "image_url":
            img = part.get("image_url")
            url = img.get("url") if isinstance(img, dict) else img
            if isinstance(url, str) and url:
                item: Dict[str, Any] = {"type": "input_image", "image_url": url}
                if isinstance(img, dict) and img.get("detail"):
                    item["detail"] = img["detail"]
                parts.append(item)
        elif kind == "input_image":
            parts.append(part)
    return parts or [{"type": "input_text", "text": ""}]


def messages_to_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate the app's chat-completions style messages to Responses input."""
    items: List[Dict[str, Any]] = []
    for msg in messages or []:
        role = msg.get("role") or "user"
        content = msg.get("content")
        if role == "system" or role == "developer":
            text = _text_of(content)
            if text:
                items.append({"role": "system", "content": [{"type": "input_text", "text": text}]})
        elif role == "assistant":
            text = _text_of(content)
            if text:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {})
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or f"call_{len(items)}",
                    "name": fn.get("name") or tc.get("name") or "",
                    "arguments": args,
                })
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id:
                items.append({"type": "function_call_output", "call_id": call_id, "output": _text_of(content)})
            else:
                items.append({"role": "user", "content": [{"type": "input_text", "text": _text_of(content)}]})
        else:
            items.append({"role": "user", "content": _user_parts(content)})
    return items


def tools_to_responses(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else None
        if isinstance(fn, dict) and fn.get("name"):
            # The Responses API treats function tools as strict unless told
            # otherwise, and strict mode rejects schemas with optional
            # properties (most of ours). Chat Completions defaults to
            # non-strict, which is what these schemas were written for.
            item = {"type": "function", "name": fn["name"],
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    "strict": bool(fn.get("strict", False))}
            if fn.get("description"):
                item["description"] = fn["description"]
            out.append(item)
        elif tool.get("type") == "function" and tool.get("name"):
            out.append(tool)
    return out


def build_payload(model: str, messages: List[Dict[str, Any]], tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "input": messages_to_input(messages),
        "store": False,
        "stream": True,
    }
    mapped = tools_to_responses(tools)
    if mapped:
        payload["tools"] = mapped
    # store/stream are required by ChatGPT plan usage; set last so nothing
    # above can override them.
    payload["store"] = False
    payload["stream"] = True
    return payload


def friendly_error(code: Optional[str], message: Optional[str] = None, status: Optional[int] = None) -> str:
    if code and code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code]
    if status == 401:
        return SIGN_IN_AGAIN
    if status == 403:
        return "ChatGPT plan usage was blocked for this request" + (f": {redact(message)}" if message else ".")
    if status == 429:
        return USAGE_LIMIT_MESSAGE
    if status == 503:
        return "ChatGPT plan usage is temporarily unavailable. Try again in a moment."
    detail = redact(message) if message else ""
    return "ChatGPT request failed" + (f": {detail}" if detail else ".")


def _sse(data: Any) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _sse_error(text: str, status: int, code: str = "") -> str:
    body = {"error": text, "text": text, "status": status}
    if code:
        body["code"] = code
    return f"event: error\ndata: {json.dumps(body)}\n\n"


def error_chunk_for_http(status_code: int, raw: str) -> str:
    code, message = "", ""
    try:
        body = json.loads(raw) if raw else {}
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
    except Exception:
        message = (raw or "")[:200]
    return _sse_error(friendly_error(code, message, status_code), status_code, code)


class StreamTranslator:
    """Turns Responses SSE events into the app's stream chunks.

    The app's protocol (src/llm_core._stream_llm_inner): ``data: {"delta"}``
    text, ``{"type": "tool_calls"}`` before the end, ``{"type": "usage"}``,
    ``event: error`` and ``data: [DONE]``.
    """

    def __init__(self) -> None:
        self.done = False
        self.completed = False
        self.emitted_text = False
        self._calls: Dict[str, Dict[str, str]] = {}
        self._order: List[str] = []

    def _call(self, key: str) -> Dict[str, str]:
        if key not in self._calls:
            self._calls[key] = {"id": "", "name": "", "arguments": ""}
            self._order.append(key)
        return self._calls[key]

    def feed(self, event: Dict[str, Any]) -> List[str]:
        if self.done or not isinstance(event, dict):
            return []
        kind = event.get("type") or ""
        out: List[str] = []
        if kind == "response.output_text.delta":
            delta = event.get("delta") or ""
            if delta:
                self.emitted_text = True
                out.append(_sse({"delta": delta}))
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            delta = event.get("delta") or ""
            if delta:
                out.append(_sse({"delta": delta, "thinking": True}))
        elif kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call = self._call(str(item.get("id") or item.get("call_id") or event.get("output_index")))
                call["id"] = item.get("call_id") or call["id"] or str(item.get("id") or "")
                call["name"] = item.get("name") or call["name"]
                if kind == "response.output_item.done" and isinstance(item.get("arguments"), str):
                    call["arguments"] = item["arguments"]
        elif kind == "response.function_call_arguments.delta":
            call = self._call(str(event.get("item_id") or event.get("output_index")))
            call["arguments"] += event.get("delta") or ""
        elif kind == "response.function_call_arguments.done":
            call = self._call(str(event.get("item_id") or event.get("output_index")))
            if isinstance(event.get("arguments"), str):
                call["arguments"] = event["arguments"]
        elif kind == "response.completed":
            self.completed = True
            calls = [self._calls[k] for k in self._order if self._calls[k]["name"]]
            if calls:
                out.append(_sse({"type": "tool_calls", "calls": calls}))
            usage = (event.get("response") or {}).get("usage") or {}
            if usage.get("input_tokens") or usage.get("output_tokens"):
                out.append(_sse({"type": "usage", "data": {
                    "input_tokens": usage.get("input_tokens") or 0,
                    "output_tokens": usage.get("output_tokens") or 0,
                }}))
            out.append("data: [DONE]\n\n")
            self.done = True
        elif kind in ("response.failed", "error"):
            err = (event.get("response") or {}).get("error") or event.get("error") or {}
            if not isinstance(err, dict):
                err = {"message": str(err)}
            code = str(err.get("code") or event.get("code") or "")
            message = err.get("message") or event.get("message") or ""
            status = 429 if code == "subscription_sharing_usage_limit_exceeded" else 502
            out.append(_sse_error(friendly_error(code, message), status, code))
            self.done = True
        elif kind == "response.incomplete":
            details = (event.get("response") or {}).get("incomplete_details") or {}
            reason = details.get("reason") or "unknown"
            text = f"ChatGPT stopped before finishing the response (reason: {reason})."
            if self.emitted_text:
                out.append(_sse({"delta": f"\n\n[{text}]"}))
                out.append("data: [DONE]\n\n")
            else:
                out.append(_sse_error(text, 502, "incomplete"))
            self.done = True
        return out

    def finish(self) -> List[str]:
        """Call when the upstream stream ends; errors if it never completed."""
        if self.done:
            return []
        self.done = True
        return [_sse_error("ChatGPT ended the response before it finished. Try again.", 502, "stream_ended")]


def parse_data_line(line: str, event_name: str = "") -> Optional[Dict[str, Any]]:
    """One SSE ``data:`` line as an event dict, or None."""
    if not line or not line.startswith("data:"):
        return None
    raw = line[5:].strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("type") and event_name:
        data["type"] = event_name
    return data


def iter_sse_events(lines: Iterable[str]) -> Iterator[Dict[str, Any]]:
    event_name = ""
    for line in lines:
        if not line:
            event_name = ""
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        event = parse_data_line(line, event_name)
        if event is not None:
            yield event


# ── Optional loopback listener ────────────────────────────────────────────

def _listener_enabled() -> bool:
    return os.getenv("ODYSSEUS_CHATGPT_LOOPBACK_LISTENER", "1").strip().lower() not in ("0", "false", "no", "off")


_DONE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Odysseus</title>"
    "<body style='font-family:system-ui;padding:40px'><h2>{title}</h2><p>{body}</p></body>"
)


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # the query string holds the code
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
        state = params.get("state") or ""
        with _pending_lock:
            pending = _pending.get(state)
        if pending is None:
            title, body, code = ("Sign-in not recognized",
                                 "Go back to Odysseus and paste this page's address into the ChatGPT card.", 400)
        else:
            try:
                complete_sign_in(params, owner=pending["owner"] or None)
                try:
                    refresh_models(pending["owner"] or None)
                except Exception as exc:
                    logger.info("ChatGPT plan: model refresh after sign-in failed: %s", redact(exc))
                title, body, code = ("Signed in to ChatGPT", "You can close this tab and return to Odysseus.", 200)
            except ChatGPTPlanError as exc:
                title, body, code = ("Sign-in didn't finish", redact(exc), 400)
            except Exception:
                logger.exception("ChatGPT plan: loopback sign-in failed")
                title, body, code = ("Sign-in didn't finish", "Something went wrong. Try again from Odysseus.", 500)
        page = _DONE_PAGE.format(title=title, body=body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


class _LoopbackListener:
    """Serves 127.0.0.1:<port>/auth/callback while a sign-in is pending."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: Optional[HTTPServer] = None
        self._deadline = 0.0

    def ensure_running(self, port: Optional[int] = None) -> bool:
        with self._lock:
            self._deadline = time.time() + PENDING_TTL_SECONDS
            if self._server is not None:
                return True
            try:
                server = HTTPServer((LOOPBACK_HOST, LOOPBACK_PORT if port is None else port), _CallbackHandler)
            except OSError:
                # Port busy (e.g. a Codex CLI login). The paste path still works.
                return False
            server.timeout = 1.0
            self._server = server
        threading.Thread(target=self._serve, args=(server,), name="chatgpt-plan-callback", daemon=True).start()
        return True

    def _serve(self, server: HTTPServer) -> None:
        try:
            while True:
                with self._lock:
                    if self._server is not server:
                        break
                    with _pending_lock:
                        _prune_pending()
                        idle = not _pending
                    if time.time() > self._deadline or idle:
                        self._server = None
                        break
                server.handle_request()
        finally:
            server.server_close()

    def stop(self) -> None:
        with self._lock:
            self._server = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> Optional[int]:
        server = self._server
        return server.server_address[1] if server else None


_LISTENER = _LoopbackListener()
