"""Usage and cost tracking: record one row per finished model call, and read
them back as summaries.

Recording happens at the model-call choke points in src/llm_core.py
(``stream_llm`` and ``llm_call_async``), so chat turns, agent rounds,
scheduled tasks, subagents, teacher and verifier passes and background
extraction all land here without each caller remembering to. Attribution
comes from the call itself (``session_id``, ``owner``) plus an optional
:func:`usage_scope` the caller wraps around the work:

    with usage_scope(source="task", task_run_id=run_id):
        await run_the_task()

Explicit arguments to :func:`record_usage` beat the scope; the scope beats
the defaults. Recording never raises: a failure is logged and the chat goes on.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

SOURCES = ("chat", "task", "subagent", "company", "tts", "other")
SCOPE_FIELDS = ("owner", "source", "session_id", "task_run_id", "company_id",
                "company_agent_id", "company_run_id")

_scope: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "odysseus_usage_scope", default=None)


def _session_factory():
    """Patched in tests."""
    from core.database import SessionLocal
    return SessionLocal()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── attribution ─────────────────────────────────────────────────────────

@contextmanager
def usage_scope(**fields) -> Iterator[Dict[str, Any]]:
    """Attribute model calls made inside this block (and inside asyncio tasks
    created inside it, which copy the context). Nested scopes inherit and
    override the outer one's fields."""
    unknown = set(fields) - set(SCOPE_FIELDS)
    if unknown:
        raise TypeError(f"unknown usage scope fields: {sorted(unknown)}")
    merged = dict(_scope.get() or {})
    merged.update({k: v for k, v in fields.items() if v is not None})
    token = _scope.set(merged)
    try:
        yield merged
    finally:
        try:
            _scope.reset(token)
        except ValueError:
            # Reset from a different context (an async generator resumed in
            # another task): fall back to clearing our fields.
            _scope.set(None)


def current_scope() -> Dict[str, Any]:
    return dict(_scope.get() or {})


# ── recording ───────────────────────────────────────────────────────────

def _find_endpoint(db, base_url: Optional[str], owner: Optional[str]):
    """The configured ModelEndpoint row serving ``base_url`` (owner's own
    row preferred over a shared one), or None."""
    if not base_url:
        return None
    from core.database import ModelEndpoint
    from src.pricing import _normalize_base
    target = _normalize_base(base_url)
    if not target:
        return None
    best = None
    try:
        rows = db.query(ModelEndpoint).all()
    except Exception:
        return None
    for ep in rows:
        base = _normalize_base(getattr(ep, "base_url", "") or "")
        if not base or not (target == base or target.startswith(base + "/")):
            continue
        ep_owner = getattr(ep, "owner", None)
        if owner and ep_owner == owner:
            return ep
        if ep_owner is None and best is None:
            best = ep
        elif best is None and not owner:
            best = ep
    return best


