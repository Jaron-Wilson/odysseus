"""Plans: the user's own Markdown plans, as plain .md files (routes/plans_routes.py).

Asked for: "I want to be able to have my own planning method instead of using
opencode, claude or anthropic, a natural .md editor so that I don't need to
use up any model for anything." Nothing here calls a model.

Each plan is one file, DATA_DIR/plans/<owner>/<name>.md, so the folder can be
opened, backed up or edited with any other tool. The file name is the plan's
title (letters, digits, spaces and a little punctuation), checked against a
strict pattern on every request, and every path is confirmed to resolve inside
the owner's folder, so a name can't reach another file. Writes go to a temp
file and are renamed into place. A deleted plan moves to the owner's .trash
folder instead of disappearing.
"""
from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from core.atomic_io import atomic_write_text
from src.upload_handler import secure_filename

EXT = ".md"
MAX_NAME = 120
MAX_BYTES = 1_000_000          # per plan; a plan is text, a megabyte is a lot
MAX_PLANS = 2000               # listing reads at most this many files
TRASH = ".trash"
DEFAULT_TITLE = "Untitled plan"

# A name: starts with a letter or digit, then letters, digits, spaces and
# - _ . , ( ) & ' + !  No slashes, no "..", no trailing dot or space.
_NAME_RE = re.compile(r"^[^\W_][\w \-.,()&'+!]*$", re.UNICODE)
# Names Windows won't create a file under (the app also runs there).
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

# The same task-line shape static/js/markdown.js renders as a checkbox.
_TASK_RE = re.compile(r"^ {0,12}[-*] \[([ xX])\] ")
_HEADING_RE = re.compile(r"^(#{1,6}) (.+)$")
_FENCE_RE = re.compile(r"^\s*```")

_lock = threading.Lock()


class PlanError(Exception):
    """A request the store refuses. `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ── Names and paths ──────────────────────────────────────────────────────

def valid_name(name: str) -> bool:
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        return False
    if name != unicodedata.normalize("NFC", name):
        return False
    if ".." in name or name.endswith((".", " ")) or "  " in name:
        return False
    if name.split(".")[0].strip().lower() in _RESERVED:
        return False
    return bool(_NAME_RE.match(name))


def name_from_title(title: str) -> str:
    """Turn whatever was typed as a title into a valid file name."""
    s = unicodedata.normalize("NFC", str(title or ""))
    s = re.sub(r"[/\\:|]+", " - ", s)                 # separators read as a dash
    s = re.sub(r"[^\w \-.,()&'+!]", "", s, flags=re.UNICODE)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[\W_]+", "", s, flags=re.UNICODE)    # must start with a letter or digit
    s = s[:MAX_NAME].rstrip(" .")
    if s.lower().endswith(EXT):
        s = s[: -len(EXT)].rstrip(" .")
    return s if valid_name(s) else DEFAULT_TITLE


def owner_dir(owner: Optional[str]) -> str:
    from src.constants import DATA_DIR
    root = os.path.realpath(os.path.join(DATA_DIR, "plans"))
    segment = secure_filename((owner or "").strip())[:80] if owner else "local"
    path = os.path.realpath(os.path.join(root, segment))
    if os.path.dirname(path) != root:
        raise PlanError(400, "Unusable owner")
    os.makedirs(path, exist_ok=True)
    return path


def _path(owner: Optional[str], name: str) -> str:
    if not valid_name(name):
        raise PlanError(400, "That is not a plan name")
    base = owner_dir(owner)
    path = os.path.join(base, name + EXT)
    # Belt and braces: the pattern already rules out separators and "..".
    if os.path.dirname(os.path.abspath(path)) != base:
        raise PlanError(400, "That is not a plan name")
    if os.path.islink(path):
        raise PlanError(404, "Plan not found")
    return path


# ── Reading ──────────────────────────────────────────────────────────────

def outline(text: str) -> dict:
    """Task counts and headings, skipping fenced code like the renderer does."""
    done = total = 0
    headings = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _TASK_RE.match(line)
        if m:
            total += 1
            done += m.group(1) in "xX"
            continue
        h = _HEADING_RE.match(line)
        if h:
            headings.append(h.group(2).strip())
    return {"tasks_done": done, "tasks_total": total, "headings": headings}


def _plain(line: str) -> str:
    """A line of Markdown as plain words, for the list's one-line summaries."""
    s = line.strip()
    s = re.sub(r"^(#{1,6}\s+|>\s*)", "", s)
    s = re.sub(r"^([-*+]|\d+[.)])\s+(\[[ xX]\]\s*)?", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)          # [text](url) -> text
    s = re.sub(r"(\*\*|__|~~|`)", "", s)
    s = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", s)
    return s.strip()


