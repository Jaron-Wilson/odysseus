"""The app's pages, for the agent: the same list the sidebar and "take me to
..." use.

static/js/toolPages.js holds the list (pages, their sidebar groups and the
words people use for them) as a strict JSON block between two markers; this
module reads that block, so `ui_control open_panel <page>` knows exactly the
pages the page can open, under the same names and aliases. Asked for
2026-09-30, after "take me to devices page please" got "I don't have an
app-navigation tool available here."
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "toolPages.js"
_BLOCK = re.compile(r"/\*\s*tool-pages:begin\s*\*/(.*?)/\*\s*tool-pages:end\s*\*/", re.S)

# Names ui_control used for panels before every page was reachable.
LEGACY = {"memories": "brain", "documents": "library", "sessions": "chats"}


def _load() -> Dict:
    try:
        m = _BLOCK.search(_JS.read_text(encoding="utf-8"))
        if m:
            return json.loads(m.group(1))
        logger.warning("tool pages: no tool-pages block in %s", _JS)
    except Exception as e:                         # a broken edit shouldn't take chat down
        logger.warning("tool pages: could not read %s: %s", _JS, e)
    return {"groups": [], "pages": []}


TAXONOMY = _load()
GROUPS: List[Dict] = TAXONOMY.get("groups", [])
PAGES: List[Dict] = TAXONOMY.get("pages", [])
_BY_KEY = {p["key"]: p for p in PAGES}
_GROUP_LABEL = {g["id"]: g["label"] for g in GROUPS}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _strip(n: str) -> str:
    """Drop the filler around a page name: "the devices page" -> "devices"."""
    n = re.sub(r"^(?:the|my)\s+", "", n)
    return re.sub(r"\s+(?:page|panel|tab|tool|app|window|screen|view)$", "", n).strip()


def page(name: str) -> Optional[Dict]:
    """The page a name means (key, label or alias, any case), or None."""
    n = _strip(_norm(name))
    if not n:
        return None
    if n in LEGACY:
        return _BY_KEY.get(LEGACY[n])
    for p in PAGES:
        if n in (_norm(p["key"]), _norm(p["label"])):
            return p
    for p in PAGES:
        if any(n == _norm(a) for a in p.get("aliases", [])):
            return p
    # "devices" for "device", "calendars" for "calendar"
    if n.endswith("s") and len(n) > 3:
        return page(n[:-1])
    return None


def split(rest: str):
    """Find the page at the start of `rest`, longest name first.
    Returns (page, what's left) or (None, rest)."""
    words = (rest or "").split()
    for k in range(min(len(words), 5), 0, -1):
        p = page(" ".join(words[:k]))
        if p:
            return p, " ".join(words[k:])
    return None, rest


def keys() -> List[str]:
    return [p["key"] for p in PAGES]


def group_label(p: Dict) -> str:
    return _GROUP_LABEL.get(p.get("group") or "", "")


def names_for_prompt() -> str:
    """Every page key, grouped: "Organize: calendar, tasks, ...; ...; also email, ..."."""
    parts = []
    for g in GROUPS:
        ks = [p["key"] for p in PAGES if p.get("group") == g["id"]]
        if ks:
            parts.append(f"{g['label']}: {', '.join(ks)}")
    other = [p["key"] for p in PAGES if not p.get("group")]
    if other:
        parts.append(f"also {', '.join(other)}")
    return "; ".join(parts)


def aliases_for_prompt(limit: int = 3) -> str:
    """A few words people use for each page: 'command line/shell/console->terminal, ...'."""
    out = []
    for p in PAGES:
        al, seen = [], {_norm(p["key"]).rstrip("s")}
        for a in p.get("aliases", []):
            base = _norm(a).rstrip("s")            # "computer" says nothing "computers" didn't
            if base in seen:
                continue
            seen.add(base)
            al.append(a)
        al = al[:limit]
        if al:
            out.append(f"{'/'.join(al)}->{p['key']}")
    return ", ".join(out)
