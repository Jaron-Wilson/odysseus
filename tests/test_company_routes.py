"""Company: an organization of AI agents (routes/company_routes.py, src/company.py).

What matters:

  * every row belongs to one account; another account's ids answer 404;
  * the org is one tree with one CEO at the root, and no reporting loops;
  * an agent's model is an Odysseus model spec the owner can actually use;
  * issues get PREFIX-n identifiers and only move along the allowed transitions;
  * checkout is atomic: of several runs racing for an issue exactly one wins,
    the holder can check out again, and a stale or finished lock is taken over;
  * @mentions in comments are parsed and queued as wake-ups for the executor;
  * every change lands in the activity feed.
"""
import sys
import threading
import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
from src import company as co  # noqa: E402

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)

GOOD_MODELS = {"qwen3:8b@Local Ollama", "gpt-5@OpenAI", "claude-sonnet@Anthropic"}


@pytest.fixture(autouse=True)
def _bind(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)

    def fake_resolve(spec, owner=None):
        if spec not in GOOD_MODELS:
            raise ValueError(f"Model '{spec}' not found on any configured endpoint")
        return "http://x/v1/chat/completions", spec.split("@")[0], {}

    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    yield


@pytest.fixture
def client():
    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    from routes.company_routes import setup_company_routes
    app.include_router(setup_company_routes())
    with TestClient(app) as c:
        yield c


def _user():
    return {"x-test-user": "u" + uuid.uuid4().hex[:8]}


API = "/api/company"


