"""Model endpoints served by an add-on (the `llm.provider` capability).

An add-on that offers models (Sign in with ChatGPT, say) serves an
OpenAI-compatible API under ``/mod/cap/llm.provider/v1/``. Its endpoint rows
have the base URL ``mod://<mod id>/v1``, which never changes, while the mod's
real port does; the URL is turned into ``http://127.0.0.1:<port>/...`` only
when a request is sent (src/llm_core.py), with the signed identity of the user
the call is for, so the add-on can use that user's own sign-in. Nothing about
the add-on's credentials ever reaches the core.

The add-on keeps its rows up to date itself, through the mod API
(``PUT``/``DELETE /api/mods/v1/llm/endpoint``): after a user signs in it
registers their models, and after they sign out it removes the row. A row
only ever points at the add-on that made it.

Optional metadata in the add-on's manifest:

    "llm_provider": {"label": "ChatGPT", "usage_provider": "chatgpt-plan",
                     "billing": "subscription", "tool_cap": true}

``usage_provider`` is the provider name usage rows and subscription plans use
(so a plan priced before the feature became an add-on keeps matching), never
one of the core's own protocol names; ``billing`` is ``subscription``,
``metered`` or ``local_free``; ``tool_cap`` caps function tools at OpenAI's
limit (src/tool_cap.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.mods import paths, providers, registry

logger = logging.getLogger(__name__)

CAP = "llm.provider"
SCHEME = "mod"
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
# Names llm_core dispatches on: an add-on may not borrow them.
_PROTOCOL_NAMES = frozenset({"openai", "anthropic", "ollama", "openrouter", "groq", "nvidia", "copilot",
                             "chatgpt-subscription", "opencode-go", "opencode-zen"})
BILLING = ("subscription", "metered", "local_free")


class ModEndpointUnavailable(Exception):
    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.message = message
        self.status = status


def is_mod_url(url: Optional[str]) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith(SCHEME + "://")


def mod_id_of(url: Optional[str]) -> Optional[str]:
    if not is_mod_url(url):
        return None
    host = (urlparse(url.strip()).hostname or "").lower()
    return host if _ID_RE.match(host) else None


def base_for(mod_id: str) -> str:
    return f"{SCHEME}://{mod_id}/v1"


def meta(mod_id: str) -> Dict[str, Any]:
    """The add-on's `llm_provider` manifest metadata, cleaned."""
    entry = registry.get(mod_id) or {}
    raw = (entry.get("manifest") or {}).get("llm_provider")
    if raw is None:
        try:
            with open(os.path.join(paths.src_dir(mod_id), "odysseus-mod.json"), "r", encoding="utf-8") as f:
                raw = (json.load(f) or {}).get("llm_provider")
        except (OSError, ValueError, AttributeError):
            raw = None
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {"label": str(raw.get("label") or entry.get("name") or mod_id)[:40]}
    up = raw.get("usage_provider")
    out["usage_provider"] = (up if isinstance(up, str) and _PROVIDER_RE.match(up) and up not in _PROTOCOL_NAMES
                             else f"mod:{mod_id}")
    out["billing"] = raw.get("billing") if raw.get("billing") in BILLING else ""
    out["tool_cap"] = bool(raw.get("tool_cap"))
    return out


def provider_name(url: str) -> str:
    """What llm_core._detect_provider calls a mod URL."""
    mid = mod_id_of(url)
    if not mid:
        return "mod"
    try:
        return meta(mid)["usage_provider"]
    except Exception:
        return f"mod:{mid}"


def unavailable_message(mod_id: Optional[str]) -> str:
    name = mod_id or "add-on"
    entry = registry.get(mod_id) if mod_id else None
    if not entry:
        return (f"These models come from the {name} add-on, which is not installed. "
                "Install it in Settings > Mods > Browse.")
    if not entry.get("enabled"):
        return f"These models come from the {entry.get('name') or name} add-on, which is turned off in Settings > Mods."
    return f"The {entry.get('name') or name} add-on is not running. Check it in Settings > Mods."


def _split(url: str) -> Tuple[str, str]:
    mid = mod_id_of(url)
    if not mid or not providers.provider(CAP, mid):
        raise ModEndpointUnavailable(unavailable_message(mid), 404 if not registry.get(mid or "") else 503)
    path = urlparse(url.strip()).path or "/"
    if not path.startswith("/v1"):
        path = "/v1" + (path if path.startswith("/") else "/" + path)
    return mid, path


def _headers(mid: str, headers: Optional[Dict], owner: Optional[str]) -> Dict[str, str]:
    h = {k: v for k, v in (headers or {}).items()
         if isinstance(k, str) and k.lower() != "authorization" and not k.lower().startswith("x-odysseus-")}
    h.update(providers.headers_for(mid, owner))
    return h


def resolve(url: str, headers: Optional[Dict] = None, owner: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """(http URL, headers) for a mod URL, from synchronous code."""
    mid, path = _split(url)
    try:
        base = providers.base_url_sync(mid)
    except providers.ProviderUnavailable:
        raise ModEndpointUnavailable(unavailable_message(mid))
    return base + providers.CAP_PREFIX + "/" + CAP + path, _headers(mid, headers, owner)


async def resolve_async(url: str, headers: Optional[Dict] = None,
                        owner: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    mid, path = _split(url)
    try:
        base = await providers.base_url(mid)
    except providers.ProviderUnavailable:
        raise ModEndpointUnavailable(unavailable_message(mid))
    return base + providers.CAP_PREFIX + "/" + CAP + path, _headers(mid, headers, owner)


# ── Endpoint rows, kept by the add-on through the mod API ────────────────

def _owner_for(user: str) -> Optional[str]:
    from src.mods import signing
    return None if user in ("", signing.LOCAL_USER, signing.CORE_USER) else user


def _invalidate_models_cache() -> None:
    try:
        from routes.model_routes import _invalidate_models_cache as inv
        inv()
    except Exception:
        pass


def _session():
    from core.database import SessionLocal
    return SessionLocal()


def upsert_endpoint(mod_id: str, user: str, models: List[str], name: str = "") -> Dict[str, Any]:
    from core.database import ModelEndpoint
    owner = _owner_for(user)
    base = base_for(mod_id)
    models = [m for m in models if isinstance(m, str) and m.strip()][:200]
    db = _session()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.base_url == base)
        q = q.filter(ModelEndpoint.owner == owner) if owner else q.filter(ModelEndpoint.owner.is_(None))
        ep = q.first()
        if ep is None:
            ep = ModelEndpoint(id=str(uuid.uuid4())[:8], base_url=base, owner=owner)
            db.add(ep)
        ep.name = (name or meta(mod_id)["label"])[:60]
        ep.api_key = None
        ep.provider_auth_id = None
        ep.is_enabled = True
        ep.model_type = "llm"
        ep.endpoint_kind = "api"
        ep.model_refresh_mode = "manual"
        if ep.supports_tools is None:
            ep.supports_tools = True
        ep.cached_models = json.dumps(models)
        db.commit()
        out = {"id": ep.id, "name": ep.name, "base_url": base, "models": models}
    finally:
        db.close()
    _invalidate_models_cache()
    return out


def remove_endpoint(mod_id: str, user: str) -> int:
    from core.database import ModelEndpoint
    owner = _owner_for(user)
    db = _session()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.base_url == base_for(mod_id))
        q = q.filter(ModelEndpoint.owner == owner) if owner else q.filter(ModelEndpoint.owner.is_(None))
        n = 0
        for ep in q.all():
            db.delete(ep)
            n += 1
        db.commit()
    finally:
        db.close()
    if n:
        _invalidate_models_cache()
    return n
