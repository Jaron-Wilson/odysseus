"""Coerce tool-call arguments to the types a tool's JSON schema declares.

Local models often send ``"30"`` for an integer, ``"true"`` for a boolean, or
a JSON string for an array. A strict tool then rejects the call, the model
retries the same thing, and eventually gives up on the argument altogether.
Converting when the intent is unambiguous removes that whole failure class.

One entry point, ``coerce_args(args, schema)``, used for every kind of tool:
native/core tools (src/tool_schemas.py), MCP tools (src/mcp_manager.py) and
mod tools (src/mods/tools.py).

It returns ``(new_args, problems)``. ``problems`` lists the arguments that
cannot be converted; ``format_problems`` turns them into a message that states
the expected type and an example, for the model.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Tuple

_EXAMPLES = {
    "integer": "30",
    "number": "1.5",
    "boolean": "true",
    "array": '["a", "b"]',
    "object": '{"key": "value"}',
    "string": '"text"',
}

_MISSING = object()


def _types(spec: Dict[str, Any]) -> List[str]:
    t = spec.get("type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    return []


def _matches(value: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    if t == "null":
        return value is None
    return True


def _to_number(value: Any, integer: bool) -> Any:
    if isinstance(value, bool):
        return _MISSING
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        try:
            f = float(s)
        except ValueError:
            return _MISSING
        is_int_text = s.lstrip("+-").isdigit()
    elif isinstance(value, float):
        f, is_int_text = value, False
    else:
        return _MISSING
    if not math.isfinite(f):
        return _MISSING
    if integer:
        return int(f) if f == int(f) else _MISSING
    return int(f) if is_int_text else f


def _coerce_items(items: list, spec: Dict[str, Any]) -> list:
    item_spec = spec.get("items")
    if not isinstance(item_spec, dict):
        return items
    out = []
    for it in items:
        c = _coerce_value(it, item_spec)
        out.append(it if c is _MISSING else c)
    return out


def _coerce_props(obj: dict, spec: Dict[str, Any]) -> dict:
    props = spec.get("properties")
    if not isinstance(props, dict):
        return obj
    out = dict(obj)
    for k, v in obj.items():
        if k in props:
            c = _coerce_value(v, props[k])
            if c is not _MISSING:
                out[k] = c
    return out


def _coerce_value(value: Any, spec: Dict[str, Any]) -> Any:
    """Return the coerced value, or _MISSING when it cannot be converted."""
    if not isinstance(spec, dict):
        return value
    types = _types(spec)
    if not types:
        return value
    if any(_matches(value, t) for t in types):
        if isinstance(value, list) and "array" in types:
            return _coerce_items(value, spec)
        if isinstance(value, dict) and "object" in types:
            return _coerce_props(value, spec)
        return value

    for t in types:
        if t in ("integer", "number") and isinstance(value, (str, float)):
            out = _to_number(value, t == "integer")
            if out is not _MISSING:
                return out
        elif t == "boolean":
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "yes"):
                    return True
                if low in ("false", "no"):
                    return False
            elif isinstance(value, int) and value in (0, 1):
                return bool(value)
        elif t in ("array", "object") and isinstance(value, str):
            s = value.strip()
            if s[:1] in ("[", "{"):
                try:
                    parsed = json.loads(s)
                except ValueError:
                    continue
                if _matches(parsed, t):
                    return _coerce_items(parsed, spec) if t == "array" else _coerce_props(parsed, spec)

    if "array" in types and value is not None and not (
            isinstance(value, str) and value.strip()[:1] == "["):
        # A single value where a list is expected becomes a one-item list.
        item_spec = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        item = _coerce_value(value, item_spec) if item_spec else value
        if item is not _MISSING:
            return [item]
    if "string" in types and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return _MISSING


def coerce_args(args: Any, schema: Any) -> Tuple[Any, List[Dict[str, str]]]:
    """Coerce top-level ``args`` to the types in ``schema`` (a JSON schema
    object with ``properties``). Unknown arguments pass through untouched."""
    if not isinstance(args, dict) or not isinstance(schema, dict):
        return args, []
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return args, []
    out = dict(args)
    problems: List[Dict[str, str]] = []
    for name, value in args.items():
        spec = props.get(name)
        if not isinstance(spec, dict):
            continue
        types = _types(spec)
        if not types:
            continue
        c = _coerce_value(value, spec)
        if c is _MISSING:
            expected = types[0] if len(types) == 1 else " or ".join(types)
            problems.append({"name": name, "expected": expected, "got": type(value).__name__,
                             "example": _EXAMPLES.get(types[0], "")})
        else:
            out[name] = c
    return out, problems


def format_problems(tool: str, problems: List[Dict[str, str]]) -> str:
    parts = []
    for p in problems:
        ex = f' Example: "{p["name"]}": {p["example"]}' if p.get("example") else ""
        parts.append(f'`{p["name"]}` must be of type {p["expected"]} (you sent a {p["got"]}).{ex}')
    return f"Invalid arguments for {tool}: " + " ".join(parts)


def coerce_and_check(tool: str, args: Any, schema: Any) -> Tuple[Any, str]:
    """Convenience wrapper: (args, error_message_or_empty)."""
    new, problems = coerce_args(args, schema)
    return new, (format_problems(tool, problems) if problems else "")
