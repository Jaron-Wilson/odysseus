"""The Paperclip integration's backend (src/paperclip.py)."""
import json
import os
import stat

import httpx
import pytest

from src import paperclip, secret_storage

TOKEN = "pcp_board_SECRETSECRET"
CID = "11111111-2222-4333-8444-555555555555"
AID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
URL = "https://paperclip.example.ts.net"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.delenv("ODYSSEUS_PAPERCLIP_URL", raising=False)
    monkeypatch.delenv("ODYSSEUS_PAPERCLIP_TOKEN", raising=False)
    monkeypatch.setattr(paperclip, "_TRANSPORT", None)
    paperclip._cache.update(at=0.0, key=None, status=None)
    return tmp_path


BODIES = {
    "/api/health": {"status": "ok", "version": "1.2.3", "deploymentMode": "authenticated",
                    "deploymentExposure": "private", "extra": "x"},
    "/api/cli-auth/me": {"userId": "u1", "isInstanceAdmin": True, "companyIds": [CID],
                         "user": {"email": "a@b"}, "keyId": "k"},
    "/api/companies": [{"id": CID, "name": "Acme", "status": "active", "issuePrefix": "ACM", "budget": 9}],
    f"/api/companies/{CID}/dashboard": {
        "companyId": CID,
        "agents": {"active": 2, "running": 1, "paused": 0, "error": 1},
        "tasks": {"open": 5, "inProgress": 2, "blocked": 1, "done": 9},
        "costs": {"monthSpendCents": 1234, "monthBudgetCents": 5000, "monthUtilizationPercent": 24.68},
        "pendingApprovals": 3},
    f"/api/companies/{CID}/agents": [{"id": AID, "name": "CEO", "role": "ceo", "title": "Chief",
                                      "status": "idle", "adapterType": "claude_local",
                                      "lastHeartbeatAt": "2026-10-10T00:00:00Z",
                                      "adapterConfig": {"apiKey": "leak"}}],
    f"/api/companies/{CID}/issues": [{"id": "i1", "identifier": "ACM-1", "title": "Do it", "status": "todo",
                                      "priority": "high", "assigneeAgentId": AID, "assigneeUserId": None,
                                      "updatedAt": "2026-10-10T00:00:00Z", "description": "long"}],
    f"/api/companies/{CID}/heartbeat-runs": [{"id": "r1", "agentId": AID, "status": "failed",
                                              "invocationSource": "timer", "createdAt": "c", "startedAt": "s",
                                              "finishedAt": "f", "error": "x" * 1000, "stdout": "big"}],
    f"/api/companies/{CID}/live-runs": [{"id": "r2", "agentId": AID, "status": "running",
                                         "invocationSource": "on_demand", "createdAt": "c"}],
    f"/api/companies/{CID}/activity": [{"id": "e1", "action": "issue.created", "entityType": "issue",
                                        "entityId": "i1", "actorType": "agent", "actorId": AID,
                                        "agentId": AID, "createdAt": "c", "details": {"secret": 1}}],
}


def _server(calls, status=None, body=None, text=None, raise_exc=None):
    def handler(request):
        assert request.method == "GET"
        calls.append(request)
        if raise_exc:
            raise raise_exc(request)
        if status is not None:
            if text is not None:
                return httpx.Response(status, text=text)
            return httpx.Response(status, json=body if body is not None else {"error": "nope"})
        b = BODIES.get(request.url.path)
        if b is None:
            return httpx.Response(404, json={"error": "not found"})
        if text is not None:
            return httpx.Response(200, text=text)
        return httpx.Response(200, json=b)
    return httpx.MockTransport(handler)


def _configure(token=TOKEN):
    return paperclip.save_config(URL, token=token, company_id=CID)


# ── Settings ────────────────────────────────────────────────────────────

def test_the_token_is_encrypted_on_disk_and_never_public(data_dir):
    pub = _configure()
    assert pub == {"url": URL, "company_id": CID, "has_token": True}
    raw = (data_dir / "paperclip.json").read_text()
    assert TOKEN not in raw
    on_disk = json.loads(raw)
    assert set(on_disk) == {"url", "token_enc", "company_id"}
    assert on_disk["token_enc"].startswith("enc:")
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(data_dir / "paperclip.json").st_mode) == 0o600
    assert TOKEN not in json.dumps(paperclip.public_config())
    assert paperclip.load_config()["token"] == TOKEN
    assert (data_dir / ".app_key").exists()


def test_an_empty_token_keeps_and_clear_token_drops(data_dir):
    _configure()
    assert paperclip.save_config(URL + "/", token="")["has_token"] is True
    assert paperclip.save_config(URL, token=None)["has_token"] is True
    assert paperclip.load_config()["token"] == TOKEN
    assert paperclip.save_config(URL, clear_token=True)["has_token"] is False
    assert json.loads((data_dir / "paperclip.json").read_text())["token_enc"] == ""


