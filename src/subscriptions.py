"""Subscription plans: what flat-rate endpoints (ChatGPT plan / subscription,
Copilot, OpenCode Go) actually cost, and who used them.

src/pricing.py prices subscription calls at $0 metered (no per-token bill),
which is right for the invoice but hides the plan's monthly price and lets a
budget ignore an agent that burns through the plan. This module adds the
fixed price back as an *allocation*, without touching the recorded rows.

Configuration (settings.json, admin API ``PUT /api/usage/subscriptions``):

``subscription_plans``
    ``{"<key>": {"name"?, "monthly_usd", "requests_per_day"?,
    "requests_per_5h"?, "tokens_per_month"?}}``. ``key`` is a model endpoint
    id, an endpoint base URL, or a provider name (``chatgpt-plan``,
    ``chatgpt-subscription``, ``copilot``, ``opencode-go``). A usage row
    matches its endpoint id first, then its endpoint's base URL, then its
    provider. The limits are what the plan says; they are shown on the Usage
    page as "used of limit" and ``tokens_per_month`` also changes how cost is
    allocated (below).

``budget_caps``
    ``{"<scope_type>:<scope_id>" | "<scope_type>": {"requests_per_day"?,
    "requests_per_month"?, "tokens_per_day"?, "tokens_per_month"?}}``. Usage
    caps on subscription calls for a budget scope (src/budgets.py). The
    ``scope_type:scope_id`` entry wins over the ``scope_type`` default.

``budget_count_subscription_allocation`` (default true)
    Whether a budget's money limit also counts the scope's allocated share of
    subscription plans.

Allocation (the period is the UTC calendar month, like the rest of usage):

* Without ``tokens_per_month`` ("time share"): the plan's accrued cost is
  ``monthly_usd x elapsed fraction of the month`` (a past month is fully
  elapsed), split across everyone who used the plan by token share:
  ``allocated = accrued x scope_tokens / plan_tokens``. Shares always add up
  to the accrued cost.
* With ``tokens_per_month`` ("capacity"): each token costs
  ``monthly_usd / tokens_per_month`` and
  ``allocated = monthly_usd x scope_tokens / max(tokens_per_month, plan_tokens)``,
  so the total never exceeds the plan price and does not grow while idle.

Tokens are input + cached input + output. Local (``local_free``) rows are
never part of any plan and stay $0.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

PLAN_LIMIT_FIELDS = ("requests_per_day", "requests_per_5h", "tokens_per_month")
CAP_FIELDS = ("requests_per_day", "requests_per_month", "tokens_per_day", "tokens_per_month")

METHOD_TIME_SHARE = "time_share"
METHOD_CAPACITY = "capacity"


# ── settings ────────────────────────────────────────────────────────────

def _get(key: str, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def get_plans() -> Dict[str, Dict[str, Any]]:
    v = _get("subscription_plans", {}) or {}
    return v if isinstance(v, dict) else {}


def get_caps() -> Dict[str, Dict[str, Any]]:
    v = _get("budget_caps", {}) or {}
    return v if isinstance(v, dict) else {}


def count_allocation_default() -> bool:
    v = _get("budget_count_subscription_allocation", True)
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(v) if v is not None else True


def _pos(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def clean_plan(key: str, v: Any) -> Dict[str, Any]:
    """Validate one plan entry; raises ValueError with a readable message."""
    if not isinstance(v, dict):
        raise ValueError(f"{key}: expected an object")
    out: Dict[str, Any] = {}
    if v.get("name"):
        out["name"] = str(v["name"])[:80]
    for f in ("monthly_usd",) + PLAN_LIMIT_FIELDS:
        if v.get(f) in (None, ""):
            continue
        try:
            n = float(v[f])
        except (TypeError, ValueError):
            raise ValueError(f"{key}.{f}: expected a number")
        if n < 0:
            raise ValueError(f"{key}.{f}: must be >= 0")
        if f != "monthly_usd":
            n = int(n)
        out[f] = n
    out.setdefault("monthly_usd", 0.0)
    return out


def clean_caps(key: str, v: Any) -> Dict[str, int]:
    if not isinstance(v, dict):
        raise ValueError(f"{key}: expected an object")
    out: Dict[str, int] = {}
    for f in CAP_FIELDS:
        if v.get(f) in (None, ""):
            continue
        try:
            n = int(float(v[f]))
        except (TypeError, ValueError):
            raise ValueError(f"{key}.{f}: expected a number")
        if n < 0:
            raise ValueError(f"{key}.{f}: must be >= 0")
        if n:
            out[f] = n
    return out


def caps_for(scope_type: str, scope_id: Optional[str],
             caps: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, int]:
    caps = get_caps() if caps is None else caps
    for k in (f"{scope_type}:{scope_id}" if scope_id else None, scope_type):
        if k and isinstance(caps.get(k), dict):
            try:
                return clean_caps(k, caps[k])
            except ValueError:
                return {}
    return {}


# ── matching rows to plans ──────────────────────────────────────────────

def _endpoint_bases(db) -> Dict[str, str]:
    """endpoint id -> normalized base URL, for plans keyed by base URL."""
    try:
        from core.database import ModelEndpoint
        from src.pricing import _normalize_base
        return {i: _normalize_base(b) for i, b in db.query(ModelEndpoint.id, ModelEndpoint.base_url).all()}
    except Exception:
        return {}


def _endpoint_names(db) -> Dict[str, str]:
    try:
        from core.database import ModelEndpoint
        return dict(db.query(ModelEndpoint.id, ModelEndpoint.name).all())
    except Exception:
        return {}


def plan_key_for(endpoint_id: Optional[str], provider: Optional[str],
                 plans: Dict[str, Any], bases: Dict[str, str]) -> Optional[str]:
    """The configured plan a subscription row belongs to, or None."""
    if endpoint_id and endpoint_id in plans:
        return endpoint_id
    base = bases.get(endpoint_id) if endpoint_id else None
    if base:
        from src.pricing import _normalize_base
        for k in plans:
            if _normalize_base(k) == base:
                return k
    if provider and provider in plans:
        return provider
    return None


def group_key_for(endpoint_id, provider, plans, bases) -> Tuple[str, bool]:
    """(key, configured): the plan key, or the provider (else endpoint id) for a
    subscription endpoint that has no price set yet."""
    k = plan_key_for(endpoint_id, provider, plans, bases)
    if k:
        return k, True
    return (provider or endpoint_id or "unknown"), False


# ── math ────────────────────────────────────────────────────────────────

def elapsed_fraction(start: datetime, end: datetime, now: Optional[datetime] = None) -> float:
    from src import usage
    now = now or usage.utcnow()
    if now >= end:
        return 1.0
    if now <= start:
        return 0.0
    return (now - start).total_seconds() / (end - start).total_seconds()


def plan_method(plan: Optional[Dict[str, Any]]) -> str:
    return METHOD_CAPACITY if plan and _pos(plan.get("tokens_per_month")) else METHOD_TIME_SHARE


def accrued_usd(plan: Dict[str, Any], plan_tokens: int, elapsed: float) -> float:
    """The part of the plan price allocated to all users so far."""
    price = _pos(plan.get("monthly_usd")) or 0.0
    if plan_method(plan) == METHOD_CAPACITY:
        cap = _pos(plan.get("tokens_per_month"))
        return price * min(1.0, plan_tokens / cap)
    return price * max(0.0, min(1.0, elapsed)) if plan_tokens > 0 else 0.0


def allocate(plan: Dict[str, Any], part_tokens: int, plan_tokens: int, elapsed: float) -> float:
    """One scope's share of the plan price (see module docstring)."""
    if part_tokens <= 0 or plan_tokens <= 0:
        return 0.0
    price = _pos(plan.get("monthly_usd")) or 0.0
    if plan_method(plan) == METHOD_CAPACITY:
        cap = _pos(plan.get("tokens_per_month"))
        return price * part_tokens / max(cap, float(plan_tokens))
    return accrued_usd(plan, plan_tokens, elapsed) * part_tokens / plan_tokens


