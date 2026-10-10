"""The Paperclip page (routes/paperclip_routes.py, static/js/paperclip*).

Asked for: show a Paperclip instance (companies, agents, issues, heartbeat
runs, activity) inside Odysseus, read-only.

The server talks to Paperclip rather than the browser so the board token
never reaches the page, and so the app's CSP can stay same-origin. Every
call is a GET against a fixed allowlist of Paperclip API paths; nothing
here can create, change or delete anything on the Paperclip side.

Settings live in DATA_DIR/paperclip.json (mode 0600): {url, token_enc,
company_id}. The token is encrypted with src/secret_storage.py and is
never returned by public_config() or any route. ODYSSEUS_PAPERCLIP_URL /
ODYSSEUS_PAPERCLIP_TOKEN fill in whatever the file leaves empty.

Upstream errors are mapped to short codes (auth_failed, host_blocked,
rate_limited, upstream_error, unreachable, ...); raw upstream bodies are
never passed through.
"""
import json
import os
import re
import threading
import time
import uuid
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

FETCH_TIMEOUT_S = 8.0
CACHE_S = 5.0
DETAIL_MAX = 200
RUN_ERROR_MAX = 300
QUERY_MAX = 200

# Test seam: when set, used as the httpx transport for every call that
# isn't given one explicitly.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

_lock = threading.Lock()
_cache: Dict = {"at": 0.0, "key": None, "status": None}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UUID_PAT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ALLOWED_PATHS = (
    re.compile(r"^/api/health$"),
    re.compile(r"^/api/cli-auth/me$"),
    re.compile(r"^/api/companies$"),
    re.compile(r"^/api/companies/" + _UUID_PAT
               + r"/(dashboard|agents|issues|heartbeat-runs|live-runs|activity)$"),
)
_STATUS_RE = re.compile(r"^[a-z_]+(,[a-z_]+)*$")


# ── Settings ────────────────────────────────────────────────────────────────

def _config_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "paperclip.json")


