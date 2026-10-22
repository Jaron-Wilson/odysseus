"""Mod tutorials: the `tutorial` field of odysseus-mod.json, its short
Markdown, and who has seen it.

Every mod should have a tutorial. It is shown the first time a user opens
one of the mod's tabs, can be replayed from the tab's header and from
Settings > Mods, and comes back when the mod raises `tutorial.version`. A
mod without one installs with a warning and can never be listed as Verified
(src/mods/catalog.py).

Two forms, one primary:

    "tutorial": {"version": 1, "steps": [
        {"title": "Wave", "body": "Press **Wave** to send an event.",
         "image": "docs/tutorial-wave.png",
         "action": {"label": "Open settings", "settings": true}}]}

    "tutorial": {"version": 1, "path": "/tutorial"}

Inline `steps` are the recommended form, so tutorials look the same in every
mod. `path` is a page of the mod for a richer tutorial; with `steps` as well
it is offered as "Full tutorial" after the steps. `image` is a file in the
mod's folder, served by the core (the mod need not be running). Bodies are a
small Markdown subset, rendered here with everything escaped first.

Who has seen which version is kept per user in the registry entry, as
`tutorial_seen: {"<user>": <version>}`, so it survives updates.
"""
from __future__ import annotations

import html
import os
import re
from typing import Any, Dict, List, Optional

MAX_STEPS = 8
MAX_TITLE = 80
MAX_BODY = 1200
MAX_LABEL = 40
MAX_VERSION = 100000
MAX_IMAGE_BYTES = 2 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
_REL_FILE_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$")

MISSING_WARNING = ("no `tutorial`: every mod should have one (it is shown the first time someone opens the mod); "
                   "a mod without one cannot be Verified")


# ── Checking the manifest field ─────────────────────────────────────────

def check(raw: Any, has_settings: bool, path_check, errors: List[str], warnings: List[str]) -> Optional[Dict[str, Any]]:
    """Validate `tutorial`. `path_check(value, where, errors)` is the
    manifest's mod-path rule. Returns the normalized tutorial or None."""
    if raw is None:
        warnings.append(MISSING_WARNING)
        return None
    if not isinstance(raw, dict):
        errors.append("`tutorial` must be an object with `steps` (or a `path`)")
        return None
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= MAX_VERSION:
        errors.append(f"`tutorial.version` must be a whole number from 1 to {MAX_VERSION}")
        version = 1
    path = path_check(raw.get("path"), "`tutorial.path`", errors) if raw.get("path") is not None else None
    steps_raw = raw.get("steps")
    steps: List[Dict[str, Any]] = []
    if steps_raw is not None:
        if not isinstance(steps_raw, list) or not steps_raw:
            errors.append("`tutorial.steps` must be a non-empty list")
            steps_raw = []
        elif len(steps_raw) > MAX_STEPS:
            errors.append(f"`tutorial.steps` can have at most {MAX_STEPS} steps")
            steps_raw = steps_raw[:MAX_STEPS]
        for i, s in enumerate(steps_raw):
            step = _check_step(s, f"tutorial.steps[{i}]", has_settings, path_check, errors)
            if step:
                steps.append(step)
    if not steps and not path and steps_raw is None:
        errors.append("`tutorial` needs `steps` (recommended) or a `path` to a tutorial page")
    return {"version": version, "steps": steps, "path": path}