def test_env_fallback(data_dir, monkeypatch):
    assert paperclip.public_config() == {"url": "", "company_id": None, "has_token": False}
    monkeypatch.setenv("ODYSSEUS_PAPERCLIP_URL", "http://127.0.0.1:3100/")
    monkeypatch.setenv("ODYSSEUS_PAPERCLIP_TOKEN", TOKEN)
    assert paperclip.public_config() == {"url": "http://127.0.0.1:3100", "company_id": None, "has_token": True}


@pytest.mark.parametrize("bad", ["ftp://x/", "javascript:alert(1)", "paperclip.local", "file:///etc/passwd",
                                 "https://x/?a=1", "https://x/#f", "https://u:p@x/", "https:///"])
def test_bad_addresses_are_refused(data_dir, bad):
    with pytest.raises(ValueError):
        paperclip.save_config(bad)
    assert not (data_dir / "paperclip.json").exists()


def test_bad_company_id_is_refused(data_dir):
    with pytest.raises(ValueError):
        paperclip.save_config(URL, company_id="../../etc")


# ── Requests ────────────────────────────────────────────────────────────

async def test_bearer_is_sent_and_paths_and_params_are_fixed(data_dir):
    _configure()
    calls = []
    t = _server(calls)
    assert (await paperclip.agents(CID, transport=t))["ok"]
    assert calls[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert calls[-1].url.query == b""
    assert calls[-1].url.path == f"/api/companies/{CID}/agents"

    await paperclip.list_companies(transport=t)
    assert dict(calls[-1].url.params) == {"scope": "accessible"}
    await paperclip.issues(CID, status="todo,in_progress", q="  fix bug  ", transport=t)
    assert dict(calls[-1].url.params) == {"view": "compact", "limit": "200", "sortField": "updated",
                                          "sortDir": "desc", "status": "todo,in_progress", "q": "fix bug"}
    await paperclip.issues(CID, transport=t)
    assert dict(calls[-1].url.params) == {"view": "compact", "limit": "200", "sortField": "updated",
                                          "sortDir": "desc"}
    await paperclip.runs(CID, transport=t)
    assert dict(calls[-1].url.params) == {"summary": "true", "limit": "50"}
    await paperclip.runs(CID, agent_id=AID, transport=t)
    assert dict(calls[-1].url.params) == {"summary": "true", "limit": "50", "agentId": AID}
    await paperclip.live_runs(CID, transport=t)
    assert dict(calls[-1].url.params) == {"limit": "50"}
    await paperclip.activity(CID, transport=t)
    assert dict(calls[-1].url.params) == {"limit": "100"}
    await paperclip.dashboard(CID, transport=t)
    assert calls[-1].url.query == b""
    assert all(c.method == "GET" for c in calls)


async def test_no_bearer_without_a_token(data_dir):
    paperclip.save_config(URL)
    calls = []
    await paperclip.list_companies(transport=_server(calls))
    assert "authorization" not in calls[-1].headers


async def test_invalid_ids_and_filters_make_no_http_calls(data_dir):
    _configure()
    calls = []
    t = _server(calls)
    for bad in ("not-a-uuid", "../health", CID + "/x", "", None, AID.upper(), CID + "\n"):
        for fn in (paperclip.dashboard, paperclip.agents, paperclip.issues, paperclip.runs,
                   paperclip.live_runs, paperclip.activity):
            assert await fn(bad, transport=t) == {"ok": False, "error": "invalid_company_id"}
    assert (await paperclip.issues(CID, status="todo;drop", transport=t))["error"] == "invalid_status"
    assert (await paperclip.runs(CID, agent_id="nope", transport=t))["error"] == "invalid_agent_id"
    assert calls == []


async def test_not_configured(data_dir):
    assert await paperclip.list_companies() == {"ok": False, "error": "not_configured"}
    s = await paperclip.fetch_status()
    assert s["configured"] is False and s["ok"] is False and s["error"] == "not_configured"


@pytest.mark.parametrize("status,body,code", [
    (401, {"error": "Unauthorized"}, "auth_failed"),
    (403, {"error": "Hostname 'evil' is not allowed"}, "host_blocked"),
    (403, {"error": "Board access required"}, "forbidden"),
    (429, {"error": "slow down"}, "rate_limited"),
    (503, {"error": "internal stack trace SECRET"}, "upstream_error"),
    (404, {"error": "nope"}, "http_404"),
])
async def test_error_mapping(data_dir, status, body, code):
    _configure()
    out = await paperclip.agents(CID, transport=_server([], status=status, body=body))
    assert out["ok"] is False and out["error"] == code
    assert "SECRET" not in json.dumps(out) and "stack" not in json.dumps(out)
    if code == "host_blocked":
        assert "paperclip.example.ts.net" in out["detail"]


@pytest.mark.parametrize("exc", [
    lambda r: httpx.ConnectError("refused", request=r),
    lambda r: httpx.ReadTimeout("slow", request=r),
])
async def test_unreachable(data_dir, exc):
    _configure()
    out = await paperclip.list_companies(transport=_server([], raise_exc=exc))
    assert out["ok"] is False and out["error"] == "unreachable"


async def test_bad_json_and_bad_shape(data_dir):
    _configure()
    assert (await paperclip.agents(CID, transport=_server([], text="<html>")))["error"] == "bad_response"
    assert (await paperclip.agents(CID, transport=_server([], status=200, body={"x": 1})))["error"] == "bad_response"


async def test_shapes_are_trimmed(data_dir):
    _configure()
    t = _server([])
    assert await paperclip.list_companies(transport=t) == {
        "ok": True, "companies": [{"id": CID, "name": "Acme", "status": "active", "issuePrefix": "ACM"}]}
    d = (await paperclip.dashboard(CID, transport=t))["dashboard"]
    assert d == {"agents": {"active": 2, "running": 1, "paused": 0, "error": 1},
                 "tasks": {"open": 5, "inProgress": 2, "blocked": 1, "done": 9},
                 "costs": {"monthSpendCents": 1234, "monthBudgetCents": 5000, "monthUtilizationPercent": 24.68},
                 "pendingApprovals": 3}
    a = (await paperclip.agents(CID, transport=t))["agents"][0]
    assert set(a) == {"id", "name", "role", "title", "status", "adapterType", "lastHeartbeatAt"}
    i = (await paperclip.issues(CID, transport=t))["issues"][0]
    assert set(i) == {"id", "identifier", "title", "status", "priority", "assigneeAgentId",
                      "assigneeUserId", "updatedAt"}
    r = (await paperclip.runs(CID, transport=t))["runs"][0]
    assert set(r) == {"id", "agentId", "status", "invocationSource", "createdAt", "startedAt",
                      "finishedAt", "error"}
    assert len(r["error"]) == paperclip.RUN_ERROR_MAX
    lr = (await paperclip.live_runs(CID, transport=t))["runs"][0]
    assert lr["id"] == "r2" and lr["error"] is None and lr["finishedAt"] is None
    e = (await paperclip.activity(CID, transport=t))["events"][0]
    assert set(e) == {"id", "action", "entityType", "entityId", "actorType", "actorId", "agentId", "createdAt"}


async def test_status_ok_and_cached(data_dir):
    _configure()
    calls = []
    s = await paperclip.fetch_status(transport=_server(calls))
    assert s == {"configured": True, "ok": True,
                 "health": {"status": "ok", "version": "1.2.3", "deploymentMode": "authenticated",
                            "deploymentExposure": "private"},
                 "me": {"userId": "u1", "isInstanceAdmin": True, "companyIds": [CID]},
                 "config": {"url": URL, "company_id": CID, "has_token": True}}
    assert [c.url.path for c in calls] == ["/api/health", "/api/cli-auth/me"]
    await paperclip.fetch_status(transport=_server(calls))
    assert len(calls) == 2
    assert TOKEN not in json.dumps(s)


async def test_status_local_trusted_without_token(data_dir, monkeypatch):
    paperclip.save_config(URL)
    monkeypatch.setitem(BODIES, "/api/health", {"status": "ok", "deploymentMode": "local_trusted"})
    calls = []
    s = await paperclip.fetch_status(transport=_server(calls))
    assert s["ok"] is True and s["me"] is None
    assert [c.url.path for c in calls] == ["/api/health"]


async def test_status_auth_failure(data_dir):
    _configure()

    def handler(request):
        if request.url.path == "/api/health":
            return httpx.Response(200, json=BODIES["/api/health"])
        return httpx.Response(401, json={"error": "bad"})
    s = await paperclip.fetch_status(transport=httpx.MockTransport(handler))
    assert s["ok"] is False and s["error"] == "auth_failed" and s["health"]["status"] == "ok"


async def test_status_unreachable(data_dir):
    _configure()
    s = await paperclip.fetch_status(
        transport=_server([], raise_exc=lambda r: httpx.ConnectError("x", request=r)))
    assert s["configured"] and not s["ok"] and s["error"] == "unreachable" and s["health"] is None


def test_paths_outside_the_allowlist_are_refused():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(paperclip._get("/api/companies/x/issues/1/delete"))
