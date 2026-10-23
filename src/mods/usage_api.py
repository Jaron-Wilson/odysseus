"""Usage scopes and budgets for mods (permission `budgets`).

Every model call a mod makes (llm/chat, src/mods/llm.py) and every model
call of an agent run it starts (src/mods/runs.py) is recorded in Usage with
source "mod" and a source scope:

    mod:<mod id>                      a call that named no scope
    mod:<mod id>/<scope path>         e.g. mod:company/c/3f2a/a/91bc
    mod:<mod id>/<scope path>/run-<run id>   an agent run's calls

A scope path is up to 8 segments of letters, digits, ".", "_" and "-",
joined by "/". Scopes nest: the spend of "c/3f2a" includes "c/3f2a/a/91bc"
and every run under it, so a mod can keep per-company, per-agent and per-run
spend apart with one path.

    GET    /api/mods/v1/usage?scope=c/3f2a[&user=&month=YYYY-MM]
           what the scope (and everything under it) cost this month
    PUT    /api/mods/v1/usage/labels            {labels: {"c/3f2a": "Acme"}}: names for scope paths; the
                                                Usage page groups a mod's spend by the first two segments
                                                of the path (mod:company/c/3f2a) and shows these names
    GET    /api/mods/v1/budgets                 the mod's budgets, each checked
    PUT    /api/mods/v1/budgets                 {scope, monthly_limit_cents, caps?}
    DELETE /api/mods/v1/budgets?scope=c/3f2a
    GET    /api/mods/v1/budgets/check?scope=c/3f2a[&limit_cents=500]

A check is src/budgets.py's: `state` ok / warn / hard_stop, `spent_cents`,
`limit_cents`, the subscription allocation and caps. The core enforces the
stored budgets itself: an agent run stops between rounds, and llm/chat is
refused (HTTP 402), once any budget at or above its scope is at hard stop.
Budgets are kept by the core in mods/<id>/budgets.json, not by the mod.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src.mods import paths

SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_SEGMENTS = 8
MAX_BUDGETS = 512
_lock = threading.Lock()
MAX_LABELS = 1000


def clean_path(path: Any, *, allow_empty: bool = True) -> str:
    """A validated scope path ("" for none). Raises ValueError."""
    if path is None or path == "":
        if allow_empty:
            return ""
        raise ValueError("scope is required")
    if not isinstance(path, str):
        raise ValueError("scope must be a string like c/3f2a/a/91bc")
    parts = path.strip("/").split("/")
    if len(parts) > MAX_SEGMENTS or not all(SEGMENT_RE.match(p) for p in parts) or any(p in (".", "..") for p in parts):
        raise ValueError(f"scope must be up to {MAX_SEGMENTS} segments of letters, digits, . _ -, joined by /")
    return "/".join(parts)


def full_scope(mod_id: str, path: str = "") -> str:
    return f"mod:{mod_id}" + (f"/{path}" if path else "")


def _file(mod_id: str) -> str:
    return os.path.join(paths.mod_dir(mod_id), "budgets.json")


def load(mod_id: str) -> Dict[str, Dict[str, Any]]:
    try:
        with open(_file(mod_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(mod_id: str, data: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(paths.mod_dir(mod_id), exist_ok=True)
    p = _file(mod_id)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(p + ".tmp", p)


def _labels_file(mod_id: str) -> str:
    return os.path.join(paths.mod_dir(mod_id), "usage_labels.json")


def load_labels(mod_id: str) -> Dict[str, str]:
    """Names the mod gave its scope paths ("c/3f2a" -> "Acme"), for the Usage page."""
    try:
        with open(_labels_file(mod_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def set_labels(mod_id: str, labels: Any) -> Dict[str, str]:
    """Merge {path: label}; an empty label removes one. At most MAX_LABELS are kept. Raises ValueError."""
    if not isinstance(labels, dict) or len(labels) > 200:
        raise ValueError("labels must be an object of {scope path: name}, at most 200 at a time")
    with _lock:
        cur = load_labels(mod_id)
        for path, name in labels.items():
            p = clean_path(path, allow_empty=False)
            if not isinstance(name, str):
                raise ValueError("a label must be a string")
            name = " ".join(name.split())[:80]
            if name:
                cur[p] = name
            else:
                cur.pop(p, None)
        if len(cur) > MAX_LABELS:
            for k in list(cur)[:len(cur) - MAX_LABELS]:
                cur.pop(k, None)
        os.makedirs(paths.mod_dir(mod_id), exist_ok=True)
        p = _labels_file(mod_id)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=1)
        os.replace(p + ".tmp", p)
    return cur


def set_budget(mod_id: str, path: str, monthly_limit_cents: Any, caps: Any = None) -> Dict[str, Any]:
    path = clean_path(path)
    try:
        limit = float(monthly_limit_cents or 0)
    except (TypeError, ValueError):
        raise ValueError("monthly_limit_cents must be a number (0 for no money limit)")
    if limit < 0 or limit > 1e9:
        raise ValueError("monthly_limit_cents must be between 0 and 1e9")
    clean_caps: Dict[str, int] = {}
    if caps:
        from src import subscriptions
        clean_caps = subscriptions.clean_caps("caps", caps)
    with _lock:
        data = load(mod_id)
        if path not in data and len(data) >= MAX_BUDGETS:
            raise ValueError(f"at most {MAX_BUDGETS} budgets per mod")
        data[path] = {"monthly_limit_cents": limit, "caps": clean_caps}
        _save(mod_id, data)
    return {"scope": path, **data[path]}


def delete_budget(mod_id: str, path: str) -> bool:
    path = clean_path(path)
    with _lock:
        data = load(mod_id)
        existed = data.pop(path, None) is not None
        if existed:
            _save(mod_id, data)
    return existed


def check(mod_id: str, path: str, owner: Optional[str] = None,
          limit_cents: Optional[float] = None) -> Dict[str, Any]:
    """src/budgets.py's check for a scope; the stored limit unless one is given."""
    from src import budgets
    path = clean_path(path)
    stored = load(mod_id).get(path) or {}
    limit = limit_cents if limit_cents is not None else stored.get("monthly_limit_cents")
    out = budgets.check("mod_scope", full_scope(mod_id, path), owner or None, limit or None,
                        caps=stored.get("caps") or {})
    out["scope"] = path
    return out


