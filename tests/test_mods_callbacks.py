"""Provider callbacks and scopes (src/mods/capabilities.py): a mod that
`provides` a capability can list the mods that declared it (`consumers`, with
their `scopes`) and call one of them back (`POST cap/<cap>/<action>?to=<mod>`),
through the same broker. This is how the Discord mod hands a slash command to
the mod that owns it."""
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import capabilities, manifest as mf, routes, signing
from tests.mods_helpers import UvicornThread, add_mod, mark_running, mods_dir  # noqa: F401


class FakeAuth:
    users = {"alice": {}, "bob": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"


class As(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        u = request.headers.get("x-test-user")
        if u:
            request.state.current_user = u
            request.state.api_token = False
        return await call_next(request)


@pytest.fixture
def env(mods_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    import src.event_bus
    monkeypatch.setattr(src.event_bus, "_resolve_event_owner", lambda owner: owner or "alice")
    capabilities._calls.clear()
    routes._calls.clear()
    app = FastAPI()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(As)
    with TestClient(app) as client:
        yield client


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def _man(**extra):
    raw = {"id": "x-mod", "name": "X", "version": "1.0.0", "runtime": {"type": "process", "command": ["x"]},
           "tutorial": {"steps": [{"title": "Hi", "body": "Hello"}]}}
    raw.update(extra)
    return raw


def _provider():
    return add_mod({"provides": ["chat.commands"]}, mod_id="chatbot")


def _consumer(**over):
    raw = {"optional": ["chat.commands"], "scopes": {"chat.commands": ["print"]}}
    raw.update(over)
    return add_mod(raw, mod_id="printer")


# ── The manifest ─────────────────────────────────────────────────────────

def test_scopes_are_checked_and_kept():
    m = mf.validate(_man(optional=["chat.commands"], scopes={"chat.commands": ["print", "print", "x_1"]}))
    assert m["scopes"] == {"chat.commands": ["print", "x_1"]}
    assert mf.validate(_man())["scopes"] == {}


@pytest.mark.parametrize("bad", [
    {"chat.commands": ["Print"]}, {"chat.commands": "print"}, {"chat.commands": [1]},
    {"other.cap": ["a"]},                           # not in requires or optional
    {"chat.commands": [f"g{i}" for i in range(17)]}, ["print"]])
def test_bad_scopes_are_refused(bad):
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(_man(optional=["chat.commands"], scopes=bad))
    assert any("scopes" in x for x in e.value.errors)


def test_the_install_review_shows_scopes():
    m = mf.validate(_man(optional=["chat.commands"], scopes={"chat.commands": ["print"]}))
    assert capabilities.availability(m)["scopes"] == {"chat.commands": ["print"]}


# ── consumers ────────────────────────────────────────────────────────────

def test_a_provider_lists_its_consumers_with_their_scopes(env):
    _, ptok = _provider()
    _consumer()
    add_mod({"optional": ["chat.commands"]}, mod_id="quiet", enabled=False)       # off: not listed
    add_mod({}, mod_id="unrelated")                                                 # did not declare it
    r = env.get("/api/mods/v1/cap/chat.commands/consumers", headers=bearer(ptok))
    assert r.status_code == 200
    rows = r.json()["consumers"]
    assert [c["id"] for c in rows] == ["printer"]
    assert rows[0]["scopes"] == ["print"] and rows[0]["requires"] is False and rows[0]["admin_only"] is False


def test_only_a_provider_may_list_consumers(env):
    _, tok = _consumer()
    r = env.get("/api/mods/v1/cap/chat.commands/consumers", headers=bearer(tok))
    assert r.status_code == 403


# ── callbacks ────────────────────────────────────────────────────────────

def _consumer_app(token, seen, answer=None):
    app = FastAPI()

    @app.post("/mod/cap/{cap}/{action}")
    async def handle(cap: str, action: str, request: Request):
        user = signing.verify_identity(token, request.headers)
        seen.append({"cap": cap, "action": action, "user": user, "caller": request.headers.get("x-odysseus-caller"),
                     "callback": request.headers.get("x-odysseus-callback"), "body": await request.json()})
        return answer if answer is not None else {"ok": True, "content": "pong"}
    return app


def test_a_provider_calls_a_consumer_back(env):
    _, ptok = _provider()
    _, ctok = _consumer()
    seen = []
    with UvicornThread(_consumer_app(ctok, seen)) as srv:
        mark_running("printer", srv.port)
        r = env.post("/api/mods/v1/cap/chat.commands/interaction?to=printer", headers=bearer(ptok),
                     json={"user": "alice", "name": "status"})
        assert r.status_code == 200
        d = r.json()
        assert d["content"] == "pong" and d["to"] == "printer" and d["provider"] == "chatbot"
        assert seen == [{"cap": "chat.commands", "action": "interaction", "user": "alice", "caller": "chatbot",
                         "callback": "1", "body": {"name": "status"}}]
    last = capabilities.read_log()[0]
    assert last["callback"] is True and last["caller"] == "chatbot" and last["provider"] == "printer"


def test_only_a_provider_may_call_back(env):
    _, ctok = _consumer()
    add_mod({"optional": ["chat.commands"]}, mod_id="other")
    r = env.post("/api/mods/v1/cap/chat.commands/interaction?to=other", headers=bearer(ctok), json={})
    assert r.status_code == 403 and "does not provide" in r.json()["detail"]


def test_the_target_must_have_declared_the_capability(env):
    _, ptok = _provider()
    add_mod({}, mod_id="plain")
    r = env.post("/api/mods/v1/cap/chat.commands/interaction?to=plain", headers=bearer(ptok), json={})
    assert r.status_code == 403 and "did not declare" in r.json()["detail"]


def test_a_missing_or_disabled_target_is_a_404(env):
    _, ptok = _provider()
    add_mod({"optional": ["chat.commands"]}, mod_id="off", enabled=False)
    for to in ("nobody", "off"):
        r = env.post(f"/api/mods/v1/cap/chat.commands/interaction?to={to}", headers=bearer(ptok), json={})
        assert r.status_code == 404


def test_a_provider_cannot_call_itself(env):
    _, ptok = add_mod({"provides": ["chat.commands"], "optional": ["chat.commands"]}, mod_id="chatbot")
    r = env.post("/api/mods/v1/cap/chat.commands/interaction?to=chatbot", headers=bearer(ptok), json={})
    assert r.status_code == 400


def test_the_user_must_be_allowed_to_use_the_target(env):
    _, ptok = add_mod({"provides": ["chat.commands"]}, mod_id="chatbot", access="users")
    add_mod({"optional": ["chat.commands"]}, mod_id="printer")             # admins only
    r = env.post("/api/mods/v1/cap/chat.commands/interaction?to=printer", headers=bearer(ptok),
                 json={"user": "bob"})
    assert r.status_code == 403


def test_a_consumer_error_comes_back_with_its_status(env):
    _, ptok = _provider()
    _consumer()
    app = FastAPI()

    @app.post("/mod/cap/{cap}/{action}")
    async def boom(cap: str, action: str):
        return JSONResponse({"error": "no such command"}, status_code=404)
    with UvicornThread(app) as srv:
        mark_running("printer", srv.port)
        r = env.post("/api/mods/v1/cap/chat.commands/interaction?to=printer", headers=bearer(ptok), json={})
        assert r.status_code == 404 and "no such command" in r.json()["detail"]
    assert capabilities.read_log()[0]["ok"] is False


def test_callbacks_are_rate_limited(env, monkeypatch):
    _, ptok = _provider()
    _consumer()

    def slow(*a):
        raise capabilities.CapError(429, "slow")
    monkeypatch.setattr(capabilities, "_rate", slow)
    r = env.post("/api/mods/v1/cap/chat.commands/interaction?to=printer", headers=bearer(ptok), json={})
    assert r.status_code == 429


def test_a_plain_call_still_goes_to_the_provider(env):
    """Without ?to= nothing changes: the consumer calls the provider."""
    _provider()
    _, ctok = _consumer()
    seen = []
    app = FastAPI()

    @app.post("/mod/cap/{cap}/{action}")
    async def handle(cap: str, action: str, request: Request):
        seen.append((request.headers.get("x-odysseus-caller"), request.headers.get("x-odysseus-callback")))
        return {"ok": True}
    with UvicornThread(app) as srv:
        mark_running("chatbot", srv.port)
        r = env.post("/api/mods/v1/cap/chat.commands/register", headers=bearer(ctok), json={})
        assert r.status_code == 200 and r.json()["provider"] == "chatbot"
    assert seen == [("printer", None)]
