"""Mod tools in the agent loop.

A mod with `tools` in its manifest serves GET <endpoint> (default /mod/tools)
returning OpenAI-style function schemas, as a list or {"tools": [...]}. Each
may carry an "x-odysseus" object next to "function":

    {"keywords": ["filament"],     extra words that select this tool
     "controls_devices": true,     always ask the user before running it
     "read_only": true}            never ask (only meaningful for mods with
                                   devices.control, whose tools ask by default)

The core names them mod__<mod id>__<tool name> and runs a call with
POST <endpoint>/<tool name>, the arguments object as the body, and signed
identity headers. The answer is JSON: {"output": "text for the model",
"data": {...}} or {"error": "..."}.

Schemas are fetched when a mod starts and cached in mods/<id>/tools.json, so
choosing tools never waits on (or starts) a mod. A turn gets a mod's tools
only when the message matches the mod's keywords or a tool's own keywords,
or the tool index (src/tool_index.py) retrieves it; never all mods' tools.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.mods import paths
from src.mods import registry
from src.mods import signing

logger = logging.getLogger(__name__)

PREFIX = "mod__"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_TOOLS_PER_MOD = 24
MAX_SELECTED_PER_MOD = 12
CALL_TIMEOUT_S = 120
MAX_OUTPUT = 12000


def qualified(mod_id: str, name: str) -> str:
    return f"{PREFIX}{mod_id}__{name}"


def split_name(q: str) -> Optional[Tuple[str, str]]:
    if not q.startswith(PREFIX):
        return None
    rest = q[len(PREFIX):]
    mod_id, sep, name = rest.partition("__")
    if not sep or not mod_id or not name:
        return None
    return mod_id, name


def _cache_file(mod_id: str) -> str:
    return os.path.join(paths.mod_dir(mod_id), "tools.json")


def _clean_schema(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    fn = raw.get("function") if raw.get("type") == "function" else raw
    if not isinstance(fn, dict):
        return None
    name = fn.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}}
    if len(json.dumps(params)) > 16000:
        return None
    x = raw.get("x-odysseus") if isinstance(raw.get("x-odysseus"), dict) else {}
    kws = [str(k).strip().lower() for k in (x.get("keywords") or []) if isinstance(k, str) and k.strip()][:16]
    return {"name": name, "description": str(fn.get("description") or "")[:1000], "parameters": params,
            "keywords": kws, "controls_devices": bool(x.get("controls_devices")),
            "read_only": bool(x.get("read_only"))}


def parse_tool_list(payload: Any) -> List[Dict[str, Any]]:
    items = payload.get("tools") if isinstance(payload, dict) else payload
    out = []
    seen = set()
    for raw in items if isinstance(items, list) else []:
        t = _clean_schema(raw)
        if t and t["name"] not in seen:
            seen.add(t["name"])
            out.append(t)
        if len(out) >= MAX_TOOLS_PER_MOD:
            break
    return out


def cached(mod_id: str) -> List[Dict[str, Any]]:
    try:
        with open(_cache_file(mod_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tools") or []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


async def refresh(mod_id: str) -> List[Dict[str, Any]]:
    """Fetch a running mod's tool list into the cache."""
    from src.mods import runtime
    entry = registry.get(mod_id) or {}
    tcfg = (entry.get("manifest") or {}).get("tools")
    if not tcfg:
        return []
    port = runtime.state(mod_id).port
    if not port:
        return cached(mod_id)
    headers = signing.sign_identity(registry.token_of(mod_id), mod_id, signing.CORE_USER)
    try:
        r = await runtime._client().get(f"http://127.0.0.1:{port}{tcfg['endpoint']}", headers=headers, timeout=10)
        r.raise_for_status()
        tools = parse_tool_list(r.json())
    except Exception as e:
        runtime.log_line(mod_id, f"[odysseus] could not read its tool list: {type(e).__name__}: {e}")
        return cached(mod_id)
    os.makedirs(paths.mod_dir(mod_id), exist_ok=True)
    with open(_cache_file(mod_id), "w", encoding="utf-8") as f:
        json.dump({"fetched": time.time(), "version": entry.get("version"), "tools": tools}, f)
    _bump()
    return tools


