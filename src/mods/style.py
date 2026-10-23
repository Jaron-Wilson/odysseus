"""Mod styles (permission `ui.style`): one stylesheet a mod may add to the main page.

CSS only. No script from a mod ever reaches the main page through this: the
core fetches the stylesheet from the mod's own server, checks it
(`sanitize`), keeps the checked copy in its own folder and serves it from its
own origin as /api/mods/ui/style/<id>.css. The page links it as
<link data-mod="<id>">, after the core's own styles and ordered by mod id.

A mod's manifest names where its server serves the stylesheet:

    "permissions": ["ui.style"],
    "ui": { "style": { "path": "/mod/style.css" } }

and the mod tells the core it changed with POST /api/mods/v1/ui/style/refresh
(the core fetches it again; the page re-links it with a new `?v=` and
repaints at once, without a reload).

What is refused (the whole stylesheet, never a silently trimmed one, so the
author sees why; the reason shows in Settings > Mods and in the mod's log):

  - more than MAX_BYTES (64 KB)
  - @import, and anything else that fetches another stylesheet
  - url() that is not a `data:image/...` URI, a `#fragment`, or one of the
    mod's own proxied files (/mods/<its id>/...); image-set(), src(),
    cross-fade(), element() (they can hide a URL in a string)
  - expression(), behavior, -moz-binding, javascript: and vbscript:
  - CSS escapes that spell any of those (\\75rl( is checked as url()), and
    anything that could close the <link>'s own context (`</`, `<!--`)

Theme tokens: the variables below are the documented way to retheme. A
declaration of a color token is served with !important added, because the
user's own theme (Settings > Theme) sets them inline on <html>, which would
otherwise beat any stylesheet. The `--ody-*` tokens are consumed by rules the
core adds after the mod's sheet, only when the mod set that token, so a mod
that sets none of them changes nothing it did not ask for.

Safe mode: `?safe=1` on the page address skips every mod stylesheet for that
load; Settings > Mods has a switch that does the same until turned off (a
cookie, so the server can leave the links out before the first paint).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

from src.mods import paths, registry, signing

logger = logging.getLogger(__name__)

PERMISSION = "ui.style"
MAX_BYTES = 64 * 1024
FETCH_TIMEOUT_S = 10
SAFE_COOKIE = "ody_mod_styles"          # "off" turns mod styles off (Settings > Mods)
SAFE_QUERY = "safe"                     # ?safe=1

STYLE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")

# Color and font tokens: the core's own variables. Declared by a mod, they win
# over the user's theme (see the module doc).
COLOR_TOKENS = ("bg", "fg", "panel", "border", "accent", "accent-ink", "fg-muted", "red", "font-family")
# Layout tokens the core adds rules for (DERIVED, below).
LAYOUT_TOKENS = ("ody-radius", "ody-font-scale", "ody-density")

TOKEN_DOCS: Dict[str, str] = {
    "--bg": "page background",
    "--fg": "main text",
    "--panel": "raised surfaces: sidebar, cards, dialogs, inputs",
    "--border": "lines and outlines (also the Workspace and Studio designs' lines)",
    "--accent": "links, active items, buttons, highlights (also the Workspace and Studio accent)",
    "--accent-ink": "text on top of --accent",
    "--fg-muted": "secondary text (also the Workspace and Studio secondary text)",
    "--red": "the theme's brand color (loading screen, warnings)",
    "--font-family": "the font stack for the whole app",
    "--ody-radius": "corner radius of buttons, inputs, cards and dialogs, like 12px",
    "--ody-font-scale": "text and spacing scale of the whole UI: 0.9 smaller, 1 normal, 1.15 larger",
    "--ody-density": "spacing scale for chat messages and lists: 0.7 is compact, 1 normal, 1.3 roomy",
}

# Rules the core adds for a layout token the mod set. Selectors are the app's
# own surfaces; each rule reads the token, so a value like `0` also works.
_RADIUS_SELECTORS = (
    "button, input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]), select, textarea, "
    ".settings-btn, .btn, .settings-select, .admin-card, .modal-content, .cal-modal-content, .msg, .list-item, "
    ".session-item, .studio-card, .composer, #input-bar, .dropdown-menu, .popup-menu, [role=dialog], [role=menu], "
    ".ws-card, .ws-compose, .ws-compose-input, .ws-bar-btn, .ws-tab, .ws-quick > *, .ws-mod-grid > *"
)
DERIVED: Dict[str, str] = {
    "ody-radius": "%s { border-radius: var(--ody-radius) !important }" % _RADIUS_SELECTORS,
    # Text and spacing scale. `zoom` is what Settings > Theme's 125% option uses: it scales px text too,
    # which a root font-size cannot. It goes on the *contents* of the sidebar, the Workspace pages, chat
    # messages and dialogs, never on the shell: the Workspace measures the shell in screen pixels and
    # would place everything wrongly if the whole page were zoomed.
    "ody-font-scale": (
        ".sidebar-inner, .ws-page > *, .msg, .modal-content { zoom: var(--ody-font-scale) !important }"
        " .modal-content { max-height: calc(85dvh / var(--ody-font-scale)) !important }"
    ),
    "ody-density": (
        ".msg { padding: calc(10px * var(--ody-density)) calc(14px * var(--ody-density)) !important;"
        " margin-bottom: calc(8px * var(--ody-density)) !important }"
        " .list-item, .session-item { padding-top: calc(6px * var(--ody-density)) !important;"
        " padding-bottom: calc(6px * var(--ody-density)) !important }"
        " .ws-grid, .ws-mod-grid { gap: calc(16px * var(--ody-density)) !important }"
        " .ws-card-body { padding: calc(14px * var(--ody-density)) !important }"
    ),
}


# The Workspace and Studio designs (every default install) derive their lines, secondary text and accent
# from their own `--st-*` variables, not from --border, --fg-muted and --accent. So when a mod sets one of
# these tokens, the core also points the matching `--st-*` variable at it; otherwise a "high contrast" sheet
# would still draw 9% grey lines.
COLOR_DERIVED: Dict[str, str] = {
    "border": "--st-line: var(--border) !important; --st-line-2: var(--border) !important;",
    "fg-muted": "--st-text-2: var(--fg-muted) !important; --st-text-3: var(--fg-muted) !important;",
    "accent": "--st-accent: var(--accent) !important;",
    "accent-ink": "--st-accent-fg: var(--accent-ink) !important;",
}


class StyleError(ValueError):
    """The stylesheet was refused. The message says why, for the mod's author."""


