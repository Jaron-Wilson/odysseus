"""The Paperclip routes (routes/paperclip_routes.py)."""
import json
import os
import re

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import paperclip_routes
from src import paperclip, secret_storage

TOKEN = "pcp_board_SECRETSECRET"
CID = "11111111-2222-4333-8444-555555555555"
AID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
URL = "https://paperclip.example.ts.net"

GETS = [
    "/api/paperclip/status",
    "/api/paperclip/config",
    "/api/paperclip/companies",
    f"/api/paperclip/companies/{CID}/dashboard",
    f"/api/paperclip/companies/{CID}/agents",
    f"/api/paperclip/companies/{CID}/issues?status=todo&q=x",
    f"/api/paperclip/companies/{CID}/runs?agent_id={AID}",
    f"/api/paperclip/companies/{CID}/live-runs",
    f"/api/paperclip/companies/{CID}/activity",
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.delenv("ODYSSEUS_PAPERCLIP_URL", raising=False)
    monkeypatch.delenv("ODYSSEUS_PAPERCLIP_TOKEN", raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    paperclip._cache.update(at=0.0, key=None, status=None)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        p = request.url.path
        if p == "/api/health":
            return httpx.Response(200, json={"status": "ok", "deploymentMode": "authenticated"})
        if p == "/api/cli-auth/me":
            return httpx.Response(200, json={"userId": "u", "companyIds": [CID], "token": TOKEN})
        if p == "/api/companies/" + CID + "/dashboard":
            return httpx.Response(200, json={"agents": {}, "echo": TOKEN})
        # Everything else: a list whose rows try to leak the token.
        return httpx.Response(200, json=[{"id": "x", "secret": TOKEN,
                                          "authorization": request.headers.get("authorization")}])
    monkeypatch.setattr(paperclip, "_TRANSPORT", httpx.MockTransport(handler))
    return tmp_path, calls


class _Auth:
    is_configured = True

    def __init__(self, admins):
        self.admins = admins

    def is_admin(self, user):
        return user in self.admins


def _app(user=None, admins=()):
    app = FastAPI()
    app.state.auth_manager = _Auth(set(admins))

    @app.middleware("http")
    async def _who(request, call_next):
        request.state.current_user = user
        return await call_next(request)

    app.include_router(paperclip_routes.setup_paperclip_routes())
    return app


def test_only_an_admin_gets_anything(env):
    data_dir, calls = env
    for user in (None, "guest"):
        c = TestClient(_app(user=user, admins={"jaron"}))
        for path in GETS:
            assert c.get(path).status_code == 403, path
        assert c.put("/api/paperclip/config", json={"url": URL, "token": TOKEN}).status_code == 403
    assert not (data_dir / "paperclip.json").exists()
    assert calls == []


def test_admin_gets_everything_and_never_the_token(env):
    data_dir, calls = env
    c = TestClient(_app(user="jaron", admins={"jaron"}))
    r = c.put("/api/paperclip/config", json={"url": URL, "token": TOKEN, "company_id": CID})
    assert r.status_code == 200
    assert r.json() == {"url": URL, "company_id": CID, "has_token": True}
    texts = [r.text]
    for path in GETS:
        r = c.get(path)
        assert r.status_code == 200, path
        texts.append(r.text)
    assert c.get("/api/paperclip/status").json()["ok"] is True
    assert c.get("/api/paperclip/companies").json() == {
        "ok": True, "companies": [{"id": "x", "name": None, "status": None, "issuePrefix": None}]}
    for t in texts:
        assert TOKEN not in t
    assert calls and all(c.method == "GET" for c in calls)
    assert all(c.headers.get("authorization") == f"Bearer {TOKEN}" for c in calls)


def test_upstream_failures_are_ok_false_with_200(env, monkeypatch):
    c = TestClient(_app(user="jaron", admins={"jaron"}))
    c.put("/api/paperclip/config", json={"url": URL, "token": TOKEN})
    monkeypatch.setattr(paperclip, "_TRANSPORT",
                        httpx.MockTransport(lambda r: httpx.Response(401, text=f"bad token {TOKEN}")))
    r = c.get(f"/api/paperclip/companies/{CID}/agents")
    assert r.status_code == 200 and r.json()["error"] == "auth_failed"
    assert TOKEN not in r.text


def test_the_config_route_validates(env):
    data_dir, calls = env
    c = TestClient(_app(user="jaron", admins={"jaron"}))
    assert c.put("/api/paperclip/config", json={"url": "ftp://x/"}).status_code == 400
    assert c.put("/api/paperclip/config", json={"url": 5}).status_code == 400
    assert c.put("/api/paperclip/config", json={"url": URL, "token": 5}).status_code == 400
    assert c.put("/api/paperclip/config", json={"url": URL, "clear_token": "yes"}).status_code == 400
    assert c.put("/api/paperclip/config", json={"url": URL, "company_id": "nope"}).status_code == 400
    assert c.put("/api/paperclip/config", content=b"nope").status_code == 400
    assert c.put("/api/paperclip/config", json=[1]).status_code == 400
    assert c.put("/api/paperclip/config", json={"url": URL, "token": TOKEN}).json()["has_token"] is True
    assert c.put("/api/paperclip/config", json={"url": URL, "token": ""}).json()["has_token"] is True
    assert c.put("/api/paperclip/config", json={"url": URL, "clear_token": True}).json()["has_token"] is False
    assert c.get("/api/paperclip/config").json() == {"url": URL, "company_id": None, "has_token": False}


def test_invalid_ids_and_filters_are_400_without_upstream_calls(env):
    data_dir, calls = env
    c = TestClient(_app(user="jaron", admins={"jaron"}))
    c.put("/api/paperclip/config", json={"url": URL, "token": TOKEN})
    for suffix in ("dashboard", "agents", "issues", "runs", "live-runs", "activity"):
        r = c.get(f"/api/paperclip/companies/not-a-uuid/{suffix}")
        assert r.status_code == 400 and r.json()["detail"] == "invalid_company_id"
    r = c.get(f"/api/paperclip/companies/{CID}/issues?status=todo;x")
    assert r.status_code == 400 and r.json()["detail"] == "invalid_status"
    r = c.get(f"/api/paperclip/companies/{CID}/runs?agent_id=../x")
    assert r.status_code == 400 and r.json()["detail"] == "invalid_agent_id"
    assert calls == []


def test_the_router_has_no_write_routes_upstream():
    """Only the config route accepts anything but GET."""
    app = _app()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/paperclip/") and path != "/api/paperclip/config":
            assert route.methods == {"GET"}, path


def test_paperclip_routes_are_not_auth_exempt_in_app():
    """The real AuthMiddleware 401s these without a session: none of app.py's
    exemptions may cover /api/paperclip/*."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"),
               encoding="utf-8").read()
    assert "setup_paperclip_routes()" in src
    assert "/api/paperclip" not in src.split("AuthMiddleware(BaseHTTPMiddleware)")[0]
    for pat in re.findall(r'_re\.compile\(\s*r"([^"]+)"\s*\)', src):
        for path in GETS:
            assert not re.match(pat, path.split("?")[0]), (pat, path)
