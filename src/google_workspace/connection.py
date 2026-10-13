"""The one Google account connection each user has, shared by every Google
product Odysseus uses (Calendar for Meet, Drive, Docs, Sheets, Slides, Tasks,
Contacts).

It uses the server's Google OAuth client (src/google_oauth.py, the one Sign
in with Google uses) and comes back through the same callback,
/api/auth/google/callback, with access_type=offline so there is a refresh
token. Turning a product on asks Google for that product's scopes plus the
scopes of every product already on, with include_granted_scopes=true
(incremental authorization). Asking again for the ones already on costs
nothing, and on Google's granular consent screen, which lists every scope
with its own checkbox, it puts them in front of the user together: one that
was unticked, or one Google dropped, is granted again instead of silently
going missing.

What the grant really holds is asked of Google, not trusted from the token
response: after a connect, and when Settings > Google loads (cached for
RECHECK_S), the tokeninfo endpoint says which scopes the access token has.
The token response's `scope` has been seen to leave out scopes granted
before. A product that was on and whose scope is gone shows as "Needs
reconnect", and the connect that lost it says so.

Storage: one file per user in DATA_DIR/google_calendar/ (the name predates
the other products; connections made for Meet keep working), 0600, with the
refresh token Fernet-encrypted by the app key (src/secret_storage.py). It is
never sent to the browser and never logged; access tokens are made from it
as needed and kept only in memory.

Turning one product off only stops Odysseus using it: Google has no way to
take back part of a grant. Turning the last product off, or Disconnect
Google, revokes the whole grant at Google and deletes the file.
"""

import base64
import hashlib
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import httpx

from src import google_oauth
from src.google_workspace import products as P

logger = logging.getLogger(__name__)

RECONNECT = "Connect it again in Settings > Google."
STORE_DIRNAME = "google_calendar"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
# How long a tokeninfo answer is trusted before Settings > Google asks again.
RECHECK_S = 300

# Tests swap in an httpx.MockTransport; nothing here talks to Google then.
_transport: Optional[httpx.AsyncBaseTransport] = None
# user key -> (access token, expires at)
_access: Dict[str, Tuple[str, float]] = {}


class GoogleError(Exception):
    """A sentence for the user about why it did not work."""


