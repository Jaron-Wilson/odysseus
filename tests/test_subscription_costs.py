"""Subscription plan costs: src/subscriptions.py allocation, the Usage
summary's subscription block and totals, and src/budgets.py counting
subscription allocation and usage caps. No real model calls."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, CompanyAgent, CompanyCompany, ModelEndpoint
from src import budgets, pricing, subscriptions, usage

PLAN_URL = "https://api.openai.com/v1/chatgpt-plan"
OPENAI = "https://api.openai.com/v1"
LOCAL = "http://localhost:8000/v1"

# Mid-month, so the current month is about half elapsed.
NOW = datetime(2026, 10, 16, 12, 0, 0)
START, END = usage.month_window(now=NOW)


@pytest.fixture
def settings(monkeypatch):
    store = {}
    monkeypatch.setattr(subscriptions, "_get", lambda key, default: store.get(key, default))
    return store


@pytest.fixture
def db(monkeypatch, settings):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(usage, "_session_factory", factory)
    monkeypatch.setattr(usage, "utcnow", lambda: NOW)
    monkeypatch.setattr(pricing, "_settings_overrides", lambda: {})
    monkeypatch.setattr(budgets, "_setting_pct", lambda key, default: default)
    yield factory
    engine.dispose()


def _plan_call(tokens, owner="jaron", hours_ago=1, **kw):
    return usage.record_usage(endpoint_url=PLAN_URL, model="gpt-5.6-luna", input_tokens=tokens,
                              owner=owner, occurred_at=NOW - timedelta(hours=hours_ago), **kw)


def _elapsed():
    return (NOW - START).total_seconds() / (END - START).total_seconds()


# ── math ────────────────────────────────────────────────────────────────

class TestAllocationMath:
    def test_time_share_splits_accrued_by_tokens(self):
        plan = {"monthly_usd": 20.0}
        assert subscriptions.accrued_usd(plan, 1000, 0.5) == pytest.approx(10.0)
        assert subscriptions.allocate(plan, 250, 1000, 0.5) == pytest.approx(2.5)
        assert subscriptions.allocate(plan, 750, 1000, 0.5) == pytest.approx(7.5)
        assert subscriptions.allocate(plan, 1000, 1000, 1.0) == pytest.approx(20.0)

    def test_unused_or_unpriced_plan_allocates_nothing(self):
        assert subscriptions.accrued_usd({"monthly_usd": 20}, 0, 0.5) == 0
        assert subscriptions.allocate({"monthly_usd": 20}, 0, 1000, 0.5) == 0
        assert subscriptions.allocate({}, 500, 1000, 0.5) == 0

    def test_capacity_method_with_tokens_per_month(self):
        plan = {"monthly_usd": 20.0, "tokens_per_month": 10_000_000}
        assert subscriptions.plan_method(plan) == "capacity"
        # 1M of a 10M plan = $2, regardless of how much of the month passed.
        assert subscriptions.allocate(plan, 1_000_000, 1_000_000, 0.1) == pytest.approx(2.0)
        assert subscriptions.allocate(plan, 1_000_000, 1_000_000, 0.9) == pytest.approx(2.0)
        # Over capacity: shares of the plan price, never more than the price.
        assert subscriptions.allocate(plan, 10_000_000, 20_000_000, 0.5) == pytest.approx(10.0)
        assert subscriptions.accrued_usd(plan, 20_000_000, 0.5) == pytest.approx(20.0)

    def test_elapsed_fraction(self):
        assert subscriptions.elapsed_fraction(START, END, START - timedelta(days=1)) == 0
        assert subscriptions.elapsed_fraction(START, END, END + timedelta(days=1)) == 1
        assert subscriptions.elapsed_fraction(START, END, NOW) == pytest.approx(0.5)

    def test_plan_matching_order(self):
        plans = {"ep1": {}, PLAN_URL: {}, "chatgpt-plan": {}}
        bases = {"ep1": PLAN_URL, "ep2": PLAN_URL, "ep3": "https://other"}
        assert subscriptions.plan_key_for("ep1", "chatgpt-plan", plans, bases) == "ep1"
        assert subscriptions.plan_key_for("ep2", "chatgpt-plan", plans, bases) == PLAN_URL
        assert subscriptions.plan_key_for("ep3", "chatgpt-plan", plans, bases) == "chatgpt-plan"
        assert subscriptions.plan_key_for("ep3", "copilot", plans, bases) is None

    def test_clean_plan_and_caps(self):
        assert subscriptions.clean_plan("k", {"monthly_usd": "20", "requests_per_5h": "80"}) == \
            {"monthly_usd": 20.0, "requests_per_5h": 80}
        with pytest.raises(ValueError):
            subscriptions.clean_plan("k", {"monthly_usd": -1})
        assert subscriptions.clean_caps("k", {"requests_per_day": "50", "tokens_per_day": 0}) == {"requests_per_day": 50}


# ── recording stays $0, local stays free ────────────────────────────────

def test_chatgpt_plan_rows_are_subscription_and_local_is_free(db, settings):
    settings["subscription_plans"] = {"chatgpt-plan": {"monthly_usd": 20}, LOCAL: {"monthly_usd": 99}}
    r = _plan_call(1000)
    assert (r["billing_type"], r["provider"], r["cost_usd"]) == ("subscription", "chatgpt-plan", 0)
    loc = usage.record_usage(endpoint_url=LOCAL, model="qwen3", input_tokens=5000, owner="jaron",
                             occurred_at=NOW - timedelta(hours=1))
    assert loc["billing_type"] == "local_free" and loc["cost_usd"] == 0
    s = usage.summary("jaron", include_fixed=False)
    # A plan keyed by a local URL never picks up local rows: they are not subscription rows.
    local_plan = next(p for p in s["subscription"]["plans"] if p["key"] == LOCAL)
    assert local_plan["tokens"] == 0 and local_plan["allocated_usd"] == 0


# ── summary ─────────────────────────────────────────────────────────────

def _seed_company(db):
    with db() as s:
        s.add(CompanyCompany(id="co", name="Co", owner="jaron", issue_prefix="CO"))
        s.flush()
        s.add(CompanyAgent(id="qa", company_id="co", name="QA", owner="jaron"))
        s.commit()
    _plan_call(600, source="company", company_id="co", company_agent_id="qa")
    _plan_call(300, session_id="chat1")
    _plan_call(100, owner="bob", source="task")
    usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=400_000, owner="jaron",
                       occurred_at=NOW - timedelta(hours=1))  # $1.00 metered


class TestSummary:
    def test_subscription_block_and_totals_admin(self, db, settings):
        settings["subscription_plans"] = {"chatgpt-plan": {"name": "ChatGPT Plus", "monthly_usd": 20,
                                                           "requests_per_5h": 80}}
        _seed_company(db)
        s = usage.summary("admin", all_users=True)
        sub = s["subscription"]
        (plan,) = sub["plans"]
        assert plan["name"] == "ChatGPT Plus" and plan["configured"]
        assert (plan["requests"], plan["tokens"], plan["plan_tokens"]) == (3, 1000, 1000)
        accrued = 20 * _elapsed()
        assert plan["accrued_usd"] == pytest.approx(accrued, rel=1e-4)
        assert plan["allocated_usd"] == pytest.approx(accrued, rel=1e-4)
        src = {r["key"]: r for r in plan["by_source"]}
        assert src["company"]["share"] == pytest.approx(0.6)
        assert src["company"]["allocated_usd"] == pytest.approx(accrued * 0.6, rel=1e-4)
        assert src["chat"]["allocated_usd"] == pytest.approx(accrued * 0.3, rel=1e-4)
        assert sum(r["allocated_usd"] for r in plan["by_source"]) == pytest.approx(accrued, rel=1e-4)
        (agent,) = plan["by_company_agent"]
        assert (agent["key"], agent["name"]) == ("qa", "QA")
        assert {r["key"] for r in plan["by_owner"]} == {"jaron", "bob"}
        assert plan["limit_usage"]["requests_per_5h"] == {"used": 3, "limit": 80, "pct": 3.8}
        # Monthly total: metered + full plan price for an admin view.
        assert s["cost"] == {"metered_usd": pytest.approx(1.0), "subscription_usd": 20.0,
                             "subscription_basis": "plan_price", "total_usd": pytest.approx(21.0)}
        assert s["by_company_agent"][0]["name"] == "QA"

    def test_owner_view_uses_allocated_share(self, db, settings):
        settings["subscription_plans"] = {"chatgpt-plan": {"monthly_usd": 20}}
        _seed_company(db)
        s = usage.summary("bob", include_fixed=False)
        (plan,) = s["subscription"]["plans"]
        assert plan["tokens"] == 100 and plan["plan_tokens"] == 1000
        assert plan["by_owner"] == []
        share = 20 * _elapsed() * 0.1
        assert s["cost"]["subscription_basis"] == "allocated"
        assert s["cost"]["subscription_usd"] == pytest.approx(share, rel=1e-4)
        assert s["cost"]["total_usd"] == pytest.approx(share, rel=1e-4)

    def test_unconfigured_subscription_endpoint_is_listed(self, db, settings):
        _plan_call(500)
        s = usage.summary("jaron", include_fixed=True)
        assert s["subscription"]["unconfigured"] == ["chatgpt-plan"]
        assert s["subscription"]["plans"][0]["allocated_usd"] == 0
        assert s["cost"]["subscription_usd"] == 0

    def test_plan_keyed_by_endpoint_id(self, db, settings):
        with db() as s:
            s.add(ModelEndpoint(id="ep-plan", name="ChatGPT", base_url=PLAN_URL))
            s.commit()
        settings["subscription_plans"] = {"ep-plan": {"monthly_usd": 200}}
        _plan_call(1000)
        (plan,) = usage.summary("jaron", include_fixed=True)["subscription"]["plans"]
        assert plan["key"] == "ep-plan" and plan["tokens"] == 1000


# ── budgets ─────────────────────────────────────────────────────────────

class TestSubscriptionBudgets:
    def test_existing_keys_and_no_plan_means_metered_only(self, db, settings):
        _plan_call(5_000_000, company_agent_id="qa")
        b = budgets.check("company_agent", "qa", "jaron", 100, now=NOW)
        assert b["spent_cents"] == 0 and b["state"] == "ok"
        assert b["subscription_requests"] == 1 and b["subscription_tokens"] == 5_000_000
        for k in ("spent_cents", "limit_cents", "pct", "state", "warn_pct", "hard_stop_pct",
                  "period_start", "period_end", "tokens", "unpriced_calls"):
            assert k in b

    def test_allocation_counts_toward_money_limit(self, db, settings):
        settings["subscription_plans"] = {"chatgpt-plan": {"monthly_usd": 20}}
        _plan_call(750, company_agent_id="qa", source="company")
        _plan_call(250, session_id="chat1")
        alloc_cents = 2000 * _elapsed() * 0.75  # about 726 cents
        b = budgets.check("company_agent", "qa", "jaron", 1000, now=NOW)
        assert b["subscription_cents_allocated"] == pytest.approx(alloc_cents, rel=1e-4)
        assert b["metered_cents"] == 0
        assert b["spent_cents"] == pytest.approx(alloc_cents, rel=1e-4)
        assert b["state"] == "ok" and b["money_state"] == "ok"
        assert budgets.check("company_agent", "qa", "jaron", 900, now=NOW)["state"] == "warn"
        stop = budgets.check("company_agent", "qa", "jaron", 500, now=NOW)
        assert stop["state"] == "hard_stop" and stop["stopped_by"] == ["money"]
        # Turned off: back to metered only.
        off = budgets.check("company_agent", "qa", "jaron", 500, now=NOW, count_subscription_allocation=False)
        assert off["spent_cents"] == 0 and off["state"] == "ok"
        assert off["subscription_cents_allocated"] == pytest.approx(alloc_cents, rel=1e-4)
        settings["budget_count_subscription_allocation"] = False
        assert budgets.check("company_agent", "qa", "jaron", 500, now=NOW)["state"] == "ok"

    def test_metered_plus_allocation(self, db, settings):
        settings["subscription_plans"] = {"chatgpt-plan": {"monthly_usd": 10, "tokens_per_month": 1_000_000}}
        _plan_call(100_000, company_agent_id="qa")  # 10% of capacity = $1.00
        usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=200_000, owner="jaron",
                           company_agent_id="qa", occurred_at=NOW - timedelta(hours=1))  # $0.50
        b = budgets.check("company_agent", "qa", "jaron", 200, now=NOW)
        assert b["metered_cents"] == pytest.approx(50)
        assert b["subscription_cents_allocated"] == pytest.approx(100)
        assert b["spent_cents"] == pytest.approx(150) and b["state"] == "ok"
        assert budgets.check("company_agent", "qa", "jaron", 150, now=NOW)["state"] == "hard_stop"

    def test_request_cap_stops_agent(self, db, settings):
        for i in range(4):
            _plan_call(10, company_agent_id="qa", hours_ago=i + 1)
        _plan_call(10, company_agent_id="qa", hours_ago=24 * 3)  # earlier this month, not today
        usage.record_usage(endpoint_url=LOCAL, model="qwen3", input_tokens=10, owner="jaron",
                           company_agent_id="qa", occurred_at=NOW - timedelta(hours=1))  # local: never capped
        b = budgets.check("company_agent", "qa", "jaron", None, now=NOW, caps={"requests_per_day": 5})
        assert b["subscription_requests_today"] == 4 and b["subscription_requests"] == 5
        assert b["state"] == "warn" and b["money_state"] == "ok"
        assert b["caps"] == [{"name": "requests_per_day", "limit": 5, "used": 4, "pct": 80.0, "state": "warn"}]
        b = budgets.check("company_agent", "qa", "jaron", None, now=NOW, caps={"requests_per_day": 4})
        assert b["state"] == "hard_stop" and b["stopped_by"] == ["requests_per_day"]
        b = budgets.check("company_agent", "qa", "jaron", None, now=NOW, caps={"requests_per_month": 5})
        assert b["state"] == "hard_stop"

    def test_caps_from_settings(self, db, settings):
        _plan_call(1000, company_agent_id="qa")
        settings["budget_caps"] = {"company_agent": {"tokens_per_month": 100_000},
                                   "company_agent:qa": {"tokens_per_day": 1000}}
        b = budgets.check("company_agent", "qa", "jaron", 0, now=NOW)
        assert [c["name"] for c in b["caps"]] == ["tokens_per_day"]
        assert b["state"] == "hard_stop"
        other = budgets.check("company_agent", "other", "jaron", 0, now=NOW)
        assert [c["name"] for c in other["caps"]] == ["tokens_per_month"] and other["state"] == "ok"
        assert budgets.check("company_agent", "qa", "jaron", 0, now=NOW, caps={})["state"] == "ok"


# ── routes ──────────────────────────────────────────────────────────────

def test_subscription_routes(db, settings, monkeypatch):
    from routes import usage_routes
    saved = {}
    monkeypatch.setattr(usage_routes, "require_user", lambda request: "")
    monkeypatch.setattr(usage_routes, "require_admin", lambda request: None)
    import src.settings as st
    monkeypatch.setattr(st, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(st, "save_settings", lambda s: (saved.update(s), settings.update(s)))
    app = FastAPI()
    app.include_router(usage_routes.setup_usage_routes())
    c = TestClient(app)
    r = c.put("/api/usage/subscriptions", json={
        "plans": {"chatgpt-plan": {"monthly_usd": "20", "requests_per_5h": 80}},
        "caps": {"company_agent:qa": {"requests_per_day": 50}},
        "count_subscription_allocation": False})
    assert r.status_code == 200, r.text
    assert saved["subscription_plans"] == {"chatgpt-plan": {"monthly_usd": 20.0, "requests_per_5h": 80}}
    assert saved["budget_caps"] == {"company_agent:qa": {"requests_per_day": 50}}
    assert saved["budget_count_subscription_allocation"] is False
    assert c.put("/api/usage/subscriptions", json={"caps": {"planet:x": {"requests_per_day": 1}}}).status_code == 400
    assert c.put("/api/usage/subscriptions", json={"plans": {"x": {"monthly_usd": -5}}}).status_code == 400
    g = c.get("/api/usage/subscriptions").json()
    assert g["plans"]["chatgpt-plan"]["monthly_usd"] == 20.0 and g["can_edit"] is True
    _plan_call(1000)
    s = c.get("/api/usage/summary").json()
    assert s["cost"]["subscription_usd"] == 20.0 and s["subscription"]["plans"][0]["key"] == "chatgpt-plan"
