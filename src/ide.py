"""The integrated IDE (routes/ide_routes.py, static/js/codePanel.js).

Asked for: "can we implement a vs code or an integrated ide into it, one for
modifying the odysseus dev and also for other projects too please."

Odysseus doesn't ship an editor. It reverse-proxies a code-server (VS Code in
the browser) that already runs on the host, at /ide/, so the editor is served
from the same origin as the app and can sit in a tab beside a chat. This
module holds what the proxy and the Code panel need that isn't HTTP:

  * the settings (the upstream URL and the folders to look for projects in),
    kept in DATA_DIR/ide.json;
  * the project list: git repos one level under each root, with the branch
    and whether the work tree is dirty;
  * path checks, so "Open folder" only opens folders under those roots;
  * the header and cookie rewriting the proxy does.
"""
import asyncio
import json
import os
import re
import threading
from typing import Dict, List, Optional
from urllib.parse import urlsplit

DEFAULT_UPSTREAM = "http://127.0.0.1:8080"
PREFIX = "/ide"
GIT_TIMEOUT_S = 4.0

_lock = threading.Lock()


# ── Settings ────────────────────────────────────────────────────────────────

def _config_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "ide.json")


def default_roots() -> List[str]:
    return [os.path.expanduser("~")]


def load_config() -> Dict:
    try:
        with open(_config_path(), encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    upstream = cfg.get("upstream") or os.environ.get("ODYSSEUS_IDE_URL") or DEFAULT_UPSTREAM
    roots = [r for r in (cfg.get("roots") or []) if isinstance(r, str) and r.strip()] or default_roots()
    return {"upstream": upstream.rstrip("/"), "roots": roots}


def validate_upstream(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_UPSTREAM
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The editor address has to be an http:// or https:// URL")
    if parts.query or parts.fragment:
        raise ValueError("The editor address can't have a query or a fragment")
    return url


def validate_roots(roots) -> List[str]:
    if isinstance(roots, str):
        roots = roots.splitlines()
    out = []
    for r in roots or []:
        r = str(r).strip()
        if not r:
            continue
        r = os.path.expanduser(r)
        if not os.path.isabs(r):
            raise ValueError(f"Project folders have to be absolute paths: {r}")
        r = os.path.normpath(r)
        if not os.path.isdir(r):
            raise ValueError(f"No such folder: {r}")
        if r not in out:
            out.append(r)
    return out


def save_config(data: Dict) -> Dict:
    cfg = load_config()
    if "upstream" in data:
        cfg["upstream"] = validate_upstream(data.get("upstream"))
    if "roots" in data:
        cfg["roots"] = validate_roots(data.get("roots")) or default_roots()
    from core.atomic_io import atomic_write_json
    with _lock:
        os.makedirs(os.path.dirname(_config_path()), exist_ok=True)
        atomic_write_json(_config_path(), cfg, indent=2)
    return load_config()


def upstream_url() -> str:
    return load_config()["upstream"]


# ── Projects ────────────────────────────────────────────────────────────────

def odysseus_checkout() -> Optional[str]:
    """The Odysseus dev checkout: the main work tree of the repo this app runs
    from (the app may itself run from a linked worktree of it)."""
    from src.constants import BASE_DIR
    here = os.path.normpath(BASE_DIR)
    git = os.path.join(here, ".git")
    if os.path.isdir(git):
        return here
    if os.path.isfile(git):
        # A linked worktree: ".git" is a file "gitdir: <main>/.git/worktrees/<name>".
        try:
            with open(git, encoding="utf-8") as f:
                line = f.read().strip()
            gitdir = line.split(":", 1)[1].strip() if line.startswith("gitdir:") else ""
            m = re.match(r"^(.*)/\.git/worktrees/[^/]+/?$", gitdir)
            if m and os.path.isdir(m.group(1)):
                return os.path.normpath(m.group(1))
        except OSError:
            pass
        return here
    return None


def _real(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def allowed_roots() -> List[str]:
    roots = [_real(r) for r in load_config()["roots"]]
    ody = odysseus_checkout()
    if ody:
        roots.append(_real(ody))
    return roots


def resolve_folder(path: str) -> str:
    """An absolute folder under one of the roots, symlinks resolved. Raises
    ValueError for anything else (relative paths, `..` out of the roots,
    files, missing folders)."""
    path = (path or "").strip()
    if not path:
        raise ValueError("Give a folder")
    path = os.path.expanduser(path)
    if not os.path.isabs(path) or "\x00" in path:
        raise ValueError("The folder has to be an absolute path")
    real = os.path.realpath(path)
    if not any(_under(real, r) for r in allowed_roots()):
        raise ValueError("That folder isn't under the project folders in Settings > System > Code editor")
    if not os.path.isdir(real):
        raise ValueError("No such folder")
    return real


def _is_repo(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


async def git_state(path: str) -> Dict:
    """Branch and dirty flag of a work tree, from one `git status` call."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "--no-optional-locks", "status", "--porcelain=v1", "-b",
            "--ignore-submodules", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    except (OSError, ValueError):
        return {"branch": None, "dirty": None}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return {"branch": None, "dirty": None}
    if proc.returncode != 0:
        return {"branch": None, "dirty": None}
    return parse_status(out.decode("utf-8", "replace"))


def parse_status(text: str) -> Dict:
    lines = text.splitlines()
    branch = None
    if lines and lines[0].startswith("## "):
        head = lines[0][3:]
        if head.startswith("No commits yet on "):
            branch = head[len("No commits yet on "):].strip()
        elif head.startswith("HEAD (no branch)"):
            branch = "detached"
        else:
            branch = head.split("...", 1)[0].split(" ", 1)[0]
        lines = lines[1:]
    return {"branch": branch, "dirty": any(l.strip() for l in lines)}


def _candidates() -> List[str]:
    seen, out = set(), []

    def add(p):
        r = _real(p)
        if r not in seen and os.path.isdir(r):
            seen.add(r)
            out.append(r)

    for root in load_config()["roots"]:
        root = os.path.expanduser(root)
        if _is_repo(root):
            add(root)
        try:
            names = sorted(os.listdir(root), key=str.lower)
        except OSError:
            continue
        for n in names:
            if n.startswith("."):
                continue
            p = os.path.join(root, n)
            if os.path.isdir(p) and _is_repo(p):
                add(p)
    return out


async def list_projects() -> List[Dict]:
    ody = odysseus_checkout()
    ody_real = _real(ody) if ody else None
    paths = _candidates()
    if ody_real:
        paths = [ody_real] + [p for p in paths if p != ody_real]
    states = await asyncio.gather(*(git_state(p) for p in paths))
    out = []
    for p, st in zip(paths, states):
        pinned = p == ody_real
        out.append({
            "name": "Odysseus dev" if pinned else os.path.basename(p),
            "path": p,
            "branch": st["branch"],
            "dirty": st["dirty"],
            "pinned": pinned,
        })
    return out


# ── Proxy helpers ───────────────────────────────────────────────────────────

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}
# Request headers that belong to Odysseus and never go to the editor.
_DROP_REQUEST = HOP_BY_HOP | {
    "host", "cookie", "authorization", "x-odysseus-internal-token", "x-odysseus-owner",
    "x-api-key", "x-auth-token", "forwarded", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-forwarded-prefix", "x-real-ip", "content-length",
}


def _is_odysseus_cookie(name: str) -> bool:
    return name.lower().startswith("odysseus")


def filter_cookie_header(value: str) -> str:
    """The Cookie header without Odysseus's own cookies (its session stays here)."""
    keep = []
    for part in (value or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name = part.split("=", 1)[0].strip()
        if _is_odysseus_cookie(name):
            continue
        keep.append(part)
    return "; ".join(keep)


def _connection_tokens(headers) -> set:
    v = headers.get("connection") or ""
    return {t.strip().lower() for t in v.split(",") if t.strip()}


def public_host(headers) -> str:
    """The host the browser used: what Tailscale Serve or another proxy in
    front of Odysseus says, else the Host header."""
    xfh = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
    return xfh or headers.get("host", "")


def public_proto(headers, scheme: str) -> str:
    xfp = (headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if xfp:
        return xfp
    return "https" if scheme in ("https", "wss") else "http"


def upstream_request_headers(headers, client_host: Optional[str], scheme: str) -> List[tuple]:
    """Headers to send upstream: hop-by-hop and Odysseus's own dropped,
    Odysseus cookies filtered out, and the X-Forwarded-* set so code-server's
    origin check sees the host the browser used."""
    drop = _DROP_REQUEST | _connection_tokens(headers)
    out = [(k, v) for k, v in headers.items() if k.lower() not in drop]
    cookie = filter_cookie_header(headers.get("cookie") or "")
    if cookie:
        out.append(("cookie", cookie))
    host = public_host(headers)
    if host:
        out.append(("x-forwarded-host", host))
    out.append(("x-forwarded-proto", public_proto(headers, scheme)))
    out.append(("x-forwarded-prefix", PREFIX))
    prior = headers.get("x-forwarded-for")
    if client_host:
        out.append(("x-forwarded-for", f"{prior}, {client_host}" if prior else client_host))
    return out


def rewrite_location(loc: str, upstream: str) -> str:
    """Keep redirects under /ide/: an absolute URL at the upstream, or a
    root-relative path, gets the prefix. Relative ones ("./login") already
    resolve under it."""
    if not loc:
        return loc
    up = urlsplit(upstream)
    base_path = up.path.rstrip("/")
    parts = urlsplit(loc)
    if parts.scheme and parts.netloc:
        if (parts.scheme, parts.netloc) != (up.scheme, up.netloc):
            return loc
        path = parts.path
        rest = path[len(base_path):] if base_path and path.startswith(base_path) else path
        loc = rest or "/"
        if parts.query:
            loc += "?" + parts.query
        if parts.fragment:
            loc += "#" + parts.fragment
    elif loc.startswith("//"):
        return loc
    elif loc.startswith("/"):
        if base_path and loc.startswith(base_path + "/"):
            loc = loc[len(base_path):]
    else:
        return loc
    if loc == PREFIX or loc.startswith(PREFIX + "/") or loc.startswith(PREFIX + "?"):
        return loc
    return PREFIX + loc


def rewrite_set_cookie(value: str) -> Optional[str]:
    """Scope an upstream cookie to /ide/ and this host. None drops it (a cookie
    that would clobber one of Odysseus's own)."""
    parts = [p.strip() for p in value.split(";")]
    if not parts or "=" not in parts[0]:
        return None
    name = parts[0].split("=", 1)[0].strip()
    if _is_odysseus_cookie(name):
        return None
    attrs, path = [], None
    for a in parts[1:]:
        if not a:
            continue
        k = a.split("=", 1)[0].strip().lower()
        if k == "domain":
            continue
        if k == "path":
            path = a.split("=", 1)[1].strip() if "=" in a else ""
            continue
        attrs.append(a)
    if not path or not path.startswith("/"):
        path = PREFIX + "/"
    elif not (path == PREFIX or path.startswith(PREFIX + "/")):
        path = PREFIX + path
    return "; ".join([parts[0], f"Path={path}"] + attrs)


def response_headers(headers, upstream: str) -> List[tuple]:
    """Headers to send back to the browser from an upstream response."""
    drop = HOP_BY_HOP | _connection_tokens(headers)
    out = []
    for k, v in headers.multi_items():
        lk = k.lower()
        if lk in drop:
            continue
        if lk == "location":
            v = rewrite_location(v, upstream)
        elif lk == "set-cookie":
            v = rewrite_set_cookie(v)
            if v is None:
                continue
        out.append((k, v))
    return out


_VERSION_RE = re.compile(r"codeServerVersion(?:&quot;|\")\s*:\s*(?:&quot;|\")([0-9][0-9A-Za-z.\-+]*)")


def version_from_html(html: str) -> Optional[str]:
    m = _VERSION_RE.search(html or "")
    return m.group(1) if m else None
