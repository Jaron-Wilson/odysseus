"""Agent runs, model choice and usage scopes for mods (src/mods/runs.py,
src/mods/llm.py, src/mods/usage_api.py). No real model calls: the agent loop,
the model resolution and the session manager are fakes."""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, UsageEvent
from src import budgets, pricing, usage
from src.mods import events, routes, runs, tools as mod_tools, usage_api
from src.mods import llm as mod_llm
from tests.mods_helpers import add_mod, mods_dir  # noqa: F401

from tests.helpers.sqlite_db import make_temp_sqlite  # noqa: E402

# Imported now, before any test swaps core.database.SessionLocal: these bind
# `from core.database import SessionLocal` at import, and binding the swapped
# one would leak this module's database into later test modules.
import importlib  # noqa: E402

for _m in ("routes.model_routes", "routes.session_routes", "src.agent_loop", "src.agent_runs",
           "src.ai_interaction", "src.chat_queue", "src.chat_subagents", "src.session_search",
           "routes.chat_helpers", "routes.chat_routes"):
    importlib.import_module(_m)

_DB = {}


@pytest.fixture(autouse=True)
def _own_core_db(monkeypatch):
    """Core rows (sessions, chat threads) in a DB of this module's own, so
    the run's background threads never touch the suite's shared one."""
    import core.database as cdb
    if _DB.get("base") is not cdb.Base:
        ts, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
        _DB.update(base=cdb.Base, ts=ts, engine=engine, tmp=tmp)
    monkeypatch.setattr(cdb, "SessionLocal", _DB["ts"])


LOCAL = "http://127.0.0.1:11434/v1/chat/completions"
CLOUD = "https://api.openai.com/v1/chat/completions"


class FakeAuth:
    users = {"alice": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"


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


@pytest.fixture
def client(mods_dir, db, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    routes._calls.clear()
    runs._RUNS.clear()
    # One event loop for the whole test, so a run's background task lives on.
    with TestClient(app) as c:
        yield c
        _drain_runs()


def _drain_runs(timeout=10.0):
    """Stop what is still running and wait for it, while this test's patches
    are still in place: a run finishing later would reach the suite's
    shared database from a worker thread."""
    import time as _t
    for r in list(runs._RUNS.values()):
        if r.status in runs.LIVE:
            runs.stop(r)
    end = _t.monotonic() + timeout
    while _t.monotonic() < end and any(r.task and not r.task.done() for r in runs._RUNS.values()):
        _t.sleep(0.02)


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def record(scope, cost, owner="alice"):
    usage.record_usage(endpoint_url=CLOUD, model="x", input_tokens=10, output_tokens=5, owner=owner,
                       source="mod", source_scope=scope, billing_type="metered", cost_usd=cost)


# ── permissions ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path, body, perm", [
    ("get", "/api/mods/v1/llm/models", None, "llm.call"),
    ("post", "/api/mods/v1/agent/runs", {"model": "m@E", "prompt": "hi"}, "agent.run"),
    ("get", "/api/mods/v1/agent/runs", None, "agent.run"),
    ("get", "/api/mods/v1/agent/runs/abc", None, "agent.run"),
    ("get", "/api/mods/v1/usage", None, "budgets"),
    ("get", "/api/mods/v1/budgets", None, "budgets"),
    ("put", "/api/mods/v1/budgets", {"scope": "a", "monthly_limit_cents": 5}, "budgets"),
])
def test_routes_need_their_permission(client, method, path, body, perm):
    _, tok = add_mod(permissions=[])
    kw = {"headers": bearer(tok)}
    if body is not None:
        kw["json"] = body
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 403 and perm in r.json()["detail"]


def test_new_permissions_are_known():
    from src.mods import manifest as mf
    assert "agent.run" in mf.PERMISSIONS and "budgets" in mf.PERMISSIONS


# ── A8: model choice and local only ──────────────────────────────────────