# ── queries ─────────────────────────────────────────────────────────────

def _tok():
    from core.database import UsageEvent
    return UsageEvent.input_tokens + UsageEvent.cached_input_tokens + UsageEvent.output_tokens


def _rows(db, start: datetime, end: datetime, extra: Iterable = (), filters: Iterable = ()):
    """Subscription rows in [start, end), grouped by endpoint, provider and
    ``extra`` columns: [(endpoint_id, provider, *extra, calls, tokens)]."""
    from sqlalchemy import func
    from core.database import UsageEvent
    extra = list(extra)
    q = db.query(UsageEvent.endpoint_id, UsageEvent.provider, *extra,
                 func.count(UsageEvent.id), func.coalesce(func.sum(_tok()), 0)
                 ).filter(UsageEvent.billing_type == "subscription",
                          UsageEvent.occurred_at >= start, UsageEvent.occurred_at < end)
    for f in filters:
        q = q.filter(f)
    return q.group_by(UsageEvent.endpoint_id, UsageEvent.provider, *extra).all()


def _plan_totals(db, start, end, plans, bases) -> Dict[str, Dict[str, int]]:
    """Plan-wide (every owner) calls and tokens per group key."""
    out: Dict[str, Dict[str, int]] = {}
    for ep, prov, calls, tok in _rows(db, start, end):
        k, _ = group_key_for(ep, prov, plans, bases)
        d = out.setdefault(k, {"requests": 0, "tokens": 0})
        d["requests"] += int(calls or 0)
        d["tokens"] += int(tok or 0)
    return out


