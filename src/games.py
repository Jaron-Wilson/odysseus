"""PC game streaming: the Games page, its settings, and the game chat tools.

Asked for: "when i go to /play on odysseus i want it to host up the port that
we go to to play games, also maybe incorporating it into odysseus more?"

The pieces, none of which Odysseus runs itself:

  Sunshine       the game streaming host on the Windows PC. Its admin API
                 (https://<pc>:47990/api/*, HTTP basic auth, a self-signed
                 certificate) lists the apps, closes the running one, adds
                 apps and handles pairing PINs.
  Moonlight Web  the browser client, on this server behind Tailscale Serve
                 at /play/. Streams start there: Sunshine only launches an
                 app when a Moonlight client asks for a stream, and has no
                 API to start one by itself.

What lives here:

  settings       DATA_DIR/games.json. The password is encrypted with
                 src/secret_storage; credentials can instead come from an env
                 file (SUNSHINE_USER / SUNSHINE_PASS, default
                 ~/.secrets/sunshine.env) so an install that already has one
                 works with no setup. Neither is ever sent to the browser.
  TLS            Sunshine's certificate is self-signed. The first contact
                 records it (no credentials are sent on that connection) and
                 every later connection must present the same one, so the
                 password can't be handed to something else on the LAN.
  status         is the PC on the tailnet, does Sunshine answer, and what is
                 running (Sunshine's GameStream /serverinfo, which needs no
                 credentials, gives the running app's id).
  covers         served by Odysseus from a covers folder or its own cache
                 (DATA_DIR/game-covers), filled from the PC (the app's
                 image-path, over SSH) or Steam's CDN.
  Steam sync     reads the installed games from the PC's Steam libraries
                 over SSH, adds the missing ones to Sunshine with a cover.

docs/games.md has the setup as it is on this install.
"""
from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_FILE = "~/.secrets/sunshine.env"
DEFAULT_MOONLIGHT_URL = "/play/"
DEFAULT_MOONLIGHT_HEALTH_URL = "http://127.0.0.1:8090/play/"
DEFAULT_STEAM_LIBRARIES = [r"C:\Program Files (x86)\Steam", r"D:\Program Files (x86)\Steam"]
SUNSHINE_PORT = 47990
CONNECT_TIMEOUT_S = 4.0
READ_TIMEOUT_S = 10.0
STATUS_CACHE_S = 4.0
COVER_MAX_BYTES = 8 * 1024 * 1024
COVER_MISS_TTL_S = 3600.0
PLACEHOLDER_MAX_BYTES = 5000       # Steam's gray "no art yet" image is ~2.6 KB
PLACEHOLDER_MAX_COLORS = 4

# Installed things in a Steam library that are not games. Steam's own tools
# by appid, and the rest by name when the store can't say.
NON_GAME_APPIDS = {
    228980: "Steamworks Common Redistributables",
    431960: "Wallpaper Engine",
    250820: "SteamVR",
    1070560: "Steam Linux Runtime", 1391110: "Steam Linux Runtime", 1628350: "Steam Linux Runtime",
    1493710: "Proton", 1580130: "Proton", 1887720: "Proton", 2180100: "Proton",
    2348590: "Proton", 2805730: "Proton", 1420170: "Proton", 1245040: "Proton",
    1113280: "Proton", 1054830: "Proton", 961940: "Proton", 858280: "Proton",
}
_NON_GAME_NAME_RE = re.compile(
    r"\b(soundtracks?|OST|original score|artbook|redistributables?|dedicated server|sdk|"
    r"wallpaper engine|steamworks|proton|steam linux runtime|steamvr|benchmark tool)\b", re.I)
# Steam store types that are something you play.
GAME_TYPES = {"game", "demo", "mod"}

_lock = threading.Lock()
_status_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_good_url: Dict[str, Any] = {"url": None, "at": 0.0}
_discovered: Dict[str, Any] = {"at": 0.0, "urls": []}
_cover_misses: Dict[str, float] = {}
_cover_locks: Dict[str, asyncio.Lock] = {}
_cover_sem: Optional[asyncio.Semaphore] = None
_sync_job: Dict[str, Any] = {"running": False, "step": "", "started_at": None, "result": None, "error": None}
_sync_task: Optional[asyncio.Task] = None

# Tests set these: an httpx transport used for every request (Sunshine,
# Steam, Moonlight Web), and the SSH runner (see run_powershell).
_http_transport: Optional[httpx.AsyncBaseTransport] = None


class GamesError(Exception):
    """Something the page or the agent should show as it is."""


# ── Settings ────────────────────────────────────────────────────────────────

def _config_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "games.json")


def cache_dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "game-covers")