@pytest.mark.parametrize("url, local", [
    ("http://127.0.0.1:11434/v1", True), ("http://100.102.86.125:11435/v1", True),
    ("http://gpu-box:8000/v1", True), ("http://box.tail90b62a.ts.net/v1", True),
    ("http://192.168.1.4/v1", True), ("https://api.openai.com/v1", False),
    ("https://openrouter.ai/api/v1", False), ("", False),
])
def test_is_local_url(url, local):
    assert mod_llm.is_local_url(url) is local


def test_llm_models_lists_specs_without_urls(client, monkeypatch):
    _, tok = add_mod(permissions=["llm.call"])
    import routes.model_routes as mr
    monkeypatch.setattr(mr, "fetch_models", lambda owner="", is_admin=False: {"items": [
        {"endpoint_name": "Box", "url": "http://127.0.0.1:11434/v1", "models": ["qwen3:8b"],
         "models_display": ["Qwen 3 8B"]},
        {"endpoint_name": "OpenAI", "url": "https://api.openai.com/v1", "models": ["gpt-5"]},
        {"endpoint_name": "Img", "url": "http://x", "models": ["sd"], "model_type": "image"}]})
    r = client.get("/api/mods/v1/llm/models", headers=bearer(tok))
    assert r.status_code == 200
    ms = r.json()["models"]
    assert [m["spec"] for m in ms] == ["qwen3:8b@Box", "gpt-5@OpenAI"]
    assert ms[0]["local"] is True and ms[1]["local"] is False and "url" not in ms[0]
    r = client.get("/api/mods/v1/llm/models?local_only=true", headers=bearer(tok))
    assert [m["spec"] for m in r.json()["models"]] == ["qwen3:8b@Box"]


def test_llm_chat_with_a_spec_local_only_and_scope(client, monkeypatch, db):
    _, tok = add_mod(permissions=["llm.call"])
    import src.ai_interaction as ai
    import src.llm_core as llm_core
    specs = []

    def fake_resolve(spec, owner=None):
        specs.append(spec)
        return (LOCAL if spec.endswith("@Box") else CLOUD), spec.split("@")[0], {}
    monkeypatch.setattr(ai, "_resolve_model", fake_resolve)

    async def fake_call(url, model, messages, **kw):
        usage.record_usage(endpoint_url=url, model=model, input_tokens=3, output_tokens=2,
                           billing_type="metered", cost_usd=0.01)
        return "hi back"
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)
    msgs = [{"role": "user", "content": "hi"}]
    r = client.post("/api/mods/v1/llm/chat", headers=bearer(tok),
                    json={"messages": msgs, "model": "qwen3:8b@Box", "local_only": True, "scope": "c/1"})
    assert r.status_code == 200 and r.json() == {"content": "hi back", "model": "qwen3:8b", "local": True}
    # A cloud model is refused when the call is local only, before any call.
    r = client.post("/api/mods/v1/llm/chat", headers=bearer(tok),
                    json={"messages": msgs, "model": "gpt-5@OpenAI", "local_only": True})
    assert r.status_code == 403 and "not a local model" in r.json()["detail"]
    s = db()
    rows = s.query(UsageEvent).all()
    s.close()
    assert len(rows) == 1 and rows[0].source == "mod" and rows[0].source_scope == "mod:test-mod/c/1"
    bad = client.post("/api/mods/v1/llm/chat", headers=bearer(tok), json={"messages": msgs, "scope": "../x"})
    assert bad.status_code == 400


# ── A13: usage scopes and budgets ────────────────────────────────────────

def test_scope_paths():
    assert usage_api.clean_path("c/3f2a/a/91bc") == "c/3f2a/a/91bc"
    assert usage_api.clean_path("/c/1/") == "c/1"
    assert usage_api.clean_path(None) == ""
    for bad in ("a b", "a/../b", "a//b", "%", "/".join("x" * 9), "a/" + "x" * 65, 5):
        with pytest.raises(ValueError):
            usage_api.clean_path(bad)


