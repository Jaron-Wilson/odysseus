"""Monthly budget checks over recorded usage (src/usage.py).

    from src import budgets
    b = budgets.check("company_agent", agent_id, owner, monthly_limit_cents=500)
    if b["state"] == "hard_stop": refuse the run
    elif b["state"] == "warn": show the warning

Metered cost always counts toward a budget. Local models (``local_free``) are
$0 and never count. Subscription plans (ChatGPT plan, Copilot, OpenCode Go)
have no per-token bill, but they are not free, so two additive rules let a
budget limit work that runs on a plan (see src/subscriptions.py):

* Allocation: when ``count_subscription_allocation`` is on (default, setting
  ``budget_count_subscription_allocation``), the scope's allocated share of
  every priced plan (``subscription_plans``) is added to ``spent_cents`` and
  so to the money limit. ``metered_cents`` and
  ``subscription_cents_allocated`` report the two parts. A plan without a
  price allocates $0.
* Usage caps: ``caps`` (or the ``budget_caps`` setting for the scope) limits
  subscription requests or tokens per UTC day or month
  (``requests_per_day``, ``requests_per_month``, ``tokens_per_day``,
  ``tokens_per_month``). Each cap gets the same warn / hard stop thresholds.

``state`` is the worst of the money state and every cap state, so a caller
that only reads ``state`` (or ``spent_cents`` against ``limit_cents``) stops a
subscription-backed agent without changes. ``money_state`` is the money-only
state and ``stopped_by`` lists what is at hard stop (``"money"`` or the cap
name).

The window is the current UTC calendar month. Thresholds default to 80% (warn)
and 100% (hard stop) and can be changed in settings (``budget_warn_pct``,
``budget_hard_stop_pct``) or per call.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_WARN = "warn"
STATE_HARD_STOP = "hard_stop"

DEFAULT_WARN_PCT = 80.0
DEFAULT_HARD_STOP_PCT = 100.0

# scope_type -> UsageEvent column (None = the owner's whole spend)
SCOPE_COLUMNS = {
    "owner": None,
    "user": None,
    "company": "company_id",
    "company_agent": "company_agent_id",
    "agent": "company_agent_id",
    "company_run": "company_run_id",
    "session": "session_id",
    "task_run": "task_run_id",
    # A mod's scope path, "mod:<id>/<path>": matches the path and everything
    # under it (src/mods/usage_api.py).
    "mod_scope": "source_scope",
}
PREFIX_SCOPES = ("mod_scope",)


def scope_filter(scope_type: str, scope_id: Optional[str]):
    """The UsageEvent filter for a scope, or None for the owner's whole spend."""
    from core.database import UsageEvent
    col_name = SCOPE_COLUMNS[scope_type]
    if not col_name:
        return None
    col = getattr(UsageEvent, col_name)
    if scope_type in PREFIX_SCOPES:
        from sqlalchemy import or_
        sid = str(scope_id or "").rstrip("/")
        return or_(col == sid, col.startswith(sid + "/", autoescape=True))
    return col == scope_id


def _setting_pct(key: str, default: float) -> float:
    try:
        from src.settings import get_setting
        v = float(get_setting(key, default))
        return v if v > 0 else default
    except Exception:
        return default


def thresholds(warn_pct: Optional[float] = None, hard_stop_pct: Optional[float] = None):
    w = float(warn_pct) if warn_pct is not None else _setting_pct("budget_warn_pct", DEFAULT_WARN_PCT)
    h = float(hard_stop_pct) if hard_stop_pct is not None else _setting_pct("budget_hard_stop_pct", DEFAULT_HARD_STOP_PCT)
    if w > h:
        w = h
    return w, h


def state_for(spent_cents: float, limit_cents: Optional[float], warn_pct: float, hard_stop_pct: float):
    """(pct, state) for a spend against a limit. No limit (None or <= 0)
    means unlimited: always ok."""
    if not limit_cents or limit_cents <= 0:
        return None, STATE_OK
    pct = spent_cents * 100.0 / float(limit_cents)
    if pct >= hard_stop_pct:
        return pct, STATE_HARD_STOP
    if pct >= warn_pct:
        return pct, STATE_WARN
    return pct, STATE_OK


def spent(scope_type: str, scope_id: Optional[str], owner: Optional[str], *,
          start: datetime, end: datetime) -> Dict[str, Any]:
    """Metered cost (USD) and token totals for a scope in [start, end)."""
    from sqlalchemy import case, func
    from core.database import UsageEvent
    from src import usage
    if scope_type not in SCOPE_COLUMNS:
        raise ValueError(f"unknown budget scope_type {scope_type!r}")
    flt = scope_filter(scope_type, scope_id)
    db = usage._session_factory()
    try:
        q = db.query(
            func.coalesce(func.sum(case((UsageEvent.billing_type == "metered", UsageEvent.cost_usd), else_=0.0)), 0.0),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(case((UsageEvent.cost_status == "unpriced", 1), else_=0)), 0),
        ).filter(UsageEvent.occurred_at >= start, UsageEvent.occurred_at < end)
        if flt is not None:
            q = q.filter(flt)
        if owner:
            q = q.filter(UsageEvent.owner == owner)
        cost, inp, cached, out, unpriced = q.one()
        return {"cost_usd": float(cost or 0.0), "input_tokens": int(inp or 0),
                "cached_input_tokens": int(cached or 0), "output_tokens": int(out or 0),
                "unpriced_calls": int(unpriced or 0)}
    finally:
        db.close()