def normalize_url(url: str) -> str:
    """The Paperclip base address, without a trailing "/"."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The Paperclip address has to be an http:// or https:// URL")
    if parts.query or parts.fragment:
        raise ValueError("The Paperclip address can't have a query or a fragment")
    if parts.username or parts.password:
        raise ValueError("The Paperclip address can't carry a username or password")
    path = (parts.path or "").rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _valid_uuid(value) -> bool:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _read_file() -> Dict:
    try:
        with open(_config_path(), encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def load_config() -> Dict:
    """{url, token, company_id} with the token decrypted. Internal only."""
    from src import secret_storage
    cfg = _read_file()
    url = cfg.get("url") or os.environ.get("ODYSSEUS_PAPERCLIP_URL") or ""
    try:
        url = normalize_url(url) if isinstance(url, str) else ""
    except ValueError:
        url = ""
    token_enc = cfg.get("token_enc") or ""
    token = secret_storage.decrypt(token_enc) if isinstance(token_enc, str) and token_enc else ""
    if not token:
        token = os.environ.get("ODYSSEUS_PAPERCLIP_TOKEN") or ""
    cid = cfg.get("company_id")
    return {"url": url, "token": token, "company_id": cid if _valid_uuid(cid) else None}


def public_config() -> Dict:
    cfg = load_config()
    return {"url": cfg["url"], "company_id": cfg["company_id"], "has_token": bool(cfg["token"])}


def save_config(url: str, token: Optional[str] = None, clear_token: bool = False,
                company_id: Optional[str] = None) -> Dict:
    """Save the settings. An empty/None token keeps the stored one;
    clear_token drops it. Raises ValueError for a bad url or company id."""
    url = normalize_url(url or "")
    company_id = (company_id or "").strip().lower() or None
    if company_id is not None and not _valid_uuid(company_id):
        raise ValueError("The company id has to be a UUID")
    from core.atomic_io import atomic_write_json
    from core.platform_compat import safe_chmod
    from src import secret_storage
    with _lock:
        current = _read_file()
        token_enc = current.get("token_enc") if isinstance(current.get("token_enc"), str) else ""
        if clear_token:
            token_enc = ""
        elif token:
            token_enc = secret_storage.encrypt(token.strip())
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write_json(path, {"url": url, "token_enc": token_enc or "", "company_id": company_id},
                          indent=2)
        safe_chmod(path, 0o600)
        _cache.update(at=0.0, key=None, status=None)
    return public_config()


# ── HTTP ────────────────────────────────────────────────────────────────────

def _err(code: str, detail: Optional[str] = None) -> Dict:
    out = {"ok": False, "error": code}
    if detail:
        out["detail"] = detail[:DETAIL_MAX]
    return out


async def _get(path: str, params: Optional[Dict] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None) -> Tuple[object, Optional[Dict]]:
    """GET one allowlisted Paperclip path -> (json, None) or (None, error dict)."""
    if not any(p.fullmatch(path) for p in _ALLOWED_PATHS):
        raise ValueError("path not allowed")
    cfg = load_config()
    if not cfg["url"]:
        return None, _err("not_configured")
    headers = {"accept": "application/json"}
    if cfg["token"]:
        headers["authorization"] = f"Bearer {cfg['token']}"
    host = urlsplit(cfg["url"]).hostname or ""
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, trust_env=False,
                                     transport=transport or _TRANSPORT, follow_redirects=False) as c:
            r = await c.get(cfg["url"] + path, params=params, headers=headers)
    except httpx.TimeoutException:
        return None, _err("unreachable", f"No answer within {FETCH_TIMEOUT_S:g} s")
    except httpx.HTTPError as e:
        return None, _err("unreachable", type(e).__name__)
    code = r.status_code
    if code == 401:
        return None, _err("auth_failed", "Paperclip rejected the token")
    if code == 403:
        try:
            body = r.text.lower()
        except Exception:
            body = ""
        if "host" in body:
            return None, _err("host_blocked",
                              f"Paperclip refused the host name {host}; add it to its allowed hostnames")
        return None, _err("forbidden", "HTTP 403")
    if code == 429:
        return None, _err("rate_limited", "HTTP 429")
    if code >= 500:
        return None, _err("upstream_error", f"HTTP {code}")
    if not 200 <= code < 300:
        return None, _err(f"http_{code}", f"HTTP {code}")
    try:
        return r.json(), None
    except ValueError:
        return None, _err("bad_response", "Not JSON")


async def _company_get(cid, suffix: str, params: Optional[Dict] = None,
                       transport=None) -> Tuple[object, Optional[Dict]]:
    if not _valid_uuid(cid):
        return None, _err("invalid_company_id")
    return await _get(f"/api/companies/{cid}/{suffix}", params, transport)


def _s(v):
    return v if isinstance(v, str) else None


def _n(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pick(d: Dict, keys) -> Dict:
    return {k: d.get(k) for k in keys}


def _rows(data, key: Optional[str] = None):
    """A list payload, or a list under `key` of a dict payload; else None."""
    if isinstance(data, dict) and key and isinstance(data.get(key), list):
        data = data[key]
    if not isinstance(data, list):
        return None
    return [x for x in data if isinstance(x, dict)]


# ── Public calls ────────────────────────────────────────────────────────────

async def fetch_status(transport=None) -> Dict:
    """Health and who-am-i, cached for a few seconds. Never raises."""
    cfg = load_config()
    pub = {"url": cfg["url"], "company_id": cfg["company_id"], "has_token": bool(cfg["token"])}
    if not cfg["url"]:
        return {"configured": False, "ok": False, "error": "not_configured",
                "health": None, "me": None, "config": pub}
    key = (cfg["url"], bool(cfg["token"]), cfg["company_id"])
    if _cache["key"] == key and _cache["status"] is not None and time.time() - _cache["at"] < CACHE_S:
        return _cache["status"]

    out: Dict = {"configured": True, "ok": False, "health": None, "me": None, "config": pub}
    data, err = await _get("/api/health", transport=transport)
    if err is None and not isinstance(data, dict):
        err = _err("bad_response")
    if err:
        out["error"] = err["error"]
        if err.get("detail"):
            out["detail"] = err["detail"]
    else:
        health = {"status": _s(data.get("status")), "version": _s(data.get("version")),
                  "deploymentMode": _s(data.get("deploymentMode")),
                  "deploymentExposure": _s(data.get("deploymentExposure"))}
        out["health"] = health
        me_err = None
        if cfg["token"]:
            me, me_err = await _get("/api/cli-auth/me", transport=transport)
            if me_err is None and not isinstance(me, dict):
                me_err = _err("bad_response")
            if me_err is None:
                ids = me.get("companyIds")
                out["me"] = {"userId": _s(me.get("userId")),
                             "isInstanceAdmin": bool(me.get("isInstanceAdmin")),
                             "companyIds": [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []}
        if out["me"] is not None or (not cfg["token"] and health["deploymentMode"] == "local_trusted"):
            out["ok"] = True
        elif me_err:
            out["error"] = me_err["error"]
            if me_err.get("detail"):
                out["detail"] = me_err["detail"]
        else:
            out["error"] = "auth_failed"
            out["detail"] = "No token set"
    _cache.update(at=time.time(), key=key, status=out)
    return out


async def list_companies(transport=None) -> Dict:
    data, err = await _get("/api/companies", {"scope": "accessible"}, transport)
    if err:
        return err
    rows = _rows(data, "companies")
    if rows is None:
        return _err("bad_response")
    return {"ok": True, "companies": [_pick(c, ("id", "name", "status", "issuePrefix")) for c in rows]}


async def dashboard(cid, transport=None) -> Dict:
    data, err = await _company_get(cid, "dashboard", None, transport)
    if err:
        return err
    if not isinstance(data, dict):
        return _err("bad_response")

    def sub(key, fields):
        d = data.get(key) if isinstance(data.get(key), dict) else {}
        return {f: _n(d.get(f)) for f in fields}

    return {"ok": True, "dashboard": {
        "agents": sub("agents", ("active", "running", "paused", "error")),
        "tasks": sub("tasks", ("open", "inProgress", "blocked", "done")),
        "costs": sub("costs", ("monthSpendCents", "monthBudgetCents", "monthUtilizationPercent")),
        "pendingApprovals": _n(data.get("pendingApprovals")),
    }}


async def agents(cid, transport=None) -> Dict:
    data, err = await _company_get(cid, "agents", None, transport)
    if err:
        return err
    rows = _rows(data, "agents")
    if rows is None:
        return _err("bad_response")
    keys = ("id", "name", "role", "title", "status", "adapterType", "lastHeartbeatAt")
    return {"ok": True, "agents": [_pick(a, keys) for a in rows]}


async def issues(cid, status: Optional[str] = None, q: Optional[str] = None, transport=None) -> Dict:
    if not _valid_uuid(cid):
        return _err("invalid_company_id")
    params = {"view": "compact", "limit": 200, "sortField": "updated", "sortDir": "desc"}
    status = (status or "").strip()
    if status:
        if not _STATUS_RE.fullmatch(status):
            return _err("invalid_status")
        params["status"] = status
    q = (q or "").strip()[:QUERY_MAX]
    if q:
        params["q"] = q
    data, err = await _company_get(cid, "issues", params, transport)
    if err:
        return err
    rows = _rows(data, "issues")
    if rows is None:
        return _err("bad_response")
    keys = ("id", "identifier", "title", "status", "priority", "assigneeAgentId", "assigneeUserId", "updatedAt")
    return {"ok": True, "issues": [_pick(i, keys) for i in rows]}


def _run(r: Dict) -> Dict:
    out = _pick(r, ("id", "agentId", "status", "invocationSource", "createdAt", "startedAt", "finishedAt"))
    e = r.get("error")
    out["error"] = e[:RUN_ERROR_MAX] if isinstance(e, str) else None
    return out


async def runs(cid, agent_id: Optional[str] = None, transport=None) -> Dict:
    if not _valid_uuid(cid):
        return _err("invalid_company_id")
    params = {"summary": "true", "limit": 50}
    agent_id = (agent_id or "").strip()
    if agent_id:
        if not _valid_uuid(agent_id):
            return _err("invalid_agent_id")
        params["agentId"] = agent_id
    data, err = await _company_get(cid, "heartbeat-runs", params, transport)
    if err:
        return err
    rows = _rows(data, "runs")
    if rows is None:
        return _err("bad_response")
    return {"ok": True, "runs": [_run(r) for r in rows]}


async def live_runs(cid, transport=None) -> Dict:
    data, err = await _company_get(cid, "live-runs", {"limit": 50}, transport)
    if err:
        return err
    rows = _rows(data, "runs")
    if rows is None:
        return _err("bad_response")
    return {"ok": True, "runs": [_run(r) for r in rows]}


async def activity(cid, transport=None) -> Dict:
    data, err = await _company_get(cid, "activity", {"limit": 100}, transport)
    if err:
        return err
    rows = _rows(data, "events")
    if rows is None:
        return _err("bad_response")
    keys = ("id", "action", "entityType", "entityId", "actorType", "actorId", "agentId", "createdAt")
    return {"ok": True, "events": [_pick(e, keys) for e in rows]}