def _agent_names(db, ids: List[str]) -> Dict[str, str]:
    if not ids:
        return {}
    try:
        from core.database import CompanyAgent
        return dict(db.query(CompanyAgent.id, CompanyAgent.name).filter(CompanyAgent.id.in_(ids)).all())
    except Exception:
        return {}


def scope_usage(db, *, column: Optional[str], scope_id: Optional[str], owner: Optional[str],
                start: datetime, end: datetime, now: Optional[datetime] = None,
                plans: Optional[Dict[str, Any]] = None, scope_filter=None) -> Dict[str, Any]:
    """A budget scope's subscription use this month: requests, tokens, its
    allocated share of every plan, and today's (UTC) requests and tokens.
    ``scope_filter`` (src/budgets.py::scope_filter) replaces the
    ``column == scope_id`` match, for prefix scopes."""
    from src import usage
    from core.database import UsageEvent
    plans = get_plans() if plans is None else plans
    now = now or usage.utcnow()
    bases = _endpoint_bases(db)
    filters = []
    if scope_filter is not None:
        filters.append(scope_filter)
    elif column:
        filters.append(getattr(UsageEvent, column) == scope_id)
    if owner:
        filters.append(UsageEvent.owner == owner)
    elapsed = elapsed_fraction(start, end, now)
    totals = _plan_totals(db, start, end, plans, bases)
    out = {"requests": 0, "tokens": 0, "allocated_usd": 0.0, "requests_today": 0, "tokens_today": 0,
           "by_plan": {}}
    for ep, prov, calls, tok in _rows(db, start, end, filters=filters):
        k, configured = group_key_for(ep, prov, plans, bases)
        calls, tok = int(calls or 0), int(tok or 0)
        out["requests"] += calls
        out["tokens"] += tok
        p = out["by_plan"].setdefault(k, {"requests": 0, "tokens": 0, "allocated_usd": 0.0,
                                          "configured": configured})
        p["requests"] += calls
        p["tokens"] += tok
    for k, p in out["by_plan"].items():
        if p["configured"]:
            p["allocated_usd"] = allocate(plans[k], p["tokens"], totals.get(k, {}).get("tokens", 0), elapsed)
            p["method"] = plan_method(plans[k])
            out["allocated_usd"] += p["allocated_usd"]
    day = datetime(now.year, now.month, now.day)
    for _ep, _prov, calls, tok in _rows(db, max(day, start), min(day + timedelta(days=1), end), filters=filters):
        out["requests_today"] += int(calls or 0)
        out["tokens_today"] += int(tok or 0)
    return out


