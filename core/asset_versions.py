"""Content-hashed URLs for the frontend's JS and CSS.

The app ships raw ES modules with no build step, so every file used to be
revalidated on every load (215 conditional requests on a warm page). Instead
the server stamps each served page with ?v=<content hash> URLs and an import
map that points every module specifier at its hashed URL. A hashed URL never
changes content, so the browser keeps it forever (immutable) and a repeat
visit makes no requests for unchanged code. Editing a file changes its hash,
which changes its URL, so deploys still show up at once.
"""
import hashlib
import json
import os
import re
import time
from typing import Dict, Tuple

_EXTS = (".js", ".css")
_TTL = 2.0  # seconds between directory rescans

_hash_cache: Dict[str, Tuple[int, int, str]] = {}  # url -> (mtime_ns, size, hash)
_state = {"at": 0.0, "map": {}}


def _scan(static_dir: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for sub in ("js", "css"):
        base = os.path.join(static_dir, sub)
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if not name.endswith(_EXTS):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, static_dir).replace(os.sep, "/")
                out["/static/" + rel] = path
    for top in ("app.js", "style.css"):
        path = os.path.join(static_dir, top)
        if os.path.exists(path):
            out["/static/" + top] = path
    return out


def versions(static_dir: str) -> Dict[str, str]:
    """Map of '/static/...' url -> short content hash, refreshed every 2s."""
    now = time.monotonic()
    if now - _state["at"] < _TTL and _state["map"]:
        return _state["map"]
    result: Dict[str, str] = {}
    for url, path in _scan(static_dir).items():
        try:
            st = os.stat(path)
        except OSError:
            continue
        cached = _hash_cache.get(url)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            result[url] = cached[2]
            continue
        with open(path, "rb") as f:
            h = hashlib.md5(f.read()).hexdigest()[:10]
        _hash_cache[url] = (st.st_mtime_ns, st.st_size, h)
        result[url] = h
    _state["at"] = now
    _state["map"] = result
    return result


_ATTR = re.compile(r'(\b(?:src|href)=")(/static/(?:js/[^"?]+\.js|css/[^"?]+\.css|app\.js|style\.css))(?:\?[^"]*)?(")')


_IMPORT = re.compile(r"""(?:^|[;\n}])\s*(?:import|export)\s[^'";]*?from\s*['"]([^'"]+)['"]|(?:^|[;\n}])\s*import\s*['"]([^'"]+)['"]""")
_graph_cache: Dict[Tuple[str, ...], list] = {}


def boot_modules(html: str, static_dir: str) -> list:
    """Every module the page's <script type=module> tags pull in through static
    imports, as '/static/...' urls. Preloading them all turns the browser's
    discover-one-level-at-a-time fetch chain into a single parallel batch."""
    roots = re.findall(r'<script[^>]*type="module"[^>]*src="(/static/[^"?]+)', html)
    vmap = versions(static_dir)
    key = tuple(roots) + (str(hash(tuple(sorted(vmap.items())))),)
    hit = _graph_cache.get(key)
    if hit is not None:
        return hit
    seen: Dict[str, None] = {}
    stack = list(roots)
    while stack:
        url = stack.pop()
        if url in seen or url not in vmap:
            continue
        seen[url] = None
        try:
            with open(os.path.join(static_dir, url[len("/static/"):]), encoding="utf-8") as f:
                src = f.read()
        except OSError:
            continue
        for m in _IMPORT.finditer(src):
            spec = m.group(1) or m.group(2)
            if not spec.startswith((".", "/")):
                continue
            spec = spec.split("?")[0]
            base = url.rsplit("/", 1)[0]
            target = os.path.normpath(spec if spec.startswith("/") else base + "/" + spec).replace(os.sep, "/")
            stack.append(target)
    _graph_cache.clear()
    _graph_cache[key] = list(seen)
    return _graph_cache[key]


def stamp_html(html: str, static_dir: str, nonce: str = "") -> str:
    """Version every local js/css reference in the page and prepend the import map."""
    vmap = versions(static_dir)

    def sub(m):
        v = vmap.get(m.group(2))
        return f"{m.group(1)}{m.group(2)}?v={v}{m.group(3)}" if v else m.group(0)

    html = _ATTR.sub(sub, html)
    imports = {u: f"{u}?v={v}" for u, v in vmap.items() if u.endswith(".js")}
    tag = f'<script type="importmap" nonce="{nonce}">{json.dumps({"imports": imports}, separators=(",", ":"))}</script>'
    preload = "".join(
        f'<link rel="modulepreload" href="{u}?v={vmap[u]}">' for u in boot_modules(html, static_dir)
    )
    return html.replace("<head>", "<head>\n  " + tag + "\n  " + preload, 1)


def cache_header_for(url_path: str, query: str, static_dir: str) -> str:
    """Cache-Control for a /static js/css request: immutable only for the current hash."""
    m = re.search(r"(?:^|&)v=([0-9a-f]+)(?:&|$)", query or "")
    if m and versions(static_dir).get("/static/" + url_path) == m.group(1):
        return "public, max-age=31536000, immutable"
    return "no-cache"
