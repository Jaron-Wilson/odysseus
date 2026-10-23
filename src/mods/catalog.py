"""Finding mods: the Browse view in Settings > Mods (docs/mods.md, "Finding mods").

Two sources, merged into one list:

- **GitHub**: repositories with the topic `odysseus-mod`, from the search API.
  With a GitHub token (Settings > Mods > Browse > Discovery settings, or
  GITHUB_TOKEN / GH_TOKEN in the environment) the search is authenticated, and
  the user's own repositories with the topic (private ones included) are
  listed too, from /user/repos.
- **Curated indexes**: JSON files (templates/mods-index/) listing reviewed mods.
  Their entries are "Verified"; a mod only found by its topic is "Community,
  not reviewed". An index can block versions and raise a mod's min_core.

Each mod's `odysseus-mod.json` is read from its default branch (the raw
contents API) for its name, version, tags, permissions, tabs and min_core.
The list is cached on disk for an hour (CACHE_TTL_S); a failed or rate
limited refresh falls back to the cached list and says so.

Everything fetched here is untrusted: manifests are only parsed and checked
(src/mods/manifest.py), READMEs are rendered to HTML and sanitized with an
allowlist, and nothing is cloned or run. Installing goes through the normal
review and approve flow (registry.prepare / confirm).

The GitHub token is only ever sent to https://api.github.com. Index URLs on
other hosts, and raw.githubusercontent.com, are fetched without it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

from src.mods import capabilities
from src.mods import manifest as mf
from src.mods import paths

logger = logging.getLogger(__name__)

TOPIC = "odysseus-mod"
GITHUB_API = "https://api.github.com"
GITHUB_API_HOST = "api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
CACHE_TTL_S = 3600
DETAIL_TTL_S = 3600
HTTP_TIMEOUT_S = 15
MAX_SEARCH_PAGES = 3
MAX_USER_REPO_PAGES = 3
MAX_REPOS = 200
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_README_BYTES = 512 * 1024
MAX_INDEX_URLS = 8
MAX_DETAIL_FILES = 200
USER_AGENT = "odysseus-mods-catalog"
# Images in READMEs, icons and screenshots are only shown from GitHub's own
# hosts, so browsing does not ping arbitrary servers.
IMAGE_HOSTS = ("raw.githubusercontent.com", "user-images.githubusercontent.com",
               "private-user-images.githubusercontent.com", "github.com", "avatars.githubusercontent.com",
               "camo.githubusercontent.com")

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SUBDIR_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_GH_URL_RES = [
    re.compile(r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?(?:[#?].*)?$", re.I),
    re.compile(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I),
    re.compile(r"^ssh://git@github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I),
    re.compile(r"^git://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I),
]

_lock = threading.RLock()


class CatalogError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


class RateLimited(CatalogError):
    def __init__(self, reset_at: Optional[float], authenticated: bool):
        when = time.strftime("%H:%M", time.localtime(reset_at)) if reset_at else "a few minutes"
        hint = "" if authenticated else " Adding a GitHub token raises the limit."
        super().__init__(f"GitHub's rate limit was reached; it resets at {when}.{hint}", 429)
        self.reset_at = reset_at


# ── Settings ─────────────────────────────────────────────────────────────

def _raw_settings() -> Dict[str, Any]:
    from src.mods import registry
    return dict(registry.get_settings().get("catalog") or {})


def _env_token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def token() -> Tuple[str, str]:
    """(token, where it came from: settings, environment or "")."""
    s = _raw_settings()
    if s.get("github_token"):
        from src.mods import registry
        t = registry._decrypt(s["github_token"]).strip()
        if t:
            return t, "settings"
    t = _env_token()
    return (t, "environment") if t else ("", "")


def index_urls() -> List[str]:
    s = _raw_settings()
    urls = s.get("index_urls")
    if urls is None:
        env = os.environ.get("ODYSSEUS_MODS_INDEX_URLS", "")
        urls = [u.strip() for u in env.split(",") if u.strip()]
    return [u for u in (urls or []) if isinstance(u, str) and u.strip()][:MAX_INDEX_URLS]


def settings() -> Dict[str, Any]:
    """What the UI may see (never the token itself)."""
    s = _raw_settings()
    _tok, src = token()
    return {"index_urls": index_urls(), "include_private": bool(s.get("include_private", True)),
            "token_set": bool(src), "token_source": src}


def _check_index_url(u: str) -> str:
    u = u.strip()
    if parse_index_location(u) is None:
        raise CatalogError(f"not an index location: {u!r} (an https URL, a GitHub owner/repo/path, "
                           "or a JSON file on this server)", 400)
    return u


def save_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    from src.mods import registry
    cur = _raw_settings()
    if "index_urls" in values:
        raw = values.get("index_urls")
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[\n,]", raw)]
        if not isinstance(raw, list):
            raise CatalogError("index_urls must be a list of URLs", 400)
        urls = [_check_index_url(str(u)) for u in raw if str(u).strip()]
        if len(urls) > MAX_INDEX_URLS:
            raise CatalogError(f"at most {MAX_INDEX_URLS} index URLs", 400)
        cur["index_urls"] = urls
    if "include_private" in values:
        cur["include_private"] = bool(values.get("include_private"))
    if values.get("clear_token"):
        cur.pop("github_token", None)
    elif isinstance(values.get("github_token"), str) and values["github_token"].strip():
        t = values["github_token"].strip()
        if not re.match(r"^[A-Za-z0-9_]{20,255}$", t):
            raise CatalogError("that does not look like a GitHub token", 400)
        cur["github_token"] = registry._encrypt(t)
    with registry._lock:
        allset = registry.get_settings()
        allset["catalog"] = cur
        registry.save_settings(allset)
    clear_cache()
    return settings()


# ── HTTP ─────────────────────────────────────────────────────────────────

class Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _transport(url: str, headers: Dict[str, str], max_bytes: int) -> Response:
    """One GET, no redirects followed. Tests replace this."""
    import httpx
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=False) as c:
        with c.stream("GET", url, headers=headers) as r:
            chunks, total = [], 0
            for chunk in r.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise CatalogError(f"{urlparse(url).netloc} sent more than {max_bytes // 1024} KB", 502)
                chunks.append(chunk)
            return Response(r.status_code, dict(r.headers), b"".join(chunks))


def _headers_for(url: str, accept: str) -> Dict[str, str]:
    h = {"User-Agent": USER_AGENT, "Accept": accept}
    p = urlparse(url)
    if p.netloc.lower() == GITHUB_API_HOST:
        h["X-GitHub-Api-Version"] = "2022-11-28"
        tok, _src = token()
        # The token only ever goes to api.github.com over https.
        if tok and p.scheme == "https":
            h["Authorization"] = f"Bearer {tok}"
    return h


def get(url: str, accept: str = "application/vnd.github+json", max_bytes: int = MAX_JSON_BYTES) -> Response:
    """GET with redirects followed by hand (at most 3), so headers, and the
    token, are worked out again for every host."""
    for _ in range(4):
        if urlparse(url).scheme != "https":
            raise CatalogError(f"refusing a non-https URL: {url}", 400)
        try:
            r = _transport(url, _headers_for(url, accept), max_bytes)
        except CatalogError:
            raise
        except Exception as e:
            raise CatalogError(f"could not reach {urlparse(url).netloc}: {type(e).__name__}")
        if r.status in (301, 302, 307, 308) and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        if r.status in (403, 429) and urlparse(url).netloc.lower() == GITHUB_API_HOST:
            remaining = r.headers.get("x-ratelimit-remaining")
            text = r.body[:500].decode("utf-8", "replace").lower()
            if remaining == "0" or "rate limit" in text or r.status == 429:
                reset = r.headers.get("x-ratelimit-reset")
                retry = r.headers.get("retry-after")
                at = float(reset) if reset and reset.isdigit() else (time.time() + int(retry) if retry and retry.isdigit() else None)
                raise RateLimited(at, bool(token()[0]))
        return r
    raise CatalogError("too many redirects")


def _api_json(path: str) -> Any:
    r = get(GITHUB_API + path)
    if r.status == 401:
        raise CatalogError("GitHub refused the token (401); check it in Discovery settings", 401)
    if r.status != 200:
        raise CatalogError(f"GitHub answered {r.status} for {path.split('?')[0]}", 404 if r.status == 404 else 502)
    try:
        return r.json()
    except ValueError:
        raise CatalogError("GitHub sent something that is not JSON")


# ── Repositories ─────────────────────────────────────────────────────────

def repo_key(url_or_name: str) -> Optional[str]:
    """"owner/repo" (lowercase) for a GitHub URL in any form, or None."""
    s = (url_or_name or "").strip()
    for rx in _GH_URL_RES:
        m = rx.match(s)
        if m:
            return f"{m.group(1)}/{m.group(2)}".lower()
    m = re.match(r"^([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})$", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}".lower().removesuffix(".git")
    return None


def _repo_view(r: Dict[str, Any]) -> Dict[str, Any]:
    owner = (r.get("owner") or {}).get("login") or str(r.get("full_name", "/")).split("/")[0]
    return {
        "full_name": str(r.get("full_name") or ""),
        "owner": owner,
        "repo": str(r.get("name") or ""),
        "html_url": str(r.get("html_url") or ""),
        "clone_url": str(r.get("clone_url") or ""),
        "ssh_url": str(r.get("ssh_url") or ""),
        "description": str(r.get("description") or "")[:300],
        "stars": int(r.get("stargazers_count") or 0),
        "updated": str(r.get("pushed_at") or r.get("updated_at") or ""),
        "default_branch": str(r.get("default_branch") or "HEAD"),
        "private": bool(r.get("private")),
        "archived": bool(r.get("archived")),
        "topics": [t for t in (r.get("topics") or []) if isinstance(t, str)][:20],
        "avatar_url": str((r.get("owner") or {}).get("avatar_url") or ""),
    }


def search_topic() -> List[Dict[str, Any]]:
    """Every repository with the topic, newest pushes first (at most MAX_REPOS)."""
    out: List[Dict[str, Any]] = []
    for page in range(1, MAX_SEARCH_PAGES + 1):
        d = _api_json(f"/search/repositories?q={quote('topic:' + TOPIC)}&sort=updated&order=desc"
                      f"&per_page=100&page={page}")
        items = d.get("items") or [] if isinstance(d, dict) else []
        out.extend(_repo_view(r) for r in items if isinstance(r, dict))
        if len(items) < 100 or len(out) >= min(MAX_REPOS, int(d.get("total_count") or 0)):
            break
    return out[:MAX_REPOS]


def viewer() -> str:
    """The login the token belongs to."""
    d = _api_json("/user")
    return str(d.get("login") or "") if isinstance(d, dict) else ""


def user_repos_with_topic() -> List[Dict[str, Any]]:
    """The user's repositories (their own, collaborations, their orgs'),
    private ones included, that have the topic. Needs a token."""
    out: List[Dict[str, Any]] = []
    for page in range(1, MAX_USER_REPO_PAGES + 1):
        items = _api_json(f"/user/repos?per_page=100&page={page}&sort=pushed"
                          "&affiliation=owner,collaborator,organization_member")
        if not isinstance(items, list):
            break
        out.extend(_repo_view(r) for r in items if isinstance(r, dict) and TOPIC in (r.get("topics") or []))
        if len(items) < 100:
            break
    return out


def repo_info(owner: str, repo: str) -> Dict[str, Any]:
    return _repo_view(_api_json(f"/repos/{quote(owner)}/{quote(repo)}"))


def _contents_path(subdir: Optional[str], name: str) -> str:
    return f"{subdir.strip('/')}/{name}" if subdir else name


def fetch_file(owner: str, repo: str, path: str, ref: Optional[str], private: bool,
               max_bytes: int) -> Optional[bytes]:
    """A file from the repository at `ref` (None: the default branch), or None
    if it is not there. With a token, or for a private repository, the raw
    contents API; without one, raw.githubusercontent.com (no API rate limit)."""
    if ref and not _REF_RE.match(ref):
        raise CatalogError("bad ref", 400)
    qpath = "/".join(quote(p) for p in path.split("/"))
    tok, _src = token()
    if tok or private:
        url = f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/contents/{qpath}"
        if ref:
            url += f"?ref={quote(ref, safe='')}"
        r = get(url, accept="application/vnd.github.raw+json", max_bytes=max_bytes)
    else:
        r = get(f"{RAW_BASE}/{quote(owner)}/{quote(repo)}/{quote(ref or 'HEAD', safe='/')}/{qpath}",
                accept="*/*", max_bytes=max_bytes)
    if r.status == 404:
        return None
    if r.status != 200:
        raise CatalogError(f"GitHub answered {r.status} for {path}")
    return r.body


def fetch_manifest(owner: str, repo: str, ref: Optional[str] = None, private: bool = False,
                   subdir: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(the parsed odysseus-mod.json, or None; a problem, or None)."""
    try:
        body = fetch_file(owner, repo, _contents_path(subdir, mf.MANIFEST_FILE), ref, private, MAX_MANIFEST_BYTES)
    except RateLimited:
        raise
    except CatalogError as e:
        return None, e.message
    if body is None:
        return None, f"no {mf.MANIFEST_FILE} on the default branch" if not ref else f"no {mf.MANIFEST_FILE} at {ref}"
    try:
        raw = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, f"{mf.MANIFEST_FILE} is not valid JSON"
    if not isinstance(raw, dict):
        return None, f"{mf.MANIFEST_FILE} is not a JSON object"
    return raw, None


