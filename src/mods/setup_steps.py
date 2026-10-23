"""Setup steps a mod adds to Settings > Setup (the guided setup checklist).

A manifest may say

    "setup": {"steps": [
        {"id": "connect-printer", "title": "Connect your printer",
         "description": "Lets the agent watch your prints and tell you when one finishes.",
         "status_path": "/setup/printer",
         "optional": true,
         "action": {"label": "Open the printer page", "path": "/printers"}}]}

* `id`          lowercase letters, digits, - and _ (unique inside the mod)
* `title`       up to 80 characters, what to do, in plain words
* `description` up to 300 characters, what it gives the person
* `status_path` optional: a page of the mod answering GET with
                {"done": true|false, "detail": "short note"}. The core asks it
                (with the usual signed identity headers, so the mod knows which
                user is looking) only while the mod is running; it never starts
                a mod just to draw the checklist. Without it, the step stays
                open until the person dismisses it ("I did this").
* `optional`    default true. A step that is not optional shows as "needs you".
* `action`      where the button goes: `path` (a page of the mod, opened in its
                first tab) or `settings: true` (the mod's settings page, which
                needs `ui.settings_path`), like a tutorial step's action.

No permission is needed: the checklist only reads what the mod says about
itself, and a step only links into the mod's own pages.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_TITLE = 80
MAX_DESC = 300
MAX_LABEL = 40
STATUS_TIMEOUT_S = 3.0
_SUB_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def check(raw: Any, has_settings: bool, path_check, errors: List[str]) -> Optional[Dict[str, Any]]:
    """Validate `setup`. `path_check(value, where, errors)` is the manifest's
    mod-path rule. Returns the normalized {"steps": [...]} or None."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        errors.append("`setup` must be an object with a `steps` list")
        return None
    out: List[Dict[str, Any]] = []
    steps = raw["steps"]
    if len(steps) > MAX_STEPS:
        errors.append(f"`setup.steps` can have at most {MAX_STEPS} steps")
        steps = steps[:MAX_STEPS]
    for i, s in enumerate(steps):
        where = f"setup.steps[{i}]"
        if not isinstance(s, dict):
            errors.append(f"`{where}` must be an object")
            continue
        sid, title, desc = s.get("id"), s.get("title"), s.get("description", "")
        ok = True
        if not isinstance(sid, str) or not _SUB_ID.match(sid):
            errors.append(f"`{where}.id` needs lowercase letters, digits, - and _")
            ok = False
        elif any(x["id"] == sid for x in out):
            errors.append(f"`{where}.id` is used twice")
            ok = False
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            errors.append(f"`{where}.title` is required, up to {MAX_TITLE} characters")
            ok = False
        if not isinstance(desc, str) or len(desc.strip()) > MAX_DESC:
            errors.append(f"`{where}.description` must be text up to {MAX_DESC} characters")
            ok = False
        optional = s.get("optional", True)
        if not isinstance(optional, bool):
            errors.append(f"`{where}.optional` must be true or false")
            ok = False
        status_path = None
        if s.get("status_path") is not None:
            status_path = path_check(s.get("status_path"), f"`{where}.status_path`", errors)
        action = None
        act = s.get("action")
        if act is not None:
            label = act.get("label") if isinstance(act, dict) else None
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL:
                errors.append(f"`{where}.action.label` is required, up to {MAX_LABEL} characters")
                ok = False
            else:
                has_path, wants_settings = act.get("path") is not None, act.get("settings") is True
                if has_path == wants_settings:
                    errors.append(f"`{where}.action` needs exactly one of `path` or `settings: true`")
                    ok = False
                elif wants_settings and not has_settings:
                    errors.append(f"`{where}.action.settings` needs `ui.settings_path`")
                    ok = False
                else:
                    action = {"label": label.strip(), "path": None, "settings": False}
                    if has_path:
                        action["path"] = path_check(act.get("path"), f"`{where}.action.path`", errors)
                    else:
                        action["settings"] = True
        if ok:
            out.append({"id": sid, "title": title.strip(), "description": desc.strip(), "optional": optional,
                        "status_path": status_path, "action": action})
    return {"steps": out}


async def _ask(entry: Dict[str, Any], step: Dict[str, Any], user: Optional[str]) -> Optional[Dict[str, Any]]:
    """The mod's own answer for one step, or None when it is not running,
    does not answer, or answers with something that is not the format."""
    from src.mods import access, registry, runtime, signing
    st = runtime.state(entry["id"])
    if st.status != "running" or not st.port:
        return None
    headers = signing.sign_identity(registry.token_of(entry["id"]), entry["id"],
                                    signing.caller(user, access.auth_disabled()))
    try:
        r = await runtime._client().get(f"http://127.0.0.1:{st.port}{step['status_path']}",
                                        headers=headers, timeout=STATUS_TIMEOUT_S)
        data = r.json()
    except Exception as e:
        logger.debug("setup step %s/%s: no answer (%s)", entry["id"], step["id"], type(e).__name__)
        return None
    if r.status_code >= 400 or not isinstance(data, dict) or not isinstance(data.get("done"), bool):
        return None
    return {"done": data["done"], "detail": str(data.get("detail") or "")[:200]}


def _link(entry: Dict[str, Any], step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The button's target as a link the app routes (#mod/<id>/<tab>?path=...)."""
    act = step.get("action")
    if not act:
        return None
    ui = (entry.get("manifest") or {}).get("ui") or {}
    tabs = ui.get("tabs") or []
    if not tabs:
        return None
    path = ui.get("settings_path") if act.get("settings") else act.get("path")
    link = f"#mod/{entry['id']}/{tabs[0]['id']}"
    if path and path != "/":
        link += f"?path={quote(path, safe='')}"
    return {"kind": "mod", "label": act["label"], "link": link}


async def collect(user: Optional[str]) -> List[Dict[str, Any]]:
    """Every enabled mod's setup steps this user may see, with the mod's own
    answer: [{mod_id, mod_name, step, answer|None, action|None}]."""
    from src.mods import access, registry
    out: List[Dict[str, Any]] = []
    for entry in sorted(registry.list_mods(), key=lambda m: m["id"]):
        if not entry.get("enabled") or not access.may_use(entry, user):
            continue
        steps = ((entry.get("manifest") or {}).get("setup") or {}).get("steps") or []
        for s in steps:
            answer = await _ask(entry, s, user) if s.get("status_path") else None
            out.append({"mod_id": entry["id"], "mod_name": entry.get("name") or entry["id"], "step": s,
                        "answer": answer, "action": _link(entry, s)})
    return out
