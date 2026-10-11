"""Model prices and billing rules: the one place cost is decided.

Ported from the browser's MODEL_INFO table (static/js/chatRenderer.js), which
now fetches this table from GET /api/pricing and prefers it. Prices are USD per
1M tokens: ``input`` (uncached), ``cached`` (cache reads), ``output``.

Billing rules, in order (see :func:`price_usage`):

1. A per-endpoint override in settings (``endpoint_pricing``, keyed by
   endpoint id or base URL) wins: its ``billing_type`` and/or prices.
2. Subscription providers (ChatGPT subscription / plan, Copilot, OpenCode Go)
   are ``subscription``: $0 metered, tokens still counted. Their monthly
   plan price is added back as an allocation by src/subscriptions.py (Usage
   page and budgets), never stored on the row.
3. Ollama and local endpoints (loopback, LAN, Tailscale, single-label hosts,
   or an endpoint marked "local") are ``local_free``: $0.
4. Everything else is ``metered``: priced from the table (most specific key
   wins) or ``unpriced`` when the model is unknown. OpenRouter ``:free``
   models cost $0.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BILLING_LOCAL_FREE = "local_free"
BILLING_SUBSCRIPTION = "subscription"
BILLING_METERED = "metered"
BILLING_UNKNOWN = "unknown"

COST_PRICED = "priced"
COST_UNPRICED = "unpriced"
COST_FREE = "free"

SUBSCRIPTION_PROVIDERS = frozenset({"chatgpt-subscription", "chatgpt-plan", "copilot", "opencode-go"})


def _p(inp: float, out: float, cached: Optional[float] = None) -> Dict[str, float]:
    return {"input": inp, "cached": inp if cached is None else cached, "output": out}


def _claude(inp: float, out: float) -> Dict[str, float]:
    # Anthropic cache reads bill at a tenth of the input price.
    return _p(inp, out, round(inp * 0.1, 6))


# Substring keys, matched case-insensitively; the longest matching key wins
# (so "gpt-4o-mini" never bills as "gpt-4o"). Cached defaults to the input
# price where the provider has no published cache discount.
MODEL_PRICES: Dict[str, Dict[str, float]] = {
    # --- Anthropic ---
    "claude-fable-5":       _claude(10.00, 50.00),
    "claude-mythos-5":      _claude(10.00, 50.00),
    "claude-opus-5-5":      _claude(4.00, 20.00),
    "claude-opus-5":        _claude(5.00, 25.00),
    "claude-opus-4-8":      _claude(5.00, 25.00),
    "claude-opus-4-7":      _claude(5.00, 25.00),
    "claude-opus-4-6":      _claude(5.00, 25.00),
    "claude-opus-4-5":      _claude(5.00, 25.00),
    "claude-opus-4":        _claude(15.00, 75.00),
    "claude-sonnet-5-5":    _claude(2.00, 10.00),
    "claude-sonnet-5":      _claude(2.00, 10.00),
    "claude-sonnet-4-6":    _claude(3.00, 15.00),
    "claude-sonnet-4-5":    _claude(3.00, 15.00),
    "claude-sonnet-4":      _claude(3.00, 15.00),
    "claude-haiku-5-5":     _claude(0.10, 0.50),
    "claude-haiku-4-5":     _claude(1.00, 5.00),
    "claude-haiku-4":       _claude(0.80, 4.00),
    "claude-haiku-3-5":     _claude(0.80, 4.00),
    "claude-3-5-sonnet":    _claude(3.00, 15.00),
    "claude-3-5-haiku":     _claude(0.80, 4.00),
    "claude-3-opus":        _claude(15.00, 75.00),
    "claude-3-sonnet":      _claude(3.00, 15.00),
    "claude-3-haiku":       _claude(0.25, 1.25),
    # --- OpenAI ---
    "gpt-5":                _p(2.00, 8.00),
    "gpt-4.1":              _p(2.00, 8.00, 0.50),
    "gpt-4.1-mini":         _p(0.40, 1.60, 0.10),
    "gpt-4.1-nano":         _p(0.10, 0.40, 0.025),
    "gpt-4o":               _p(2.50, 10.00, 1.25),
    "gpt-4o-mini":          _p(0.15, 0.60, 0.075),
    "gpt-4-turbo":          _p(10.00, 30.00),
    "o1":                   _p(15.00, 60.00, 7.50),
    "o1-mini":              _p(3.00, 12.00, 1.50),
    "o1-pro":               _p(150.0, 600.0),
    "o3":                   _p(2.00, 8.00, 0.50),
    "o3-mini":              _p(1.10, 4.40, 0.55),
    "o4-mini":              _p(1.10, 4.40, 0.275),
    # --- DeepSeek ---
    "deepseek-chat":        _p(0.27, 1.10),
    "deepseek-coder":       _p(0.27, 1.10),
    "deepseek-reasoner":    _p(0.55, 2.19),
    "deepseek-r1":          _p(0.55, 2.19),
    "deepseek-v3":          _p(0.27, 1.10),
    "deepseek-v2":          _p(0.14, 0.28),
    # --- Google ---
    "gemini-2.5-pro":       _p(1.25, 10.00),
    "gemini-2.5-flash":     _p(0.15, 0.60),
    "gemini-2.0-flash":     _p(0.10, 0.40),
    "gemini-1.5-pro":       _p(1.25, 5.00),
    "gemini-1.5-flash":     _p(0.075, 0.30),
    "gemma-3":              _p(0.10, 0.10),
    # --- Mistral ---
    "mistral-large":        _p(2.00, 6.00),
    "mistral-medium":       _p(2.00, 6.00),
    "mistral-small":        _p(0.20, 0.60),
    "mistral-nemo":         _p(0.15, 0.15),
    "mixtral":              _p(0.24, 0.24),
    "codestral":            _p(0.30, 0.90),
    "pixtral":              _p(2.00, 6.00),
    # --- xAI ---
    "grok-4":               _p(3.00, 15.00),
    "grok-3":               _p(3.00, 15.00),
    "grok-2":               _p(2.00, 10.00),
    # --- Meta ---
    "llama-4":              _p(0.20, 0.20),
    "llama-3.3":            _p(0.20, 0.20),
    "llama-3.2":            _p(0.20, 0.20),
    "llama-3.1":            _p(0.20, 0.20),
    "llama-3":              _p(0.20, 0.20),
    # --- Qwen ---
    "qwen3":                _p(0.30, 1.20),
    "qwen2.5":              _p(0.30, 1.20),
    "qwq":                  _p(0.30, 1.20),
    # --- Cohere ---
    "command-a":            _p(2.50, 10.00),
    "command-r-plus":       _p(2.50, 10.00),
    "command-r":            _p(0.15, 0.60),
    # --- Perplexity ---
    "sonar-pro":            _p(3.00, 15.00),
    "sonar":                _p(1.00, 1.00),
    # --- MiniMax ---
    "minimax":              _p(0.70, 0.70),
    # --- Kimi / Moonshot ---
    "moonshot":             _p(1.00, 1.00),
    "kimi":                 _p(1.00, 1.00),
    # --- Microsoft ---
    "phi-4":                _p(0.07, 0.14),
    "phi-3":                _p(0.07, 0.14),
    # --- Nvidia ---
    "nemotron":             _p(0.30, 1.20),
    # --- Nous ---
    "hermes":               _p(0.20, 0.20),
}


def match_model_key(name: Optional[str], keys=None) -> Optional[str]:
    """The longest key that is a substring of ``name`` (same rule as
    static/js/model/matchKey.js). Also tries the name with dots between
    digits turned into dashes, so OpenRouter's ``anthropic/claude-sonnet-4.5``
    finds ``claude-sonnet-4-5``."""
    n = (name or "").lower()
    if not n:
        return None
    keys = MODEL_PRICES.keys() if keys is None else keys
    best = None
    variants = {n}
    variants.add(re.sub(r"(?<=\d)\.(?=\d)", "-", n))
    for v in variants:
        for key in keys:
            if key in v and (best is None or len(key) > len(best)):
                best = key
    return best


def model_price(model: Optional[str]) -> Optional[Dict[str, float]]:
    key = match_model_key(model)
    return dict(MODEL_PRICES[key], key=key) if key else None


# ── endpoint classification ─────────────────────────────────────────────

def _normalize_base(url: Optional[str]) -> str:
    url = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models", "/v1/messages", "/messages", "/responses"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


def is_local_host(host: Optional[str]) -> bool:
    """Loopback, private LAN, link-local, Tailscale CGNAT, ``.local`` names and
    single-label hosts (Docker service names) are local. Mirrors
    chatRenderer.js::isLocalEndpoint."""
    host = (host or "").lower().strip("[]")
    if not host:
        return False
    if host in ("localhost", "0.0.0.0", "host.docker.internal") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
        return True
    return ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")


def detect_provider(url: Optional[str]) -> str:
    if not url:
        return "unknown"
    try:
        from src.llm_core import _detect_provider
        return _detect_provider(url)
    except Exception:
        return "openai"


def provider_label(provider: Optional[str], url: Optional[str]) -> Optional[str]:
    """The provider name stored with usage: llm_core calls every unknown host
    "openai" (the protocol), which reads wrong for a local llama.cpp box."""
    if provider == "openai" and url:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if not (host == "openai.com" or host.endswith(".openai.com")):
            return "openai-compatible"
    return provider


def _settings_overrides() -> Dict[str, Any]:
    try:
        from src.settings import get_setting
        val = get_setting("endpoint_pricing", {}) or {}
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def endpoint_override(endpoint_id: Optional[str], base_url: Optional[str],
                      overrides: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    overrides = _settings_overrides() if overrides is None else overrides
    if not overrides:
        return None
    for k in (endpoint_id, _normalize_base(base_url)):
        if k and isinstance(overrides.get(k), dict):
            return overrides[k]
    return None


def _num(v, default=None):
    try:
        f = float(v)
        return f if f >= 0 else default
    except (TypeError, ValueError):
        return default


def classify(base_url: Optional[str], *, provider: Optional[str] = None,
             endpoint_kind: Optional[str] = None) -> str:
    """Billing type for an endpoint, before any override."""
    if not base_url:
        return BILLING_UNKNOWN
    provider = provider or detect_provider(base_url)
    if provider in SUBSCRIPTION_PROVIDERS:
        return BILLING_SUBSCRIPTION
    if provider == "ollama":
        return BILLING_LOCAL_FREE
    kind = (endpoint_kind or "auto").strip().lower()
    if kind == "local":
        return BILLING_LOCAL_FREE
    if kind in ("api", "proxy"):
        return BILLING_METERED
    try:
        host = urlparse(base_url).hostname
    except Exception:
        host = None
    if is_local_host(host):
        return BILLING_LOCAL_FREE
    return BILLING_METERED


def price_usage(*, base_url: Optional[str], model: Optional[str], input_tokens: int = 0,
                cached_input_tokens: int = 0, output_tokens: int = 0,
                provider: Optional[str] = None, endpoint_id: Optional[str] = None,
                endpoint_kind: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decide billing type and cost for one call.

    Returns ``{billing_type, cost_usd, cost_status, provider, price_key}``.
    """
    provider = provider or detect_provider(base_url)
    inp, cached, out = max(0, int(input_tokens or 0)), max(0, int(cached_input_tokens or 0)), max(0, int(output_tokens or 0))
    ov = endpoint_override(endpoint_id, base_url, overrides)
    billing = classify(base_url, provider=provider, endpoint_kind=endpoint_kind)
    if ov and ov.get("billing_type") in (BILLING_LOCAL_FREE, BILLING_SUBSCRIPTION, BILLING_METERED):
        billing = ov["billing_type"]
    elif ov and any(_num(ov.get(k)) is not None for k in ("input", "output")):
        billing = BILLING_METERED

    result = {"billing_type": billing, "cost_usd": 0.0, "provider": provider, "price_key": None}
    if billing in (BILLING_LOCAL_FREE, BILLING_SUBSCRIPTION):
        result["cost_status"] = COST_FREE
        return result
    if billing == BILLING_UNKNOWN:
        result["cost_status"] = COST_UNPRICED
        return result

    price = None
    if ov and _num(ov.get("input")) is not None and _num(ov.get("output")) is not None:
        i = _num(ov.get("input"))
        price = {"input": i, "output": _num(ov.get("output")),
                 "cached": _num(ov.get("cached"), i), "key": "endpoint override"}
    elif (model or "").lower().endswith(":free"):
        result["cost_status"] = COST_FREE
        result["price_key"] = ":free"
        return result
    else:
        price = model_price(model)
    if not price:
        result["cost_status"] = COST_UNPRICED
        return result
    cost = (inp * price["input"] + cached * price["cached"] + out * price["output"]) / 1_000_000
    result.update(cost_usd=round(cost, 8), cost_status=COST_PRICED, price_key=price.get("key"))
    return result


def pricing_table() -> Dict[str, Any]:
    """What GET /api/pricing returns."""
    return {
        "unit": "usd_per_1m_tokens",
        "models": {k: dict(v) for k, v in MODEL_PRICES.items()},
        "subscription_providers": sorted(SUBSCRIPTION_PROVIDERS),
        "endpoint_overrides": _settings_overrides(),
        "subscription_plans": _subscription_plans(),
    }


def _subscription_plans() -> Dict[str, Any]:
    try:
        from src.subscriptions import get_plans
        return get_plans()
    except Exception:
        return {}