def stop_reason(mod_id: str, path: str) -> Optional[str]:
    """Why a call or run under `path` must stop: a stored budget at or above
    it is at hard stop. None when it may go on."""
    path = clean_path(path)
    stored = load(mod_id)
    if not stored:
        return None
    parts = path.split("/") if path else []
    for i in range(len(parts), -1, -1):
        p = "/".join(parts[:i])
        if p not in stored:
            continue
        c = check(mod_id, p)
        if c["state"] == "hard_stop":
            what = "money" if "money" in c.get("stopped_by", []) else ", ".join(c.get("stopped_by") or []) or "budget"
            return (f"the monthly budget of scope '{p or '(whole mod)'}' was reached ({what}: "
                    f"{c['spent_cents'] / 100:.2f} of {(c['limit_cents'] or 0) / 100:.2f} USD)")
    return None


def spent(mod_id: str, path: str, owner: Optional[str] = None, month: Optional[str] = None) -> Dict[str, Any]:
    from src import budgets, usage
    path = clean_path(path)
    start, end = usage.month_window(month)
    s = budgets.spent("mod_scope", full_scope(mod_id, path), owner or None, start=start, end=end)
    return {"scope": path, "period_start": start.isoformat() + "Z", "period_end": end.isoformat() + "Z", **s}


# ── Routes ───────────────────────────────────────────────────────────────

def setup(router: APIRouter, mod_for) -> None:
    """`mod_for(request, permission)` is routes._mod (the mod's token check)."""

    def _bad(e: ValueError):
        return HTTPException(400, str(e))

    def _owner(e: Dict[str, Any], user: str) -> Optional[str]:
        from src.mods import access
        user = (user or "").strip()
        return user if user and access.known_user(user) and access.may_use(e, user) else None

    @router.get("/api/mods/v1/usage")
    async def v1_usage(request: Request, scope: str = "", user: str = "", month: str = ""):
        e = mod_for(request, "budgets")
        try:
            out = spent(e["id"], scope, _owner(e, user), month or None)
        except ValueError as ex:
            raise _bad(ex)
        return out

    @router.put("/api/mods/v1/usage/labels")
    async def v1_usage_labels(request: Request):
        e = mod_for(request, "budgets")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Expected a JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        try:
            return {"labels": set_labels(e["id"], body.get("labels"))}
        except ValueError as ex:
            raise _bad(ex)

    @router.get("/api/mods/v1/budgets")
    async def v1_budgets(request: Request):
        e = mod_for(request, "budgets")
        items: List[Dict[str, Any]] = []
        for path, b in sorted(load(e["id"]).items()):
            items.append({"scope": path, **b, "check": check(e["id"], path)})
        return {"budgets": items}

    @router.put("/api/mods/v1/budgets")
    async def v1_budget_put(request: Request):
        e = mod_for(request, "budgets")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Expected a JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        try:
            return set_budget(e["id"], body.get("scope") or "", body.get("monthly_limit_cents"), body.get("caps"))
        except ValueError as ex:
            raise _bad(ex)

    @router.delete("/api/mods/v1/budgets")
    async def v1_budget_del(request: Request, scope: str = ""):
        e = mod_for(request, "budgets")
        try:
            return {"ok": True, "existed": delete_budget(e["id"], scope)}
        except ValueError as ex:
            raise _bad(ex)

    @router.get("/api/mods/v1/budgets/check")
    async def v1_budget_check(request: Request, scope: str = "", limit_cents: Optional[float] = None,
                              user: str = ""):
        e = mod_for(request, "budgets")
        try:
            return check(e["id"], scope, _owner(e, user), limit_cents)
        except ValueError as ex:
            raise _bad(ex)
