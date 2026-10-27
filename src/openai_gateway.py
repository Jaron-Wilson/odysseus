"""The models configured in Odysseus, seen as one OpenAI-compatible provider.

Other tools (opencode, scripts, editors) can point at Odysseus with a ``ody_`` API token and use every chat model
added under Settings > Models, instead of each tool keeping its own list. The routes are in
routes/openai_gateway_routes.py; this module decides which models exist and where a request for one goes.

A model is listed as ``<endpoint-slug>:<model>``, for example ``local-ollama:qwen3:8b`` (the slug comes from the
endpoint's name). A bare model name also works while exactly one endpoint has it.

Only endpoints that speak the OpenAI chat-completions format are exposed. Anthropic, ChatGPT sign-in, Copilot, native
Ollama (ollama.com /api) and add-on endpoints use other formats or credentials and are left out.
"""
import json
import re
from typing import Dict, List, Optional, Tuple

from core.database import ModelEndpoint, SessionLocal
from src.auth_helpers import owner_filter
from src.endpoint_resolver import (
    _NON_CHAT_MODEL,
    _endpoint_enabled_models,
    build_chat_url,
    build_headers,
    normalize_base,
    resolve_endpoint_runtime,
)
from src.llm_core import _detect_provider

# provider names from _detect_provider that do not take an OpenAI chat-completions request
_UNSUPPORTED = {"anthropic", "chatgpt-subscription", "chatgpt-plan", "copilot", "ollama"}
# on top of the resolver's list: embedding models such as nomic-embed-text do not answer chat requests
_NOT_CHAT = _NON_CHAT_MODEL + ("embed",)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "endpoint"


def _json_list(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return v if isinstance(v, list) else []


def supported_base(base: str) -> bool:
    """True when requests to this base can be passed through in the OpenAI format."""
    try:
        from src.mods.llm_endpoints import is_mod_url
        if is_mod_url(base):
            return False
    except Exception:
        pass
    return _detect_provider(base) not in _UNSUPPORTED


def _chat_models(ep) -> List[str]:
    """Enabled and pinned models of an endpoint that look like chat models, in order, without repeats."""
    hidden = set(_json_list(getattr(ep, "hidden_models", None)))
    seen, out = set(), []
    for m in list(_endpoint_enabled_models(ep)) + _json_list(getattr(ep, "pinned_models", None)):
        m = str(m)
        if m in seen or m in hidden or any(p in m.lower() for p in _NOT_CHAT):
            continue
        seen.add(m)
        out.append(m)
    return out


def _entries(db, owner: Optional[str]) -> List[Tuple[object, str, str]]:
    """(endpoint row, slug, model) for every model this owner may use. Slugs are unique per endpoint."""
    q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
    q = owner_filter(q, ModelEndpoint, owner or "")
    out, used = [], set()
    # oldest first, so the endpoint that has had a name longest keeps it plain when two share one
    for ep in sorted(q.all(), key=lambda e: (str(e.name or "").lower(), str(getattr(e, "created_at", "") or ""), str(e.id))):
        if (getattr(ep, "model_type", None) or "llm") != "llm":
            continue
        if not supported_base(normalize_base(getattr(ep, "base_url", "") or "")):
            continue
        slug = _slug(ep.name)
        if slug in used:
            slug = f"{slug}-{str(ep.id)[:4]}"
        used.add(slug)
        for m in _chat_models(ep):
            out.append((ep, slug, m))
    return out


def catalog(owner: Optional[str]) -> List[Dict[str, str]]:
    """The model list: [{"id", "endpoint_id", "endpoint_name", "model"}]."""
    db = SessionLocal()
    try:
        return [{"id": f"{slug}:{m}", "endpoint_id": ep.id, "endpoint_name": ep.name or slug, "model": m}
                for ep, slug, m in _entries(db, owner)]
    finally:
        db.close()


def resolve(model_id: str, owner: Optional[str]) -> Tuple[str, Dict[str, str], str]:
    """(chat url, request headers, the model's name at that endpoint). LookupError says why a request cannot go."""
    wanted = (model_id or "").strip()
    if not wanted:
        raise LookupError("no model given")
    db = SessionLocal()
    try:
        rows = _entries(db, owner)
        hit = [r for r in rows if f"{r[1]}:{r[2]}" == wanted]
        if not hit:
            hit = [r for r in rows if r[2] == wanted]      # a bare model name, if only one endpoint has it
            if len(hit) > 1:
                raise LookupError(f"'{wanted}' is on several endpoints; use one of: "
                                  + ", ".join(f"{s}:{m}" for _, s, m in hit))
        if not hit:
            raise LookupError(f"no model '{wanted}'; GET /v1/models lists what is available")
        ep, _slug_, model = hit[0]
        try:
            base, key = resolve_endpoint_runtime(ep, owner=owner or None)
        except Exception as e:
            raise LookupError(f"could not get credentials for that endpoint: {e}")
        return build_chat_url(base), build_headers(key, base), model
    finally:
        db.close()