def _text(v: Any, n: int) -> str:
    return v.strip()[:n] if isinstance(v, str) else ""


def summarize(raw: Optional[Dict[str, Any]], min_core_override: Optional[str] = None) -> Dict[str, Any]:
    """What the Browse view shows of a fetched manifest. It is checked with
    the same rules as an install; `compatible` is whether this core could
    install it now."""
    if not isinstance(raw, dict):
        return {"valid": False, "compatible": False, "errors": ["no manifest"], "tags": [], "permissions": []}
    errors: List[str] = []
    warnings: List[str] = []
    try:
        man = mf.validate(raw)
        warnings = man.get("warnings") or []
    except mf.ManifestError as e:
        man = None
        errors = [x for x in e.errors if not x.startswith("this mod needs mod API")]
    min_core = _text(raw.get("min_core"), 20) or "0.1"
    if not mf.VERSION_RE.match(min_core):
        min_core = "0.1"
    if min_core_override and mf.VERSION_RE.match(min_core_override) and \
            mf._version_tuple(min_core_override) > mf._version_tuple(min_core):
        min_core = min_core_override
    tags = (man or {}).get("tags")
    if tags is None:
        tags = [t for t in (raw.get("tags") or []) if isinstance(t, str) and mf.TAG_RE.match(t)][:mf.MAX_TAGS] \
            if isinstance(raw.get("tags"), list) else []
    perms = (man or {}).get("permissions")
    if perms is None:
        perms = sorted({p for p in (raw.get("permissions") or []) if isinstance(p, str) and p in mf.PERMISSIONS}) \
            if isinstance(raw.get("permissions"), list) else []
    ui = (man or {}).get("ui") or {}
    caps = {}
    for k in capabilities.KINDS:
        v = (man or {}).get(k)
        if v is None:
            v = [c for c in (raw.get(k) or []) if isinstance(c, str) and capabilities.CAP_RE.match(c)][
                :capabilities.MAX_PER_LIST] if isinstance(raw.get(k), list) else []
        caps[k] = v
    return {
        **caps,
        "valid": not errors,
        "compatible": not errors and mf.core_satisfies(min_core),
        "errors": errors[:10],
        "warnings": warnings[:10],
        "id": _text(raw.get("id"), 32),
        "name": _text(raw.get("name"), 60),
        "version": _text(raw.get("version"), 40),
        "description": _text(raw.get("description"), 300),
        "author": _text(raw.get("author"), 100),
        "license": _text(raw.get("license"), 60),
        "min_core": min_core,
        "tags": tags,
        "icon_url": image_url((man or {}).get("icon_url") or ""),
        "screenshots": [image_url(u) for u in ((man or {}).get("screenshots") or []) if image_url(u)],
        "permissions": perms,
        "runtime": (man or {}).get("runtime") or {},
        "tabs": ui.get("tabs") or [],
        "home_cards": ui.get("home_cards") or [],
        "tools": (man or {}).get("tools"),
        "events": (man or {}).get("events"),
        # A mod with no tutorial is never Verified (see verified_status).
        "has_tutorial": bool((man or {}).get("tutorial")),
        "tutorial_steps": len(((man or {}).get("tutorial") or {}).get("steps") or []),
    }