# ── The manifest part ─────────────────────────────────────────────────────

def check_manifest(raw: Any, errors: List[str]) -> Optional[Dict[str, str]]:
    """`ui.style`: {"path": "/mod/style.css"}, where the mod's server serves its stylesheet."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append("`ui.style` must be an object like {\"path\": \"/mod/style.css\"}")
        return None
    path = raw.get("path", "/mod/style.css")
    if not isinstance(path, str) or not STYLE_PATH_RE.match(path) or ".." in path.split("/") or "//" in path:
        errors.append(f"`ui.style.path` must be a path starting with / (got {path!r})")
        return None
    return {"path": path}


# ── Checking a stylesheet ─────────────────────────────────────────────────

_ESC_RE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\r\n\f]?|(.))", re.S)
_URL_RE = re.compile(r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^)\"']*))\s*\)", re.I)
_BANNED = (
    (r"@import", "@import is not allowed (a mod's stylesheet is one file)"),
    (r"@namespace", "@namespace is not allowed"),
    (r"expression", "expression() is not allowed"),
    (r"(?<![\w-])behavior\s*:", "`behavior` is not allowed"),
    (r"-moz-binding", "`-moz-binding` is not allowed"),
    (r"javascript\s*:", "javascript: is not allowed"),
    (r"vbscript\s*:", "vbscript: is not allowed"),
    (r"image-set\(", "image-set() is not allowed (use url() with a data: image)"),
    (r"cross-fade\(", "cross-fade() is not allowed"),
    (r"(?<![\w-])element\(", "element() is not allowed"),
    (r"(?<![\w-])src\(", "src() is not allowed (use url() with a data: image)"),
    (r"(?<![\w-])paint\(", "paint() is not allowed"),
    (r"</", "`</` is not allowed"),
    (r"<!--", "`<!--` is not allowed"),
)
_DATA_IMAGE_RE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp|avif|bmp|x-icon|svg\+xml)[;,]", re.I)


def _strip_comments(css: str, glue: str = " ") -> str:
    """The stylesheet without /* comments */, leaving strings alone. `glue` replaces each comment."""
    out: List[str] = []
    i, n = 0, len(css)
    while i < n:
        c = css[i]
        if c == "/" and css.startswith("/*", i):
            j = css.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(glue)
        elif c in "\"'":
            j = i + 1
            while j < n and css[j] != c:
                j += 2 if css[j] == "\\" else 1
            out.append(css[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _unescape(css: str) -> str:
    def one(m):
        if m.group(1):
            cp = int(m.group(1), 16)
            return chr(cp) if 0 < cp < 0x110000 else "�"
        return m.group(2)
    return _ESC_RE.sub(one, css)


def _own_asset(target: str, mod_id: str) -> bool:
    """/mods/<this mod>/...: a file the mod serves itself, through the proxy."""
    if not target.startswith(f"/mods/{mod_id}/") or "//" in target[1:] or "\\" in target:
        return False
    path = target.split("?", 1)[0].split("#", 1)[0]
    return ".." not in path.split("/") and "%2e" not in path.lower() and "%2f" not in path.lower()


def sanitize(css: str, mod_id: str) -> str:
    """The checked stylesheet (comments removed, color tokens made to win), or StyleError."""
    if not isinstance(css, str):
        raise StyleError("the stylesheet is not text")
    if len(css.encode("utf-8", "replace")) > MAX_BYTES:
        raise StyleError(f"the stylesheet is larger than {MAX_BYTES // 1024} KB")
    if "\x00" in css:
        raise StyleError("the stylesheet contains a NUL character")
    code = _strip_comments(css)
    # Checked with comments removed outright, so `u/**/rl(` is read as url( (a browser would not, but nothing legit needs it).
    norm = _unescape(_strip_comments(css, ""))
    low = re.sub(r"\s+", " ", norm).lower()
    for pattern, why in _BANNED:
        if re.search(pattern, low):
            raise StyleError(why)
    urls = _URL_RE.findall(norm)
    if len(urls) != len(re.findall(r"url\(", norm, re.I)):
        raise StyleError("a url() is malformed")
    for q1, q2, bare in urls:
        target = (q1 or q2 or bare).strip()
        if _DATA_IMAGE_RE.match(target) or target.startswith("#") or _own_asset(target, mod_id):
            continue
        raise StyleError(f"url({target[:60]!r}) points outside this server; only data: images and "
                         f"/mods/{mod_id}/... files are allowed")
    return finalize(code)


def finalize(code: str) -> str:
    """Make color tokens win over the user's theme, and add the rules for the layout tokens set."""
    names = "|".join(re.escape(t) for t in COLOR_TOKENS)
    code = re.sub(r"(?<![\w-])(--(?:%s))(\s*:\s*)([^;{}]*?)(\s*)(?=[;}]|$)" % names,
                  lambda m: m.group(0) if "!important" in m.group(3).lower()
                  else f"{m.group(1)}{m.group(2)}{m.group(3)} !important{m.group(4)}", code)
    def has(tok: str) -> bool:
        return bool(re.search(r"(?<![\w-])--%s\s*:" % re.escape(tok), code))

    extra = [rule for tok, rule in DERIVED.items() if has(tok)]
    studio = " ".join(rule for tok, rule in COLOR_DERIVED.items() if has(tok))
    if studio:
        extra.insert(0, "html.ui-studio { %s }" % studio)
    if extra:
        code = code.rstrip() + "\n\n/* Odysseus: rules for the tokens set above */\n" + "\n".join(extra) + "\n"
    return code.strip() + "\n"


