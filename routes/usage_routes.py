"""Usage and cost: GET /api/pricing (the shared price table, src/pricing.py),
GET /api/usage/* (src/usage.py summaries, owner-scoped; admins may ask for
everyone with all=1) and GET /api/usage/budget (src/budgets.py)."""
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src.auth_helpers import require_user


def _is_admin(request: Request, user: str) -> bool:
    if not user:
        return True  # single-user / auth off: everything is the operator's
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    try:
        return bool(auth_mgr and auth_mgr.is_admin(user))
    except Exception:
        return False


def _window(month: Optional[str], start: Optional[str], end: Optional[str]):
    from src import usage
    try:
        if start or end:
            s = datetime.fromisoformat(start) if start else usage.month_window()[0]
            e = datetime.fromisoformat(end) if end else usage.utcnow()
            s, e = s.replace(tzinfo=None), e.replace(tzinfo=None)
            if e <= s:
                raise ValueError("end must be after start")
            return s, e
        return usage.month_window(month)
    except ValueError as exc:
        raise HTTPException(400, f"Bad window: {exc}")


def _scope(request: Request, all_users: bool):
    user = require_user(request)
    if all_users and not _is_admin(request, user):
        raise HTTPException(403, "Admin only")
    return user, all_users


def setup_usage_routes() -> APIRouter:
    router = APIRouter(tags=["usage"])

    @router.get("/api/pricing")
    async def pricing(request: Request):
        require_user(request)
        from src import pricing as _p
        return _p.pricing_table()

    @router.put("/api/pricing/overrides")
    async def set_overrides(request: Request):
        """Replace the per-endpoint price overrides (admin). Body:
        ``{"<endpoint id or base url>": {"billing_type"?, "input"?, "cached"?, "output"?}}``."""
        require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected an object")
        clean = {}
        for k, v in body.items():
            if not isinstance(v, dict):
                raise HTTPException(400, f"{k}: expected an object")
            entry = {}
            if v.get("billing_type") is not None:
                if v["billing_type"] not in ("local_free", "subscription", "metered"):
                    raise HTTPException(400, f"{k}: bad billing_type")
                entry["billing_type"] = v["billing_type"]
            for f in ("input", "cached", "output"):
                if v.get(f) is not None:
                    try:
                        n = float(v[f])
                    except (TypeError, ValueError):
                        raise HTTPException(400, f"{k}.{f}: expected a number")
                    if n < 0:
                        raise HTTPException(400, f"{k}.{f}: must be >= 0")
                    entry[f] = n
            clean[str(k)] = entry
        from src.settings import load_settings, save_settings
        settings = dict(load_settings())
        settings["endpoint_pricing"] = clean
        save_settings(settings)
        return {"ok": True, "endpoint_overrides": clean}

    @router.get("/api/usage/summary")
    async def usage_summary(request: Request, month: Optional[str] = None,
                            start: Optional[str] = None, end: Optional[str] = None,
                            all: bool = False):
        user, all_users = _scope(request, all)
        s, e = _window(month, start, end)
        from src import usage
        admin = _is_admin(request, user)
        out = await asyncio.to_thread(usage.summary, user, all_users=all_users, start=s, end=e,
                                      include_fixed=admin)
        out["can_view_all"] = admin
        return out

    @router.get("/api/usage/subscriptions")
    async def get_subscriptions(request: Request):
        """Subscription plan prices and limits, budget caps, and the
        subscription endpoints they can apply to (src/subscriptions.py)."""
        user = require_user(request)
        from src import subscriptions, usage

        def load():
            db = usage._session_factory()
            try:
                return subscriptions.subscription_endpoints(db)
            finally:
                db.close()
        return {
            "plans": subscriptions.get_plans(),
            "caps": subscriptions.get_caps(),
            "count_subscription_allocation": subscriptions.count_allocation_default(),
            "endpoints": await asyncio.to_thread(load),
            "can_edit": _is_admin(request, user),
        }

    @router.put("/api/usage/subscriptions")
    async def set_subscriptions(request: Request):
        """Save subscription settings (admin). Body, every key optional:
        ``{"plans": {"<endpoint id | base url | provider>": {"name"?, "monthly_usd",
        "requests_per_day"?, "requests_per_5h"?, "tokens_per_month"?}},
        "caps": {"<scope_type>[:<scope_id>]": {"requests_per_day"?, "requests_per_month"?,
        "tokens_per_day"?, "tokens_per_month"?}}, "count_subscription_allocation": bool}``.
        ``plans`` and ``caps`` replace the stored maps."""
        require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected an object")
        from src import budgets, subscriptions
        from src.settings import load_settings, save_settings
        settings = dict(load_settings())
        try:
            if "plans" in body:
                if not isinstance(body["plans"], dict):
                    raise ValueError("plans: expected an object")
                settings["subscription_plans"] = {
                    str(k).strip(): subscriptions.clean_plan(str(k), v)
                    for k, v in body["plans"].items() if str(k).strip()}
            if "caps" in body:
                if not isinstance(body["caps"], dict):
                    raise ValueError("caps: expected an object")
                caps = {}
                for k, v in body["caps"].items():
                    k = str(k).strip()
                    if k.split(":", 1)[0] not in budgets.SCOPE_COLUMNS:
                        raise ValueError(f"{k}: unknown scope_type")
                    c = subscriptions.clean_caps(k, v)
                    if c:
                        caps[k] = c
                settings["budget_caps"] = caps
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if "count_subscription_allocation" in body:
            settings["budget_count_subscription_allocation"] = bool(body["count_subscription_allocation"])
        save_settings(settings)
        return {"ok": True, "plans": settings.get("subscription_plans", {}),
                "caps": settings.get("budget_caps", {}),
                "count_subscription_allocation": settings.get("budget_count_subscription_allocation", True)}

    @router.get("/api/usage/events")
    async def usage_events(request: Request, month: Optional[str] = None,
                           start: Optional[str] = None, end: Optional[str] = None,
                           all: bool = False, limit: int = 100):
        user, all_users = _scope(request, all)
        s, e = _window(month, start, end)
        from src import usage
        return {"events": await asyncio.to_thread(usage.events, user, all_users=all_users,
                                                  start=s, end=e, limit=limit)}

    @router.get("/api/usage/budget")
    async def usage_budget(request: Request, scope_type: str = "owner", scope_id: Optional[str] = None,
                           limit_cents: Optional[float] = None):
        """Where a scope stands against a monthly limit, for the caller's rows."""
        user = require_user(request)
        from src import budgets
        if scope_type not in budgets.SCOPE_COLUMNS:
            raise HTTPException(400, "Unknown scope_type")
        return await asyncio.to_thread(budgets.check, scope_type, scope_id, user or None, limit_cents)

    return router