def test_scopes_nest_by_prefix(db):
    record("mod:company/c/1/a/1/run-x", 0.10)
    record("mod:company/c/1/a/2", 0.20)
    record("mod:company/c/10", 0.40)          # "c/10" is not under "c/1"
    record("mod:other/c/1", 0.80)
    w = usage.month_window()
    s = lambda scope: round(budgets.spent("mod_scope", scope, None, start=w[0], end=w[1])["cost_usd"], 4)  # noqa: E731
    assert s("mod:company/c/1") == 0.30
    assert s("mod:company/c/1/a/1") == 0.10
    assert s("mod:company") == 0.70
    assert s("mod:company/c/1%") == 0.0       # LIKE wildcards are escaped
    summ = usage.summary(None, all_users=True)
    by_mod = {r["mod_id"]: round(r["metered_cost_usd"], 4) for r in summ["by_mod"]}
    assert by_mod == {"company": 0.70, "other": 0.80}


def test_budgets_api_and_enforcement(client, db):
    _, tok = add_mod(permissions=["budgets"], mod_id="company")
    h = bearer(tok)
    assert client.put("/api/mods/v1/budgets", headers=h, json={"scope": "c/1", "monthly_limit_cents": 50}).status_code == 200
    assert client.put("/api/mods/v1/budgets", headers=h, json={"scope": "c/1", "monthly_limit_cents": -1}).status_code == 400
    record("mod:company/c/1/a/1/run-1", 0.45)
    c = client.get("/api/mods/v1/budgets/check?scope=c/1", headers=h).json()
    assert c["state"] == "warn" and c["limit_cents"] == 50 and round(c["spent_cents"]) == 45
    assert usage_api.stop_reason("company", "c/1/a/1") is None
    record("mod:company/c/1/a/2", 0.10)
    assert "c/1" in usage_api.stop_reason("company", "c/1/a/1")
    assert usage_api.stop_reason("company", "c/2") is None
    u = client.get("/api/mods/v1/usage?scope=c/1/a/1", headers=h).json()
    assert round(u["cost_usd"], 2) == 0.45
    lst = client.get("/api/mods/v1/budgets", headers=h).json()["budgets"]
    assert lst[0]["scope"] == "c/1" and lst[0]["check"]["state"] == "hard_stop"
    assert client.delete("/api/mods/v1/budgets?scope=c/1", headers=h).json()["existed"] is True
    assert usage_api.stop_reason("company", "c/1/a/1") is None


# ── A11: agent runs ──────────────────────────────────────────────────────

class FakeSM:
    def __init__(self):
        self.sessions, self.messages = {}, {}

    def create_session(self, sid, name, url, model, owner=None, parent_session_id=None):
        s = type("S", (), {})()
        s.headers = {}
        self.sessions[sid] = {"name": name, "owner": owner, "parent": parent_session_id, "obj": s}
        return s

    def add_message(self, sid, msg):
        self.messages.setdefault(sid, []).append(msg)

    def save_sessions(self):
        pass