def _read_raw() -> Dict[str, Any]:
    try:
        with open(_config_path(), encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _write_raw(cfg: Dict[str, Any]) -> None:
    from core.atomic_io import atomic_write_json
    os.makedirs(os.path.dirname(_config_path()), exist_ok=True)
    atomic_write_json(_config_path(), cfg, indent=2)
    try:
        os.chmod(_config_path(), 0o600)
    except OSError:
        pass


def normalize_sunshine_url(url: str) -> str:
    """https://host[:port] for Sunshine's admin API (port 47990 when left out)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError(f"Not a Sunshine address: {url!r}. Use https://<pc>:47990")
    if parts.username or parts.password:
        raise ValueError("Put the Sunshine username and password in their own fields, not in the address")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("A Sunshine address is just https://<pc>:47990, with no path")
    try:
        port = parts.port or SUNSHINE_PORT
    except ValueError:
        raise ValueError(f"Not a Sunshine address: {url!r}")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}"


def normalize_moonlight_url(url: str) -> str:
    """The Moonlight Web page: a path on this host ("/play/") or a full URL."""
    url = (url or "").strip()
    if not url:
        return DEFAULT_MOONLIGHT_URL
    if url.startswith("/"):
        if url.startswith("//") or any(c in url for c in "\\\r\n\"'<> ?#"):
            raise ValueError("The Moonlight Web path must look like /play/")
        return url if url.endswith("/") else url + "/"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("The Moonlight Web address must be a path like /play/ or an http(s):// URL")
    if parts.query or parts.fragment:
        raise ValueError("The Moonlight Web address can't have a query or a fragment")
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def _http_url(url: str, what: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{what} must be an http(s):// URL")
    return url


# Windows paths go into PowerShell as single-quoted literals (_ps doubles any
# quote), so an apostrophe is fine; the rest is refused anyway.
_WIN_PATH_RE = re.compile(r"^[A-Za-z]:\\[^\"`$;|&<>\r\n\x00]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")


def _win_path(p: str, what: str) -> str:
    p = (p or "").strip().replace("/", "\\").rstrip("\\")
    if not p:
        return ""
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    if not _WIN_PATH_RE.match(p) or ".." in p.split("\\"):
        raise ValueError(f"{what} must be a Windows path like C:\\Users\\you\\sunshine-covers")
    return p


def _local_dir(p: str, what: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    if "\x00" in p:
        raise ValueError(f"{what} is not a folder path")
    full = os.path.expanduser(p)
    if not os.path.isabs(full):
        raise ValueError(f"{what} must be an absolute path")
    return p


def _as_list(v) -> List[str]:
    if isinstance(v, str):
        return [x for x in re.split(r"[\s,]+", v) if x]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str) and x.strip()]
    return []


def read_env_file(path: str) -> Dict[str, str]:
    """SUNSHINE_* lines of a KEY=VALUE env file. Nothing else is read out."""
    out: Dict[str, str] = {}
    if not path:
        return out
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                k, v = line.split("=", 1)
                k = k.strip()
                if not k.startswith("SUNSHINE_"):
                    continue
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k] = v
    except (OSError, UnicodeDecodeError):
        return {}
    return out


def load_config() -> Dict[str, Any]:
    """The settings with defaults filled in. Holds the encrypted password:
    for the server only (see public_config)."""
    raw = _read_raw()
    cfg = {
        "sunshine_urls": [],
        "username": "",
        "password": "",
        "credentials_file": DEFAULT_CREDENTIALS_FILE,
        "moonlight_url": DEFAULT_MOONLIGHT_URL,
        "moonlight_health_url": DEFAULT_MOONLIGHT_HEALTH_URL,
        "play_in": "odysseus",
        "covers_dir": "",
        "pc_host": "",
        "pc_covers_dir": "",
        "steam_libraries": list(DEFAULT_STEAM_LIBRARIES),
        "nightly_sync": False,
        "tls": "pin",
        "pins": {},
        "last_sync": None,
    }
    for k in cfg:
        if k in raw and raw[k] is not None:
            cfg[k] = raw[k]
    if not isinstance(cfg["pins"], dict):
        cfg["pins"] = {}
    cfg["sunshine_urls"] = [u for u in _as_list(cfg["sunshine_urls"])][:6]
    cfg["steam_libraries"] = _as_list(cfg["steam_libraries"]) if not isinstance(cfg["steam_libraries"], list) \
        else [x for x in cfg["steam_libraries"] if isinstance(x, str)]
    if cfg["play_in"] not in ("odysseus", "new_tab"):
        cfg["play_in"] = "odysseus"
    if cfg["tls"] not in ("pin", "off"):
        cfg["tls"] = "pin"
    return cfg


def credentials(cfg: Optional[Dict] = None) -> Tuple[str, str, str]:
    """(username, password, where from: "settings" | "file" | "")."""
    cfg = cfg or load_config()
    from src.secret_storage import decrypt
    user = (cfg.get("username") or "").strip()
    pw = decrypt(cfg.get("password") or "")
    if user and pw:
        return user, pw, "settings"
    env = read_env_file(cfg.get("credentials_file") or "")
    fu, fp = env.get("SUNSHINE_USER", ""), env.get("SUNSHINE_PASS", "") or env.get("SUNSHINE_PASSWORD", "")
    if fu and fp:
        return fu, fp, "file"
    return "", "", ""


def configured_urls(cfg: Optional[Dict] = None) -> Tuple[List[str], str]:
    """The Sunshine addresses to try in order, and where they came from."""
    cfg = cfg or load_config()
    urls, src = [], ""
    for u in cfg.get("sunshine_urls") or []:
        try:
            n = normalize_sunshine_url(u)
        except ValueError:
            continue
        if n and n not in urls:
            urls.append(n)
    if urls:
        return urls, "settings"
    env = read_env_file(cfg.get("credentials_file") or "")
    for key in ("SUNSHINE_URLS", "SUNSHINE_URL"):
        for u in _as_list(env.get(key, "")):
            try:
                n = normalize_sunshine_url(u)
            except ValueError:
                continue
            if n and n not in urls:
                urls.append(n)
    if urls:
        return urls, "file"
    for u in _as_list(os.environ.get("ODYSSEUS_SUNSHINE_URLS", "")):
        try:
            n = normalize_sunshine_url(u)
        except ValueError:
            continue
        if n and n not in urls:
            urls.append(n)
    return urls, ("env" if urls else "")


def is_configured(cfg: Optional[Dict] = None) -> bool:
    """Cheap: are there credentials to use (the tools hide without them)."""
    try:
        return bool(credentials(cfg)[2])
    except Exception:
        return False


def _fingerprint(pem: str) -> str:
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
    except Exception:
        return ""
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, 16, 2)) + "…"


def public_config() -> Dict[str, Any]:
    """The settings for the browser: no password, no certificate."""
    cfg = load_config()
    _, _, source = credentials(cfg)
    urls, url_source = configured_urls(cfg)
    cred_file = cfg.get("credentials_file") or ""
    return {
        "sunshine_urls": cfg["sunshine_urls"],
        "effective_urls": urls,
        "urls_from": url_source or ("tailnet" if _discovered["urls"] else ""),
        "username": cfg.get("username") or "",
        "has_password": bool(cfg.get("password")),
        "credentials_from": source,
        "credentials_file": cred_file,
        "credentials_file_exists": bool(cred_file) and os.path.isfile(os.path.expanduser(cred_file)),
        "moonlight_url": cfg["moonlight_url"],
        "moonlight_health_url": cfg["moonlight_health_url"],
        "play_in": cfg["play_in"],
        "covers_dir": cfg["covers_dir"],
        "pc_host": cfg["pc_host"],
        "pc_covers_dir": cfg["pc_covers_dir"],
        "steam_libraries": cfg["steam_libraries"],
        "nightly_sync": bool(cfg["nightly_sync"]),
        "tls": cfg["tls"],
        "certificates": {k: _fingerprint(v) for k, v in (cfg.get("pins") or {}).items() if isinstance(v, str)},
        "last_sync": cfg.get("last_sync"),
    }


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the fields present in `data`. `password` is only changed when
    sent ("" with clear_password to remove it)."""
    if not isinstance(data, dict):
        raise ValueError("Send the settings as a JSON object")
    from src.secret_storage import encrypt
    with _lock:
        raw = _read_raw()
        if "sunshine_urls" in data:
            urls = []
            for u in _as_list(data["sunshine_urls"]):
                n = normalize_sunshine_url(u)
                if n and n not in urls:
                    urls.append(n)
            if len(urls) > 6:
                raise ValueError("At most 6 Sunshine addresses")
            raw["sunshine_urls"] = urls
        if "username" in data:
            u = str(data["username"] or "").strip()
            if len(u) > 128 or any(c in u for c in "\r\n:"):
                raise ValueError("That username can't be used")
            raw["username"] = u
        if data.get("clear_password"):
            raw["password"] = ""
        elif isinstance(data.get("password"), str) and data["password"]:
            if len(data["password"]) > 256 or any(c in data["password"] for c in "\r\n"):
                raise ValueError("That password can't be used")
            raw["password"] = encrypt(data["password"])
        if "credentials_file" in data:
            p = str(data["credentials_file"] or "").strip()
            if p and not os.path.isabs(os.path.expanduser(p)):
                raise ValueError("The credentials file must be an absolute path (or start with ~/)")
            if len(p) > 512 or "\x00" in p:
                raise ValueError("That credentials file path can't be used")
            raw["credentials_file"] = p
        if "moonlight_url" in data:
            raw["moonlight_url"] = normalize_moonlight_url(str(data["moonlight_url"] or ""))
        if "moonlight_health_url" in data:
            raw["moonlight_health_url"] = _http_url(str(data["moonlight_health_url"] or ""),
                                                    "The Moonlight Web health check")
        if "play_in" in data:
            if data["play_in"] not in ("odysseus", "new_tab"):
                raise ValueError("play_in is 'odysseus' or 'new_tab'")
            raw["play_in"] = data["play_in"]
        if "covers_dir" in data:
            raw["covers_dir"] = _local_dir(str(data["covers_dir"] or ""), "The covers folder")
        if "pc_host" in data:
            h = str(data["pc_host"] or "").strip().lower()
            if h and not _HOST_RE.match(h):
                raise ValueError("The PC is its tailnet name or address, like desktop-jaron")
            raw["pc_host"] = h
        if "pc_covers_dir" in data:
            raw["pc_covers_dir"] = _win_path(str(data["pc_covers_dir"] or ""), "The covers folder on the PC")
        if "steam_libraries" in data:
            libs = [_win_path(x, "A Steam library") for x in _as_list_lines(data["steam_libraries"])]
            raw["steam_libraries"] = [x for x in libs if x][:8]
        if "nightly_sync" in data:
            raw["nightly_sync"] = bool(data["nightly_sync"])
        if "tls" in data:
            if data["tls"] not in ("pin", "off"):
                raise ValueError("tls is 'pin' or 'off'")
            raw["tls"] = data["tls"]
        _write_raw(raw)
        _status_cache.update(at=0.0, data=None)
        _good_url.update(url=None, at=0.0)
    return public_config()


def _as_list_lines(v) -> List[str]:
    if isinstance(v, str):
        return [x.strip() for x in v.splitlines() if x.strip()]
    return _as_list(v)


def forget_certificates() -> Dict[str, Any]:
    with _lock:
        raw = _read_raw()
        raw["pins"] = {}
        _write_raw(raw)
        _good_url.update(url=None, at=0.0)
        _status_cache.update(at=0.0, data=None)
    return public_config()


def _set_last_sync(result: Dict[str, Any]) -> None:
    with _lock:
        raw = _read_raw()
        raw["last_sync"] = result
        _write_raw(raw)


def moonlight_frame_origin() -> Optional[str]:
    """The Moonlight Web origin for the page's CSP frame-src, when it is on
    another origin (the default /play/ is this origin: 'self' covers it)."""
    try:
        url = load_config()["moonlight_url"]
    except Exception:
        return None
    if not url or url.startswith("/"):
        return None
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


# ── Talking to Sunshine ────────────────────────────────────────────────────

def _hostport(url: str) -> Tuple[str, int]:
    p = urlsplit(url)
    return p.hostname or "", p.port or SUNSHINE_PORT


def _key(url: str) -> str:
    h, port = _hostport(url)
    return f"{h}:{port}"


def _fetch_cert(host: str, port: int) -> str:
    """The certificate the server presents. No credentials go out on this
    connection: it only reads the handshake."""
    return ssl.get_server_certificate((host, port), timeout=CONNECT_TIMEOUT_S)


def _pinned_context(pem: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata=pem)
    ctx.check_hostname = False          # a LAN IP or a tailnet name, never in the cert
    ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return ctx


async def _verify_for(url: str, cfg: Dict[str, Any]):
    """What httpx should verify this Sunshine with: its recorded certificate
    (recording it on first contact), or nothing when TLS checks are off."""
    if not url.startswith("https://") or _http_transport is not None:
        return True
    if cfg.get("tls") == "off":
        return False
    key = _key(url)
    pem = (cfg.get("pins") or {}).get(key)
    if not pem:
        host, port = _hostport(url)
        pem = await asyncio.to_thread(_fetch_cert, host, port)
        with _lock:
            raw = _read_raw()
            pins = raw.get("pins") if isinstance(raw.get("pins"), dict) else {}
            pem = pins.get(key) or pem            # another request may have recorded it first
            pins[key] = pem
            raw["pins"] = pins
            _write_raw(raw)
        cfg.setdefault("pins", {})[key] = pem
    return _pinned_context(pem)


def _client(verify=True, timeout: Optional[httpx.Timeout] = None, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout or httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        verify=verify, trust_env=False, transport=_http_transport, follow_redirects=False, **kw)


def _ordered(urls: List[str]) -> List[str]:
    good = _good_url.get("url")
    if good in urls and time.time() - _good_url.get("at", 0) < 300:
        return [good] + [u for u in urls if u != good]
    return list(urls)


async def sunshine_urls(cfg: Optional[Dict] = None) -> List[str]:
    cfg = cfg or load_config()
    urls, _ = configured_urls(cfg)
    if urls:
        return urls
    return await discover_sunshine()


async def sunshine_request(method: str, path: str, json_body: Any = None,
                           cfg: Optional[Dict] = None) -> Tuple[str, httpx.Response]:
    """Call Sunshine's admin API at the first address that answers.
    Returns (base URL used, response). Raises GamesError with a message
    fit for the page."""
    cfg = cfg or load_config()
    user, pw, _ = credentials(cfg)
    if not user:
        raise GamesError("No Sunshine username and password: set them in Settings > Devices > Game streaming")
    urls = await sunshine_urls(cfg)
    if not urls:
        raise GamesError("No Sunshine address: set one in Settings > Devices > Game streaming")
    errors = []
    for base in _ordered(urls):
        try:
            verify = await _verify_for(base, cfg)
        except (OSError, ssl.SSLError) as e:
            errors.append(f"{base}: not reachable ({type(e).__name__})")
            continue
        try:
            async with _client(verify=verify, auth=(user, pw)) as c:
                r = await c.request(method, base + path, json=json_body,
                                    headers={"accept": "application/json"})
        except httpx.ConnectError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                raise GamesError(f"{base} presented a different certificate than the one recorded the first "
                                 "time. If Sunshine was reinstalled, use Forget certificates in Settings "
                                 "> Devices > Game streaming; otherwise something else is answering there.")
            errors.append(f"{base}: not reachable")
            continue
        except httpx.TimeoutException:
            errors.append(f"{base}: no answer in time")
            continue
        except httpx.HTTPError as e:
            errors.append(f"{base}: {type(e).__name__}")
            continue
        if r.status_code == 401:
            raise GamesError("Sunshine refused the username or password")
        _good_url.update(url=base, at=time.time())
        return base, r
    raise GamesError("Sunshine is not answering (" + "; ".join(errors) + ")")


async def _sunshine_json(method: str, path: str, json_body: Any = None, cfg=None) -> Tuple[str, Any]:
    base, r = await sunshine_request(method, path, json_body, cfg)
    if r.status_code >= 400:
        detail = ""
        try:
            detail = (r.json() or {}).get("error") or ""
        except ValueError:
            pass
        raise GamesError(f"Sunshine answered HTTP {r.status_code}{': ' + str(detail)[:200] if detail else ''}")
    try:
        return base, r.json()
    except ValueError:
        return base, {}


async def discover_sunshine() -> List[str]:
    """With no address set: the user's online Windows machines on the tailnet
    whose port 47990 serves Sunshine's page. The probe sends no credentials."""
    if time.time() - _discovered["at"] < 600:
        return list(_discovered["urls"])
    found: List[str] = []
    try:
        from src import machines
        peers = await asyncio.to_thread(machines.peers)
    except Exception:
        peers = []
    cands = [p for p in peers if p.get("os") == "windows" and p.get("online") and not p.get("shared")
             and not p.get("is_self") and p.get("ips")]

    async def probe(p):
        ip = next((i for i in p["ips"] if ":" not in i), p["ips"][0])
        url = normalize_sunshine_url(f"https://{ip}:{SUNSHINE_PORT}")
        try:
            async with _client(verify=False, timeout=httpx.Timeout(3.0)) as c:
                r = await c.get(url + "/")
            if "sunshine" in r.text[:20000].lower():
                return url
        except httpx.HTTPError:
            return None
        return None
    for u in await asyncio.gather(*(probe(p) for p in cands[:6])):
        if u:
            found.append(u)
    _discovered.update(at=time.time(), urls=found)
    return found


# ── Apps ───────────────────────────────────────────────────────────────────

_STEAM_RE = re.compile(r"steam://rungameid/(\d+)", re.I)
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def steam_appid(app: Dict[str, Any]) -> Optional[int]:
    """The Steam appid an app runs (cmd, or a detached command)."""
    cmds = [app.get("cmd") or ""]
    det = app.get("detached")
    if isinstance(det, list):
        cmds += [d for d in det if isinstance(d, str)]
    for c in cmds:
        m = _STEAM_RE.search(c if isinstance(c, str) else "")
        if m:
            return int(m.group(1))
    return None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "app"


def cover_key(app: Dict[str, Any]) -> str:
    sid = steam_appid(app)
    if sid:
        return str(sid)
    img = app.get("image-path") or ""
    if isinstance(img, str) and img:
        stem = re.split(r"[\\/]", img)[-1].rsplit(".", 1)[0]
        if _KEY_RE.match(stem):
            return stem
    return _slug(app.get("name") or "")


def _kind(app: Dict[str, Any]) -> str:
    if steam_appid(app):
        return "steam"
    name = (app.get("name") or "").lower()
    cmd = (app.get("cmd") or "") if isinstance(app.get("cmd"), str) else ""
    if "steam://open/bigpicture" in cmd.lower() or "big picture" in name:
        return "bigpicture"
    if name == "desktop" or (not cmd and not app.get("detached")):
        return "desktop"
    return "other"


def public_app(app: Dict[str, Any], index: int) -> Dict[str, Any]:
    """What the page and the agent see of a Sunshine app: no commands."""
    key = cover_key(app)
    local = find_local_cover(key)
    return {
        "index": index,
        "name": str(app.get("name") or f"App {index + 1}"),
        "kind": _kind(app),
        "steam_appid": steam_appid(app),
        "cover": f"/api/games/cover/{quote(key)}" + (f"?v={int(os.path.getmtime(local))}" if local else ""),
        "has_cover": bool(local),
    }


async def raw_apps(cfg=None) -> List[Dict[str, Any]]:
    _, data = await _sunshine_json("GET", "/api/apps", cfg=cfg)
    apps = data.get("apps") if isinstance(data, dict) else data
    return [a for a in (apps or []) if isinstance(a, dict)]


async def list_apps() -> List[Dict[str, Any]]:
    return [public_app(a, i) for i, a in enumerate(await raw_apps())]


def find_app(apps: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """An app by name: exact, then ignoring case and punctuation, then the
    one name containing it, then the closest."""
    if not name:
        return None
    n = name.strip().lower()
    for a in apps:
        if a["name"].lower() == n:
            return a
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())  # noqa: E731
    nn = norm(name)
    for a in apps:
        if norm(a["name"]) == nn:
            return a
    hits = [a for a in apps if nn and nn in norm(a["name"])]
    if len(hits) == 1:
        return hits[0]
    import difflib
    best = difflib.get_close_matches(n, [a["name"].lower() for a in apps], n=1, cutoff=0.6)
    if best:
        return next(a for a in apps if a["name"].lower() == best[0])
    return hits[0] if hits else None


async def stop_app() -> Dict[str, Any]:
    """Close whatever Sunshine is running (POST /api/apps/close)."""
    _, data = await _sunshine_json("POST", "/api/apps/close", {})
    _status_cache.update(at=0.0, data=None)
    ok = not isinstance(data, dict) or data.get("status") not in (False, "false")
    return {"ok": ok}


async def pending_pairings() -> List[Dict[str, Any]]:
    _, data = await _sunshine_json("GET", "/api/pin")
    rows = (data or {}).get("pairings") if isinstance(data, dict) else None
    return [{"id": str(p.get("id") or ""), "name": str(p.get("name") or ""), "address": str(p.get("address") or "")}
            for p in (rows or []) if isinstance(p, dict)]


async def submit_pin(pairing_id: str, pin: str, name: str) -> Dict[str, Any]:
    if not re.fullmatch(r"\d{4}", pin or ""):
        raise GamesError("The PIN is the 4 digits Moonlight shows")
    body = {"pin": pin, "name": (name or "Moonlight")[:64]}
    if pairing_id:
        body["pairing_id"] = str(pairing_id)[:64]
    _, data = await _sunshine_json("POST", "/api/pin", body)
    ok = not isinstance(data, dict) or data.get("status") not in (False, "false")
    return {"ok": ok}


async def clients() -> List[Dict[str, Any]]:
    try:
        _, data = await _sunshine_json("GET", "/api/clients/list")
    except GamesError:
        return []
    rows = ((data.get("named_certs") or data.get("clients") or []) if isinstance(data, dict) else [])
    return [{"name": str(c.get("name") or "")} for c in rows if isinstance(c, dict)]


# ── Status ─────────────────────────────────────────────────────────────────

def pc_target(cfg: Optional[Dict] = None, urls: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """The PC as a tailnet peer: the one set in settings, else the one a
    Sunshine address points at."""
    cfg = cfg or load_config()
    from src import machines
    try:
        peers = machines.peers()
    except Exception:
        peers = []
    if cfg.get("pc_host"):
        p = machines.find_peer(peers, cfg["pc_host"])
        return p or {"host": cfg["pc_host"], "name": cfg["pc_host"], "dns": cfg["pc_host"], "ips": [],
                     "os": "windows", "online": None, "is_self": False}
    for u in (urls if urls is not None else configured_urls(cfg)[0]) + list(_discovered["urls"]):
        p = machines.find_peer(peers, urlsplit(u).hostname or "")
        if p:
            return p
    return None


def parse_serverinfo(xml_text: str) -> Dict[str, Any]:
    """GameStream /serverinfo -> {busy, app_id, hostname}."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text[:65536])
    except ET.ParseError:
        return {}
    def txt(tag):
        el = root.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""
    try:
        app_id = int(txt("currentgame") or 0)
    except ValueError:
        app_id = 0
    state = txt("state")
    return {"busy": app_id > 0 or state.endswith("_BUSY"), "app_id": app_id or None, "hostname": txt("hostname")}


async def serverinfo(base: str) -> Dict[str, Any]:
    """The running app's id from Sunshine's GameStream HTTP port (the admin
    port minus one). No credentials: it is the page Moonlight reads first."""
    host, port = _hostport(base)
    h = f"[{host}]" if ":" in host else host
    url = f"http://{h}:{port - 1}/serverinfo?uniqueid=0123456789ABCDEF&uuid=odysseus"
    try:
        async with _client(timeout=httpx.Timeout(4.0, connect=3.0)) as c:
            r = await c.get(url)
        if r.status_code != 200:
            return {}
        return parse_serverinfo(r.text)
    except httpx.HTTPError:
        return {}


def _running_name(apps: List[Dict[str, Any]], app_id: Optional[int]) -> Optional[str]:
    if not app_id:
        return None
    for a in apps:
        for k in ("id", "uuid"):
            if str(a.get(k) or "") == str(app_id):
                return str(a.get("name") or "")
    return None


async def status(force: bool = False) -> Dict[str, Any]:
    """PC online?, Sunshine reachable?, what is running. Never raises."""
    if not force and _status_cache["data"] is not None and time.time() - _status_cache["at"] < STATUS_CACHE_S:
        return _status_cache["data"]
    cfg = load_config()
    _, _, cred_src = credentials(cfg)
    out: Dict[str, Any] = {"configured": bool(cred_src), "credentials_from": cred_src,
                           "moonlight_url": cfg["moonlight_url"], "play_in": cfg["play_in"]}
    try:
        urls = await sunshine_urls(cfg)
    except Exception:
        urls = []
    pc = await asyncio.to_thread(pc_target, cfg, urls)
    out["pc"] = ({"name": pc.get("name") or pc.get("host"), "host": pc.get("host"),
                  "online": pc.get("online")} if pc else None)
    out["sunshine"] = {"ok": False, "url": None, "error": None}
    out["running"] = None
    out["apps"] = 0
    if not cred_src:
        out["sunshine"]["error"] = "Not set up: add the Sunshine username and password in Settings > Devices"
    elif not urls:
        out["sunshine"]["error"] = "No Sunshine address set, and none found on the tailnet"
    else:
        try:
            base, data = await _sunshine_json("GET", "/api/apps", cfg=cfg)
            apps = [a for a in ((data or {}).get("apps") or []) if isinstance(a, dict)] if isinstance(data, dict) else []
            out["sunshine"] = {"ok": True, "url": base, "error": None}
            out["apps"] = len(apps)
            info = await serverinfo(base)
            if info.get("busy"):
                out["running"] = {"app_id": info.get("app_id"), "name": _running_name(apps, info.get("app_id"))}
            if out["pc"] is not None:
                out["pc"]["online"] = True          # Sunshine answered, whatever the tailnet says
        except GamesError as e:
            out["sunshine"]["error"] = str(e)
    if out["pc"] is None and out["sunshine"]["ok"]:
        out["pc"] = {"name": urlsplit(out["sunshine"]["url"]).hostname, "host": None, "online": True}
    _status_cache.update(at=time.time(), data=out)
    return out


async def moonlight_health(cfg: Optional[Dict] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    url = cfg.get("moonlight_health_url") or ""
    if not url:
        return {"ok": None, "url": "", "detail": "No health check address set"}
    try:
        async with _client(timeout=httpx.Timeout(4.0, connect=3.0)) as c:
            r = await c.get(url)
        if r.status_code == 200:
            return {"ok": True, "url": url, "detail": "Answering"}
        return {"ok": False, "url": url, "detail": f"HTTP {r.status_code}"}
    except httpx.TimeoutException:
        return {"ok": False, "url": url, "detail": "No answer in time"}
    except httpx.HTTPError as e:
        return {"ok": False, "url": url, "detail": f"Not running ({type(e).__name__})"}


async def health() -> Dict[str, Any]:
    """Settings > Devices: Sunshine on the PC and Moonlight Web here."""
    st, ml = await asyncio.gather(status(force=True), moonlight_health())
    return {"sunshine": st["sunshine"], "pc": st["pc"], "running": st["running"], "apps": st["apps"],
            "configured": st["configured"], "moonlight": ml}


async def test_connection() -> Dict[str, Any]:
    """The Test button: reach Sunshine with the saved settings."""
    try:
        base, data = await _sunshine_json("GET", "/api/apps")
    except GamesError as e:
        return {"ok": False, "error": str(e)}
    apps = (data or {}).get("apps") or [] if isinstance(data, dict) else []
    return {"ok": True, "url": base, "apps": len(apps)}


# ── SSH to the PC ──────────────────────────────────────────────────────────

async def _run_ssh(argv: List[str], stdin: Optional[bytes], timeout: float) -> Dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        return {"rc": proc.returncode, "out": out.decode("utf-8", errors="replace"),
                "err": err.decode(errors="replace").strip()}
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"rc": -1, "out": "", "err": f"timed out after {timeout:.0f}s"}
    except FileNotFoundError as e:
        return {"rc": -1, "out": "", "err": str(e)}


async def run_powershell(script: str, stdin: Optional[bytes] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """Run a PowerShell script on the PC over SSH, the way Settings > Devices
    reaches it (src/machines.py: same keys, same user, encoded command)."""
    from src import machines
    cfg = load_config()
    urls = await sunshine_urls(cfg)
    pc = await asyncio.to_thread(pc_target, cfg, urls)
    if pc is None:
        raise GamesError("Can't tell which machine is the PC: set it in Settings > Devices > Game streaming")
    if pc.get("os") and pc["os"] != "windows":
        raise GamesError(f"{pc.get('name')} is not a Windows machine")
    host = pc.get("dns") or (pc["ips"][0] if pc.get("ips") else pc["host"])
    user = machines.load_prefs().get(pc.get("host") or "", {}).get("ssh_user") or getpass.getuser()
    keys = machines._ssh_keys()
    if not keys:
        raise GamesError("This server has no SSH key to reach the PC (see Settings > Devices)")
    remote = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
              "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n" + script]
    last = ""
    for key in keys:
        r = await _run_ssh(machines._ssh_argv(user, host, key, remote, "windows"), stdin, timeout)
        if r["rc"] == 0:
            return r
        last = r["err"] or r["out"]
        if "Permission denied" not in last:
            break
    raise GamesError(("SSH to the PC was refused; add this server's key to it (Settings > Devices)"
                      if "Permission denied" in last else f"SSH to the PC failed: {last[:300] or 'no output'}"))


def _ps(s: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + s.replace("'", "''") + "'"


def manifests_script(libraries: List[str]) -> str:
    libs = ",".join(_ps(_win_path(x, "A Steam library")) for x in libraries if x)
    return (
        "$ErrorActionPreference='SilentlyContinue'\n"
        f"$libs=@({libs})\n"
        "$out=New-Object System.Collections.ArrayList\n"
        "foreach($l in $libs){$d=Join-Path $l 'steamapps'\n"
        " if(Test-Path -LiteralPath $d){Get-ChildItem -LiteralPath $d -Filter 'appmanifest_*.acf' -File |"
        " ForEach-Object{[void]$out.Add([ordered]@{library=$l;file=$_.Name;"
        "text=[IO.File]::ReadAllText($_.FullName)})}}}\n"
        "ConvertTo-Json -InputObject @($out) -Compress -Depth 3\n")


def parse_acf(text: str) -> Dict[str, str]:
    """The top-level "key" "value" pairs of a Steam appmanifest."""
    out: Dict[str, str] = {}
    depth = 0
    for line in (text or "").splitlines():
        s = line.strip()
        if s == "{":
            depth += 1
            continue
        if s == "}":
            depth -= 1
            continue
        if depth != 1:
            continue
        m = re.match(r'^"([^"]+)"\s+"((?:[^"\\]|\\.)*)"', s)
        if m:
            out[m.group(1).lower()] = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
    return out


async def installed_steam_games(cfg: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """[{appid, name, library, installed}] from both Steam libraries."""
    cfg = cfg or load_config()
    libs = cfg.get("steam_libraries") or DEFAULT_STEAM_LIBRARIES
    r = await run_powershell(manifests_script(libs), timeout=90)
    raw = (r.get("out") or "").strip().lstrip("\ufeff")
    try:
        rows = json.loads(raw) if raw else []
    except ValueError:
        raise GamesError("The PC's answer about its Steam libraries wasn't readable")
    if isinstance(rows, dict):
        rows = [rows]
    out, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        acf = parse_acf(row.get("text") or "")
        try:
            appid = int(acf.get("appid") or re.sub(r"\D", "", row.get("file") or "") or 0)
        except ValueError:
            continue
        if not appid or appid in seen:
            continue
        seen.add(appid)
        try:
            flags = int(acf.get("stateflags") or 4)
        except ValueError:
            flags = 4
        out.append({"appid": appid, "name": acf.get("name") or f"Steam app {appid}",
                    "library": row.get("library") or "", "installed": bool(flags & 4)})
    return out


# ── Steam store and CDN ────────────────────────────────────────────────────

def _types_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "games-steam-types.json")


def _load_types() -> Dict[str, str]:
    try:
        with open(_types_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


async def steam_type(appid: int, client: httpx.AsyncClient, cache: Dict[str, str]) -> Optional[str]:
    """The store's type for an app ("game", "dlc", "music", "application",
    "tool"...), cached; None when the store doesn't say."""
    k = str(appid)
    if k in cache:
        return cache[k] or None
    try:
        r = await client.get("https://store.steampowered.com/api/appdetails",
                             params={"appids": k, "filters": "basic"})
        d = (r.json() or {}).get(k) or {}
        t = (d.get("data") or {}).get("type") if d.get("success") else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    cache[k] = (t or "").lower()
    return cache[k] or None


def classify(game: Dict[str, Any], store_type: Optional[str]) -> Optional[str]:
    """Why an installed Steam app is not added, or None for a game."""
    if not game.get("installed", True):
        return "not fully installed"
    if game["appid"] in NON_GAME_APPIDS:
        return f"not a game ({NON_GAME_APPIDS[game['appid']]})"
    if store_type and store_type not in GAME_TYPES:
        return f"not a game (Steam lists it as {store_type})"
    if _NON_GAME_NAME_RE.search(game.get("name") or ""):
        return "not a game (by its name)"
    return None


STEAM_CDN = [
    "https://cdn.cloudflare.steamstatic.com/steam/apps/{id}/library_600x900_2x.jpg",
    "https://cdn.cloudflare.steamstatic.com/steam/apps/{id}/library_600x900.jpg",
]


def _count_colors(data: bytes) -> Optional[int]:
    try:
        p = subprocess.run(["identify", "-format", "%k", "-"], input=data, capture_output=True, timeout=20)
        return int(p.stdout.decode().strip().split()[0]) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
        return None


def is_placeholder(data: bytes) -> bool:
    """Steam's CDN answers some new games with a small flat gray image."""
    if len(data) <= PLACEHOLDER_MAX_BYTES:
        return True
    colors = _count_colors(data)
    return colors is not None and colors <= PLACEHOLDER_MAX_COLORS


def to_png(data: bytes) -> bytes:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    p = subprocess.run(["convert", "-", "png:-"], input=data, capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout.startswith(b"\x89PNG"):
        raise GamesError("Could not convert the cover to PNG (is ImageMagick installed?)")
    return p.stdout


async def steam_cdn_cover(appid: int, client: Optional[httpx.AsyncClient] = None) -> Optional[bytes]:
    """The game's 600x900 library art from Steam, or None (including when
    Steam only has its gray placeholder)."""
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), trust_env=False,
                                   transport=_http_transport, follow_redirects=True)
    try:
        for tpl in STEAM_CDN:
            try:
                r = await client.get(tpl.format(id=int(appid)))
            except httpx.HTTPError:
                continue
            if r.status_code != 200 or not r.headers.get("content-type", "image/").startswith("image/"):
                continue
            data = r.content[:COVER_MAX_BYTES + 1]
            if len(data) > COVER_MAX_BYTES:
                continue
            if await asyncio.to_thread(is_placeholder, data):
                continue
            return data
    finally:
        if own:
            await client.aclose()
    return None