NO_TUTORIAL_REASON = "Not shown as Verified: its manifest has no tutorial (every verified mod needs one)."


def verified_status(listed_verified: bool, summary: Dict[str, Any]) -> Tuple[bool, str]:
    """Whether a catalog entry shows as Verified, and if an index said so but
    it is not, why. An index can vouch for a mod, but a mod without a
    tutorial (or with a broken manifest) cannot be Verified."""
    if not listed_verified:
        return False, ""
    if not summary.get("has_tutorial"):
        return False, NO_TUTORIAL_REASON
    return True, ""


IMAGE_PROXY = "/api/mods/catalog/image?u="
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGE_BYTES = 3 * 1024 * 1024


def allowed_image(u: str) -> bool:
    if not isinstance(u, str) or not mf.HTTPS_URL_RE.match(u):
        return False
    return (urlparse(u).hostname or "").lower() in IMAGE_HOSTS


def image_url(u: str) -> str:
    """The proxied URL for an https image on a GitHub host, or "". Images go
    through the core (fetch_image) because the page's CSP allows no outside
    images, and so the browser never contacts the image's host itself."""
    return IMAGE_PROXY + quote(u, safe="") if allowed_image(u) else ""


def fetch_image(u: str) -> Tuple[bytes, str]:
    """(bytes, content type) of an allowed image. Only raster types (no SVG,
    which can hold scripts), fetched without the token."""
    if not allowed_image(u):
        raise CatalogError("not an image this browser may show", 400)
    url = u
    for _ in range(4):
        if not allowed_image(url):
            raise CatalogError("the image redirected somewhere else", 400)
        try:
            r = _transport(url, {"User-Agent": USER_AGENT, "Accept": "image/*"}, MAX_IMAGE_BYTES)
        except CatalogError:
            raise
        except Exception as e:
            raise CatalogError(f"could not fetch the image: {type(e).__name__}")
        if r.status in (301, 302, 307, 308) and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        break
    if r.status != 200:
        raise CatalogError(f"the image answered {r.status}", 404 if r.status == 404 else 502)
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in IMAGE_TYPES:
        sig = r.body[:12]
        ctype = ("image/png" if sig.startswith(b"\x89PNG") else "image/jpeg" if sig.startswith(b"\xff\xd8")
                 else "image/gif" if sig[:4] == b"GIF8" else "image/webp" if sig[8:12] == b"WEBP" else "")
        if not ctype:
            raise CatalogError("not a PNG, JPEG, GIF or WebP image", 415)
    return r.body, ctype


# ── Indexes ──────────────────────────────────────────────────────────────