def _excerpt(text: str) -> str:
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith("#"):
            continue
        s = _plain(line)
        if s:
            return s[:140]
    return ""


def _version(st: os.stat_result) -> str:
    return f"{st.st_mtime_ns}-{st.st_size}"


def _summary(name: str, text: str, st: os.stat_result) -> dict:
    o = outline(text)
    return {
        "name": name,
        "heading": o["headings"][0] if o["headings"] else "",
        "excerpt": _excerpt(text),
        "tasks_done": o["tasks_done"],
        "tasks_total": o["tasks_total"],
        "size": st.st_size,
        "updated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "version": _version(st),
    }


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read(MAX_BYTES + 1)


def _snippet(text: str, q: str) -> str:
    """The first line that mentions `q`, as plain words, trimmed around the hit."""
    for line in text.splitlines():
        if q not in line.lower():
            continue
        s = _plain(line) or line.strip()
        i = s.lower().find(q)
        if i < 0:
            return s[:140]
        start = max(0, i - 50)
        return ("..." if start else "") + s[start:start + 140]
    return ""


def list_plans(owner: Optional[str], q: str = "") -> list[dict]:
    base = owner_dir(owner)
    q = (q or "").strip().lower()
    out = []
    with os.scandir(base) as it:
        entries = [e for e in it if e.name.endswith(EXT) and e.is_file(follow_symlinks=False)]
    for e in entries[:MAX_PLANS]:
        name = e.name[: -len(EXT)]
        if not valid_name(name):
            continue
        try:
            text = _read(e.path)
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        item = _summary(name, text, st)
        if q:
            in_name = q in name.lower()
            snippet = _snippet(text, q)
            if not in_name and not snippet:
                continue
            item["match"] = snippet
        out.append(item)
    out.sort(key=lambda p: p["updated_at"], reverse=True)
    return out


def get_plan(owner: Optional[str], name: str) -> dict:
    path = _path(owner, name)
    try:
        text = _read(path)
        st = os.stat(path)
    except FileNotFoundError:
        raise PlanError(404, "Plan not found")
    return {**_summary(name, text, st), "content": text}


# ── Writing ──────────────────────────────────────────────────────────────

def _check_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise PlanError(413, "That plan is over 1 MB")


def template(title: str) -> str:
    return f"# {title}\n\n## Goal\n\n\n## Steps\n\n- [ ] "


def create_plan(owner: Optional[str], title: str, content: Optional[str] = None) -> dict:
    name = name_from_title(title)
    text = template(name) if content is None else str(content)
    _check_size(text)
    with _lock:
        base = owner_dir(owner)
        candidate, n = name, 2
        while os.path.lexists(os.path.join(base, candidate + EXT)):
            suffix = f" {n}"
            candidate = name[: MAX_NAME - len(suffix)].rstrip(" .") + suffix
            n += 1
        atomic_write_text(_path(owner, candidate), text)
    return get_plan(owner, candidate)


def save_plan(owner: Optional[str], name: str, content: str, base_version: Optional[str] = None) -> dict:
    content = str(content)
    _check_size(content)
    with _lock:
        path = _path(owner, name)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            raise PlanError(404, "Plan not found")
        if base_version and base_version != _version(st):
            raise PlanError(409, "This plan changed somewhere else since you opened it")
        atomic_write_text(path, content)
        st = os.stat(path)
    return _summary(name, content, st)


def rename_plan(owner: Optional[str], name: str, title: str) -> dict:
    new = name_from_title(title)
    with _lock:
        src = _path(owner, name)
        if not os.path.exists(src):
            raise PlanError(404, "Plan not found")
        if new != name:
            dst = _path(owner, new)
            # A change of case only is the same file on a case-insensitive disk.
            if os.path.lexists(dst) and new.lower() != name.lower():
                raise PlanError(409, f'A plan called "{new}" already exists')
            os.rename(src, dst)
    return get_plan(owner, new)


def delete_plan(owner: Optional[str], name: str) -> dict:
    with _lock:
        path = _path(owner, name)
        if not os.path.exists(path):
            raise PlanError(404, "Plan not found")
        trash = os.path.join(owner_dir(owner), TRASH)
        os.makedirs(trash, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.replace(path, os.path.join(trash, f"{name} {stamp}{EXT}"))
    return {"ok": True, "name": name}
