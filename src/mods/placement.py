"""Where a mod's tabs go in the navigation (the sidebar groups and the
Workspace "+" menu).

A tab in the manifest may say `section` (a nav group id from
static/js/toolPages.js, read through src/tool_pages.py, or `mods`), `after`
(the key of a built-in page in that group, like `print`) and `order` (a whole
number). The admin can override a mod's group, or hide it from the sidebar,
in Settings > Mods; that choice is kept in the registry entry as
`placement: {"section": <id or null>, "hidden": <bool>}` and wins over the
manifest.

Within a group, mod tabs anchored after the same built-in page (or at the end
of the group, when `after` is not given) are sorted by `order` (default 0),
then by title.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

MODS_GROUP = "mods"
MODS_LABEL = "Mods"
ORDER_MIN, ORDER_MAX = -1000, 1000

# Built-in pages that are moving (or may move) out of the core into mods. A
# mod tab with `replaces: "<key>"` takes over the page's old links (`#adsb`,
# a saved Workspace tab with that key); with no such mod installed, and the
# built-in page gone, those links show a "This feature is now a mod" card.
# static/js/modLinksCore.js has the same list (tests/test_mods_links.py).
LEGACY_PAGES: Dict[str, str] = {
    "adsb": "ADS-B receiver",
    "games": "Games",
    "print": "3D print helper",
    "devops": "DevOps",
    "company": "Company",
    "code": "Code",
    "terminal": "Terminal",
    "browser": "Browser",
    "claude-sessions": "Claude sessions",
    "devices": "Devices",
    "usage": "Usage",
    "plans": "Plans",
    "whats-new": "What's new",
}


def _taxonomy():
    from src import tool_pages
    return tool_pages.GROUPS, tool_pages.PAGES


def groups() -> List[Dict[str, str]]:
    """Every group a mod tab can go in, in sidebar order: [{id, label}]."""
    gs, _ = _taxonomy()
    return [{"id": g["id"], "label": g["label"]} for g in gs] + [{"id": MODS_GROUP, "label": MODS_LABEL}]


def group_ids() -> List[str]:
    return [g["id"] for g in groups()]


def page_keys(section: str) -> List[str]:
    """The built-in pages in a group, in their order (what `after` may name)."""
    _, pages = _taxonomy()
    return [p["key"] for p in pages if p.get("group") == section]


def check_tab(t: Dict[str, Any], where: str, errors: List[str], warnings: List[str]) -> Dict[str, Any]:
    """Validate a manifest tab's `section`, `after` and `order`. A wrong type
    is an error; a section or page this core does not have is a warning, and
    the tab goes in the Mods group (or at the end of its group)."""
    out: Dict[str, Any] = {"section": None, "after": None, "order": 0, "replaces": None}
    sec = t.get("section")
    if sec is not None:
        if not isinstance(sec, str) or not sec.strip():
            errors.append(f"`{where}.section` must be a group id like create")
        elif sec.strip() not in group_ids():
            warnings.append(f"`{where}.section` {sec!r} is not a nav group here ({', '.join(group_ids())}); "
                            f"it goes in {MODS_LABEL}")
        else:
            out["section"] = sec.strip()
    after = t.get("after")
    if after is not None:
        if not isinstance(after, str) or not after.strip():
            errors.append(f"`{where}.after` must be the key of a built-in page, like print")
        else:
            after = after.strip()
            keys = page_keys(out["section"]) if out["section"] else []
            if after in keys:
                out["after"] = after
            elif sec is not None and out["section"] is None:
                pass   # already warned about the section
            else:
                in_group = out["section"] or MODS_LABEL
                warnings.append(f"`{where}.after` {after!r} is not a page in {in_group}; "
                                "it goes at the end of the group")
    rep = t.get("replaces")
    if rep is not None:
        if not isinstance(rep, str) or not rep.strip():
            errors.append(f"`{where}.replaces` must be the key of a built-in page, like adsb")
        elif rep.strip() not in LEGACY_PAGES:
            warnings.append(f"`{where}.replaces` {rep!r} is not a page that can move to a mod "
                            f"({', '.join(LEGACY_PAGES)}); it is ignored")
        else:
            out["replaces"] = rep.strip()
    order = t.get("order")
    if order is not None:
        if isinstance(order, bool) or not isinstance(order, int) or not ORDER_MIN <= order <= ORDER_MAX:
            errors.append(f"`{where}.order` must be a whole number from {ORDER_MIN} to {ORDER_MAX}")
        else:
            out["order"] = order
    return out


def replacements(entries: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Which mod tab takes over each legacy page key: {key: {mod, tab, title}}.
    The caller passes the mods this user may use, enabled ones only; when
    two claim the same key, the first by mod id wins."""
    out: Dict[str, Dict[str, str]] = {}
    for e in sorted(entries, key=lambda x: x["id"]):
        for t in ((e.get("manifest") or {}).get("ui") or {}).get("tabs") or []:
            key = t.get("replaces")
            if key in LEGACY_PAGES and key not in out:
                out[key] = {"mod": e["id"], "tab": t["id"], "title": t.get("title") or e.get("name") or e["id"]}
    return out


def clean_override(section: Any, hidden: Any) -> Dict[str, Any]:
    """The user's override, checked. An empty section means the manifest's."""
    if section in (None, ""):
        section = None
    elif not isinstance(section, str) or section not in group_ids():
        raise ValueError(f"section must be one of {', '.join(group_ids())}")
    return {"section": section, "hidden": bool(hidden)}


def resolve(tab: Dict[str, Any], override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Where one tab goes: {section, after, order, hidden}. The user's
    override wins over the manifest; a section that has gone away (or none)
    means the Mods group; `after` only holds in the manifest's own group."""
    override = override or {}
    ids = group_ids()
    declared = tab.get("section") if tab.get("section") in ids else None
    chosen = override.get("section") if override.get("section") in ids else None
    section = chosen or declared or MODS_GROUP
    after = tab.get("after") if section == declared else None
    if after not in page_keys(section):
        after = None
    order = tab.get("order")
    if isinstance(order, bool) or not isinstance(order, int):
        order = 0
    return {"section": section, "after": after, "order": order, "hidden": bool(override.get("hidden"))}


def sort_key(item: Dict[str, Any]):
    """Sidebar order for placed tabs: by group, by anchor (the built-in page
    it follows, in page order; the end of the group last), then order, title."""
    ids = group_ids()
    sec = item.get("section")
    keys = page_keys(sec) if sec else []
    after = item.get("after")
    anchor = keys.index(after) if after in keys else len(keys)
    return (ids.index(sec) if sec in ids else len(ids), anchor, item.get("order") or 0,
            str(item.get("title") or "").casefold(), str(item.get("mod") or ""), str(item.get("tab") or ""))


def nav(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The placed tabs of these registry entries, sorted, hidden ones left out:
    [{mod, tab, title, icon, section, after, order}]. The caller has already
    dropped disabled mods and mods the user may not use."""
    out = []
    for e in entries:
        ui = (e.get("manifest") or {}).get("ui") or {}
        override = e.get("placement") or {}
        for t in ui.get("tabs") or []:
            p = resolve(t, override)
            if p["hidden"]:
                continue
            out.append({"mod": e["id"], "tab": t["id"], "title": t.get("title") or e.get("name") or e["id"],
                        "icon": t.get("icon") or "", "section": p["section"], "after": p["after"],
                        "order": p["order"]})
    out.sort(key=sort_key)
    return out