@pytest.fixture
def fake_run_env(monkeypatch):
    """Fake model resolution, sessions and agent loop. `seen` holds what the
    loop saw; `script` the rounds it plays (list of SSE chunks per round)."""
    import src.ai_interaction as ai
    import src.agent_loop as al
    sm = FakeSM()
    seen = {}
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    # The detached-run hooks (queued messages, subagents) read the suite's
    # shared database; from the run's threads that would disturb other tests.
    import src.chat_queue as cq
    import src.chat_subagents as cs
    monkeypatch.setattr(cq, "on_run_started", lambda sid: None)
    monkeypatch.setattr(cq, "on_run_finished", lambda sid, status: None)
    monkeypatch.setattr(cs, "on_run_finished", lambda sid, status: None)
    import core.database as cdb
    monkeypatch.setattr(cdb, "set_session_mode", lambda sid, mode: None)
    monkeypatch.setattr(ai, "_resolve_model", lambda spec, owner=None: (
        LOCAL if spec.endswith("@Box") else CLOUD, spec.split("@")[0], {}))
    monkeypatch.setattr(runs, "_set_folder", lambda sid, folder: seen.setdefault("folders", []).append(folder))
    monkeypatch.setattr(runs, "_owned_session", lambda sid, owner: sid == "ws-existing")
    import src.model_context as mc
    monkeypatch.setattr(mc, "get_context_length", lambda url, model: 32768)
    sent = []
    monkeypatch.setattr(events, "send_to", lambda mod_id, etype, data: sent.append((mod_id, etype, data)))
    script = {"rounds": [['data: {"delta": "Working"}\n\n'], ['data: {"delta": " done"}\n\n']], "sleep": 0}

    async def fake_loop(url, model, messages, **kw):
        seen.update(url=url, model=model, messages=messages, kw=kw, ctx=runs.current(),
                    forced=runs.forced_tools(kw.get("owner")), header=runs.run_header("test-mod"),
                    other_header=runs.run_header("other"), scope=usage.current_scope())
        for i, chunks in enumerate(script["rounds"]):
            if i:
                yield f'data: {json.dumps({"type": "agent_step", "round": i + 1})}\n\n'
            for c in chunks:
                if script["sleep"]:
                    await asyncio.sleep(script["sleep"])
                yield c
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", fake_loop)
    return {"sm": sm, "seen": seen, "sent": sent, "script": script}


def wait_done(client, tok, rid, timeout=5):
    t = time.time()
    while time.time() - t < timeout:
        v = client.get(f"/api/mods/v1/agent/runs/{rid}", headers=bearer(tok)).json()
        if v["status"] not in ("queued", "running"):
            return v
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def with_tools(mod_id="test-mod"):
    import os
    from src.mods import paths
    os.makedirs(paths.mod_dir(mod_id), exist_ok=True)
    with open(os.path.join(paths.mod_dir(mod_id), "tools.json"), "w") as f:
        json.dump({"tools": [{"name": "list_issues", "description": "", "parameters": {}, "keywords": []},
                             {"name": "comment", "description": "", "parameters": {}, "keywords": []}]}, f)