_generation = 0


def _bump() -> None:
    global _generation
    _generation += 1


def generation() -> int:
    """Changes whenever the set of enabled mod tools may have changed."""
    enabled = tuple(sorted((m["id"], m.get("version"), m.get("access")) for m in registry.list_mods()
                           if m.get("enabled")))
    return hash((_generation, enabled))


def _user_allowed(entry: Dict[str, Any], owner: Optional[str]) -> bool:
    from src.mods import access
    return access.may_use(entry, owner)


def enabled_tools(owner: Optional[str] = None) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    for entry in registry.list_mods():
        man = entry.get("manifest") or {}
        if not entry.get("enabled") or not man.get("tools") or not _user_allowed(entry, owner):
            continue
        for t in cached(entry["id"]):
            yield entry, t


def index_docs() -> Dict[str, str]:
    """Qualified name -> text, for the tool index."""
    docs = {}
    for entry, t in enabled_tools(owner=None):
        kws = ", ".join(((entry.get("manifest") or {}).get("tools") or {}).get("keywords", []) + t["keywords"])
        docs[qualified(entry["id"], t["name"])] = (
            f"Tool: {t['name']} (mod: {entry.get('name')})\n{t['description']}" + (f"\nKeywords: {kws}" if kws else ""))
    return docs


def _kw_hit(keywords: Iterable[str], text: str) -> bool:
    for kw in keywords:
        if kw and re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text):
            return True
    return False


def select(query: str, owner: Optional[str] = None, retrieved: Optional[Set[str]] = None) -> List[str]:
    """Qualified names of the mod tools relevant to this message."""
    text = (query or "").lower()
    retrieved = retrieved or set()
    picked: List[str] = []
    per_mod: Dict[str, int] = {}
    for entry, t in enabled_tools(owner):
        q = qualified(entry["id"], t["name"])
        mod_kw = ((entry.get("manifest") or {}).get("tools") or {}).get("keywords", [])
        if q in retrieved or _kw_hit(mod_kw, text) or _kw_hit(t["keywords"], text):
            if per_mod.get(entry["id"], 0) >= MAX_SELECTED_PER_MOD:
                continue
            per_mod[entry["id"]] = per_mod.get(entry["id"], 0) + 1
            picked.append(q)
    return picked


def schemas(names: Iterable[str], owner: Optional[str] = None) -> List[Dict[str, Any]]:
    want = set(names)
    out = []
    for entry, t in enabled_tools(owner):
        q = qualified(entry["id"], t["name"])
        if q in want:
            out.append({"type": "function", "function": {
                "name": q, "description": f"[Mod: {entry.get('name')}] {t['description']}",
                "parameters": t["parameters"]}})
    return out


def prompt_lines(names: Iterable[str], owner: Optional[str] = None) -> str:
    """Prompt text for the selected mod tools (models without native tools call
    them as a fenced block named after the tool, with JSON arguments)."""
    lines = []
    for s in schemas(names, owner):
        fn = s["function"]
        props = ", ".join(sorted((fn["parameters"].get("properties") or {}).keys()))
        lines.append(f"  - {fn['name']}: {fn['description'][:200]}" + (f" (args: {props})" if props else ""))
    if not lines:
        return ""
    return ("\n\nMod tools (installed add-ons; call them like any tool, arguments as a JSON object):\n"
            + "\n".join(lines))


def _needs_approval(entry: Dict[str, Any], t: Dict[str, Any]) -> bool:
    if t.get("controls_devices"):
        return True
    return "devices.control" in (entry.get("granted_permissions") or []) and not t.get("read_only")