def librarycache_script(steam_root: str, appid: int) -> str:
    root = _win_path(steam_root, "The Steam folder")
    return (
        "$ErrorActionPreference='SilentlyContinue'\n"
        f"$lc=Join-Path {_ps(root)} 'appcache\\librarycache'\n"
        f"$d=Join-Path $lc '{int(appid)}'\n"
        "$f=$null\n"
        "if(Test-Path -LiteralPath $d){$f=Get-ChildItem -LiteralPath $d -Recurse -File -Filter 'library_capsule.jpg' |"
        " Sort-Object Length -Descending | Select-Object -First 1\n"
        " if(-not $f){$f=Get-ChildItem -LiteralPath $d -Recurse -File -Filter 'library_600x900*.jpg' |"
        " Sort-Object Length -Descending | Select-Object -First 1}}\n"
        f"if(-not $f){{$f=Get-ChildItem -LiteralPath $lc -File -Filter '{int(appid)}_library_600x900*.jpg' |"
        " Sort-Object Length -Descending | Select-Object -First 1}\n"
        "if($f){[Convert]::ToBase64String([IO.File]::ReadAllBytes($f.FullName))}\n")


def read_file_script(path: str) -> str:
    p = _win_path(path, "The cover path")
    if not re.search(r"\.(png|jpe?g)$", p, re.I):
        raise GamesError("Only .png and .jpg covers are read from the PC")
    return ("$ErrorActionPreference='SilentlyContinue'\n"
            f"if(Test-Path -LiteralPath {_ps(p)}){{[Convert]::ToBase64String([IO.File]::ReadAllBytes({_ps(p)}))}}\n")


