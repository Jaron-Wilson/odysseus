"""Model choice for mods: the models a mod may pick, and where a call goes.

    GET  /api/mods/v1/llm/models         [{spec, model, name, endpoint, local, offline}]
    POST /api/mods/v1/llm/chat           {messages, model?, model_role?, local_only?, scope?, ...}

A model is named by a spec, "model@Endpoint Name", the same form the model
pickers save (src/ai_interaction.py::_resolve_model). Without a spec the
call goes to the user's `utility` (or `default`) model.

`local_only: true` is a privacy promise: the call (or agent run, see
src/mods/runs.py) is refused unless the model's endpoint is on this machine
or the local network/tailnet. It never falls back to a cloud model.

Every call a mod makes is recorded in Usage with source "mod" and a
`source_scope` of "mod:<id>" plus the scope path the mod gave
(src/mods/usage_api.py), so its spend shows up and its budgets apply.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


class ModelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def is_local_url(url: Optional[str]) -> bool:
    """True for a model server on this machine or the local network/tailnet
    (loopback, private, CGNAT 100.64/10, .local/.lan/.ts.net names, a bare
    host name)."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost", "host.docker.internal") or host.endswith(
            (".local", ".lan", ".internal", ".ts.net", ".home")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def models(owner: Optional[str]) -> List[Dict[str, Any]]:
    """Every chat model `owner` can pick, as specs. Endpoint URLs are not
    shown; `local` says whether the endpoint is local."""
    from routes.model_routes import fetch_models
    from src.mods import access
    out: List[Dict[str, Any]] = []
    items = fetch_models(owner=owner or "", is_admin=access.is_admin_name(owner)).get("items") or []
    for item in items:
        if (item.get("model_type") or "llm") != "llm" or not item.get("endpoint_name"):
            continue
        ids = list(item.get("models") or []) + list(item.get("models_extra") or [])
        shown = list(item.get("models_display") or []) + list(item.get("models_extra_display") or [])
        local = is_local_url(item.get("url") or "")
        for i, mid in enumerate(ids):
            out.append({"spec": f"{mid}@{item['endpoint_name']}", "model": mid,
                        "name": shown[i] if i < len(shown) else mid.split("/")[-1],
                        "endpoint": item["endpoint_name"], "local": local, "offline": bool(item.get("offline"))})
    return out


def resolve(owner: Optional[str], spec: Optional[str] = None, role: str = "utility",
            local_only: bool = False) -> Tuple[str, str, Dict[str, str]]:
    """(url, model id, headers) for a spec, or for the user's model of `role`.
    Raises ModelError (400 bad spec, 403 not local, 503 nothing configured)."""
    if spec is not None and (not isinstance(spec, str) or not spec.strip() or len(spec) > 300):
        raise ModelError(400, "model must be a spec like model@Endpoint Name (GET llm/models)")
    if spec:
        from src.ai_interaction import _resolve_model
        try:
            url, model, headers = _resolve_model(spec.strip(), owner or None)
        except Exception as e:
            raise ModelError(400, f"Model {spec} is not available: {str(e)[:300]}")
    else:
        if role not in ("utility", "default"):
            raise ModelError(400, "model_role must be utility or default")
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint(role, owner=owner or None)
        if not url or not model:
            raise ModelError(503, "No model is configured (Settings > AI Defaults)")
    if local_only and not is_local_url(url):
        raise ModelError(403, f"{spec or 'The ' + role + ' model'} is not a local model, and this call is local only")
    return url, model, dict(headers or {})