def parse_index_location(u: str) -> Optional[Dict[str, Any]]:
    """Where an index lives. GitHub forms become {owner, repo, path, ref}, so
    a private index repository can be read with the token through the API;
    any other https URL is fetched as is (without the token); an absolute
    path or file:// URL is a file on this server."""
    u = (u or "").strip()
    if not u:
        return None
    if u.startswith("file://") or u.startswith("/"):
        p = u[7:] if u.startswith("file://") else u
        return {"kind": "file", "path": p} if p.startswith("/") else None
    m = re.match(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", u)
    if m:
        return {"kind": "github", "owner": m.group(1), "repo": m.group(2), "ref": m.group(3), "path": m.group(4)}
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/(?:blob|raw)/([^/]+)/(.+))?/?$", u)
    if m:
        return {"kind": "github", "owner": m.group(1), "repo": m.group(2), "ref": m.group(3),
                "path": m.group(4) or "index.json"}
    m = re.match(r"^([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})(/[A-Za-z0-9._/-]+\.json)?(?:@([A-Za-z0-9._/-]+))?$", u)
    if m and not u.startswith("http"):
        return {"kind": "github", "owner": m.group(1), "repo": m.group(2), "ref": m.group(4),
                "path": (m.group(3) or "/index.json").lstrip("/")}
    if re.match(r"^https://[^\s]+$", u):
        return {"kind": "url", "url": u}
    return None


def _read_index_location(loc: Dict[str, Any]) -> bytes:
    if loc["kind"] == "file":
        try:
            if os.path.getsize(loc["path"]) > MAX_JSON_BYTES:
                raise CatalogError("the index file is larger than 2 MB")
            with open(loc["path"], "rb") as f:
                return f.read()
        except OSError as e:
            raise CatalogError(f"could not read {loc['path']}: {e.strerror}")
    if loc["kind"] == "github":
        # Private index repositories are expected, so try the API (with the
        # token, if there is one) and fall back to raw for public ones.
        body = fetch_file(loc["owner"], loc["repo"], loc["path"], loc.get("ref"), private=True,
                          max_bytes=MAX_JSON_BYTES)
        if body is None:
            raise CatalogError(f"no {loc['path']} in {loc['owner']}/{loc['repo']} "
                               "(or it is private and there is no token that can read it)")
        return body
    r = get(loc["url"], accept="application/json", max_bytes=MAX_JSON_BYTES)
    if r.status != 200:
        raise CatalogError(f"{urlparse(loc['url']).netloc} answered {r.status}")
    return r.body


def parse_index(data: Any, source: str) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """The entries of an index file, by repo key, and a problem per bad entry.
    The format is templates/mods-index/index.schema.json."""
    problems: List[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("mods"), list):
        return {}, [f"{source}: an index is a JSON object with a `mods` list"]
    if data.get("schema_version", 1) != 1:
        return {}, [f"{source}: schema_version {data.get('schema_version')!r} is not supported"]
    name = _text(data.get("name"), 80) or source
    out: Dict[str, Dict[str, Any]] = {}
    for i, e in enumerate(data["mods"][:1000]):
        where = f"{name}: mods[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{where} is not an object")
            continue
        key = repo_key(str(e.get("repo") or ""))
        if not key:
            problems.append(f"{where}: `repo` must be a GitHub repository URL")
            continue
        mid = e.get("id")
        if mid is not None and (not isinstance(mid, str) or not mf.ID_RE.match(mid)):
            problems.append(f"{where}: `id` is not a mod id")
            continue
        blocked = e.get("blocked_versions") or []
        if not isinstance(blocked, list) or not all(isinstance(v, str) and len(v) <= 40 for v in blocked):
            problems.append(f"{where}: `blocked_versions` must be a list of versions")
            blocked = []
        min_core = e.get("min_core")
        if min_core is not None and (not isinstance(min_core, str) or not mf.VERSION_RE.match(min_core)):
            problems.append(f"{where}: `min_core` must look like 0.1")
            min_core = None
        subdir = e.get("subdir")
        if subdir is not None and (not isinstance(subdir, str) or not _SUBDIR_RE.match(subdir)
                                   or ".." in subdir.split("/")):
            problems.append(f"{where}: `subdir` must be a plain relative path")
            subdir = None
        tags = [t for t in (e.get("tags") or []) if isinstance(t, str) and mf.TAG_RE.match(t)][:mf.MAX_TAGS] \
            if isinstance(e.get("tags"), list) else []
        caps: Dict[str, List[str]] = {}
        for kind in capabilities.KINDS:
            errs: List[str] = []
            caps[kind] = capabilities._check_list(e.get(kind), kind, errs)
            if errs:
                problems.append(f"{where}: {errs[0]}")
        out[key] = {"repo": key, "id": mid, "verified": e.get("verified", True) is True,
                    "notes": _text(e.get("notes"), 500), "blocked_versions": [_norm_version(v) for v in blocked],
                    "min_core": min_core, "subdir": subdir, "tags": tags, "index": name, **caps}
    return out, problems


def _norm_version(v: str) -> str:
    v = (v or "").strip()
    return v[1:] if v[:1] in ("v", "V") and v[1:2].isdigit() else v