def upload_script(pc_dir: str, filename: str) -> str:
    d = _win_path(pc_dir, "The covers folder on the PC")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.png", filename):
        raise GamesError("Bad cover file name")
    return (
        "$ErrorActionPreference='Stop'\n"
        f"$d={_ps(d)}\n"
        "New-Item -ItemType Directory -Force -Path $d | Out-Null\n"
        "$b=[Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())\n"
        f"[IO.File]::WriteAllBytes((Join-Path $d {_ps(filename)}),$b)\n"
        "'ok'\n")


def _b64_out(r: Dict[str, Any]) -> Optional[bytes]:
    s = re.sub(r"\s+", "", (r.get("out") or "").lstrip("\ufeff"))
    if not s:
        return None
    try:
        data = base64.b64decode(s, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    return data if 0 < len(data) <= COVER_MAX_BYTES else None


async def pc_librarycache_cover(appid: int, steam_root: str) -> Optional[bytes]:
    try:
        return _b64_out(await run_powershell(librarycache_script(steam_root, appid), timeout=40))
    except GamesError:
        return None


async def pc_file(path: str) -> Optional[bytes]:
    try:
        return _b64_out(await run_powershell(read_file_script(path), timeout=40))
    except (GamesError, ValueError):
        return None


async def upload_cover(pc_dir: str, appid: int, png: bytes) -> str:
    """Put the cover in the PC's covers folder; returns its Windows path."""
    name = f"{int(appid)}.png"
    r = await run_powershell(upload_script(pc_dir, name), stdin=base64.b64encode(png) + b"\n", timeout=60)
    if "ok" not in (r.get("out") or ""):
        raise GamesError(f"Could not save the cover on the PC: {(r.get('err') or '')[:200]}")
    return pc_dir.rstrip("\\") + "\\" + name


# ── Covers served by Odysseus ──────────────────────────────────────────────

def _cover_dirs(cfg: Optional[Dict] = None) -> List[str]:
    cfg = cfg or load_config()
    dirs = []
    if cfg.get("covers_dir"):
        dirs.append(os.path.expanduser(cfg["covers_dir"]))
    dirs.append(cache_dir())
    return dirs


def find_local_cover(key: str, cfg: Optional[Dict] = None) -> Optional[str]:
    if not _KEY_RE.match(key or "") or key.startswith("."):
        return None
    for d in _cover_dirs(cfg):
        for ext in (".png", ".jpg", ".jpeg"):
            p = os.path.join(d, key + ext)
            if os.path.isfile(p) and os.path.realpath(p).startswith(os.path.realpath(d) + os.sep):
                return p
    return None


def _save_cache(key: str, png: bytes) -> str:
    os.makedirs(cache_dir(), exist_ok=True)
    p = os.path.join(cache_dir(), key + ".png")
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png)
    os.replace(tmp, p)
    return p