_SEVERITY = {STATE_OK: 0, STATE_WARN: 1, STATE_HARD_STOP: 2}


def _worst(states):
    return max(states, key=lambda s: _SEVERITY.get(s, 0), default=STATE_OK)


def subscription_usage(scope_type: str, scope_id: Optional[str], owner: Optional[str], *,
                       start: datetime, end: datetime, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Subscription requests, tokens and allocated plan cost for a scope."""
    from src import subscriptions, usage
    if scope_type not in SCOPE_COLUMNS:
        raise ValueError(f"unknown budget scope_type {scope_type!r}")
    db = usage._session_factory()
    try:
        return subscriptions.scope_usage(db, column=SCOPE_COLUMNS[scope_type], scope_id=scope_id,
                                         owner=owner, start=start, end=end, now=now,
                                         scope_filter=scope_filter(scope_type, scope_id))
    finally:
        db.close()


def check(scope_type: str, scope_id: Optional[str], owner: Optional[str],
          monthly_limit_cents: Optional[float], *, warn_pct: Optional[float] = None,
          hard_stop_pct: Optional[float] = None, now: Optional[datetime] = None,
          count_subscription_allocation: Optional[bool] = None,
          caps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Where a scope stands against its monthly limit.

    Returns ``{spent_cents, limit_cents, pct, state, warn_pct, hard_stop_pct,
    period_start, period_end, tokens, unpriced_calls}`` where ``state`` is
    ``ok`` / ``warn`` / ``hard_stop``. ``spent_cents`` is a float (calls can
    cost fractions of a cent). ``owner`` narrows to one user's rows; pass
    None to count everyone's (e.g. a shared company). ``unpriced_calls``
    counts metered calls whose model has no known price: they add $0 here, so
    a non-zero value means the spend may be understated.

    Subscription additions (module docstring): ``spent_cents`` includes
    ``subscription_cents_allocated`` when ``count_subscription_allocation``
    (None = the setting, default on). ``caps`` overrides the ``budget_caps``
    setting for this scope (pass ``{}`` for none). Extra keys:
    ``metered_cents``, ``subscription_cents_allocated``,
    ``count_subscription_allocation``, ``subscription_requests``,
    ``subscription_tokens``, ``subscription_requests_today``,
    ``subscription_tokens_today``, ``subscription_by_plan``, ``caps`` (list of
    ``{name, limit, used, pct, state}``), ``money_state``, ``stopped_by``.
    """
    from src import subscriptions, usage
    start, end = usage.month_window(now=now)
    w, h = thresholds(warn_pct, hard_stop_pct)
    s = spent(scope_type, scope_id, owner, start=start, end=end)
    metered_cents = round(s["cost_usd"] * 100.0, 4)
    try:
        sub = subscription_usage(scope_type, scope_id, owner, start=start, end=end, now=now)
    except Exception:
        logger.warning("[budgets] subscription usage unavailable", exc_info=True)
        sub = {"requests": 0, "tokens": 0, "allocated_usd": 0.0, "requests_today": 0,
               "tokens_today": 0, "by_plan": {}}
    if count_subscription_allocation is None:
        count_subscription_allocation = subscriptions.count_allocation_default()
    sub_cents = round(sub["allocated_usd"] * 100.0, 4)
    spent_cents = round(metered_cents + (sub_cents if count_subscription_allocation else 0.0), 4)
    limit = float(monthly_limit_cents) if monthly_limit_cents else None
    pct, money_state = state_for(spent_cents, limit, w, h)

    cap_limits = (subscriptions.caps_for(scope_type, scope_id) if caps is None
                  else subscriptions.clean_caps("caps", caps))
    used_by = {"requests_per_day": sub["requests_today"], "requests_per_month": sub["requests"],
               "tokens_per_day": sub["tokens_today"], "tokens_per_month": sub["tokens"]}
    cap_rows = []
    for name in subscriptions.CAP_FIELDS:
        lim = cap_limits.get(name)
        if not lim:
            continue
        cpct, cstate = state_for(used_by[name], lim, w, h)
        cap_rows.append({"name": name, "limit": lim, "used": used_by[name],
                         "pct": round(cpct, 2) if cpct is not None else None, "state": cstate})
    state = _worst([money_state] + [c["state"] for c in cap_rows])
    stopped_by = (["money"] if money_state == STATE_HARD_STOP else []) + \
        [c["name"] for c in cap_rows if c["state"] == STATE_HARD_STOP]
    return {
        "scope_type": scope_type, "scope_id": scope_id,
        "spent_cents": spent_cents,
        "limit_cents": limit,
        "pct": round(pct, 2) if pct is not None else None,
        "state": state,
        "warn_pct": w, "hard_stop_pct": h,
        "period_start": start.isoformat() + "Z", "period_end": end.isoformat() + "Z",
        "tokens": {"input": s["input_tokens"], "cached_input": s["cached_input_tokens"],
                   "output": s["output_tokens"]},
        "unpriced_calls": s["unpriced_calls"],
        "metered_cents": metered_cents,
        "subscription_cents_allocated": sub_cents,
        "count_subscription_allocation": bool(count_subscription_allocation),
        "subscription_requests": sub["requests"],
        "subscription_tokens": sub["tokens"],
        "subscription_requests_today": sub["requests_today"],
        "subscription_tokens_today": sub["tokens_today"],
        "subscription_by_plan": sub["by_plan"],
        "caps": cap_rows,
        "money_state": money_state,
        "stopped_by": stopped_by,
    }
