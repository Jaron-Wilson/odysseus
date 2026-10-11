"""Usage and cost tracking: src/pricing.py, src/usage.py, src/budgets.py and
the llm_core recording hook. No real model calls."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ModelEndpoint, Session as DBSession, UsageEvent
from src import budgets, pricing, usage


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(usage, "_session_factory", factory)
    monkeypatch.setattr(pricing, "_settings_overrides", lambda: {})
    monkeypatch.setattr(budgets, "_setting_pct", lambda key, default: default)
    yield factory
    engine.dispose()


ANTHROPIC = "https://api.anthropic.com/v1"
OPENAI = "https://api.openai.com/v1"
OPENROUTER = "https://openrouter.ai/api/v1"


# ── pricing ─────────────────────────────────────────────────────────────

class TestPricing:
    def test_known_model_priced(self):
        r = pricing.price_usage(base_url=OPENAI, model="gpt-4o", input_tokens=1_000_000, output_tokens=1_000_000)
        assert r["billing_type"] == "metered"
        assert r["cost_status"] == "priced"
        assert r["cost_usd"] == pytest.approx(12.50)

    def test_most_specific_key_wins(self):
        assert pricing.match_model_key("gpt-4o-mini-2024-07-18") == "gpt-4o-mini"
        assert pricing.match_model_key("claude-opus-4-8") == "claude-opus-4-8"
        assert pricing.match_model_key("claude-opus-4-1-20250805") == "claude-opus-4"

    def test_cached_input_price(self):
        r = pricing.price_usage(base_url=ANTHROPIC, model="claude-sonnet-4-5", input_tokens=0,
                                cached_input_tokens=1_000_000, output_tokens=0)
        assert r["cost_usd"] == pytest.approx(0.30)

    def test_openrouter_dotted_names_and_unknown(self):
        r = pricing.price_usage(base_url=OPENROUTER, model="anthropic/claude-sonnet-4.5",
                                input_tokens=1_000_000)
        assert (r["provider"], r["cost_status"]) == ("openrouter", "priced")
        assert r["cost_usd"] == pytest.approx(3.0)
        r = pricing.price_usage(base_url=OPENROUTER, model="openrouter/auto", input_tokens=1000)
        assert r["billing_type"] == "metered" and r["cost_status"] == "unpriced" and r["cost_usd"] == 0
        r = pricing.price_usage(base_url=OPENROUTER, model="meta-llama/llama-3.3-70b:free", input_tokens=1000)
        assert r["cost_status"] == "free" and r["cost_usd"] == 0

    @pytest.mark.parametrize("url", [
        "http://localhost:8080/v1", "http://127.0.0.1:11434", "http://192.168.1.20:8000/v1",
        "http://100.101.102.103:8000/v1", "http://llamaswap:8080/v1", "http://box.local/v1",
    ])
    def test_local_is_free(self, url):
        r = pricing.price_usage(base_url=url, model="qwen2.5-coder-32b", input_tokens=5000, output_tokens=500)
        assert r["billing_type"] == "local_free"
        assert r["cost_status"] == "free" and r["cost_usd"] == 0

    def test_endpoint_kind_overrides_host(self):
        assert pricing.classify("http://100.80.1.2:4000/v1", provider="openai", endpoint_kind="proxy") == "metered"
        assert pricing.classify("https://my-gpu.example.com/v1", provider="openai", endpoint_kind="local") == "local_free"

    @pytest.mark.parametrize("provider", ["chatgpt-subscription", "chatgpt-plan", "copilot"])
    def test_subscription_is_zero_but_counted(self, provider):
        r = pricing.price_usage(base_url="https://chatgpt.com/backend-api/codex", model="gpt-5",
                                provider=provider, input_tokens=10_000, output_tokens=1000)
        assert r["billing_type"] == "subscription"
        assert r["cost_usd"] == 0 and r["cost_status"] == "free"

    def test_unpriced_model(self):
        r = pricing.price_usage(base_url=OPENAI, model="totally-new-model-9", input_tokens=10)
        assert r["cost_status"] == "unpriced" and r["billing_type"] == "metered"

    def test_endpoint_override(self):
        ov = {"ep1": {"input": 1.0, "output": 2.0}, "https://gpu.example.com/v1": {"billing_type": "local_free"}}
        r = pricing.price_usage(base_url="http://10.0.0.5:8000/v1", model="mystery", endpoint_id="ep1",
                                input_tokens=1_000_000, output_tokens=1_000_000, overrides=ov)
        assert r["billing_type"] == "metered" and r["cost_usd"] == pytest.approx(3.0)
        r = pricing.price_usage(base_url="https://gpu.example.com/v1/chat/completions", model="gpt-4o",
                                input_tokens=1000, overrides=ov)
        assert r["billing_type"] == "local_free" and r["cost_usd"] == 0

    def test_no_endpoint_is_unknown(self):
        r = pricing.price_usage(base_url=None, model="gpt-4o", input_tokens=10)
        assert r["billing_type"] == "unknown" and r["cost_status"] == "unpriced"

    def test_table_shape(self):
        t = pricing.pricing_table()
        assert t["models"]["gpt-4o"] == {"input": 2.5, "cached": 1.25, "output": 10.0}
        assert "copilot" in t["subscription_providers"]


# ── recording ───────────────────────────────────────────────────────────

class TestRecordUsage:
    def test_records_and_prices(self, db):
        row = usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=1000,
                                 output_tokens=500, owner="alice", session_id="s1", idempotency_key="k1")
        assert row["source"] == "chat"
        assert row["cost_status"] == "priced"
        assert row["cost_usd"] == pytest.approx((1000 * 2.5 + 500 * 10) / 1e6)

    def test_idempotent(self, db):
        a = usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=10, idempotency_key="same")
        b = usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=99, idempotency_key="same")
        assert a["id"] == b["id"] and b["input_tokens"] == 10
        s = db()
        assert s.query(UsageEvent).count() == 1
        s.close()

    def test_nothing_to_record(self, db):
        assert usage.record_usage(endpoint_url=OPENAI, model="gpt-4o") is None

    def test_failure_is_isolated(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(usage, "_session_factory", boom)
        assert usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=5) is None

    def test_scope_attribution_and_explicit_wins(self, db):
        with usage.usage_scope(source="task", task_run_id="run-1", owner="bob"):
            with usage.usage_scope(company_id="co-1"):
                row = usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=5)
            row2 = usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=5, source="other")
        assert (row["source"], row["task_run_id"], row["owner"], row["company_id"]) == ("task", "run-1", "bob", "co-1")
        assert row2["source"] == "other" and row2["company_id"] is None
        assert usage.current_scope() == {}

    def test_owner_from_session_and_endpoint_id(self, db):
        s = db()
        s.add(DBSession(id="sess-9", name="Chat", owner="carol", endpoint_url="", model=""))
        s.add(ModelEndpoint(id="ep-local", name="Box", base_url="https://gpu.example.com/v1",
                            endpoint_kind="local", owner=None))
        s.commit()
        s.close()
        row = usage.record_usage(endpoint_url="https://gpu.example.com/v1/chat/completions", model="gpt-4o",
                                 input_tokens=100, session_id="sess-9")
        assert row["owner"] == "carol"
        assert row["endpoint_id"] == "ep-local"
        assert row["billing_type"] == "local_free"

    def test_provider_label_for_local_openai_compatible(self, db):
        row = usage.record_usage(endpoint_url="http://localhost:8080/v1", model="qwen3", input_tokens=5,
                                 provider="openai")
        assert row["provider"] == "openai-compatible"
        row = usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=5, provider="openai")
        assert row["provider"] == "openai"

    def test_tokens_from_usage(self):
        assert usage.tokens_from_usage("openai", {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}) == \
            {"input_tokens": 60, "cached_input_tokens": 40, "output_tokens": 5}
        assert usage.tokens_from_usage("anthropic", {"input_tokens": 10, "cached_input_tokens": 900,
                                                     "cache_write_tokens": 50, "output_tokens": 5}) == \
            {"input_tokens": 60, "cached_input_tokens": 900, "output_tokens": 5}


class TestStreamHook:
    def test_stream_llm_records_last_usage_once(self, db, monkeypatch):
        from src import llm_core

        async def fake_inner(url, model, messages, *a, **kw):
            yield 'data: {"delta": "hi"}\n\n'
            yield 'data: ' + json.dumps({"type": "usage", "data": {"input_tokens": 5, "output_tokens": 1}}) + '\n\n'
            yield 'data: ' + json.dumps({"type": "usage", "data": {"input_tokens": 50, "output_tokens": 10}}) + '\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(llm_core, "_stream_llm_inner", fake_inner)

        async def run():
            return [c async for c in llm_core.stream_llm(OPENAI, "gpt-4o-mini", [], session_id="s-x", owner="dave")]

        chunks = asyncio.run(run())
        assert chunks[-1] == "data: [DONE]\n\n"
        s = db()
        rows = s.query(UsageEvent).all()
        assert len(rows) == 1
        assert (rows[0].input_tokens, rows[0].output_tokens, rows[0].owner, rows[0].session_id) == (50, 10, "dave", "s-x")
        assert rows[0].model == "gpt-4o-mini" and rows[0].cost_status == "priced"
        s.close()

    def test_stream_survives_recording_failure(self, monkeypatch):
        from src import llm_core

        async def fake_inner(url, model, messages, *a, **kw):
            yield 'data: ' + json.dumps({"type": "usage", "data": {"input_tokens": 5, "output_tokens": 1}}) + '\n\n'
            yield "data: [DONE]\n\n"

        def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(llm_core, "_stream_llm_inner", fake_inner)
        monkeypatch.setattr(usage, "_session_factory", boom)

        async def run():
            return [c async for c in llm_core.stream_llm(OPENAI, "gpt-4o", [])]

        assert asyncio.run(run())[-1] == "data: [DONE]\n\n"


# ── summaries ───────────────────────────────────────────────────────────

def _seed(now):
    usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=1_000_000, owner="alice",
                       session_id="a1", occurred_at=now - timedelta(hours=1))
    usage.record_usage(endpoint_url="http://localhost:8000/v1", model="qwen3-32b", input_tokens=2000,
                       output_tokens=100, owner="alice", source="task", occurred_at=now - timedelta(hours=2))
    usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", output_tokens=1_000_000, owner="bob",
                       occurred_at=now - timedelta(hours=1), company_id="co", company_agent_id="ag")
    # Last month: outside the default window.
    usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=1_000_000, owner="alice",
                       occurred_at=usage.month_window(now=now)[0] - timedelta(days=1))


class TestSummary:
    def test_owner_scoped(self, db):
        now = usage.utcnow()
        _seed(now)
        s = usage.summary("alice")
        t = s["totals"]
        assert t["calls"] == 2
        assert t["metered_cost_usd"] == pytest.approx(2.5)
        assert t["free_tokens"] == 2100
        assert {r["key"] for r in s["by_source"]} == {"chat", "task"}
        assert {r["key"] for r in s["by_billing_type"]} == {"metered", "local_free"}
        assert s["by_session"][0]["key"] == "a1"
        assert s["by_company"] == []
        assert sum(d["calls"] for d in s["daily"]) == 2
        assert s["daily"][0]["date"] == usage.month_window()[0].strftime("%Y-%m-%d")

    def test_admin_all_users(self, db):
        _seed(usage.utcnow())
        s = usage.summary("admin", all_users=True)
        assert s["totals"]["calls"] == 3
        assert s["totals"]["metered_cost_usd"] == pytest.approx(12.5)
        assert {r["key"] for r in s["by_owner"]} == {"alice", "bob"}
        assert s["by_company"][0]["key"] == "co" and s["by_company_agent"][0]["key"] == "ag"

    def test_other_user_sees_nothing_of_alice(self, db):
        _seed(usage.utcnow())
        s = usage.summary("mallory")
        assert s["totals"]["calls"] == 0

    def test_month_window(self):
        assert usage.month_window("2026-12") == (datetime(2026, 12, 1), datetime(2027, 1, 1))


# ── budgets ─────────────────────────────────────────────────────────────

class TestBudgets:
    @pytest.mark.parametrize("spent,limit,state", [
        (0, 1000, "ok"), (799, 1000, "ok"), (800, 1000, "warn"), (999.9, 1000, "warn"),
        (1000, 1000, "hard_stop"), (5000, 1000, "hard_stop"), (5000, None, "ok"), (5000, 0, "ok"),
    ])
    def test_state_for(self, spent, limit, state):
        assert budgets.state_for(spent, limit, 80.0, 100.0)[1] == state

    def test_check_counts_only_metered(self, db):
        usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=200_000, owner="alice",
                           company_agent_id="ag1")  # $0.50
        usage.record_usage(endpoint_url="http://localhost:1234/v1", model="gpt-4o", input_tokens=9_000_000,
                           owner="alice", company_agent_id="ag1")
        usage.record_usage(endpoint_url="https://chatgpt.com/backend-api/codex", model="gpt-5",
                           provider="chatgpt-subscription", input_tokens=9_000_000, owner="alice",
                           company_agent_id="ag1")
        b = budgets.check("company_agent", "ag1", "alice", 60)
        assert b["spent_cents"] == pytest.approx(50.0)
        assert b["state"] == "warn" and b["pct"] == pytest.approx(83.33, abs=0.01)
        assert b["tokens"]["input"] == 18_200_000
        assert budgets.check("company_agent", "ag1", "alice", 50)["state"] == "hard_stop"
        assert budgets.check("company_agent", "ag1", "alice", 1000)["state"] == "ok"
        assert budgets.check("company_agent", "other", "alice", 1)["spent_cents"] == 0

    def test_configurable_thresholds(self, db):
        usage.record_usage(endpoint_url=OPENAI, model="gpt-4o", input_tokens=200_000, owner="a", company_id="c")
        assert budgets.check("company", "c", None, 100, warn_pct=40, hard_stop_pct=60)["state"] == "warn"
        assert budgets.check("company", "c", None, 100, warn_pct=40, hard_stop_pct=50)["state"] == "hard_stop"

    def test_unknown_scope(self, db):
        with pytest.raises(ValueError):
            budgets.check("planet", "x", None, 100)