def load_indexes() -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Every configured index, merged (the first index to list a repo wins,
    but blocked versions add up). Cached with the catalog."""
    merged: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    for u in index_urls():
        loc = parse_index_location(u)
        if not loc:
            problems.append(f"{u}: not an index location")
            continue
        try:
            data = json.loads(_read_index_location(loc).decode("utf-8"))
        except RateLimited:
            raise
        except CatalogError as e:
            problems.append(f"{u}: {e.message}")
            continue
        except (ValueError, UnicodeDecodeError):
            problems.append(f"{u}: not valid JSON")
            continue
        entries, bad = parse_index(data, u)
        problems.extend(bad)
        for k, e in entries.items():
            if k in merged:
                merged[k]["blocked_versions"] = sorted(set(merged[k]["blocked_versions"]) | set(e["blocked_versions"]))
            else:
                merged[k] = e
    return merged, problems


# ── The cache ────────────────────────────────────────────────────────────

def _cache_dir() -> str:
    return os.path.join(paths.mods_root(), ".catalog")


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fingerprint() -> str:
    tok, _src = token()
    s = _raw_settings()
    raw = json.dumps([hashlib.sha256(tok.encode()).hexdigest() if tok else "", index_urls(),
                      bool(s.get("include_private", True)), mf.CORE_API_VERSION])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def clear_cache() -> None:
    import shutil
    shutil.rmtree(_cache_dir(), ignore_errors=True)


def cached() -> Optional[Dict[str, Any]]:
    """The last catalog, however old, or None. Never fetches."""
    d = _read_json(os.path.join(_cache_dir(), "catalog.json"))
    return d if isinstance(d, dict) and isinstance(d.get("entries"), dict) else None


# ── Building the catalog ─────────────────────────────────────────────────

def _installed_github() -> Dict[str, Dict[str, Any]]:
    """Installed mods that came from GitHub, by repo key."""
    from src.mods import registry
    out = {}
    for m in registry.list_mods():
        src = m.get("source") or {}
        k = repo_key(src.get("url") or "") if src.get("type") == "git" else None
        if k:
            out[k] = m
    return out


def _latest_tag(owner: str, repo: str) -> Optional[str]:
    """The highest version-looking tag (v1.2.3 or 1.2.3), or None."""
    tags = _api_json(f"/repos/{quote(owner)}/{quote(repo)}/tags?per_page=50")
    best = None
    for t in tags if isinstance(tags, list) else []:
        name = str((t or {}).get("name") or "")
        if mf.VERSION_RE.match(_norm_version(name)):
            if best is None or mf._version_tuple(_norm_version(name)) > mf._version_tuple(_norm_version(best)):
                best = name
    return best


def build(force: bool = False) -> Dict[str, Any]:
    """The catalog: {entries: {repo key: entry}, fetched_at, warnings, ...}.
    From the cache when it is fresh, else from GitHub and the indexes. A
    refresh that fails keeps the old entries and says why in `warnings`."""
    with _lock:
        old = cached()
        fp = _fingerprint()
        now = time.time()
        if old and old.get("fingerprint") == fp and not force and now - old.get("fetched_at", 0) < CACHE_TTL_S:
            return old
        if old and old.get("fingerprint") == fp and now < (old.get("rate_limited_until") or 0):
            old = dict(old)
            old["warnings"] = list(dict.fromkeys((old.get("warnings") or []) + [old.get("rate_limit_message") or
                                                                                  "GitHub's rate limit was reached"]))
            old["stale"] = True
            return old
        return _refresh(old if old and old.get("fingerprint") == fp else None, fp)


def _refresh(old: Optional[Dict[str, Any]], fp: str) -> Dict[str, Any]:
    tok, tok_src = token()
    include_private = bool(_raw_settings().get("include_private", True))
    warnings: List[str] = []
    rate: Optional[RateLimited] = None
    repos: Dict[str, Dict[str, Any]] = {}
    searched = False
    try:
        for r in search_topic():
            repos[r["full_name"].lower()] = dict(r, found_by=["topic"])
        searched = True
    except RateLimited as e:
        rate = e
    except CatalogError as e:
        warnings.append(f"GitHub search failed: {e.message}")
    me = ""
    if tok and include_private and not rate:
        try:
            me = viewer()
            for r in user_repos_with_topic():
                k = r["full_name"].lower()
                repos.setdefault(k, dict(r, found_by=[]))
                repos[k]["private"] = r["private"]
                if "yours" not in repos[k]["found_by"]:
                    repos[k]["found_by"].append("yours")
        except RateLimited as e:
            rate = e
        except CatalogError as e:
            warnings.append(f"Listing your repositories failed: {e.message}")
    try:
        index, problems = load_indexes()
    except RateLimited as e:
        rate, index, problems = e, {}, []
    warnings.extend(problems)

    if rate and not searched and old:
        # Nothing new to show: keep the old list, and do not ask again until
        # the limit resets.
        out = dict(old)
        out["rate_limited_until"] = rate.reset_at or (time.time() + 300)
        out["rate_limit_message"] = rate.message
        out["warnings"] = [rate.message + " Showing the list from before."] + warnings
        out["stale"] = True
        _write_json(os.path.join(_cache_dir(), "catalog.json"), out)
        return out
    if rate:
        warnings.insert(0, rate.message)

    installed = _installed_github()
    old_entries = (old or {}).get("entries") or {}
    # Repositories that are not found by search but are in an index, or are
    # installed (to check them for updates).
    extra = [k for k in list(index) + list(installed) if k not in repos]
    for k in dict.fromkeys(extra):
        owner, name = k.split("/", 1)
        prev = old_entries.get(k)
        try:
            if rate:
                raise rate
            r = repo_info(owner, name)
            repos[k] = dict(r, found_by=[])
        except CatalogError as e:
            if prev:
                repos[k] = {f: prev.get(f) for f in ("full_name", "owner", "repo", "html_url", "clone_url", "ssh_url",
                                                     "description", "stars", "updated", "default_branch", "private",
                                                     "archived", "topics", "avatar_url")}
                repos[k]["found_by"] = []
            else:
                repos[k] = {"full_name": f"{owner}/{name}", "owner": owner, "repo": name,
                            "html_url": f"https://github.com/{owner}/{name}",
                            "clone_url": f"https://github.com/{owner}/{name}.git",
                            "ssh_url": f"git@github.com:{owner}/{name}.git", "description": "", "stars": 0,
                            "updated": "", "default_branch": "HEAD", "private": False, "archived": False,
                            "topics": [], "avatar_url": "", "found_by": [], "repo_error": e.message}
            if isinstance(e, RateLimited):
                rate = e
        if me and (repos[k].get("owner") or "").lower() == me.lower() and "yours" not in repos[k]["found_by"]:
            repos[k]["found_by"].append("yours")
    for k, r in repos.items():
        if me and (r.get("owner") or "").lower() == me.lower() and "yours" not in r["found_by"]:
            r["found_by"].append("yours")

    def work(item):
        k, r = item
        ix = index.get(k) or {}
        prev = old_entries.get(k)
        # Unchanged since last time: keep the manifest we already have.
        if prev and prev.get("updated") and prev.get("updated") == r.get("updated") and prev.get("raw_manifest") \
                and prev.get("subdir") == ix.get("subdir"):
            return k, prev.get("raw_manifest"), None
        try:
            raw, err = fetch_manifest(r["owner"], r["repo"], None, bool(r.get("private")), ix.get("subdir"))
        except RateLimited as e:
            return k, (prev or {}).get("raw_manifest"), e.message
        return k, raw, err

    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = list(ex.map(work, list(repos.items())))

    entries: Dict[str, Dict[str, Any]] = {}
    for k, raw, err in fetched:
        r = repos[k]
        ix = index.get(k)
        summary = summarize(raw, (ix or {}).get("min_core"))
        tags = list(dict.fromkeys((summary.get("tags") or []) + ((ix or {}).get("tags") or [])))[:mf.MAX_TAGS]
        # An index may also say what a mod provides, needs and may use (so a
        # capability search finds it even when its manifest could not be read).
        for kind in capabilities.KINDS:
            summary[kind] = list(dict.fromkeys((summary.get(kind) or []) + ((ix or {}).get(kind) or [])))[
                :capabilities.MAX_PER_LIST]
        listed = bool(ix) or "topic" in r["found_by"] or "yours" in r["found_by"]
        verified, dropped = verified_status(bool(ix and ix.get("verified")), summary)
        entries[k] = dict(r, raw_manifest=raw, manifest_error=err, manifest=summary, tags=tags,
                          verified=verified, verified_dropped=dropped, indexed=bool(ix),
                          index=({f: ix[f] for f in ("index", "notes", "blocked_versions", "min_core", "verified")}
                                 if ix else None),
                          subdir=(ix or {}).get("subdir"), listed=listed, yours="yours" in r["found_by"])

    # The newest tag, for installed mods that were installed from a tag.
    for k, m in installed.items():
        e = entries.get(k)
        ref = (m.get("source") or {}).get("ref")
        if not e or not ref:
            continue
        prev = old_entries.get(k) or {}
        if prev.get("updated") == e.get("updated") and "latest_tag" in prev:
            e["latest_tag"] = prev["latest_tag"]
            continue
        try:
            e["latest_tag"] = None if rate else _latest_tag(e["owner"], e["repo"])
        except CatalogError:
            e["latest_tag"] = prev.get("latest_tag")

    out = {"fingerprint": fp, "fetched_at": time.time(), "entries": entries, "warnings": warnings,
           "stale": False, "token_source": tok_src, "viewer": me, "index_count": len(index_urls()),
           "rate_limited_until": (rate.reset_at or time.time() + 300) if rate else 0,
           "rate_limit_message": rate.message if rate else ""}
    _write_json(os.path.join(_cache_dir(), "catalog.json"), out)
    return out


# ── Install state, blocked versions, updates ─────────────────────────────

def _install_state(e: Dict[str, Any], installed_by_repo: Dict[str, Dict[str, Any]],
                   installed_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    k = e["full_name"].lower()
    m = installed_by_repo.get(k)
    same_source = bool(m)
    if not m and e["manifest"].get("id"):
        m = installed_by_id.get(e["manifest"]["id"])
    if not m:
        return {"installed": False}
    out = {"installed": True, "installed_id": m["id"], "installed_version": m.get("version"),
           "same_source": same_source}
    if same_source:
        out.update(update_for(m, e))
    return out


def update_for(m: Dict[str, Any], e: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Whether a newer version of an installed mod is out: the default
    branch's manifest version for a mod installed from the default branch (or
    a branch), the newest version tag for one installed from a tag."""
    if not e:
        return {"update_available": False}
    cur = str(m.get("version") or "0")
    ref = (m.get("source") or {}).get("ref")
    latest_tag = e.get("latest_tag")
    if ref and latest_tag and mf.VERSION_RE.match(_norm_version(ref)):
        newer = mf._version_tuple(_norm_version(latest_tag)) > mf._version_tuple(cur)
        return {"update_available": newer, "latest_version": _norm_version(latest_tag), "latest_ref": latest_tag}
    v = (e.get("manifest") or {}).get("version") or ""
    if not ref and v and mf.VERSION_RE.match(v):
        return {"update_available": mf._version_tuple(v) > mf._version_tuple(cur), "latest_version": v,
                "latest_ref": None}
    return {"update_available": False}