async def cover_path(key: str) -> Optional[str]:
    """A local file for the cover, fetching it once if needed: the app's own
    image on the PC (its image-path, over SSH), else Steam's art."""
    global _cover_sem
    if not _KEY_RE.match(key or "") or key.startswith("."):
        return None
    local = find_local_cover(key)
    if local:
        return local
    if time.time() - _cover_misses.get(key, 0) < COVER_MISS_TTL_S:
        return None
    lock = _cover_locks.setdefault(key, asyncio.Lock())
    if _cover_sem is None:
        _cover_sem = asyncio.Semaphore(3)
    async with lock:
        local = find_local_cover(key)
        if local:
            return local
        async with _cover_sem:
            data = None
            try:
                apps = await raw_apps()
            except GamesError:
                apps = []
            app = next((a for a in apps if cover_key(a) == key), None)
            img = (app or {}).get("image-path") or ""
            if isinstance(img, str) and _WIN_PATH_RE.match(img.replace("/", "\\")):
                data = await pc_file(img.replace("/", "\\"))
            if data is None and key.isdigit():
                data = await steam_cdn_cover(int(key))
            if data is None:
                _cover_misses[key] = time.time()
                return None
            try:
                png = await asyncio.to_thread(to_png, data)
            except GamesError:
                _cover_misses[key] = time.time()
                return None
            return _save_cache(key, png)


