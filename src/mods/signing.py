"""Signed identity headers for mods, and the frame tokens of mod pages.

Identity (core to mod). Every request the core forwards to a mod, and every
event it delivers, carries:

    X-Odysseus-User  who the request is for (never empty):
                       - the signed-in user, for pages and API calls through
                         the proxy, and for tool calls (the user the agent
                         works for);
                       - "local" for those when sign-in is turned off;
                       - "odysseus-core" (CORE_USER) for what the core does
                         on its own: reading the tool list, delivering
                         events, and a tool call that has no user.
    X-Odysseus-Mod   the mod id
    X-Odysseus-Ts    unix seconds
    X-Odysseus-Sig   hex HMAC-SHA256(key=ODYSSEUS_MOD_TOKEN,
                         msg="v1\\n<mod id>\\n<user>\\n<ts>")

A mod checks the signature with its own token and rejects a timestamp more
than SKEW_S seconds away. Rotating the token rotates the key.

Frame tokens (browser to core). Mod pages are shown in sandboxed iframes with
an opaque origin, so the browser does not send the session cookie with the
page's own requests. The page is therefore loaded under
/mods/<id>/~<token>/..., and the token (HMAC of user, mod and expiry with a
core-only secret) is what authenticates those requests. It works only for
that one mod and expires after FRAME_TTL_S.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Dict, Optional, Tuple

SKEW_S = 300
FRAME_TTL_S = 12 * 3600
IDENTITY_HEADERS = ("x-odysseus-user", "x-odysseus-mod", "x-odysseus-ts", "x-odysseus-sig")

CORE_USER = "odysseus-core"
LOCAL_USER = "local"
# A /mods/<id>/public/ path of a mod granted public.http: nobody signed in
# (a webhook). The mod accepts it only on its /public/ routes.
PUBLIC_USER = "odysseus-public"

_frame_secret: Optional[bytes] = None


def caller(user: Optional[str], auth_disabled: bool) -> str:
    """The X-Odysseus-User for a call made for `user` (see the module doc)."""
    if user:
        return user
    return LOCAL_USER if auth_disabled else CORE_USER


def new_token() -> str:
    return "odm_" + secrets.token_urlsafe(32)


def sign_identity(token: str, mod_id: str, user: str, ts: Optional[int] = None) -> Dict[str, str]:
    ts = int(ts if ts is not None else time.time())
    msg = f"v1\n{mod_id}\n{user}\n{ts}".encode()
    sig = hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()
    return {"X-Odysseus-User": user, "X-Odysseus-Mod": mod_id, "X-Odysseus-Ts": str(ts), "X-Odysseus-Sig": sig}


def verify_identity(token: str, headers, now: Optional[float] = None) -> Optional[str]:
    """The user named by valid identity headers, else None (the mod side of
    sign_identity; the SDK has its own copy so mods need not import the core)."""
    get = headers.get
    user, mod_id, ts, sig = (get("X-Odysseus-User") or get("x-odysseus-user"),
                             get("X-Odysseus-Mod") or get("x-odysseus-mod"),
                             get("X-Odysseus-Ts") or get("x-odysseus-ts"),
                             get("X-Odysseus-Sig") or get("x-odysseus-sig"))
    if not (user and mod_id and ts and sig):
        return None
    try:
        t = int(ts)
    except ValueError:
        return None
    if abs((now if now is not None else time.time()) - t) > SKEW_S:
        return None
    want = sign_identity(token, mod_id, user, t)["X-Odysseus-Sig"]
    return user if hmac.compare_digest(want, sig) else None


def _secret() -> bytes:
    """A per-install secret for frame tokens, kept in DATA_DIR/mods/.frame_secret."""
    global _frame_secret
    if _frame_secret is None:
        from src.mods import paths
        p = os.path.join(paths.mods_root(), ".frame_secret")
        try:
            with open(p, "rb") as f:
                data = f.read().strip()
            if len(data) >= 32:
                _frame_secret = data
        except FileNotFoundError:
            pass
        if _frame_secret is None:
            data = secrets.token_hex(32).encode()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            _frame_secret = data
    return _frame_secret


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def frame_token(mod_id: str, user: str, ttl: int = FRAME_TTL_S, now: Optional[float] = None) -> str:
    exp = int((now if now is not None else time.time()) + ttl)
    body = f"{mod_id}|{user}|{exp}".encode()
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()[:18]
    return _b64(body) + "." + _b64(sig)


def check_frame_token(token: str, mod_id: str, now: Optional[float] = None) -> Optional[str]:
    """The user a valid frame token for this mod was made for, else None."""
    try:
        b, s = token.split(".", 1)
        body = _unb64(b)
        sig = _unb64(s)
    except Exception:
        return None
    want = hmac.new(_secret(), body, hashlib.sha256).digest()[:18]
    if not hmac.compare_digest(want, sig):
        return None
    try:
        mid, user, exp = body.decode().split("|")
        exp_i = int(exp)
    except Exception:
        return None
    if mid != mod_id or exp_i < (now if now is not None else time.time()):
        return None
    return user


def split_frame_path(rest: str) -> Tuple[Optional[str], str]:
    """'~<token>/a/b' -> (token, '/a/b'); anything else -> (None, '/' + rest)."""
    if rest.startswith("~"):
        tok, _, tail = rest[1:].partition("/")
        return tok, "/" + tail
    return None, "/" + rest