def updates() -> Dict[str, Dict[str, Any]]:
    """Update info for installed mods, from the cached catalog (no fetching)."""
    c = cached() or {}
    entries = c.get("entries") or {}
    out = {}
    for k, m in _installed_github().items():
        info = update_for(m, entries.get(k))
        if info.get("update_available"):
            out[m["id"]] = info
    return out


def blocked(source_url: str, version: str) -> Optional[str]:
    """Why this version of the mod at this source is blocked by an index, or
    None. Uses the cached indexes (fetching them only when there is no cache)."""
    k = repo_key(source_url or "")
    if not k or not index_urls():
        return None
    c = cached()
    if c is not None and c.get("fingerprint") == _fingerprint():
        e = (c.get("entries") or {}).get(k) or {}
        ix = e.get("index") or {}
    else:
        try:
            ix = load_indexes()[0].get(k) or {}
        except Exception as ex:  # an index that cannot be read blocks nothing
            logger.info("[mods] indexes unavailable for the blocked check: %s", ex)
            return None
        e = {"full_name": k}
    if _norm_version(version) in (ix.get("blocked_versions") or []):
        note = ix.get("notes") or ""
        return f"version {version} of {e.get('full_name') or k} is blocked by {ix.get('index') or 'an index'}" + \
            (f": {note}" if note else "")
    return None


# ── Queries ──────────────────────────────────────────────────────────────

def _public_entry(e: Dict[str, Any], installed_by_repo, installed_by_id) -> Dict[str, Any]:
    man = e.get("manifest") or {}
    ix = e.get("index") or {}
    version = man.get("version") or ""
    out = {k: e.get(k) for k in ("full_name", "owner", "repo", "html_url", "description", "stars", "updated",
                                  "default_branch", "private", "archived", "tags", "verified", "indexed", "yours",
                                  "subdir", "manifest_error", "repo_error")}
    out.update({
        "id": man.get("id") or "", "name": man.get("name") or e.get("repo"), "version": version,
        "summary": man.get("description") or e.get("description") or "",
        "author": man.get("author") or "", "min_core": man.get("min_core") or "",
        "valid": bool(man.get("valid")) and not e.get("manifest_error"),
        "compatible": bool(man.get("compatible")) and not e.get("manifest_error"),
        "manifest_errors": man.get("errors") or [],
        "permissions": man.get("permissions") or [],
        "tab_count": len(man.get("tabs") or []), "card_count": len(man.get("home_cards") or []),
        "has_tools": bool(man.get("tools")), "icon_url": man.get("icon_url") or image_url(e.get("avatar_url") or ""),
        "notes": ix.get("notes") or "",
        "blocked_versions": ix.get("blocked_versions") or [],
        "version_blocked": bool(version) and _norm_version(version) in (ix.get("blocked_versions") or []),
        "install_source": install_source(e),
        "has_tutorial": bool(man.get("has_tutorial")),
        "provides": man.get("provides") or [], "requires": man.get("requires") or [],
        "optional": man.get("optional") or [],
    })
    # Judged again from this manifest (details may be of another version).
    verified, dropped = verified_status(bool(ix.get("verified")) if ix else bool(e.get("verified")), man)
    out.update({"verified": verified, "verified_dropped": dropped, "badge": "verified" if verified else "community"})
    out.update(_install_state(e, installed_by_repo, installed_by_id))
    return out


def install_source(e: Dict[str, Any]) -> str:
    """The git URL Install uses: https for public repositories, SSH for
    private ones (the server's own GitHub key; the token is never put in a
    git URL)."""
    if e.get("private"):
        return e.get("ssh_url") or f"git@github.com:{e['full_name']}.git"
    return e.get("clone_url") or f"https://github.com/{e['full_name']}.git"