# ── Steam sync ─────────────────────────────────────────────────────────────

_APP_DEFAULTS = {"output": "", "exclude-global-prep-cmd": False, "elevated": False,
                 "auto-detach": True, "wait-all": True, "exit-timeout": 5, "prep-cmd": [], "detached": []}
_NO_COPY = {"name", "cmd", "image-path", "uuid", "id", "index", "detached", "prep-cmd"}


def new_app(name: str, appid: int, image_path: str, template: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A Sunshine app for a Steam game, shaped like the user's other Steam
    apps (their field types: Sunshine has used both "true" and true)."""
    app = dict(_APP_DEFAULTS)
    if template:
        for k, v in template.items():
            if k not in _NO_COPY:
                app[k] = v
    for k in ("auto-detach", "wait-all"):
        app[k] = "true" if template and isinstance(template.get(k), str) else True
    app.update({"name": name, "cmd": f"steam://rungameid/{int(appid)}", "image-path": image_path,
                "prep-cmd": [], "detached": [], "index": -1})
    return app


def default_pc_covers_dir(apps: List[Dict[str, Any]], user: str) -> str:
    """Where the PC's covers go: the folder most of the apps' covers are in,
    else C:\\Users\\<user>\\sunshine-covers."""
    counts: Dict[str, int] = {}
    for a in apps:
        img = a.get("image-path") or ""
        if isinstance(img, str) and _WIN_PATH_RE.match(img.replace("/", "\\")) and "\\" in img.replace("/", "\\"):
            d = img.replace("/", "\\").rsplit("\\", 1)[0]
            counts[d] = counts.get(d, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    return f"C:\\Users\\{user}\\sunshine-covers"


async def game_cover(appid: int, steam_root: str, client: httpx.AsyncClient) -> Tuple[Optional[bytes], str]:
    """(PNG, where it came from) for a new game: Steam's CDN, else the PC's
    own Steam library cache."""
    data = await steam_cdn_cover(appid, client)
    src = "steam"
    if data is None:
        data = await pc_librarycache_cover(appid, steam_root)
        src = "pc-library-cache"
        if data is not None and await asyncio.to_thread(is_placeholder, data):
            data = None
    if data is None:
        return None, "none"
    try:
        return await asyncio.to_thread(to_png, data), src
    except GamesError:
        return None, "none"


async def sync_steam(progress=None) -> Dict[str, Any]:
    """Add the PC's installed Steam games that Sunshine doesn't have yet.

    Returns {added, already, skipped, removed, covers, errors}. "removed"
    lists Sunshine's Steam apps that are no longer installed; they are left
    in Sunshine for the user to remove."""
    def step(s):
        _sync_job["step"] = s
        if progress:
            progress(s)
    cfg = load_config()
    step("Reading Sunshine's apps")
    apps = await raw_apps(cfg)
    have = {steam_appid(a) for a in apps} - {None}
    template = next((a for a in apps if steam_appid(a)), None)
    step("Reading the Steam libraries on the PC")
    games = await installed_steam_games(cfg)
    installed_ids = {g["appid"] for g in games if g["installed"]}
    user = getpass.getuser()
    try:
        from src import machines
        pc = await asyncio.to_thread(pc_target, cfg)
        user = machines.load_prefs().get((pc or {}).get("host") or "", {}).get("ssh_user") or user
    except Exception:
        pass
    pc_dir = cfg.get("pc_covers_dir") or default_pc_covers_dir(apps, user)
    steam_root = (cfg.get("steam_libraries") or DEFAULT_STEAM_LIBRARIES)[0]
    result: Dict[str, Any] = {"added": [], "already": [], "skipped": [], "removed": [], "covers": {},
                              "errors": [], "at": datetime.now().isoformat(timespec="seconds")}
    types = _load_types()
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), trust_env=False,
                                 transport=_http_transport, follow_redirects=True) as client:
        for g in sorted(games, key=lambda g: g["name"].lower()):
            if g["appid"] in have:
                result["already"].append(g["name"])
                continue
            pre = classify(g, None)
            if pre:
                result["skipped"].append({"name": g["name"], "reason": pre})
                continue
            st = await steam_type(g["appid"], client, types)
            why = classify(g, st)
            if why:
                result["skipped"].append({"name": g["name"], "reason": why})
                continue
            step(f"Adding {g['name']}")
            image_path = ""
            try:
                png, src = await game_cover(g["appid"], steam_root, client)
                if png:
                    image_path = await upload_cover(pc_dir, g["appid"], png)
                    _save_cache(str(g["appid"]), png)
                result["covers"][g["name"]] = src
            except GamesError as e:
                result["covers"][g["name"]] = "none"
                result["errors"].append(f"{g['name']} cover: {e}")
            try:
                await _sunshine_json("POST", "/api/apps", new_app(g["name"], g["appid"], image_path, template), cfg)
                result["added"].append(g["name"])
            except GamesError as e:
                result["errors"].append(f"{g['name']}: {e}")
    try:
        os.makedirs(os.path.dirname(_types_path()), exist_ok=True)
        with open(_types_path(), "w", encoding="utf-8") as f:
            json.dump(types, f)
    except OSError:
        pass
    for a in apps:
        sid = steam_appid(a)
        if sid and sid not in installed_ids:
            result["removed"].append(str(a.get("name") or sid))
    _status_cache.update(at=0.0, data=None)
    _set_last_sync({k: result[k] for k in ("at", "added", "removed", "errors")} |
                   {"skipped": len(result["skipped"]), "already": len(result["already"])})
    return result


def sync_state() -> Dict[str, Any]:
    return dict(_sync_job)


def start_sync() -> Dict[str, Any]:
    """Run the Steam sync in the background (it takes a while: SSH, the
    store, covers). The page polls sync_state()."""
    if _sync_job["running"]:
        return sync_state()

    async def run():
        try:
            _sync_job["result"] = await sync_steam()
        except GamesError as e:
            _sync_job["error"] = str(e)
        except Exception as e:
            logger.warning("Steam sync failed: %s", e, exc_info=True)
            _sync_job["error"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            _sync_job.update(running=False, step="")
    global _sync_task
    _sync_job.update(running=True, step="Starting", started_at=time.time(), result=None, error=None)
    _sync_task = asyncio.get_running_loop().create_task(run())
    return sync_state()


def summarize_sync(r: Dict[str, Any]) -> str:
    lines = [f"Added {len(r['added'])} game(s) to Sunshine" + (": " + ", ".join(r["added"]) if r["added"] else ".")]
    if r.get("removed"):
        lines.append("In Sunshine but no longer installed (left as they are): " + ", ".join(r["removed"]))
    if r.get("skipped"):
        lines.append("Skipped: " + "; ".join(f"{s['name']} ({s['reason']})" for s in r["skipped"][:20]))
    lines.append(f"Already in Sunshine: {len(r.get('already') or [])}.")
    if r.get("errors"):
        lines.append("Problems: " + "; ".join(r["errors"][:10]))
    return "\n".join(lines)


async def nightly_loop():
    """Steam sync once a night (04:30), only when it is switched on."""
    while True:
        now = datetime.now()
        nxt = now.replace(hour=4, minute=30, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep(max(60, (nxt - now).total_seconds()))
        try:
            if load_config().get("nightly_sync") and is_configured() and not _sync_job["running"]:
                r = await sync_steam()
                logger.info("[games] nightly Steam sync: added %d", len(r["added"]))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[games] nightly Steam sync failed: %s", e)


def launch_link(name: str) -> str:
    """The Odysseus link that opens the Games page and starts this game's
    stream in Moonlight Web (static/js/gamesPanel.js resolves it there)."""
    return "/#games?play=" + quote(name, safe="")