# ── What is stored, and who is eligible ───────────────────────────────────

def _dir(mod_id: str) -> str:
    # Not under the mod's data/ folder (the mod can write there): only the core writes here.
    return os.path.join(paths.mod_dir(mod_id), "ui-style")


def eligible(entry: Optional[Dict[str, Any]]) -> bool:
    """Enabled, approved for ui.style, and its manifest names a stylesheet."""
    if not entry or not entry.get("enabled"):
        return False
    if PERMISSION not in (entry.get("granted_permissions") or []):
        return False
    return bool((((entry.get("manifest") or {}).get("ui") or {}).get("style") or {}).get("path"))


def _meta(mod_id: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(_dir(mod_id), "meta.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _store(mod_id: str, css: Optional[str], entry: Dict[str, Any], error: str = "") -> Dict[str, Any]:
    d = _dir(mod_id)
    os.makedirs(d, exist_ok=True)
    meta = {"hash": hashlib.sha256(css.encode()).hexdigest()[:10] if css else "", "bytes": len(css.encode()) if css else 0,
            "error": error, "at": time.time(), "mod_version": entry.get("version"), "commit": entry.get("commit")}
    f = os.path.join(d, "style.css")
    if css:
        tmp = f + f".tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(css)
        os.replace(tmp, f)
    elif os.path.exists(f):
        os.remove(f)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    return meta


def clear(mod_id: str) -> None:
    shutil.rmtree(_dir(mod_id), ignore_errors=True)


def read(mod_id: str) -> Optional[str]:
    try:
        with open(os.path.join(_dir(mod_id), "style.css"), "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def status(mod_id: str) -> Dict[str, Any]:
    """For Settings > Mods: whether the mod's stylesheet is in use, its size, and why not if refused."""
    m = _meta(mod_id)
    return {"active": bool(m.get("hash")), "bytes": m.get("bytes", 0), "error": m.get("error", ""), "version": m.get("hash", "")}


def active(entry: Dict[str, Any]) -> bool:
    """Whether this mod's stylesheet goes on the page now (an eligible mod with a good copy)."""
    if not eligible(entry):
        return False
    m = _meta(entry["id"])
    return bool(m.get("hash")) and m.get("mod_version") == entry.get("version") and m.get("commit") == entry.get("commit")


def listing() -> List[Dict[str, str]]:
    """The stylesheets to link, in mod id order: [{id, url, v}]."""
    out = []
    for entry in sorted(registry.list_mods(), key=lambda m: m["id"]):
        if active(entry):
            v = _meta(entry["id"])["hash"]
            out.append({"id": entry["id"], "v": v, "url": f"/api/mods/ui/style/{entry['id']}.css?v={v}"})
    return out


def is_safe(request) -> bool:
    """Mod styles are off for this request: ?safe=1, or the Settings switch."""
    try:
        if request.query_params.get(SAFE_QUERY, "").lower() in ("1", "true", "yes", "on"):
            return True
        return (request.cookies.get(SAFE_COOKIE) or "").lower() == "off"
    except Exception:
        return False


def head_links(request) -> str:
    """The <link data-mod> tags for the main page's <head> (empty in safe mode). No script."""
    if is_safe(request):
        return '<meta name="ody-mod-styles" content="off">\n'
    out = ['<meta name="ody-mod-styles" content="on">']
    for s in listing():
        out.append(f'<link rel="stylesheet" data-mod="{s["id"]}" href="{s["url"]}">')
    return "\n".join(out) + "\n"


def with_links(html: str, request) -> str:
    """`html` with the mod stylesheet links just before </head>, so they come last and win the cascade."""
    links = head_links(request)
    i = html.find("</head>")
    return html if i < 0 else html[:i] + links + html[i:]


# ── Fetching from the mod ─────────────────────────────────────────────────

async def refresh(mod_id: str) -> Dict[str, Any]:
    """Fetch the mod's stylesheet, check it and keep it. Returns status(). A refused stylesheet
    is removed (the page goes back to normal) and the reason kept."""
    from src.mods import runtime
    entry = registry.get(mod_id)
    if not eligible(entry):
        clear(mod_id)
        return {"active": False, "bytes": 0, "error": "", "version": ""}
    path = entry["manifest"]["ui"]["style"]["path"]
    port = await runtime.ensure_running(mod_id)
    if not port:
        err = f"the mod is not running ({runtime.state(mod_id).last_error or runtime.state(mod_id).status})"
        if not read(mod_id):
            _store(mod_id, None, entry, err)
        return dict(status(mod_id), error=err)
    headers = signing.sign_identity(registry.token_of(mod_id), mod_id, signing.CORE_USER)
    try:
        async with runtime._client().stream("GET", f"http://127.0.0.1:{port}{path}", headers=headers,
                                            timeout=FETCH_TIMEOUT_S) as r:
            r.raise_for_status()
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES + 1024:
                    raise StyleError(f"the stylesheet is larger than {MAX_BYTES // 1024} KB")
                chunks.append(chunk)
        css = sanitize(b"".join(chunks).decode("utf-8", "replace"), mod_id)
        err = ""
    except StyleError as e:
        css, err = None, str(e)
    except Exception as e:
        css, err = None, f"could not read {path}: {type(e).__name__}"
    meta = _store(mod_id, css, entry, err)
    if err:
        runtime.log_line(mod_id, f"[odysseus] stylesheet refused: {err}")
    else:
        runtime.log_line(mod_id, f"[odysseus] stylesheet in use ({meta['bytes']} bytes, v {meta['hash']})")
    return status(mod_id)


async def ensure_fresh() -> None:
    """Fetch the stylesheet of every eligible mod that has no current copy (after an install,
    an update or a restart of the core)."""
    for entry in registry.list_mods():
        m = _meta(entry["id"])
        if eligible(entry) and not active(entry) and not (m.get("error") and time.time() - m.get("at", 0) < 60):
            await refresh(entry["id"])