def _truthy(v: Optional[str]) -> Optional[bool]:
    if v is None or str(v).strip() == "":
        return None
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def query(q: str = "", tag: str = "", verified: Optional[str] = None, compatible: Optional[str] = None,
          refresh: bool = False, capability: str = "") -> Dict[str, Any]:
    """The Browse list. `capability` keeps only mods that provide it (the
    install review's "find a provider")."""
    from src.mods import registry
    c = build(force=refresh)
    mods = registry.list_mods()
    by_repo = _installed_github()
    by_id = {m["id"]: m for m in mods}
    want_verified, want_compat = _truthy(verified), _truthy(compatible)
    words = [w for w in re.split(r"\s+", (q or "").strip().lower()) if w][:8]
    tag = (tag or "").strip().lower()
    results, tag_counts = [], {}
    for e in (c.get("entries") or {}).values():
        if not e.get("listed"):
            continue
        p = _public_entry(e, by_repo, by_id)
        for t in p.get("tags") or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        if tag and tag not in (p.get("tags") or []):
            continue
        if want_verified is not None and p["verified"] != want_verified:
            continue
        if want_compat is not None and p["compatible"] != want_compat:
            continue
        if capability and capability not in (p.get("provides") or []):
            continue
        if words:
            hay = " ".join([p["name"] or "", p["id"], p["summary"], p["full_name"], p["author"],
                            " ".join(p.get("tags") or []), " ".join(p.get("provides") or [])]).lower()
            if not all(w in hay for w in words):
                continue
        results.append(p)
    results.sort(key=lambda p: (not p["verified"], not p["yours"], not p["compatible"], -(p["stars"] or 0),
                                str(p["name"]).casefold()))
    tags = [{"tag": t, "count": tag_counts.get(t, 0), "suggested": t in mf.SUGGESTED_TAGS,
             "description": mf.SUGGESTED_TAGS.get(t, "")}
            for t in list(mf.SUGGESTED_TAGS) + sorted(set(tag_counts) - set(mf.SUGGESTED_TAGS))]
    upd = {}
    for k, m in by_repo.items():
        info = update_for(m, (c.get("entries") or {}).get(k))
        if info.get("update_available"):
            upd[m["id"]] = info
    return {"results": results, "tags": tags, "fetched_at": c.get("fetched_at"), "stale": bool(c.get("stale")),
            "warnings": c.get("warnings") or [], "token_source": c.get("token_source") or "",
            "viewer": c.get("viewer") or "", "index_count": c.get("index_count") or 0, "updates": upd,
            "core_api": mf.CORE_API_VERSION, "settings": settings(), "capability": capability or ""}


# ── Details ──────────────────────────────────────────────────────────────

_README_TAGS = {"a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "details", "div", "dl", "dt", "em",
                "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "kbd", "li", "ol", "p", "pre", "s", "span",
                "strong", "sub", "summary", "sup", "table", "tbody", "td", "th", "thead", "tr", "ul"}
_README_ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "title", "width", "height"},
                 "td": {"align"}, "th": {"align"}, "code": {"class"}, "details": {"open"}}


def render_readme(text: str, blob_base: str = "", raw_base: str = "") -> str:
    """Markdown to sanitized HTML: no scripts, styles, event handlers, forms,
    iframes or javascript: links; relative links point at GitHub; images only
    from GitHub's hosts; every link opens a new tab without a referrer."""
    import markdown
    import nh3
    html = markdown.markdown(text or "", extensions=["extra", "sane_lists"], output_format="html")

    def attr_filter(tag: str, attr: str, value: str) -> Optional[str]:
        if tag == "a" and attr == "href":
            v = value.strip()
            if v.startswith("#"):
                return None
            if not re.match(r"^[a-z][a-z0-9+.-]*:", v, re.I):
                v = urljoin(blob_base, v) if blob_base else ""
            return v if re.match(r"^(https?:|mailto:)", v, re.I) else None
        if tag == "img" and attr == "src":
            v = value.strip()
            if not re.match(r"^[a-z][a-z0-9+.-]*:", v, re.I):
                v = urljoin(raw_base, v) if raw_base else ""
            return image_url(v) or None
        if tag == "code" and attr == "class":
            return value if re.match(r"^language-[A-Za-z0-9_+-]{1,30}$", value) else None
        return value

    out = nh3.clean(html, tags=_README_TAGS, attributes=_README_ATTRS, attribute_filter=attr_filter,
                    url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow",
                    clean_content_tags={"script", "style", "iframe", "object", "embed", "noscript", "template",
                                        "form", "textarea", "select", "svg", "math"},
                    set_tag_attribute_values={"a": {"target": "_blank"}})
    # An <img> whose src was refused is useless; drop it.
    return re.sub(r"<img(?![^>]*\ssrc=)[^>]*>", "", out)


def _detail_path(owner: str, repo: str, ref: Optional[str]) -> str:
    h = hashlib.sha256(f"{owner}/{repo}@{ref or ''}".lower().encode()).hexdigest()[:20]
    return os.path.join(_cache_dir(), "details", h + ".json")


def _prune_details() -> None:
    d = os.path.join(_cache_dir(), "details")
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime)
    except OSError:
        return
    for f in files[:-MAX_DETAIL_FILES]:
        try:
            os.remove(f)
        except OSError:
            pass


def _page_label(key: Optional[str]) -> str:
    from src import tool_pages
    for p in tool_pages.PAGES:
        if p.get("key") == key:
            return p.get("label") or key
    return key or ""