def test_a_run_end_to_end(client, fake_run_env):
    _, tok = add_mod(permissions=["agent.run"], manifest_overrides={"tools": {"endpoint": "/mod/tools"}})
    with_tools()
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok), json={
        "user": "alice", "model": "qwen3:8b@Box", "local_only": True, "system": "You are Ada.",
        "prompt": "Work your issues", "folder": "Company", "workspace": {"title": "Ada · Acme"},
        "tools": {"mod": ["list_issues"], "core": ["bash", "ask_user"], "deny": ["web_search"]},
        "scope": "c/1/a/2", "max_rounds": 7, "gate": "never", "metadata": {"agent": "2"}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["local"] is True and out["workspace_id"] and out["session_id"]
    v = wait_done(client, tok, out["id"])
    assert v["status"] == "succeeded", v
    assert v["text"].strip() == "Working\n\n done" and v["rounds"] == 2
    seen = fake_run_env["seen"]
    assert seen["model"] == "qwen3:8b" and seen["messages"][0] == {"role": "system", "content": "You are Ada."}
    kw = seen["kw"]
    assert kw["max_rounds"] == 7 and kw["context_length"] == 32768 and kw["session_id"] == out["session_id"]
    assert "ask_user" in kw["disabled_tools"] and "web_search" in kw["disabled_tools"]
    assert kw["relevant_tools"] == {"bash"}
    assert seen["forced"] == ["mod__test-mod__list_issues"]
    assert seen["header"] == out["id"] and seen["other_header"] is None
    assert seen["scope"]["source"] == "mod"
    assert seen["scope"]["source_scope"] == f"mod:test-mod/c/1/a/2/run-{out['id']}"
    sm = fake_run_env["sm"]
    assert sm.sessions[out["session_id"]]["parent"] == out["workspace_id"]
    assert sm.sessions[out["workspace_id"]]["name"] == "Ada · Acme"
    assert [m.role for m in sm.messages[out["session_id"]]] == ["user", "assistant"]
    (mid, etype, data), = fake_run_env["sent"]
    assert (mid, etype, data["id"], data["metadata"]) == ("test-mod", "agent.run.finished", out["id"], {"agent": "2"})
    assert runs.current() is None
    # Another mod cannot see it.
    _, tok2 = add_mod(permissions=["agent.run"], mod_id="other")
    assert client.get(f"/api/mods/v1/agent/runs/{out['id']}", headers=bearer(tok2)).status_code == 404
    lst = client.get("/api/mods/v1/agent/runs", headers=bearer(tok)).json()["runs"]
    assert [x["id"] for x in lst] == [out["id"]] and "text" not in lst[0]


def test_existing_workspace_is_reused_and_no_workspace_files_the_thread(client, fake_run_env):
    _, tok = add_mod(permissions=["agent.run"])
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok), json={
        "model": "m@Box", "prompt": "x", "workspace": {"session_id": "ws-existing"}, "gate": "never"}).json()
    assert r["workspace_id"] == "ws-existing"
    wait_done(client, tok, r["id"])
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok), json={
        "model": "m@Box", "prompt": "x", "gate": "never"}).json()
    assert r["workspace_id"] is None
    wait_done(client, tok, r["id"])
    assert fake_run_env["seen"]["folders"] == ["Test Mod"]


@pytest.mark.parametrize("body, status, words", [
    ({"prompt": "x"}, 400, "model is required"),
    ({"model": "m@Box"}, 400, "messages"),
    ({"model": "m@Box", "messages": [{"role": "assistant", "content": "x"}]}, 400, "ending with a user"),
    ({"model": "m@Box", "prompt": "x", "tools": {"core": "bash"}}, 400, "tools.core"),
    ({"model": "m@Box", "prompt": "x", "gate": "sometimes"}, 400, "gate"),
    ({"model": "m@Box", "prompt": "x", "checkpoint": "http://evil/"}, 400, "checkpoint"),
    ({"model": "m@Box", "prompt": "x", "scope": "a b"}, 400, "scope"),
    ({"model": "gpt-5@OpenAI", "prompt": "x", "local_only": True}, 403, "not a local model"),
])
def test_bad_run_requests(client, fake_run_env, body, status, words):
    _, tok = add_mod(permissions=["agent.run"])
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok), json=body)
    assert r.status_code == status and words in r.json()["detail"]


def test_stop_cost_limit_and_budget(client, fake_run_env, monkeypatch):
    _, tok = add_mod(permissions=["agent.run", "budgets"])
    fake_run_env["script"]["rounds"] = [['data: {"delta": "a"}\n\n']] * 5
    # The run's own cost limit, read between rounds.
    monkeypatch.setattr(runs, "_run_cost", lambda run: {"cost_usd": 0.6, "input_tokens": 1, "output_tokens": 1,
                                                        "unpriced_calls": 0})
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok),
                    json={"model": "m@Box", "prompt": "x", "max_cost_usd": 0.5, "gate": "never"}).json()
    v = wait_done(client, tok, r["id"])
    assert v["status"] == "cancelled" and "cost limit" in v["error"] and v["rounds"] == 2
    assert v["usage"]["cost_usd"] == 0.6
    # A stored budget at hard stop refuses a new run at once.
    usage_api.set_budget("test-mod", "c/1", 10)
    monkeypatch.setattr(usage_api, "check", lambda mod_id, path, owner=None, limit_cents=None: {
        "state": "hard_stop", "stopped_by": ["money"], "spent_cents": 12, "limit_cents": 10})
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok),
                    json={"model": "m@Box", "prompt": "x", "scope": "c/1/a/1", "gate": "never"})
    assert r.status_code == 402 and "c/1" in r.json()["detail"]


