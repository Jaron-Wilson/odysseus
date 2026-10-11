"""Monthly budget checks over recorded usage (src/usage.py).

    from src import budgets
    b = budgets.check("company_agent", agent_id, owner, monthly_limit_cents=500)
    if b["state"] == "hard_stop": refuse the run
    elif b["state"] == "warn": show the warning

Only metered cost counts toward a budget. Local models and subscription plans
are $0 here (their tokens are still reported in ``tokens``). The window is the
current UTC calendar month. Thresholds default to 80% (warn) and 100% (hard
stop) and can be changed in settings (``budget_warn_pct``,
``budget_hard_stop_pct``) or per call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

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
}


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
    col_name = SCOPE_COLUMNS[scope_type]
    db = usage._session_factory()
    try:
        q = db.query(
            func.coalesce(func.sum(case((UsageEvent.billing_type == "metered", UsageEvent.cost_usd), else_=0.0)), 0.0),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(case((UsageEvent.cost_status == "unpriced", 1), else_=0)), 0),
        ).filter(UsageEvent.occurred_at >= start, UsageEvent.occurred_at < end)
        if col_name:
            q = q.filter(getattr(UsageEvent, col_name) == scope_id)
        if owner:
            q = q.filter(UsageEvent.owner == owner)
        cost, inp, cached, out, unpriced = q.one()
        return {"cost_usd": float(cost or 0.0), "input_tokens": int(inp or 0),
                "cached_input_tokens": int(cached or 0), "output_tokens": int(out or 0),
                "unpriced_calls": int(unpriced or 0)}
    finally:
        db.close()


def check(scope_type: str, scope_id: Optional[str], owner: Optional[str],
          monthly_limit_cents: Optional[float], *, warn_pct: Optional[float] = None,
          hard_stop_pct: Optional[float] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Where a scope stands against its monthly limit.

    Returns ``{spent_cents, limit_cents, pct, state, warn_pct, hard_stop_pct,
    period_start, period_end, tokens, unpriced_calls}`` where ``state`` is
    ``ok`` / ``warn`` / ``hard_stop``. ``spent_cents`` is a float (calls can
    cost fractions of a cent). ``owner`` narrows to one user's rows; pass
    None to count everyone's (e.g. a shared company). ``unpriced_calls``
    counts metered calls whose model has no known price: they add $0 here, so
    a non-zero value means the spend may be understated.
    """
    from src import usage
    start, end = usage.month_window(now=now)
    w, h = thresholds(warn_pct, hard_stop_pct)
    s = spent(scope_type, scope_id, owner, start=start, end=end)
    spent_cents = round(s["cost_usd"] * 100.0, 4)
    limit = float(monthly_limit_cents) if monthly_limit_cents else None
    pct, state = state_for(spent_cents, limit, w, h)
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
    }