def _versions(owner: str, repo: str, blocked_list: List[str]) -> List[Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        rel = _api_json(f"/repos/{quote(owner)}/{quote(repo)}/releases?per_page=20")
        for r in rel if isinstance(rel, list) else []:
            name = str((r or {}).get("tag_name") or "")
            if name and _REF_RE.match(name) and not r.get("draft"):
                out[name] = {"ref": name, "kind": "release", "title": _text(r.get("name"), 100),
                             "date": str(r.get("published_at") or ""), "prerelease": bool(r.get("prerelease"))}
    except RateLimited:
        raise
    except CatalogError:
        pass
    tags = _api_json(f"/repos/{quote(owner)}/{quote(repo)}/tags?per_page=50")
    for t in tags if isinstance(tags, list) else []:
        name = str((t or {}).get("name") or "")
        if name and _REF_RE.match(name) and name not in out:
            out[name] = {"ref": name, "kind": "tag", "title": "", "date": "", "prerelease": False}
    vs = list(out.values())
    for v in vs:
        v["blocked"] = _norm_version(v["ref"]) in blocked_list
    vs.sort(key=lambda v: mf._version_tuple(_norm_version(v["ref"])) if mf.VERSION_RE.match(_norm_version(v["ref"]))
            else (-1, -1, -1), reverse=True)
    return vs


def details(owner: str, repo: str, ref: Optional[str] = None) -> Dict[str, Any]:
    """Everything the details panel shows for one repository."""
    from src.mods import placement, registry
    if not OWNER_RE.match(owner or "") or not REPO_RE.match(repo or ""):
        raise CatalogError("not a GitHub repository name", 400)
    ref = (ref or "").strip() or None
    if ref and not _REF_RE.match(ref):
        raise CatalogError("the branch or tag has characters that are not allowed", 400)
    k = f"{owner}/{repo}".lower()
    c = build()
    e = (c.get("entries") or {}).get(k)
    p = _detail_path(owner, repo, ref)
    d = _read_json(p)
    if not (isinstance(d, dict) and d.get("fingerprint") == c.get("fingerprint")
            and time.time() - d.get("fetched_at", 0) < DETAIL_TTL_S and d.get("updated") == (e or {}).get("updated")):
        d = _fetch_details(owner, repo, ref, e, c)
        _write_json(p, d)
        _prune_details()
    # Install state is live, not cached.
    by_repo = _installed_github()
    by_id = {m["id"]: m for m in registry.list_mods()}
    entry = d["entry"]
    # Updates are judged from the default branch (the catalog entry), not
    # from the ref being looked at.
    entry.update(_install_state(e or dict(entry, manifest=d["manifest_full"]), by_repo, by_id))
    man = d["manifest_full"]
    tabs = []
    for t in man.get("tabs") or []:
        pl = placement.resolve(t)
        groups = {g["id"]: g["label"] for g in placement.groups()}
        tabs.append({"id": t.get("id"), "title": t.get("title"), "icon": t.get("icon"), "path": t.get("path"),
                     "section": pl["section"], "section_label": groups.get(pl["section"], pl["section"]),
                     "after": pl["after"], "after_label": _page_label(pl["after"]) if pl["after"] else "",
                     "order": pl["order"]})
    ix = entry.get("index") or {}
    version = man.get("version") or ""
    return {
        "repo": entry, "ref": ref, "readme_html": d.get("readme_html") or "", "readme_missing": d.get("readme_missing"),
        "manifest": {k2: man.get(k2) for k2 in ("id", "name", "version", "description", "author", "license",
                                                 "min_core", "tags", "valid", "compatible", "errors", "warnings",
                                                 "icon_url", "screenshots", "home_cards", "tools", "events",
                                                 "has_tutorial", "tutorial_steps")},
        "manifest_error": d.get("manifest_error"),
        "runtime": {"type": (man.get("runtime") or {}).get("type"), "command": (man.get("runtime") or {}).get("command"),
                    "setup": (man.get("runtime") or {}).get("setup") or [],
                    "start": (man.get("runtime") or {}).get("start")},
        "permissions": mf.describe_permissions(man.get("permissions") or []),
        # What it provides, needs and may use, and whether this Odysseus has a provider now.
        "capabilities": capabilities.availability({k2: man.get(k2) or [] for k2 in capabilities.KINDS}),
        "tabs": tabs,
        "versions": d.get("versions") or [],
        "versions_error": d.get("versions_error"),
        "version_blocked": bool(version) and _norm_version(version) in (ix.get("blocked_versions") or []),
        "install": {"source": entry.get("install_source"), "ref": ref, "subdir": entry.get("subdir")},
        "core_api": mf.CORE_API_VERSION,
        "warnings": c.get("warnings") if c.get("stale") else [],
    }


def _fetch_details(owner: str, repo: str, ref: Optional[str], e: Optional[Dict[str, Any]],
                   c: Dict[str, Any]) -> Dict[str, Any]:
    from src.mods import registry
    if e is None:
        info = repo_info(owner, repo)
        ix = None
        e = dict(info, found_by=[], tags=[], verified=False, indexed=False, index=None, subdir=None,
                 listed=False, yours=bool(c.get("viewer")) and info["owner"].lower() == c["viewer"].lower(),
                 raw_manifest=None, manifest_error=None, manifest=summarize(None))
        raw, err = fetch_manifest(owner, info["repo"], None, info["private"])
        e["raw_manifest"], e["manifest_error"] = raw, err
        e["manifest"] = summarize(raw)
        e["tags"] = e["manifest"].get("tags") or []
    ix = e.get("index") or {}
    owner, repo = e.get("owner") or owner, e.get("repo") or repo
    raw, merr = e.get("raw_manifest"), e.get("manifest_error")
    if ref:
        raw, merr = fetch_manifest(owner, repo, ref, bool(e.get("private")), e.get("subdir"))
    full = summarize(raw, ix.get("min_core"))
    readme_html, readme_missing = "", False
    branch = ref or e.get("default_branch") or "HEAD"
    sub = (e.get("subdir").strip("/") + "/") if e.get("subdir") else ""
    blob_base = f"https://github.com/{owner}/{repo}/blob/{branch}/{sub}"
    raw_base = f"{RAW_BASE}/{owner}/{repo}/{branch}/{sub}"
    try:
        body = None
        if token()[0] or e.get("private"):
            path = f"/repos/{quote(owner)}/{quote(repo)}/readme" + (f"/{quote(e['subdir'])}" if e.get("subdir") else "")
            r = get(GITHUB_API + path + (f"?ref={quote(ref, safe='')}" if ref else ""),
                    accept="application/vnd.github.raw+json", max_bytes=MAX_README_BYTES)
            body = r.body if r.status == 200 else None
        else:
            for name in ("README.md", "readme.md", "Readme.md", "README"):
                body = fetch_file(owner, repo, sub + name, ref or e.get("default_branch"), False, MAX_README_BYTES)
                if body is not None:
                    break
        if body is None:
            readme_missing = True
        else:
            readme_html = render_readme(body.decode("utf-8", "replace"), blob_base, raw_base)
    except CatalogError as ex:
        readme_missing = True
        logger.info("[mods] readme for %s/%s: %s", owner, repo, ex.message)
    versions, verr = [], None
    try:
        versions = _versions(owner, repo, ix.get("blocked_versions") or [])
    except CatalogError as ex:
        verr = ex.message
    entry = _public_entry(dict(e, manifest=full, manifest_error=merr), {}, {})
    entry["index"] = e.get("index")
    return {"fingerprint": c.get("fingerprint"), "fetched_at": time.time(), "updated": e.get("updated"),
            "entry": entry, "manifest_full": full, "manifest_error": merr, "readme_html": readme_html,
            "readme_missing": readme_missing, "versions": versions, "versions_error": verr}