def _company(client, h, **kw):
    r = client.post(f"{API}/companies", headers=h, json={"name": "Odysseus Labs", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _agent(client, h, cid, **kw):
    body = {"name": "Ada", "role": "ceo", "model": "qwen3:8b@Local Ollama", **kw}
    r = client.post(f"{API}/companies/{cid}/agents", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _issue(client, h, cid, **kw):
    r = client.post(f"{API}/companies/{cid}/issues", headers=h, json={"title": "Do the thing", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _actions(client, h, cid):
    return [a["action"] for a in client.get(f"{API}/companies/{cid}/activity?limit=200", headers=h).json()["activity"]]


# ── Companies and owner scoping ──────────────────────────────────────────

def test_company_round_trip_and_prefix(client):
    h = _user()
    c = _company(client, h)
    assert c["issue_prefix"] == "OL" and c["status"] == "active" and c["issue_counter"] == 0
    # A second company with the same name gets a different prefix.
    c2 = _company(client, h)
    assert c2["issue_prefix"] != c["issue_prefix"]
    # An explicit prefix that's taken is refused.
    r = client.post(f"{API}/companies", headers=h, json={"name": "X", "issue_prefix": "OL"})
    assert r.status_code == 409
    r = client.post(f"{API}/companies", headers=h, json={"name": "X", "issue_prefix": "1bad"})
    assert r.status_code == 422
    r = client.patch(f"{API}/companies/{c['id']}", headers=h,
                     json={"status": "paused", "monthly_budget_cents": 5000, "require_approval_for_new_agents": True})
    assert r.status_code == 200
    assert r.json()["status"] == "paused" and r.json()["monthly_budget_cents"] == 5000
    assert client.patch(f"{API}/companies/{c['id']}", headers=h, json={"status": "bogus"}).status_code == 422
    names = [x["id"] for x in client.get(f"{API}/companies", headers=h).json()["companies"]]
    assert c["id"] in names and c2["id"] in names
    assert "company.updated" in _actions(client, h, c["id"])
    assert client.delete(f"{API}/companies/{c2['id']}", headers=h).json() == {"ok": True}
    assert client.get(f"{API}/companies/{c2['id']}", headers=h).status_code == 404


def test_another_account_sees_nothing(client):
    ann, bob = _user(), _user()
    c = _company(client, ann)
    a = _agent(client, ann, c["id"])
    i = _issue(client, ann, c["id"])
    assert client.get(f"{API}/companies", headers=bob).json()["companies"] == []
    for url in (f"/companies/{c['id']}", f"/companies/{c['id']}/agents", f"/companies/{c['id']}/issues",
                f"/companies/{c['id']}/org", f"/companies/{c['id']}/activity", f"/agents/{a['id']}",
                f"/issues/{i['id']}", f"/issues/{i['id']}/comments", f"/companies/{c['id']}/dashboard"):
        assert client.get(API + url, headers=bob).status_code == 404, url
    assert client.patch(f"{API}/issues/{i['id']}", headers=bob, json={"title": "mine"}).status_code == 404
    assert client.delete(f"{API}/companies/{c['id']}", headers=bob).status_code == 404
    assert client.post(f"{API}/issues/{i['id']}/checkout", headers=bob,
                       json={"agent_id": a["id"], "run_id": "r1"}).status_code == 404
    # Bob can't point his own issue at Ann's agent either.
    bc = _company(client, bob)
    r = client.post(f"{API}/companies/{bc['id']}/issues", headers=bob,
                    json={"title": "t", "assignee_agent_id": a["id"]})
    assert r.status_code == 422


def test_unauthenticated_is_refused_when_auth_is_configured(client, monkeypatch):
    class Auth:
        is_configured = True
    client.app.state.auth_manager = Auth()
    try:
        assert client.get(f"{API}/companies").status_code == 401
    finally:
        del client.app.state.auth_manager


# ── Org tree ─────────────────────────────────────────────────────────────

def test_org_needs_a_ceo_first_and_only_one(client):
    h = _user()
    cid = _company(client, h)["id"]
    r = client.post(f"{API}/companies/{cid}/agents", headers=h, json={"name": "Bo", "role": "engineer"})
    assert r.status_code == 422 and "CEO" in r.json()["detail"]
    ceo = _agent(client, h, cid)
    r = client.post(f"{API}/companies/{cid}/agents", headers=h, json={"name": "Cy", "role": "ceo"})
    assert r.status_code == 409
    r = client.post(f"{API}/companies/{cid}/agents", headers=h, json={"name": "Bo", "role": "engineer"})
    assert r.status_code == 422 and "reports to" in r.json()["detail"]
    cto = _agent(client, h, cid, name="Bo", role="cto", reports_to=ceo["id"])
    eng = _agent(client, h, cid, name="Cy", role="engineer", reports_to=cto["id"])
    r = client.post(f"{API}/companies/{cid}/agents", headers=h,
                    json={"name": "Ada", "role": "qa", "reports_to": ceo["id"]})
    assert r.status_code == 409   # names are unique in a company
    tree = client.get(f"{API}/companies/{cid}/org", headers=h).json()
    assert tree["root"]["id"] == ceo["id"]
    assert tree["root"]["reports"][0]["id"] == cto["id"]
    assert tree["root"]["reports"][0]["reports"][0]["id"] == eng["id"]
    assert tree["unplaced"] == []


def test_org_rejects_cycles_and_self_reports(client):
    h = _user()
    cid = _company(client, h)["id"]
    ceo = _agent(client, h, cid)
    a = _agent(client, h, cid, name="A", role="cto", reports_to=ceo["id"])
    b = _agent(client, h, cid, name="B", role="engineer", reports_to=a["id"])
    c = _agent(client, h, cid, name="C", role="engineer", reports_to=b["id"])
    r = client.patch(f"{API}/agents/{a['id']}", headers=h, json={"reports_to": c["id"]})
    assert r.status_code == 422 and "loop" in r.json()["detail"]
    r = client.patch(f"{API}/agents/{a['id']}", headers=h, json={"reports_to": a["id"]})
    assert r.status_code == 422
    r = client.patch(f"{API}/agents/{ceo['id']}", headers=h, json={"reports_to": a["id"]})
    assert r.status_code == 422   # the CEO reports to no one
    r = client.patch(f"{API}/agents/{c['id']}", headers=h, json={"role": "ceo", "reports_to": None})
    assert r.status_code == 409   # one CEO
    r = client.patch(f"{API}/agents/{ceo['id']}", headers=h, json={"role": "engineer"})
    assert r.status_code == 409   # demoting the CEO would leave no root
    # A legal move works.
    r = client.patch(f"{API}/agents/{c['id']}", headers=h, json={"reports_to": a["id"]})
    assert r.status_code == 200 and r.json()["reports_to"] == a["id"]
    # Another company's agent isn't a valid manager.
    other = _company(client, h)["id"]
    oc = _agent(client, h, other)
    r = client.patch(f"{API}/agents/{c['id']}", headers=h, json={"reports_to": oc["id"]})
    assert r.status_code == 422


def test_make_ceo_and_terminate_keep_one_tree(client):
    h = _user()
    cid = _company(client, h)["id"]
    ceo = _agent(client, h, cid)
    cto = _agent(client, h, cid, name="Bo", role="cto", reports_to=ceo["id"])
    eng = _agent(client, h, cid, name="Cy", role="engineer", reports_to=cto["id"])
    r = client.patch(f"{API}/agents/{ceo['id']}", headers=h, json={"status": "terminated"})
    assert r.status_code == 409
    r = client.post(f"{API}/agents/{cto['id']}/make-ceo", headers=h, json={"old_ceo_role": "cfo"})
    assert r.status_code == 200 and r.json()["role"] == "ceo" and r.json()["reports_to"] is None
    tree = client.get(f"{API}/companies/{cid}/org", headers=h).json()
    assert tree["root"]["id"] == cto["id"]
    under = {n["id"]: n for n in tree["root"]["reports"]}
    assert set(under) == {ceo["id"], eng["id"]} and under[ceo["id"]]["role"] == "cfo"
    # Terminating a manager hands their reports to their own manager.
    _agent(client, h, cid, name="Di", role="qa", reports_to=ceo["id"])
    r = client.patch(f"{API}/agents/{ceo['id']}", headers=h, json={"status": "terminated"})
    assert r.status_code == 200
    tree = client.get(f"{API}/companies/{cid}/org", headers=h).json()
    assert {n["name"] for n in tree["root"]["reports"]} == {"Cy", "Di"}
    assert [u["id"] for u in tree["unplaced"]] == [ceo["id"]]
    r = client.patch(f"{API}/agents/{ceo['id']}", headers=h, json={"status": "active"})
    assert r.status_code == 422   # terminated is final
    assert "agent.terminated" in _actions(client, h, cid) and "agent.made_ceo" in _actions(client, h, cid)


# ── Model spec ───────────────────────────────────────────────────────────

def test_model_spec_is_validated_on_save(client):
    h = _user()
    cid = _company(client, h)["id"]
    r = client.post(f"{API}/companies/{cid}/agents", headers=h,
                    json={"name": "Ada", "role": "ceo", "model": "nope@Nowhere"})
    assert r.status_code == 422 and "not available" in r.json()["detail"]
    a = _agent(client, h, cid, model="gpt-5@OpenAI")
    assert a["model"] == "gpt-5@OpenAI"
    r = client.patch(f"{API}/agents/{a['id']}", headers=h, json={"model": "claude-sonnet@Anthropic"})
    assert r.status_code == 200 and r.json()["model"] == "claude-sonnet@Anthropic"
    assert client.patch(f"{API}/agents/{a['id']}", headers=h, json={"model": "bad@x"}).status_code == 422
    assert client.patch(f"{API}/agents/{a['id']}", headers=h, json={"model": ""}).json()["model"] == ""


def test_model_resolution_is_owner_scoped(monkeypatch):
    seen = {}

    def fake(spec, owner=None):
        seen["owner"] = owner
        return "u", "m", {}
    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "_resolve_model", fake)
    assert co.validate_model_spec(" m@E ", "ann") == "m@E"
    assert seen["owner"] == "ann"
    assert co.validate_model_spec("", "ann") is None


def test_model_spec_falls_back_to_the_endpoints_cached_list(monkeypatch):
    """A local endpoint that's briefly offline still accepts its known models."""
    from src import ai_interaction

    def offline(spec, owner=None):
        raise ValueError("No enabled endpoints found")
    monkeypatch.setattr(ai_interaction, "_resolve_model", offline)
    db = _TS()
    ep = cdb.ModelEndpoint(id=uuid.uuid4().hex, name="Box " + uuid.uuid4().hex[:4], base_url="http://10.0.0.9:8000/v1",
                           owner="carl", is_enabled=True, cached_models='["qwen3-122b"]')
    db.add(ep)
    db.commit()
    name = ep.name
    db.close()
    assert co.validate_model_spec(f"qwen3-122b@{name}", "carl") == f"qwen3-122b@{name}"
    with pytest.raises(co.CompanyError):
        co.validate_model_spec(f"other@{name}", "carl")
    with pytest.raises(co.CompanyError):
        co.validate_model_spec(f"qwen3-122b@{name}", "dora")   # not her endpoint


def test_agent_settings_validate(client):
    h = _user()
    cid = _company(client, h)["id"]
    a = _agent(client, h, cid, heartbeat={"enabled": True, "interval_sec": 600},
               enabled_tools="web_search, read_file", monthly_budget_cents=1200)
    assert a["heartbeat"] == {**co.DEFAULT_HEARTBEAT, "enabled": True, "interval_sec": 600}
    assert a["enabled_tools"] == ["web_search", "read_file"]
    assert a["monthly_budget_cents"] == 1200 and a["spent_cents"] == 0
    r = client.patch(f"{API}/agents/{a['id']}", headers=h, json={"heartbeat": {"wake_on_mention": False}})
    assert r.json()["heartbeat"]["interval_sec"] == 600 and r.json()["heartbeat"]["wake_on_mention"] is False
    for bad in ({"heartbeat": {"interval_sec": 5}}, {"heartbeat": {"nope": True}}, {"role": "king"},
                {"status": "running"}, {"monthly_budget_cents": -1}, {"session_id": "not-a-chat"}):
        assert client.patch(f"{API}/agents/{a['id']}", headers=h, json=bad).status_code == 422, bad


# ── Issues ───────────────────────────────────────────────────────────────

def test_identifiers_count_up_per_company(client):
    h = _user()
    c = _company(client, h, issue_prefix="ODY")
    ids = [_issue(client, h, c["id"])["identifier"] for _ in range(3)]
    assert ids == ["ODY-1", "ODY-2", "ODY-3"]
    other = _company(client, h, issue_prefix="ZED")
    assert _issue(client, h, other["id"])["identifier"] == "ZED-1"
    assert client.patch(f"{API}/companies/{c['id']}", headers=h, json={"issue_prefix": "NEW"}).status_code == 409


def test_identifiers_are_unique_under_parallel_creates(client):
    h = _user()
    cid = _company(client, h)["id"]
    out, errs = [], []

    def make():
        r = client.post(f"{API}/companies/{cid}/issues", headers=h, json={"title": "p"})
        (out if r.status_code == 200 else errs).append(r)
    ts = [threading.Thread(target=make) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, [e.text for e in errs]
    nums = sorted(r.json()["number"] for r in out)
    assert nums == list(range(1, len(out) + 1))
    assert len(set(nums)) == len(nums)


def test_status_transitions(client):
    h = _user()
    cid = _company(client, h)["id"]
    i = _issue(client, h, cid)
    assert i["status"] == "backlog"

    def move(to):
        return client.patch(f"{API}/issues/{i['id']}", headers=h, json={"status": to})
    r = move("done")
    assert r.status_code == 422 and "backlog to done" in r.json()["detail"]
    assert move("todo").status_code == 200
    r = move("in_progress")
    assert r.status_code == 200 and r.json()["started_at"]
    assert move("in_review").status_code == 200
    r = move("done")
    assert r.status_code == 200 and r.json()["completed_at"]
    assert move("in_progress").status_code == 422   # done only reopens to todo
    r = move("todo")
    assert r.status_code == 200 and r.json()["completed_at"] is None
    r = move("cancelled")
    assert r.status_code == 200 and r.json()["cancelled_at"]
    assert move("weird").status_code == 422
    r = client.post(f"{API}/companies/{cid}/issues", headers=h, json={"title": "x", "status": "done"})
    assert r.status_code == 422
    acts = client.get(f"{API}/companies/{cid}/activity?entity_id={i['id']}", headers=h).json()["activity"]
    changes = [a["details"]["changes"]["status"] for a in acts if a["action"] == "issue.status_changed"]
    assert {"from": "in_review", "to": "done"} in changes


def test_issue_list_filters_and_sort(client):
    h = _user()
    cid = _company(client, h)["id"]
    ceo = _agent(client, h, cid)
    p = client.post(f"{API}/companies/{cid}/projects", headers=h, json={"name": "Site", "color": "#3B82F6"}).json()
    assert p["color"] == "#3b82f6"
    low = _issue(client, h, cid, title="low", priority="low")
    crit = _issue(client, h, cid, title="crit", priority="critical", assignee_agent_id=ceo["id"])
    med = _issue(client, h, cid, title="med", project_id=p["id"], status="todo")
    high = _issue(client, h, cid, title="high", priority="high", assignee_user="me")

    def titles(qs=""):
        return [x["title"] for x in client.get(f"{API}/companies/{cid}/issues{qs}", headers=h).json()["issues"]]
    assert titles() == ["crit", "high", "med", "low"]
    assert titles("?status=todo") == ["med"]
    assert titles("?status=backlog,todo&priority=low,medium") == ["med", "low"]
    assert titles(f"?assignee_agent_id={ceo['id']}") == ["crit"]
    assert titles("?assignee_user=me") == ["high"]
    assert titles(f"?project_id={p['id']}") == ["med"]
    assert titles("?q=cri") == ["crit"]
    # Within a priority, the most recently updated comes first.
    med2 = _issue(client, h, cid, title="med2")
    client.patch(f"{API}/issues/{med['id']}", headers=h, json={"description": "touched"})
    assert titles("?priority=medium") == ["med", "med2"]
    r = client.post(f"{API}/companies/{cid}/issues", headers=h,
                    json={"title": "both", "assignee_agent_id": ceo["id"], "assignee_user": "me"})
    assert r.status_code == 422
    assert {low["id"], high["id"], crit["id"], med2["id"]}


def test_labels_sub_issues_and_relations(client):
    h = _user()
    cid = _company(client, h)["id"]
    bug = client.post(f"{API}/companies/{cid}/labels", headers=h, json={"name": "bug", "color": "#ff0000"}).json()
    assert client.post(f"{API}/companies/{cid}/labels", headers=h, json={"name": "bug"}).status_code == 409
    parent = _issue(client, h, cid, title="parent", label_ids=[bug["id"]])
    assert [lb["name"] for lb in parent["labels"]] == ["bug"]
    child = _issue(client, h, cid, title="child", parent_id=parent["id"])
    full = client.get(f"{API}/issues/{parent['id']}", headers=h).json()
    assert [s["id"] for s in full["sub_issues"]] == [child["id"]]
    r = client.patch(f"{API}/issues/{parent['id']}", headers=h, json={"parent_id": child["id"]})
    assert r.status_code == 422   # no loops
    r = client.post(f"{API}/issues/{child['id']}/relations", headers=h, json={"related_issue_id": parent["id"]})
    assert r.status_code == 200 and r.json()["blocks"][0]["id"] == parent["id"]
    assert client.post(f"{API}/issues/{parent['id']}/relations", headers=h,
                       json={"related_issue_id": child["id"]}).status_code == 409
    blocked = client.get(f"{API}/issues/{parent['id']}", headers=h).json()["blocked_by"]
    assert blocked[0]["id"] == child["id"]
    assert client.delete(f"{API}/relations/{blocked[0]['relation_id']}", headers=h).status_code == 200
    r = client.patch(f"{API}/issues/{parent['id']}", headers=h, json={"label_ids": []})
    assert r.json()["labels"] == []
    assert client.get(f"{API}/companies/{cid}/issues?label_id={bug['id']}", headers=h).json()["issues"] == []


# ── Checkout ─────────────────────────────────────────────────────────────

def _run(h, agent_id, status="running"):
    """A live run of the agent: checkout only accepts one of those."""
    db = _TS()
    try:
        run = co.create_run(db, h["x-test-user"], agent_id, "on_demand")
        run.status = status
        db.commit()
        return run.id
    finally:
        db.close()


def _team(client, h):
    cid = _company(client, h)["id"]
    ceo = _agent(client, h, cid)
    eng = _agent(client, h, cid, name="Bo", role="engineer", reports_to=ceo["id"])
    return cid, ceo, eng


def test_checkout_claims_and_the_holder_can_repeat(client):
    h = _user()
    cid, ceo, eng = _team(client, h)
    i = _issue(client, h, cid, status="todo")
    run1, run2, run3 = _run(h, eng["id"]), _run(h, eng["id"]), _run(h, ceo["id"])
    r = client.post(f"{API}/issues/{i['id']}/checkout", headers=h, json={"agent_id": eng["id"], "run_id": run1})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "in_progress" and j["checkout_run_id"] == run1 and j["assignee_agent_id"] == eng["id"]
    assert client.post(f"{API}/issues/{i['id']}/checkout", headers=h,
                       json={"agent_id": eng["id"], "run_id": run1}).status_code == 200
    r = client.post(f"{API}/issues/{i['id']}/checkout", headers=h, json={"agent_id": eng["id"], "run_id": run2})
    assert r.status_code == 409 and "another run" in r.json()["detail"]
    r = client.post(f"{API}/issues/{i['id']}/checkout", headers=h, json={"agent_id": ceo["id"], "run_id": run3})
    assert r.status_code == 409 and "assigned to someone else" in r.json()["detail"]
    # Only the holder releases, unless forced.
    assert client.post(f"{API}/issues/{i['id']}/release", headers=h, json={"run_id": run2}).status_code == 409
    r = client.post(f"{API}/issues/{i['id']}/release", headers=h, json={"run_id": run1})
    assert r.status_code == 200 and r.json()["checkout_run_id"] is None and r.json()["status"] == "todo"
    acts = _actions(client, h, cid)
    assert acts.count("issue.checked_out") == 1 and "issue.released" in acts
    # Done issues can't be checked out.
    d = _issue(client, h, cid, status="todo")
    client.patch(f"{API}/issues/{d['id']}", headers=h, json={"status": "in_progress"})
    client.patch(f"{API}/issues/{d['id']}", headers=h, json={"status": "done"})
    assert client.post(f"{API}/issues/{d['id']}/checkout", headers=h,
                       json={"agent_id": eng["id"], "run_id": run2}).status_code == 409
    assert client.post(f"{API}/issues/{d['id']}/checkout", headers=h,
                       json={"agent_id": eng["id"], "run_id": run2, "expected_statuses": ["done"]}).status_code == 422


def test_checkout_needs_a_live_run_of_that_agent(client):
    h = _user()
    cid, ceo, eng = _team(client, h)
    i = _issue(client, h, cid, status="todo")
    url = f"{API}/issues/{i['id']}/checkout"
    r = client.post(url, headers=h, json={"agent_id": eng["id"], "run_id": "made-up"})
    assert r.status_code == 422 and "live run" in r.json()["detail"]
    assert client.post(url, headers=h, json={"agent_id": eng["id"], "run_id": _run(h, eng["id"], "succeeded")}).status_code == 409
    assert client.post(url, headers=h, json={"agent_id": eng["id"], "run_id": _run(h, ceo["id"])}).status_code == 409
    # Queued counts as live (the executor claims before the run starts).
    assert client.post(url, headers=h, json={"agent_id": eng["id"], "run_id": _run(h, eng["id"], "queued")}).status_code == 200


def test_concurrent_checkouts_have_exactly_one_winner(client):
    h = _user()
    cid, ceo, eng = _team(client, h)
    others = [_agent(client, h, cid, name=f"E{n}", role="engineer", reports_to=ceo["id"]) for n in range(5)]
    i = _issue(client, h, cid, status="todo")
    results = []
    barrier = threading.Barrier(len(others) + 1)

    def go(agent, run):
        barrier.wait()
        r = client.post(f"{API}/issues/{i['id']}/checkout", headers=h, json={"agent_id": agent["id"], "run_id": run})
        results.append((r.status_code, run))
    ts = [threading.Thread(target=go, args=(a, _run(h, a["id"]))) for a in [eng] + others]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    codes = sorted(c for c, _ in results)
    assert codes.count(200) == 1 and codes.count(409) == len(others)
    winner = next(run for c, run in results if c == 200)
    assert client.get(f"{API}/issues/{i['id']}", headers=h).json()["checkout_run_id"] == winner


def test_stale_and_finished_locks_are_taken_over(client, monkeypatch):
    h = _user()
    cid, ceo, eng = _team(client, h)
    i = _issue(client, h, cid, status="todo", assignee_agent_id=eng["id"])
    old, new = _run(h, eng["id"]), _run(h, eng["id"])
    assert client.post(f"{API}/issues/{i['id']}/checkout", headers=h,
                       json={"agent_id": eng["id"], "run_id": old}).status_code == 200
    # Age the lock past the stale limit.
    db = _TS()
    row = db.get(cdb.CompanyIssue, i["id"])
    row.execution_locked_at = row.execution_locked_at - timedelta(seconds=co.STALE_LOCK_SECONDS + 5)
    db.commit()
    db.close()
    r = client.post(f"{API}/issues/{i['id']}/checkout", headers=h, json={"agent_id": eng["id"], "run_id": new})
    assert r.status_code == 200 and r.json()["checkout_run_id"] == new
    took = [a for a in client.get(f"{API}/companies/{cid}/activity", headers=h).json()["activity"]
            if a["action"] == "issue.checked_out"][0]
    assert took["details"]["took_over_from"] == old and took["run_id"] == new
    # A run that has finished gives up its lock even when the lock is fresh.
    db = _TS()
    run = co.create_run(db, h["x-test-user"], eng["id"], "assignment", issue_id=i["id"])
    run_id = run.id
    db.close()
    nxt = _run(h, eng["id"])
    client.post(f"{API}/issues/{i['id']}/release", headers=h, json={"force": True})
    assert client.post(f"{API}/issues/{i['id']}/checkout", headers=h,
                       json={"agent_id": eng["id"], "run_id": run_id}).status_code == 200
    db = _TS()
    db.query(cdb.CompanyRun).filter(cdb.CompanyRun.id == run_id).update({"status": "failed"})
    db.commit()
    db.close()
    r = client.post(f"{API}/issues/{i['id']}/checkout", headers=h, json={"agent_id": eng["id"], "run_id": nxt})
    assert r.status_code == 200 and r.json()["checkout_run_id"] == nxt
    runs = client.get(f"{API}/companies/{cid}/runs", headers=h).json()["runs"]
    assert next(x for x in runs if x["id"] == run_id)["status"] == "failed"


def test_paused_agent_cannot_check_out(client):
    h = _user()
    cid, ceo, eng = _team(client, h)
    client.patch(f"{API}/agents/{eng['id']}", headers=h, json={"status": "paused"})
    i = _issue(client, h, cid, status="todo")
    r = client.post(f"{API}/issues/{i['id']}/checkout", headers=h, json={"agent_id": eng["id"], "run_id": _run(h, eng["id"])})
    assert r.status_code == 409


# ── Comments, mentions, wake-ups ─────────────────────────────────────────

class _A:
    def __init__(self, id, name):
        self.id, self.name = id, name


def test_parse_mentions():
    agents = [_A("1", "Ada"), _A("2", "Ada Lovelace"), _A("3", "Bo"), _A("4", "Grace Hopper")]
    assert co.parse_mentions("@Ada Lovelace please look", agents) == ["2"]
    assert co.parse_mentions("@ada and @bo, then @grace-hopper", agents) == ["1", "3", "4"]
    assert co.parse_mentions("mail ada@ada.com or @Adams", agents) == []
    assert co.parse_mentions("@Bo @bo @BO.", agents) == ["3"]
    assert co.parse_mentions("no mentions", agents) == []


def test_comment_mentions_queue_wakeups(client):
    h = _user()
    cid, ceo, eng = _team(client, h)
    quiet = _agent(client, h, cid, name="Quiet One", role="qa", reports_to=ceo["id"],
                   heartbeat={"wake_on_mention": False})
    i = _issue(client, h, cid)
    r = client.post(f"{API}/issues/{i['id']}/comments", headers=h,
                    json={"body": "@Bo can you take this? cc @quiet-one and @Nobody"})
    assert r.status_code == 200
    cm = r.json()
    assert cm["mentions"] == [eng["id"], quiet["id"]] and cm["author_type"] == "user"
    wk = client.get(f"{API}/companies/{cid}/wakeups", headers=h).json()["wakeups"]
    mention = [w for w in wk if w["source"] == "mention"]
    assert [w["agent_id"] for w in mention] == [eng["id"]] and mention[0]["comment_id"] == cm["id"]
    assert [c["body"] for c in client.get(f"{API}/issues/{i['id']}/comments", headers=h).json()["comments"]] \
        == [cm["body"]]
    assert "comment.created" in _actions(client, h, cid)
    assert client.post(f"{API}/issues/{i['id']}/comments", headers=h, json={"body": "  "}).status_code == 422


def test_assignment_and_wake_queue_wakeups(client):
    h = _user()
    cid, ceo, eng = _team(client, h)
    i = _issue(client, h, cid)
    r = client.patch(f"{API}/issues/{i['id']}", headers=h, json={"assignee_agent_id": eng["id"]})
    assert r.status_code == 200
    r = client.post(f"{API}/agents/{ceo['id']}/wake", headers=h)
    assert r.status_code == 200 and r.json()["source"] == "on_demand"
    wk = client.get(f"{API}/companies/{cid}/wakeups", headers=h).json()["wakeups"]
    assert [(w["agent_id"], w["source"]) for w in wk] == [(eng["id"], "assignment"), (ceo["id"], "on_demand")]
    acts = _actions(client, h, cid)
    assert "issue.assigned" in acts and "agent.wake_requested" in acts
    client.patch(f"{API}/agents/{eng['id']}", headers=h, json={"status": "paused"})
    assert client.post(f"{API}/agents/{eng['id']}/wake", headers=h).status_code == 409


# ── Goals, projects, approvals, dashboard ────────────────────────────────

def test_goals_hierarchy(client):
    h = _user()
    cid = _company(client, h)["id"]
    top = client.post(f"{API}/companies/{cid}/goals", headers=h, json={"title": "Ship 1.0"}).json()
    sub = client.post(f"{API}/companies/{cid}/goals", headers=h,
                      json={"title": "Docs", "parent_id": top["id"], "level": "team"}).json()
    assert sub["parent_id"] == top["id"] and sub["level"] == "team"
    assert client.patch(f"{API}/goals/{top['id']}", headers=h, json={"parent_id": sub["id"]}).status_code == 422
    assert client.patch(f"{API}/goals/{sub['id']}", headers=h, json={"status": "achieved"}).json()["status"] == "achieved"
    assert client.patch(f"{API}/goals/{sub['id']}", headers=h, json={"level": "galaxy"}).status_code == 422
    assert client.delete(f"{API}/goals/{top['id']}", headers=h).status_code == 200
    assert [g["parent_id"] for g in client.get(f"{API}/companies/{cid}/goals", headers=h).json()["goals"]] == [None]


def test_projects_crud(client):
    h = _user()
    cid = _company(client, h)["id"]
    p = client.post(f"{API}/companies/{cid}/projects", headers=h, json={"name": "Site", "target_date": "2026-12-01"}).json()
    assert p["status"] == "backlog"
    assert client.patch(f"{API}/projects/{p['id']}", headers=h, json={"status": "in_progress"}).json()["status"] == "in_progress"
    assert client.patch(f"{API}/projects/{p['id']}", headers=h, json={"target_date": "soon"}).status_code == 422
    assert client.post(f"{API}/companies/{cid}/projects", headers=h, json={}).status_code == 422
    i = _issue(client, h, cid, project_id=p["id"])
    assert client.delete(f"{API}/projects/{p['id']}", headers=h).status_code == 200
    assert client.get(f"{API}/issues/{i['id']}", headers=h).json()["project_id"] is None


def test_approvals_record_decisions(client):
    h = _user()
    cid = _company(client, h)["id"]
    ap = client.post(f"{API}/companies/{cid}/approvals", headers=h,
                     json={"type": "strategy", "title": "Q4 plan", "payload": {"text": "Grow"}}).json()
    assert ap["status"] == "pending"
    r = client.post(f"{API}/approvals/{ap['id']}/decide", headers=h, json={"decision": "approved", "note": "Go"})
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["decided_at"]
    assert client.post(f"{API}/approvals/{ap['id']}/decide", headers=h, json={"decision": "rejected"}).status_code == 409
    assert client.post(f"{API}/approvals/{ap['id']}/decide", headers=h, json={"decision": "maybe"}).status_code == 422
    bad = client.post(f"{API}/companies/{cid}/approvals", headers=h,
                      json={"type": "hire_agent", "payload": {"model": "bad@x"}})
    assert bad.status_code == 422
    pend = client.get(f"{API}/companies/{cid}/approvals?status=pending", headers=h).json()["approvals"]
    assert pend == []
    assert "approval.approved" in _actions(client, h, cid)


def test_agent_hire_waits_for_approval(client):
    """An agent hiring (PR C's path) creates a pending agent and a hire_agent
    approval when the company asks for approvals; approving activates it."""
    h = _user()
    owner = h["x-test-user"]
    cid = _company(client, h, require_approval_for_new_agents=True)["id"]
    ceo = _agent(client, h, cid)   # the person hires directly
    assert ceo["status"] == "idle"
    db = _TS()
    hired = co.create_agent(db, owner, co.Actor("agent", ceo["id"], "run-x"), cid,
                            {"name": "Bo", "role": "engineer", "reports_to": ceo["id"], "model": "gpt-5@OpenAI"})
    db.close()
    assert hired["status"] == "pending_approval"
    aps = client.get(f"{API}/companies/{cid}/approvals?status=pending", headers=h).json()["approvals"]
    assert aps[0]["type"] == "hire_agent" and aps[0]["payload"]["agent_id"] == hired["id"]
    assert aps[0]["payload"]["model"] == "gpt-5@OpenAI"
    r = client.post(f"{API}/approvals/{aps[0]['id']}/decide", headers=h, json={"decision": "approved"})
    assert r.status_code == 200
    assert client.get(f"{API}/agents/{hired['id']}", headers=h).json()["status"] == "idle"
    # The CEO that asked is woken to hear the answer.
    wk = client.get(f"{API}/companies/{cid}/wakeups", headers=h).json()["wakeups"]
    assert any(w["source"] == "approval" and w["agent_id"] == ceo["id"] for w in wk)


def test_dashboard_counts(client):
    h = _user()
    cid, ceo, eng = _team(client, h)
    _issue(client, h, cid)
    _issue(client, h, cid, status="todo")
    client.post(f"{API}/companies/{cid}/approvals", headers=h, json={"type": "plan"})
    d = client.get(f"{API}/companies/{cid}/dashboard", headers=h).json()
    assert d["agents"]["idle"] == 2 and d["agent_total"] == 2
    assert d["issues"]["backlog"] == 1 and d["issues"]["todo"] == 1 and d["issue_total"] == 2
    assert d["pending_approvals"] == 1
    assert d["recent_activity"][0]["action"] == "approval.created"


def test_every_mutation_is_logged(client):
    h = _user()
    cid, ceo, eng = _team(client, h)
    i = _issue(client, h, cid)
    client.patch(f"{API}/issues/{i['id']}", headers=h, json={"priority": "high"})
    client.patch(f"{API}/agents/{eng['id']}", headers=h, json={"title": "Staff engineer"})
    g = client.post(f"{API}/companies/{cid}/goals", headers=h, json={"title": "G"}).json()
    client.delete(f"{API}/goals/{g['id']}", headers=h)
    client.delete(f"{API}/issues/{i['id']}", headers=h)
    acts = _actions(client, h, cid)
    for a in ("company.created", "agent.created", "issue.created", "issue.updated", "agent.updated",
              "goal.created", "goal.deleted", "issue.deleted"):
        assert a in acts, a
    feed = client.get(f"{API}/companies/{cid}/activity", headers=h).json()["activity"]
    assert all(a["actor_type"] == "user" and a["actor_id"] == h["x-test-user"] for a in feed)