async def execute(tool: str, content: str, owner: Optional[str], session_id: Optional[str]) -> Tuple[str, Dict]:
    """Run a mod tool call for the agent (src/tool_execution.py)."""
    from src.mods import runtime
    parts = split_name(tool)
    if not parts:
        return f"{tool}: unknown", {"error": f"Unknown tool {tool}", "exit_code": 1}
    mod_id, name = parts
    entry = registry.get(mod_id)
    if not entry or not entry.get("enabled"):
        return f"{tool}: unavailable", {"error": f"The {mod_id} mod is not installed or is turned off.", "exit_code": 1}
    if not _user_allowed(entry, owner):
        return f"{tool}: BLOCKED", {"error": "This mod is only available to admins.", "exit_code": 1}
    t = next((x for x in cached(mod_id) if x["name"] == name), None)
    if not t:
        return f"{tool}: unknown", {"error": f"The {mod_id} mod has no tool {name}.", "exit_code": 1}
    try:
        args = json.loads(content) if (content or "").strip() else {}
    except (json.JSONDecodeError, TypeError):
        return f"{tool}: bad arguments", {"error": "Arguments must be a JSON object.", "exit_code": 1}
    if not isinstance(args, dict):
        return f"{tool}: bad arguments", {"error": "Arguments must be a JSON object.", "exit_code": 1}
    args = {k: v for k, v in args.items() if not str(k).startswith("_")}

    if _needs_approval(entry, t):
        from src import action_approvals as AA
        if not AA.take_approved(tool, content):
            if t.get("controls_devices") and "devices.control" not in (entry.get("granted_permissions") or []):
                return f"{tool}: BLOCKED", {"error": (f"The {mod_id} mod was not granted devices.control, so "
                                                      f"its device tools cannot run."), "exit_code": 1}
            info = {"kind": "mod_action", "action": name,
                    "title": f"{entry.get('name')}: {name.replace('_', ' ')}",
                    "details": [("Mod", entry.get("name") or mod_id), ("Tool", name)] + [
                        (str(k), json.dumps(v) if not isinstance(v, str) else v) for k, v in list(args.items())[:10]],
                    "body": json.dumps(args, indent=1)[:4000], "body_label": "Arguments"}
            e = AA.create(owner, session_id or "", tool, content, info, source="chat")
            return f"{tool}: waiting for approval", {
                "action_approval": {"id": e["id"], "title": e["title"], "status": e["status"]},
                "output": (f"Not run yet: \"{e['title']}\" controls a device, so it needs the user's approval "
                           f"first (approval id {e['id']}). It is shown in the chat with Approve and Deny. Your "
                           "turn ends here: do not call it again or try another way, and do not say it is done. "
                           "If they approve, it runs exactly as you asked and you get its result."),
                "exit_code": 0}

    port = await runtime.ensure_running(mod_id)
    if not port:
        st = runtime.state(mod_id)
        return f"{tool}: not running", {"error": f"The {mod_id} mod is not running ({st.last_error or st.status}).",
                                         "exit_code": 1}
    tcfg = (entry.get("manifest") or {}).get("tools") or {}
    from src.mods import access
    headers = signing.sign_identity(registry.token_of(mod_id), mod_id, signing.caller(owner, access.auth_disabled()))
    headers["Content-Type"] = "application/json"
    if session_id:
        headers["X-Odysseus-Session"] = session_id
    url = f"http://127.0.0.1:{port}{tcfg.get('endpoint', '/mod/tools').rstrip('/')}/{name}"
    try:
        r = await runtime._client().post(url, content=json.dumps(args), headers=headers, timeout=CALL_TIMEOUT_S)
    except Exception as e:
        return f"mod: {tool}", {"error": f"The {mod_id} mod did not answer: {type(e).__name__}", "exit_code": 1}
    try:
        data = r.json()
    except ValueError:
        data = {"output": r.text}
    if not isinstance(data, dict):
        data = {"output": json.dumps(data)}
    if r.status_code >= 400 or data.get("error"):
        return f"mod: {tool}", {"error": str(data.get("error") or f"HTTP {r.status_code}")[:2000], "exit_code": 1}
    out = data.get("output")
    if not isinstance(out, str):
        out = json.dumps(out if out is not None else data.get("data", data), default=str)
    result: Dict[str, Any] = {"output": out[:MAX_OUTPUT], "exit_code": 0}
    if isinstance(data.get("data"), (dict, list)) and isinstance(data.get("output"), str):
        result["data"] = data["data"]
    runtime.touch(mod_id)
    return f"mod: {entry.get('name')} {name}", result