def summarize(db, *, owner: Optional[str], all_users: bool, start: datetime, end: datetime,
              include_fixed: bool, now: Optional[datetime] = None,
              plans: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The Usage page's subscription block. ``include_fixed`` (admins and
    single-user installs) counts the full plan prices in the monthly total;
    otherwise the viewer's allocated share is used."""
    from src import usage
    from core.database import UsageEvent
    plans = get_plans() if plans is None else plans
    now = now or usage.utcnow()
    bases = _endpoint_bases(db)
    names = _endpoint_names(db)
    elapsed = elapsed_fraction(start, end, now)
    totals = _plan_totals(db, start, end, plans, bases)
    filters = [UsageEvent.owner == owner] if (owner and not all_users) else []

    groups: Dict[str, Dict[str, Any]] = {}

    def group(k: str, configured: bool, ep=None, prov=None) -> Dict[str, Any]:
        g = groups.get(k)
        if g is None:
            plan = plans.get(k) if configured else None
            g = groups[k] = {
                "key": k, "configured": configured,
                "name": (plan or {}).get("name") or names.get(k) or (names.get(ep) if ep else None) or k,
                "provider": prov, "monthly_usd": float((plan or {}).get("monthly_usd") or 0.0),
                "limits": {f: plan.get(f) for f in PLAN_LIMIT_FIELDS if plan and plan.get(f)},
                "method": plan_method(plan) if plan else None,
                "requests": 0, "tokens": 0,
                "plan_requests": totals.get(k, {}).get("requests", 0),
                "plan_tokens": totals.get(k, {}).get("tokens", 0),
                "by_source": {}, "by_company_agent": {}, "by_owner": {},
            }
        return g

    for k, plan in plans.items():
        group(k, True)
    for ep, prov, calls, tok in _rows(db, start, end, filters=filters):
        k, configured = group_key_for(ep, prov, plans, bases)
        g = group(k, configured, ep, prov)
        g["provider"] = g["provider"] or prov
        g["requests"] += int(calls or 0)
        g["tokens"] += int(tok or 0)
    for col, bucket in ((UsageEvent.source, "by_source"), (UsageEvent.company_agent_id, "by_company_agent"),
                        (UsageEvent.owner, "by_owner")):
        if bucket == "by_owner" and not all_users:
            continue
        for ep, prov, key, calls, tok in _rows(db, start, end, extra=[col], filters=filters):
            if bucket == "by_company_agent" and not key:
                continue
            k, configured = group_key_for(ep, prov, plans, bases)
            b = group(k, configured, ep, prov)[bucket].setdefault(key, {"key": key, "requests": 0, "tokens": 0})
            b["requests"] += int(calls or 0)
            b["tokens"] += int(tok or 0)

    # Plan-limit usage (plan-wide, every owner): today, last 5 h, this month.
    day = datetime(now.year, now.month, now.day)
    windows = {"requests_per_day": (day, day + timedelta(days=1)),
               "requests_per_5h": (now - timedelta(hours=5), now + timedelta(seconds=1))}
    win_used: Dict[str, Dict[str, int]] = {}
    for f, (ws, we) in windows.items():
        if not any(g["limits"].get(f) for g in groups.values()):
            continue
        for ep, prov, calls, _tok_ in _rows(db, ws, we):
            k, _ = group_key_for(ep, prov, plans, bases)
            win_used.setdefault(k, {}).setdefault(f, 0)
            win_used[k][f] += int(calls or 0)

    agent_ids = sorted({a for g in groups.values() for a in g["by_company_agent"]})
    agent_names = _agent_names(db, agent_ids)
    out_plans, fixed, accrued, allocated = [], 0.0, 0.0, 0.0
    for g in groups.values():
        plan = plans.get(g["key"]) if g["configured"] else None
        g["share"] = (g["tokens"] / g["plan_tokens"]) if g["plan_tokens"] else 0.0
        g["accrued_usd"] = round(accrued_usd(plan, g["plan_tokens"], elapsed), 6) if plan else 0.0
        g["allocated_usd"] = round(allocate(plan, g["tokens"], g["plan_tokens"], elapsed), 6) if plan else 0.0
        used = win_used.get(g["key"], {})
        g["limit_usage"] = {}
        for f, lim in g["limits"].items():
            u = g["plan_tokens"] if f == "tokens_per_month" else used.get(f, 0)
            g["limit_usage"][f] = {"used": u, "limit": lim, "pct": round(u * 100.0 / lim, 1) if lim else None}
        for bucket in ("by_source", "by_company_agent", "by_owner"):
            rows = list(g[bucket].values())
            for r in rows:
                r["share"] = (r["tokens"] / g["tokens"]) if g["tokens"] else 0.0
                r["allocated_usd"] = round(allocate(plan, r["tokens"], g["plan_tokens"], elapsed), 6) if plan else 0.0
                if bucket == "by_company_agent":
                    r["name"] = agent_names.get(r["key"])
            rows.sort(key=lambda r: r["tokens"], reverse=True)
            g[bucket] = rows
        fixed += g["monthly_usd"]
        accrued += g["accrued_usd"]
        allocated += g["allocated_usd"]
        out_plans.append(g)
    out_plans.sort(key=lambda g: (not g["configured"], -g["monthly_usd"], -g["tokens"]))
    return {
        "plans": out_plans,
        "fixed_usd": round(fixed, 6),
        "accrued_usd": round(accrued, 6),
        "allocated_usd": round(allocated, 6),
        "elapsed_fraction": round(elapsed, 4),
        "include_fixed": include_fixed,
        "unconfigured": [g["key"] for g in out_plans if not g["configured"]],
    }


def subscription_endpoints(db) -> List[Dict[str, Any]]:
    """Configured model endpoints that bill as subscription (for the plan
    editor), with the plan key each one would match."""
    from src import pricing
    plans = get_plans()
    bases = _endpoint_bases(db)
    out = []
    try:
        from core.database import ModelEndpoint
        rows = db.query(ModelEndpoint).all()
    except Exception:
        rows = []
    for ep in rows:
        b = pricing.price_usage(base_url=ep.base_url, model=None, endpoint_id=ep.id,
                                endpoint_kind=getattr(ep, "endpoint_kind", None))
        if b["billing_type"] != pricing.BILLING_SUBSCRIPTION:
            continue
        out.append({"id": ep.id, "name": ep.name, "base_url": ep.base_url, "provider": b["provider"],
                    "plan_key": plan_key_for(ep.id, b["provider"], plans, bases)})
    return out