def _check_step(s: Any, where: str, has_settings: bool, path_check, errors: List[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(s, dict):
        errors.append(f"`{where}` must be an object with a `title` and a `body`")
        return None
    title = s.get("title")
    body = s.get("body")
    ok = True
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
        errors.append(f"`{where}.title` is required, up to {MAX_TITLE} characters")
        ok = False
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
        errors.append(f"`{where}.body` is required: short Markdown, up to {MAX_BODY} characters")
        ok = False
    out: Dict[str, Any] = {"title": title.strip() if isinstance(title, str) else "",
                           "body": body.strip() if isinstance(body, str) else "", "image": None, "action": None}
    img = s.get("image")
    if img is not None:
        if (not isinstance(img, str) or not _REL_FILE_RE.match(img) or ".." in img.split("/")
                or os.path.splitext(img)[1].lower() not in IMAGE_TYPES):
            errors.append(f"`{where}.image` must be a relative path to a PNG, JPEG, GIF or WebP file in the mod")
            ok = False
        else:
            out["image"] = img
    act = s.get("action")
    if act is not None:
        if not isinstance(act, dict):
            errors.append(f"`{where}.action` must be an object with a `label` and a `path` or `settings`")
            ok = False
        else:
            label = act.get("label")
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL:
                errors.append(f"`{where}.action.label` is required, up to {MAX_LABEL} characters")
                ok = False
            has_path, wants_settings = act.get("path") is not None, act.get("settings") is True
            if has_path == wants_settings:
                errors.append(f"`{where}.action` needs exactly one of `path` (a page of the mod) or `settings: true`")
                ok = False
            elif wants_settings and not has_settings:
                errors.append(f"`{where}.action.settings` needs `ui.settings_path`")
                ok = False
            n = len(errors)
            p = path_check(act.get("path"), f"`{where}.action.path`", errors) if has_path else None
            if len(errors) != n:
                ok = False
            out["action"] = {"label": (label or "").strip(), "path": p, "settings": wants_settings}
    return out if ok else None


def missing_images(tut: Optional[Dict[str, Any]], mod_dir: str) -> List[str]:
    """Warnings for step images that are not in the mod's folder."""
    out = []
    for i, s in enumerate((tut or {}).get("steps") or []):
        if s.get("image") and _image_file(mod_dir, s["image"]) is None:
            out.append(f"`tutorial.steps[{i}].image` {s['image']!r} is not a file in the mod (or is over 2 MB)")
    return out


def _image_file(mod_dir: str, rel: str) -> Optional[str]:
    root = os.path.realpath(mod_dir)
    p = os.path.realpath(os.path.join(root, rel))
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        return None
    if os.path.splitext(p)[1].lower() not in IMAGE_TYPES or os.path.getsize(p) > MAX_IMAGE_BYTES:
        return None
    return p


def image_file(entry: Dict[str, Any], step: int) -> Optional[tuple]:
    """(file path, content type) of an installed mod's step image, or None."""
    from src.mods import paths
    steps = (((entry.get("manifest") or {}).get("tutorial")) or {}).get("steps") or []
    if not 0 <= step < len(steps) or not steps[step].get("image"):
        return None
    p = _image_file(paths.src_dir(entry["id"]), steps[step]["image"])
    return (p, IMAGE_TYPES[os.path.splitext(p)[1].lower()]) if p else None


# ── Markdown ─────────────────────────────────────────────────────────────

_CODE_RE = re.compile(r"`([^`\n]{1,200})`")
_LINK_RE = re.compile(r"\[([^\]\n]{1,120})\]\(([^)\s]{1,400})\)")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_DEEP_LINK_RE = re.compile(r"^#mod/[a-z][a-z0-9-]{1,31}/[a-z0-9][a-z0-9_-]{0,31}(?:\?path=[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?$")


def _link(label: str, url: str) -> str:
    raw = html.unescape(url)
    if re.match(r"^https://[^\s\"'<>]+$", raw):
        return (f'<a href="{html.escape(raw, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>')
    if _DEEP_LINK_RE.match(raw):
        return f'<a href="/{html.escape(raw, quote=True)}" data-mod-link="{html.escape(raw, quote=True)}">{label}</a>'
    return label


def _inline(text: str) -> str:
    """One line: escaped, then `code`, [links](https://...), **bold**, *italic*."""
    parts = _CODE_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{html.escape(part)}</code>")
            continue
        s = html.escape(part, quote=True)
        s = _LINK_RE.sub(lambda m: _link(m.group(1), m.group(2)), s)
        s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
        s = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", s)
        out.append(s)
    return "".join(out)


def render_markdown(text: str) -> str:
    """A small, safe Markdown: paragraphs, line breaks, `-`/`*`/`1.` lists,
    `code`, **bold**, *italic* and links (https, or a #mod/ deep link).
    Everything else is shown as text: no HTML, images or scripts get through."""
    blocks: List[str] = []
    para: List[str] = []
    items: List[str] = []
    ordered = False

    def flush():
        nonlocal para, items
        if para:
            blocks.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para = []
        if items:
            tag = "ol" if ordered else "ul"
            blocks.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
            items = []

    for line in str(text or "")[:MAX_BODY].splitlines():
        s = line.strip()
        m = re.match(r"^(?:([-*])|(\d{1,2})[.)])\s+(.*)$", s)
        if not s:
            flush()
        elif m:
            if para:
                flush()
            is_ol = m.group(2) is not None
            if items and is_ol != ordered:
                flush()
            ordered = is_ol
            items.append(m.group(3))
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "".join(blocks)


# ── Per-user state ───────────────────────────────────────────────────────

def of(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return ((entry.get("manifest") or {}).get("tutorial")) or None


def seen(entry: Dict[str, Any], user: str) -> bool:
    """Has this user seen the current version (skipping counts)?"""
    tut = of(entry)
    if not tut:
        return True
    try:
        return int((entry.get("tutorial_seen") or {}).get(user or "", 0)) >= int(tut.get("version") or 1)
    except (TypeError, ValueError):
        return False


def summary(entry: Dict[str, Any], user: str) -> Optional[Dict[str, Any]]:
    """What /api/mods/ui says about a mod's tutorial (the steps load on demand)."""
    tut = of(entry)
    if not tut:
        return None
    return {"version": tut.get("version") or 1, "steps": len(tut.get("steps") or []),
            "has_page": bool(tut.get("path")), "seen": seen(entry, user)}


def public(entry: Dict[str, Any], user: str, base: str) -> Optional[Dict[str, Any]]:
    """The tutorial to show: steps with their Markdown rendered, image URLs
    served by the core, and the tutorial page's frame URL."""
    tut = of(entry)
    if not tut:
        return None
    mid = entry["id"]
    steps = []
    for i, s in enumerate(tut.get("steps") or []):
        act = s.get("action")
        steps.append({
            "title": s.get("title") or "",
            "body_html": render_markdown(s.get("body") or ""),
            "image_url": f"/api/mods/{mid}/tutorial/image/{i}" if s.get("image") else None,
            "action": ({"label": act.get("label"), "path": act.get("path"), "settings": bool(act.get("settings"))}
                       if act else None),
        })
    return {"mod": mid, "name": entry.get("name") or mid, "version": tut.get("version") or 1,
            "seen": seen(entry, user), "steps": steps, "url": base + tut["path"] if tut.get("path") else None}