class _RedactAccessToken(logging.Filter):
    """httpx logs every request URL at INFO, and tokeninfo takes the access
    token in its query string. Keep it out of the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any("access_token=" in str(a) for a in record.args):
            record.args = tuple(re.sub(r"access_token=[^&\s\"]*", "access_token=[redacted]", str(a))
                                if "access_token=" in str(a) else a for a in record.args)
        return True


logging.getLogger("httpx").addFilter(_RedactAccessToken())


# ── storage ──

def _dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, STORE_DIRNAME)


def _key(user: Optional[str]) -> str:
    u = user or ""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", u)[:40] or "_default"
    return f"{safe}-{hashlib.sha256(u.encode('utf-8')).hexdigest()[:8]}"


def _path(user: Optional[str]) -> str:
    return os.path.join(_dir(), _key(user) + ".json")


def load(user: Optional[str]) -> Dict:
    try:
        with open(_path(user), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(user: Optional[str], data: Dict) -> None:
    from core.atomic_io import atomic_write_json
    from core.platform_compat import safe_chmod
    os.makedirs(_dir(), exist_ok=True)
    atomic_write_json(_path(user), data, indent=2)
    safe_chmod(_path(user), 0o600)


def _forget(user: Optional[str]) -> None:
    _access.pop(_key(user), None)
    try:
        os.remove(_path(user))
    except FileNotFoundError:
        pass


def _refresh_token(user: Optional[str]) -> str:
    from src import secret_storage
    return secret_storage.decrypt(str(load(user).get("refresh_token") or ""))


def granted_scopes(user: Optional[str]) -> List[str]:
    return str(load(user).get("scopes") or "").split()


def _stored_products(d: Dict) -> List[str]:
    """The products switched on. A connection made before products existed
    was made for Meet, so it is Calendar."""
    if "products" in d and isinstance(d.get("products"), list):
        return [str(p) for p in d["products"] if str(p) in P.PRODUCTS]
    return ["calendar"] if P.CALENDAR_SCOPE in str(d.get("scopes") or "").split() else []


def _granted(product: str, scopes: List[str]) -> bool:
    need = (P.PRODUCTS.get(product) or {}).get("scopes") or []
    return bool(need) and all(s in scopes for s in need)


def enabled_products(user: Optional[str]) -> List[str]:
    """Products on AND with their scopes granted (and their parent on)."""
    d = load(user)
    if not d.get("refresh_token"):
        return []
    scopes = str(d.get("scopes") or "").split()
    on = [p for p in _stored_products(d) if _granted(p, scopes)]
    return [p for p in on if not P.PRODUCTS[p].get("parent") or P.PRODUCTS[p]["parent"] in on]


def is_enabled(user: Optional[str], product: str) -> bool:
    return product in enabled_products(user)


def needs_reconnect(user: Optional[str]) -> List[str]:
    """Products switched on whose scopes the grant no longer holds: Google
    dropped them, or a box was unticked on a later consent screen."""
    d = load(user)
    if not d.get("refresh_token"):
        return []
    scopes = str(d.get("scopes") or "").split()
    return [p for p in _stored_products(d) if not _granted(p, scopes)]


def _short(product: str) -> str:
    label = P.PRODUCTS[product]["label"]
    return label[len("Google "):] if label.startswith("Google ") else label


def lost_message(products: List[str]) -> str:
    return " ".join(f"Google did not keep access to {_short(p)}. Connect {_short(p)} again and keep "
                    "every box ticked on Google's consent screen." for p in products)


# ── connecting ──

def status(user: Optional[str]) -> Dict:
    """What Settings > Google shows. Nothing secret."""
    d = load(user)
    scopes = str(d.get("scopes") or "").split()
    on = enabled_products(user)
    lost = needs_reconnect(user)
    unverified = [str(p) for p in d.get("unverified") or []]
    items = []
    for key, p in P.PRODUCTS.items():
        items.append({
            "key": key,
            "label": p["label"],
            "about": p["about"],
            "api": p["api"],
            "parent": p.get("parent") or "",
            "scopes": list(p["scopes"]) + list(p.get("extra") or []),
            "granted": _granted(key, scopes),
            "enabled": key in on,
            "needs_reconnect": key in lost,
            # Optional permissions (`extra`) a product that is on still lacks,
            # e.g. the calendar list for a Calendar connected before it was
            # asked for. Connecting the product again asks for them.
            "missing_extra": [s for s in (p.get("extra") or []) if s not in scopes] if key in on else [],
            "unverified": key in on and key in unverified,
            "tools": P.tools_for(key) + list(p.get("tools") or []),
        })
    return {
        "configured": google_oauth.client_config()["configured"],
        "connected": bool(d.get("refresh_token")),
        "email": str(d.get("email") or ""),
        "scopes": scopes,
        "connected_at": d.get("connected_at") or 0,
        "checked_at": d.get("checked_at") or 0,
        "products": items,
    }


def clean_products(raw) -> List[str]:
    items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
    out: List[str] = []
    for k in items:
        k = str(k or "").strip().lower()
        if k in P.PRODUCTS and k not in out:
            out.append(k)
    # A child product needs its parent's permission too.
    for k in list(out):
        parent = P.PRODUCTS[k].get("parent")
        if parent and parent not in out:
            out.insert(0, parent)
    return out


def connect_url(request, user: Optional[str], products: List[str]) -> str:
    """Google's consent page for these products. It asks again for the
    scopes of every product already on (see the module docstring for why);
    the state remembers only the new ones as what this connect is for."""
    if not google_oauth.client_config()["configured"]:
        raise GoogleError("No Google OAuth client is set up on this server yet.")
    products = clean_products(products)
    if not products:
        raise GoogleError("Pick a Google product to connect.")
    d = load(user)
    on = _stored_products(d) if d.get("refresh_token") else []
    return google_oauth.authorize_url(
        request, {"purpose": "google", "user": user or "", "products": products},
        scope=" ".join(P.scopes_for(products + [p for p in on if p not in products])),
        # Offline with consent so Google hands over a refresh token every
        # time, even for an account that connected before.
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )


def _claims(id_token: str) -> Dict:
    # Straight from Google's token endpoint over TLS, as in auth_routes.
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def client(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


async def _revoke(token: str) -> None:
    if not token:
        return
    try:
        async with client(10) as c:
            await c.post(google_oauth.api_url(google_oauth.REVOKE_URL), data={"token": token})
    except httpx.HTTPError:
        pass


async def _tokeninfo(access: str) -> Optional[List[str]]:
    """The scopes Google says this access token has, or None when Google
    could not be asked or the token is not this server's client's."""
    if not access:
        return None
    try:
        async with client(10) as c:
            r = await c.get(google_oauth.api_url(TOKENINFO_URL), params={"access_token": access})
        if r.status_code != 200:
            logger.info("[google] tokeninfo said %s", r.status_code)
            return None
        info = r.json()
    except Exception as e:                  # network, bad JSON
        logger.info("[google] tokeninfo: %s", type(e).__name__)
        return None
    if not isinstance(info, dict) or "scope" not in info:
        return None
    aud = str(info.get("aud") or info.get("azp") or "")
    cid = google_oauth.client_config()["client_id"]
    if aud and cid and aud != cid:
        logger.warning("[google] tokeninfo: the token belongs to another OAuth client")
        return None
    return str(info.get("scope") or "").split()


async def recheck(user: Optional[str], force: bool = False) -> Dict:
    """Ask Google which scopes the grant holds now and save them. At most
    every RECHECK_S unless forced or something is unverified. Refreshes the
    access token if needed. Returns {"checked": bool, "lost": [products]};
    never raises, and nothing in it is secret."""
    nothing = {"checked": False, "lost": []}
    d = load(user)
    if not d.get("refresh_token"):
        return nothing
    recent = time.time() - float(d.get("checked_at") or 0) < RECHECK_S
    if recent and not force and not d.get("unverified"):
        return nothing
    try:
        async with client(15) as c:
            access = await token(c, user)
    except GoogleError as e:
        logger.info("[google] recheck for %s: %s", user or "-", e)
        return nothing
    scopes = await _tokeninfo(access)
    if scopes is None:
        return nothing
    now = load(user)                         # a connect may have finished meanwhile
    if not now.get("refresh_token") or now.get("refresh_token") != d.get("refresh_token"):
        return nothing
    before = str(now.get("scopes") or "").split()
    now["products"] = _stored_products(now)  # pin a Meet-era Calendar so a loss shows
    lost = [p for p in now["products"] if _granted(p, before) and not _granted(p, scopes)]
    now["scopes"] = " ".join(scopes)
    now["unverified"] = []
    now["checked_at"] = time.time()
    _save(user, now)
    if lost:
        logger.info("[google] recheck for %s: no longer granted %s", user or "-", ",".join(lost))
    return {"checked": True, "lost": lost}


async def finish_connect(user: str, code: str, state_entry: Dict) -> Tuple[bool, str]:
    """Trade the code for tokens and keep the refresh token. (ok, message).

    state_entry is the payload from google_oauth.pop_state(): the products
    asked for, the PKCE code_verifier and the redirect_uri. A state from the
    older Meet-only flow (purpose "calendar") asked for Calendar.
    """
    if not isinstance(state_entry, dict):
        state_entry = {}
    asked = clean_products(state_entry.get("products") or
                           (["calendar"] if state_entry.get("purpose") == "calendar" else []))
    if not google_oauth.client_config()["configured"]:
        return False, "No Google OAuth client is set up on this server."
    try:
        r = await google_oauth.exchange_code(code, state_entry, transport=_transport)
    except Exception as e:
        logger.warning("[google] connect: %s", type(e).__name__)
        return False, "Could not reach Google to finish connecting."
    if r.status_code != 200:
        logger.warning("[google] connect: token endpoint said %s", r.status_code)
        return False, ("Google turned the connection down. The redirect URI must match the one "
                       f"registered in the Google console exactly: {state_entry.get('redirect_uri', '')}")
    tok = r.json()
    # The token response's own list, which can leave out scopes granted
    # before; tokeninfo says what the grant really holds.
    returned = str(tok.get("scope") or "").split()
    email = str(_claims(str(tok.get("id_token") or "")).get("email") or "")
    old = load(user)
    old_refresh = _refresh_token(user)
    same_account = not old.get("email") or not email or old.get("email") == email
    refresh = str(tok.get("refresh_token") or "") or (old_refresh if same_account else "")
    if not refresh:
        return False, ("Google did not send a refresh token. Remove Odysseus from your Google "
                       "account's third-party access, then connect again.")
    had = same_account and bool(old.get("refresh_token"))
    old_scopes = str(old.get("scopes") or "").split() if had else []
    old_on = _stored_products(old) if had else []
    verified = await _tokeninfo(str(tok.get("access_token") or ""))
    unverified: List[str] = []
    if verified is not None:
        granted = verified
    else:
        # Google could not be asked. Keep what this same account had plus
        # what came back, and mark the products resting only on the old list
        # so the next look at Settings > Google checks them.
        granted = list(dict.fromkeys(old_scopes + returned))
        unverified = [p for p in old_on if _granted(p, granted) and not _granted(p, returned)]
    got = [p for p in asked if _granted(p, granted)]
    missing = [p for p in asked if p not in got]
    lost = [p for p in old_on if p not in asked and _granted(p, old_scopes) and not _granted(p, granted)]
    if not got:
        names = ", ".join(P.PRODUCTS[p]["label"] for p in missing)
        msg = (f"Google did not grant access to {names}. Connect again and leave its box "
               "ticked on Google's consent screen.")
        return False, (msg + " " + lost_message(lost)) if lost else msg
    products = old_on + got
    products = [p for i, p in enumerate(products) if p in P.PRODUCTS and p not in products[:i]]
    from src import secret_storage
    _save(user, {"email": email or str(old.get("email") or ""),
                 "refresh_token": secret_storage.encrypt(refresh),
                 "scopes": " ".join(granted), "products": products,
                 "unverified": unverified,
                 "checked_at": time.time() if verified is not None else 0,
                 "connected_at": time.time()})
    _access.pop(_key(user), None)
    if tok.get("access_token"):
        _access[_key(user)] = (str(tok["access_token"]), time.time() + int(tok.get("expires_in") or 3600) - 60)
    if not same_account and old_refresh:
        await _revoke(old_refresh)          # the account it replaced
    logger.info("[google] connected %s for %s%s%s", ",".join(got), user or "-",
                f"; lost {','.join(lost)}" if lost else "",
                "; scopes unverified" if verified is None else "")
    names = ", ".join(P.PRODUCTS[p]["label"] for p in got)
    extra = ""
    if missing:
        extra += " Not granted: " + ", ".join(P.PRODUCTS[p]["label"] for p in missing) + "."
    if lost:
        extra += " " + lost_message(lost)
    if "calendar" in got and P.MEET_SETTINGS_SCOPE not in granted:
        extra += " Meet settings were not allowed, so meetings it makes keep your default access."
    return True, f"Connected {names} as {email or 'your Google account'}.{extra} You can close this tab."


async def disconnect(user: Optional[str], product: Optional[str] = None) -> None:
    """Stop using one product, or (no product, or the last one) forget the
    connection and ask Google to revoke it."""
    d = load(user)
    if product and d.get("refresh_token"):
        left = [p for p in _stored_products(d) if p != product and P.PRODUCTS[p].get("parent") != product]
        if left:
            d["products"] = left
            _save(user, d)
            logger.info("[google] %s off for %s", product, user or "-")
            return
    refresh = _refresh_token(user)
    _forget(user)
    await _revoke(refresh)
    logger.info("[google] disconnected for %s", user or "-")


# ── calling Google ──

async def token(c: httpx.AsyncClient, user: Optional[str], fresh: bool = False) -> str:
    k = _key(user)
    cached = _access.get(k)
    if cached and not fresh and cached[1] > time.time():
        return cached[0]
    refresh = _refresh_token(user)
    if not refresh:
        raise GoogleError("Google is not connected. " + RECONNECT)
    cfg = google_oauth.client_config()
    if not cfg["configured"]:
        raise GoogleError("The Google OAuth client is no longer set up on this server.")
    try:
        r = await c.post(google_oauth.api_url(google_oauth.TOKEN_URL), data={
            "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
            "refresh_token": refresh, "grant_type": "refresh_token"})
    except httpx.HTTPError:
        raise GoogleError("Could not reach Google.")
    if r.status_code != 200:
        err = ""
        try:
            err = str(r.json().get("error") or "")
        except ValueError:
            pass
        if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
            _forget(user)
            raise GoogleError("Google no longer accepts the saved connection (revoked or expired). " + RECONNECT)
        raise GoogleError(f"Google would not refresh access ({r.status_code}).")
    tok = r.json()
    access = str(tok.get("access_token") or "")
    _access[k] = (access, time.time() + int(tok.get("expires_in") or 3600) - 60)
    return access


async def call(c: httpx.AsyncClient, user: Optional[str], method: str, url: str, **kw) -> httpx.Response:
    """One API call with a fresh access token, retried once on a 401."""
    headers = dict(kw.pop("headers", None) or {})
    r = None
    for attempt in (0, 1):
        headers["Authorization"] = f"Bearer {await token(c, user, fresh=bool(attempt))}"
        try:
            r = await c.request(method, google_oauth.api_url(url), headers=headers, **kw)
        except httpx.HTTPError:
            raise GoogleError("Could not reach Google.")
        if r.status_code != 401:
            return r
    return r


def reason(r: httpx.Response) -> str:
    """Google's machine-readable reason for an error. Carries no token or
    file content, so it is safe to log."""
    try:
        err = r.json().get("error") or {}
    except ValueError:
        return ""
    if not isinstance(err, dict):
        return str(err)
    reasons = " ".join(str(e.get("reason") or "") for e in err.get("errors") or [] if isinstance(e, dict))
    details = " ".join(str(d.get("reason") or "") for d in err.get("details") or [] if isinstance(d, dict))
    return f"{err.get('status') or ''} {reasons} {details} {err.get('message') or ''}"


def explain(r: httpx.Response, product: str, what: str = "") -> GoogleError:
    """A GoogleError for a failed API call, saying what to do about it."""
    p = P.PRODUCTS.get(product) or {}
    why = reason(r)
    logger.info("[google] %s %s said %s: %s", product, what or "call", r.status_code, why.strip()[:200])
    if "SERVICE_DISABLED" in why or "accessNotConfigured" in why:
        return GoogleError(f"The {p.get('api', 'Google API')} is not enabled in the Google Cloud project "
                           "(APIs & Services > Library > Enable). See docs/google.md.")
    if r.status_code == 401 or "insufficient" in why.lower() or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in why:
        return GoogleError(f"Google did not accept the saved access to {p.get('label', 'Google')}. " + RECONNECT)
    if r.status_code == 404:
        hint = ""
        if product in ("drive", "drive_all", "docs", "sheets", "slides"):
            hint = (" Check the link. With Drive's narrow access Odysseus only sees files it made or you "
                    "opened with it; 'Read all my Drive files' in Settings > Google widens that.")
        return GoogleError(f"Google could not find that {what or 'item'}, or this account cannot open it.{hint}")
    if r.status_code == 403:
        return GoogleError(f"Google refused: this account is not allowed to {what or 'do that'} "
                           f"({why.strip()[:160] or 'forbidden'}).")
    if r.status_code == 429:
        return GoogleError("Google says too many requests right now; try again in a minute.")
    if r.status_code == 400:
        return GoogleError(f"Google did not accept the request: {why.strip()[:240] or 'bad request'}.")
    return GoogleError(f"Google {p.get('label', '')} answered {r.status_code}.".replace("  ", " "))
