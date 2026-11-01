"""The models configured in Odysseus, seen as one OpenAI-compatible provider.

Other tools (opencode, scripts, editors) can point at Odysseus with a ``ody_`` API token and use every chat model
added under Settings > Models, instead of each tool keeping its own list. The routes are in
routes/openai_gateway_routes.py; this module decides which models exist and where a request for one goes.

A model is listed as ``<endpoint-slug>:<model>``, for example ``local-ollama:qwen3:8b`` (the slug comes from the
endpoint's name). A bare model name also works while exactly one endpoint has it.

Every enabled chat model the user can use is listed. Speech models (kokoro, tts-1 ...) are not: they belong to
/v1/audio/speech (src/gateway_speech.py) and never answer a chat, so the auto router cannot pick them either. Endpoints that speak the OpenAI chat-completions format are
passed straight through. The others (Anthropic, ChatGPT sign-in, Copilot, native Ollama /api, add-on models) are
translated: routes/openai_gateway_routes.py sends the request through Odysseus's own provider layer (src/llm_core.py,
the code a chat turn uses) and turns the answer back into OpenAI chat-completions, streamed or not.
"""
import json
import re
from typing import Dict, List, Optional, Tuple

from core.database import ModelEndpoint, SessionLocal
from src.auth_helpers import owner_filter
from src.endpoint_resolver import (
    _NON_CHAT_MODEL,
    _endpoint_cached_models,
    _endpoint_enabled_models,
    build_chat_url,
    build_headers,
    normalize_base,
    resolve_endpoint_runtime,
)
from src.llm_core import _detect_provider

# provider names from _detect_provider that do not take a plain OpenAI chat-completions request
_TRANSLATED = {"anthropic", "chatgpt-subscription", "chatgpt-plan", "copilot", "ollama"}
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


def needs_translation(base: str) -> bool:
    """True when this base does not take an OpenAI chat-completions request as it is."""
    try:
        from src.mods.llm_endpoints import is_mod_url
        if is_mod_url(base):
            return True
    except Exception:
        pass
    return _detect_provider(base) in _TRANSLATED


# a model id with one of these in it is a speech model (OpenAI's tts-1 and gpt-4o-mini-tts, Kokoro-FastAPI's "kokoro", ...).
# Speech models are offered as tts:<endpoint> by src/gateway_speech.py and are never chat models.
SPEECH_MODEL_HINTS = ("tts", "kokoro", "piper", "orpheus", "parler", "cosyvoice", "chatterbox")


def is_speech_model(name: str) -> bool:
    low = str(name).lower()
    return any(h in low for h in SPEECH_MODEL_HINTS)


def voice_setting() -> Tuple[Optional[str], str]:
    """(id of the endpoint chosen as the voice in Settings > Text to speech, else None; the saved speech model)."""
    try:
        from src.settings import load_settings
        s = load_settings()
        p = str(s.get("tts_provider") or "")
        return (p.split(":", 1)[1] if p.startswith("endpoint:") else None), str(s.get("tts_model") or "")
    except Exception:
        return None, ""


def speech_models(ep, voice: Optional[Tuple[Optional[str], str]] = None) -> List[str]:
    """Every model of an endpoint (hidden ones too) that is a speech model: by its name, or the speech model saved
    for the endpoint that is the voice in Settings. A model with tts in its name comes first."""
    voice_id, voice_model = voice if voice is not None else voice_setting()
    out = []
    for m in [str(x) for x in list(_endpoint_cached_models(ep)) + _json_list(getattr(ep, "pinned_models", None))]:
        if m not in out and (is_speech_model(m) or (str(ep.id) == voice_id and voice_model and m == voice_model)):
            out.append(m)
    return sorted(out, key=lambda m: "tts" not in m.lower())


def _chat_models(ep, voice: Optional[Tuple[Optional[str], str]] = None) -> List[str]:
    """Enabled and pinned models of an endpoint that look like chat models, in order, without repeats."""
    hidden = set(_json_list(getattr(ep, "hidden_models", None)))
    speech = set(speech_models(ep, voice))
    seen, out = set(), []
    for m in list(_endpoint_enabled_models(ep)) + _json_list(getattr(ep, "pinned_models", None)):
        m = str(m)
        if m in seen or m in hidden or m in speech or any(p in m.lower() for p in _NOT_CHAT):
            continue
        seen.add(m)
        out.append(m)
    return out


def _entries(db, owner: Optional[str]) -> List[Tuple[object, str, str]]:
    """(endpoint row, slug, model) for every model this owner may use. Slugs are unique per endpoint."""
    q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
    q = owner_filter(q, ModelEndpoint, owner or "")
    out, used = [], set()
    voice = voice_setting()
    # oldest first, so the endpoint that has had a name longest keeps it plain when two share one
    for ep in sorted(q.all(), key=lambda e: (str(e.name or "").lower(), str(getattr(e, "created_at", "") or ""), str(e.id))):
        if (getattr(ep, "model_type", None) or "llm") != "llm":
            continue
        slug = _slug(ep.name)
        if slug in used:
            slug = f"{slug}-{str(ep.id)[:4]}"
        used.add(slug)
        for m in _chat_models(ep, voice):
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


def resolve(model_id: str, owner: Optional[str]) -> Tuple[str, Dict[str, str], str, bool]:
    """(chat url, request headers, the model's name at that endpoint, whether it needs translating).
    LookupError says why a request cannot go."""
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
        return build_chat_url(base), build_headers(key, base), model, needs_translation(base)
    finally:
        db.close()