def test_checkpoint_can_stop_a_run(client, fake_run_env, monkeypatch):
    _, tok = add_mod(permissions=["agent.run"])
    fake_run_env["script"]["rounds"] = [['data: {"delta": "a"}\n\n']] * 5
    calls = []

    async def fake_cp(run):
        calls.append(run.round)
        return "Ada was paused" if run.round >= 3 else None
    monkeypatch.setattr(runs, "_checkpoint", fake_cp)
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok),
                    json={"model": "m@Box", "prompt": "x", "checkpoint": "/mod/runs/checkpoint", "gate": "never"}).json()
    v = wait_done(client, tok, r["id"])
    assert v["status"] == "cancelled" and v["error"] == "Ada was paused" and calls == [2, 3]


def test_delete_stops_a_running_run(client, fake_run_env):
    _, tok = add_mod(permissions=["agent.run"])
    fake_run_env["script"].update(rounds=[['data: {"delta": "a"}\n\n']] * 50, sleep=0.05)
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok),
                    json={"model": "m@Box", "prompt": "x", "gate": "never"}).json()
    time.sleep(0.2)
    assert client.delete(f"/api/mods/v1/agent/runs/{r['id']}", headers=bearer(tok)).json()["ok"] is True
    v = wait_done(client, tok, r["id"])
    assert v["status"] == "cancelled"


def test_concurrency_limit(client, fake_run_env, monkeypatch):
    _, tok = add_mod(permissions=["agent.run"])
    monkeypatch.setattr(runs, "MAX_LIVE_PER_MOD", 1)
    fake_run_env["script"].update(rounds=[['data: {"delta": "a"}\n\n']] * 20, sleep=0.05)
    a = client.post("/api/mods/v1/agent/runs", headers=bearer(tok), json={"model": "m@Box", "prompt": "x", "gate": "never"})
    b = client.post("/api/mods/v1/agent/runs", headers=bearer(tok), json={"model": "m@Box", "prompt": "x", "gate": "never"})
    assert a.status_code == 200 and b.status_code == 429
    client.delete(f"/api/mods/v1/agent/runs/{a.json()['id']}", headers=bearer(tok))
    wait_done(client, tok, a.json()["id"])


def test_local_model_waits_for_quiet(client, fake_run_env, monkeypatch):
    _, tok = add_mod(permissions=["agent.run"])
    waited = []

    async def fake_quiet(label):
        waited.append(label)
    monkeypatch.setattr(runs, "_wait_quiet", fake_quiet)
    r = client.post("/api/mods/v1/agent/runs", headers=bearer(tok), json={"model": "m@Box", "prompt": "x"}).json()
    wait_done(client, tok, r["id"])
    r2 = client.post("/api/mods/v1/agent/runs", headers=bearer(tok), json={"model": "m@OpenAI", "prompt": "x"}).json()
    wait_done(client, tok, r2["id"])
    assert len(waited) == 1     # the cloud run did not wait


def test_tool_calls_carry_the_run_header(mods_dir, monkeypatch):
    """src/mods/tools.py::execute adds X-Odysseus-Run only for the run's own mod."""
    with runs.run_context(mod_id="company", run_id="r1", tools=True, other_mods=False):
        assert runs.run_header("company") == "r1"
        assert runs.run_header("print-hub") is None
    assert runs.run_header("company") is None


def test_interactive_gate_ignores_mod_api_and_mod_page_polls():
    from src.interactive_gate import should_track_interactive_request as track
    assert not track("/api/mods/v1/agent/runs/x", "GET")
    assert not track("/mods/company/api/companies/1/live", "GET")
    assert track("/mods/company/api/companies/1/issues", "POST")