def _session_owner(db, session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    try:
        from core.database import Session as DBSession
        row = db.query(DBSession.owner).filter(DBSession.id == session_id).first()
        return row[0] if row else None
    except Exception:
        return None


def record_usage(*, endpoint_url: Optional[str] = None, model: Optional[str] = None,
                 input_tokens: int = 0, cached_input_tokens: int = 0, output_tokens: int = 0,
                 idempotency_key: Optional[str] = None, owner: Optional[str] = None,
                 source: Optional[str] = None, session_id: Optional[str] = None,
                 task_run_id: Optional[str] = None, company_id: Optional[str] = None,
                 company_agent_id: Optional[str] = None, company_run_id: Optional[str] = None,
                 endpoint_id: Optional[str] = None, provider: Optional[str] = None,
                 billing_type: Optional[str] = None, cost_usd: Optional[float] = None,
                 occurred_at: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Store one usage event. Returns the stored row as a dict (the existing
    row when ``idempotency_key`` was already recorded), or None when there
    was nothing to record or recording failed. Never raises.

    ``input_tokens`` is uncached input; ``cached_input_tokens`` is cache reads.
    ``billing_type``/``cost_usd`` skip the price table (for non-token costs).
    """
    try:
        return _record(**locals())
    except Exception:
        logger.warning("[usage] could not record usage for %s", model, exc_info=True)
        return None


def _record(**kw) -> Optional[Dict[str, Any]]:
    inp = max(0, int(kw["input_tokens"] or 0))
    cached = max(0, int(kw["cached_input_tokens"] or 0))
    out = max(0, int(kw["output_tokens"] or 0))
    if not (inp or cached or out) and not kw["cost_usd"]:
        return None
    scope = current_scope()
    attr = {f: (kw.get(f) if kw.get(f) is not None else scope.get(f)) for f in SCOPE_FIELDS}
    key = kw["idempotency_key"] or f"auto:{uuid.uuid4()}"

    from core.database import UsageEvent
    from sqlalchemy.exc import IntegrityError
    from src import pricing

    db = _session_factory()
    try:
        existing = db.query(UsageEvent).filter(UsageEvent.idempotency_key == key).first()
        if existing is not None:
            return _row(existing)
        if not attr["owner"]:
            attr["owner"] = _session_owner(db, attr["session_id"])
        source = attr["source"] or ("chat" if attr["session_id"] else "other")
        if source not in SOURCES:
            source = "other"
        ep = None
        endpoint_id = kw["endpoint_id"]
        endpoint_kind = None
        if kw["endpoint_url"]:
            ep = _find_endpoint(db, kw["endpoint_url"], attr["owner"])
            if ep is not None:
                endpoint_id = endpoint_id or ep.id
                endpoint_kind = getattr(ep, "endpoint_kind", None)
        if kw["billing_type"] is not None or kw["cost_usd"] is not None:
            cost = float(kw["cost_usd"] or 0.0)
            billing = kw["billing_type"] or pricing.BILLING_METERED
            priced = {"billing_type": billing, "cost_usd": cost,
                      "cost_status": pricing.COST_PRICED if cost else pricing.COST_FREE,
                      "provider": kw["provider"] or (pricing.detect_provider(kw["endpoint_url"]) if kw["endpoint_url"] else None)}
        else:
            priced = pricing.price_usage(
                base_url=kw["endpoint_url"], model=kw["model"], input_tokens=inp,
                cached_input_tokens=cached, output_tokens=out, provider=kw["provider"],
                endpoint_id=endpoint_id, endpoint_kind=endpoint_kind)
        row = UsageEvent(
            id=str(uuid.uuid4()), owner=attr["owner"], occurred_at=kw["occurred_at"] or utcnow(),
            source=source, session_id=attr["session_id"], task_run_id=attr["task_run_id"],
            company_id=attr["company_id"], company_agent_id=attr["company_agent_id"],
            company_run_id=attr["company_run_id"], endpoint_id=endpoint_id,
            provider=pricing.provider_label(priced.get("provider"), kw["endpoint_url"]), model=kw["model"],
            billing_type=priced["billing_type"], input_tokens=inp, cached_input_tokens=cached,
            output_tokens=out, cost_usd=float(priced["cost_usd"] or 0.0),
            cost_status=priced["cost_status"], idempotency_key=key,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Lost a race on the same idempotency key: the other write wins.
            db.rollback()
            existing = db.query(UsageEvent).filter(UsageEvent.idempotency_key == key).first()
            return _row(existing) if existing is not None else None
        return _row(row)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()


def _row(r) -> Dict[str, Any]:
    return {
        "id": r.id, "owner": r.owner,
        "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        "source": r.source, "session_id": r.session_id, "task_run_id": r.task_run_id,
        "company_id": r.company_id, "company_agent_id": r.company_agent_id,
        "company_run_id": r.company_run_id, "endpoint_id": r.endpoint_id,
        "provider": r.provider, "model": r.model, "billing_type": r.billing_type,
        "input_tokens": r.input_tokens, "cached_input_tokens": r.cached_input_tokens,
        "output_tokens": r.output_tokens, "cost_usd": r.cost_usd,
        "cost_status": r.cost_status, "idempotency_key": r.idempotency_key,
    }


def tokens_from_usage(provider: Optional[str], usage: Dict[str, Any]) -> Dict[str, int]:
    """Normalize a provider usage dict (as llm_core emits it) to
    ``{input_tokens (uncached), cached_input_tokens, output_tokens}``.

    Anthropic reports fresh input separately from cache reads and writes;
    OpenAI-style providers include cache reads inside the prompt count.
    Anthropic cache writes are folded into input (billed at the input rate,
    slightly under their 1.25x price)."""
    usage = usage or {}
    raw_in = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    if provider == "anthropic":
        inp = raw_in + int(usage.get("cache_write_tokens") or 0)
    else:
        cached = min(cached, raw_in)
        inp = raw_in - cached
    return {"input_tokens": max(0, inp), "cached_input_tokens": max(0, cached), "output_tokens": max(0, out)}


# ── reading ─────────────────────────────────────────────────────────────

def month_window(month: Optional[str] = None, now: Optional[datetime] = None):
    """[start, end) of a UTC calendar month, ``YYYY-MM`` or this month."""
    now = now or utcnow()
    if month:
        y, m = (int(x) for x in month.split("-", 1))
    else:
        y, m = now.year, now.month
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), m % 12 + 1, 1)
    return start, end


def _base_query(db, owner: Optional[str], all_users: bool, start: datetime, end: datetime):
    from core.database import UsageEvent
    q = db.query(UsageEvent).filter(UsageEvent.occurred_at >= start, UsageEvent.occurred_at < end)
    if not all_users:
        q = q.filter(UsageEvent.owner == owner) if owner else q
    return q


def _agg_columns():
    from sqlalchemy import case, func
    from core.database import UsageEvent
    metered = case((UsageEvent.billing_type == "metered", UsageEvent.cost_usd), else_=0.0)
    free_tok = case((UsageEvent.billing_type != "metered",
                     UsageEvent.input_tokens + UsageEvent.cached_input_tokens + UsageEvent.output_tokens), else_=0)
    unpriced = case((UsageEvent.cost_status == "unpriced", 1), else_=0)
    return [
        func.count(UsageEvent.id).label("calls"),
        func.coalesce(func.sum(UsageEvent.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(UsageEvent.cached_input_tokens), 0).label("cached_input_tokens"),
        func.coalesce(func.sum(UsageEvent.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(metered), 0.0).label("metered_cost_usd"),
        func.coalesce(func.sum(free_tok), 0).label("free_tokens"),
        func.coalesce(func.sum(unpriced), 0).label("unpriced_calls"),
    ]


def _agg_dict(r) -> Dict[str, Any]:
    d = {
        "calls": int(r.calls or 0),
        "input_tokens": int(r.input_tokens or 0),
        "cached_input_tokens": int(r.cached_input_tokens or 0),
        "output_tokens": int(r.output_tokens or 0),
        "metered_cost_usd": round(float(r.metered_cost_usd or 0.0), 6),
        "free_tokens": int(r.free_tokens or 0),
        "unpriced_calls": int(r.unpriced_calls or 0),
    }
    d["total_tokens"] = d["input_tokens"] + d["cached_input_tokens"] + d["output_tokens"]
    d["metered_tokens"] = d["total_tokens"] - d["free_tokens"]
    return d


def _group(q, col, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = q.with_entities(col.label("key"), *_agg_columns()).group_by(col).all()
    out = [dict(_agg_dict(r), key=r.key) for r in rows]
    out.sort(key=lambda d: (d["metered_cost_usd"], d["total_tokens"]), reverse=True)
    return out[:limit] if limit else out


def summary(owner: Optional[str], *, all_users: bool = False, start: Optional[datetime] = None,
            end: Optional[datetime] = None, session_limit: int = 25) -> Dict[str, Any]:
    """Totals plus breakdowns for ``[start, end)`` (this UTC month by default).
    ``owner`` scopes to one user unless ``all_users`` (admins)."""
    from sqlalchemy import func
    from core.database import UsageEvent
    if start is None or end is None:
        s, e = month_window()
        start, end = start or s, end or e
    db = _session_factory()
    try:
        q = _base_query(db, owner, all_users, start, end)
        total = q.with_entities(*_agg_columns()).one()
        by_billing = _group(q, UsageEvent.billing_type)
        by_session = _group(q.filter(UsageEvent.session_id != None), UsageEvent.session_id, session_limit)  # noqa: E711
        _name_sessions(db, by_session)
        day = func.strftime("%Y-%m-%d", UsageEvent.occurred_at)
        daily_rows = q.with_entities(day.label("key"), *_agg_columns()).group_by(day).all()
        daily_map = {r.key: _agg_dict(r) for r in daily_rows}
        daily = []
        d = start
        _now = utcnow()
        last = min(end, datetime(_now.year, _now.month, _now.day) + timedelta(days=1))
        while d < last:
            k = d.strftime("%Y-%m-%d")
            daily.append(dict(daily_map.get(k) or _agg_dict(_Zero()), date=k))
            d += timedelta(days=1)
        return {
            "start": start.isoformat() + "Z", "end": end.isoformat() + "Z",
            "scope": "all" if all_users else (owner or "single-user"),
            "totals": _agg_dict(total),
            "by_billing_type": by_billing,
            "by_model": _group(q, UsageEvent.model),
            "by_provider": _group(q, UsageEvent.provider),
            "by_source": _group(q, UsageEvent.source),
            "by_session": by_session,
            "by_company": _group(q.filter(UsageEvent.company_id != None), UsageEvent.company_id),  # noqa: E711
            "by_company_agent": _group(q.filter(UsageEvent.company_agent_id != None), UsageEvent.company_agent_id),  # noqa: E711
            "by_owner": _group(q, UsageEvent.owner) if all_users else [],
            "daily": daily,
        }
    finally:
        db.close()


class _Zero:
    calls = input_tokens = cached_input_tokens = output_tokens = free_tokens = unpriced_calls = 0
    metered_cost_usd = 0.0


def _name_sessions(db, rows: List[Dict[str, Any]]) -> None:
    ids = [r["key"] for r in rows if r.get("key")]
    if not ids:
        return
    try:
        from core.database import Session as DBSession
        names = dict(db.query(DBSession.id, DBSession.name).filter(DBSession.id.in_(ids)).all())
    except Exception:
        names = {}
    for r in rows:
        r["name"] = names.get(r["key"])


def events(owner: Optional[str], *, all_users: bool = False, start: Optional[datetime] = None,
           end: Optional[datetime] = None, limit: int = 100) -> List[Dict[str, Any]]:
    from core.database import UsageEvent
    if start is None or end is None:
        s, e = month_window()
        start, end = start or s, end or e
    db = _session_factory()
    try:
        q = _base_query(db, owner, all_users, start, end)
        return [_row(r) for r in q.order_by(UsageEvent.occurred_at.desc()).limit(max(1, min(limit, 1000))).all()]
    finally:
        db.close()


def run_tokens(task_run_id: str) -> int:
    """Total tokens recorded for one task run (fills task_runs.tokens_used)."""
    from sqlalchemy import func
    from core.database import UsageEvent
    db = _session_factory()
    try:
        v = db.query(func.coalesce(func.sum(
            UsageEvent.input_tokens + UsageEvent.cached_input_tokens + UsageEvent.output_tokens), 0)
        ).filter(UsageEvent.task_run_id == task_run_id).scalar()
        return int(v or 0)
    finally:
        db.close()
