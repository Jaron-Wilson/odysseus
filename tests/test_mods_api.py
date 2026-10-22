"""The mod API /api/mods/v1 (src/mods/routes.py): the token, permission
scopes, rate limits, events between mods, storage and settings; and the
admin routes refusing non-admins."""
import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import events, notify, registry, routes
from tests.mods_helpers import add_mod, mods_dir  # noqa: F401


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
def client(mods_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(As)
    routes._calls.clear()
    return TestClient(app)


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def test_no_or_bad_token(client):
    assert client.get("/api/mods/v1/me").status_code == 401
    assert client.get("/api/mods/v1/me", headers=bearer("odm_nope")).status_code == 401
    assert client.get("/api/mods/v1/me", headers=bearer("ody_user_api_token")).status_code == 401


def test_me_and_disabled_mods(client):
    _, tok = add_mod(permissions=["notify"])
    r = client.get("/api/mods/v1/me", headers=bearer(tok))
    assert r.status_code == 200 and r.json()["id"] == "test-mod" and r.json()["permissions"] == ["notify"]
    registry.set_enabled("test-mod", False)
    assert client.get("/api/mods/v1/me", headers=bearer(tok)).status_code == 403


@pytest.mark.parametrize("method, path, body, perm", [
    ("post", "/api/mods/v1/notify", {"title": "x"}, "notify"),
    ("post", "/api/mods/v1/events", {"type": "a.b"}, "events.emit"),
    ("get", "/api/mods/v1/events/subscriptions", None, "events.subscribe"),
    ("get", "/api/mods/v1/devices", None, "devices.read"),
    ("post", "/api/mods/v1/devices/phone/command", {"command": "notify"}, "devices.control"),
    ("get", "/api/mods/v1/kv", None, "storage"),
    ("post", "/api/mods/v1/llm/chat", {"messages": [{"role": "user", "content": "hi"}]}, "llm.call"),
    ("get", "/api/mods/v1/stats?scope=devops", None, "stats.read"),
    ("get", "/api/mods/v1/stats/transcripts/0123abcd", None, "stats.read"),
    ("get", "/api/mods/v1/machines", None, "machines.read"),
])
def test_each_route_needs_its_permission(client, method, path, body, perm):
    _, tok = add_mod(permissions=[])
    kw = {"headers": bearer(tok)}
    if body is not None:
        kw["json"] = body
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 403 and perm in r.json()["detail"]


def test_notify_goes_through_the_core(client, monkeypatch):
    _, tok = add_mod(permissions=["notify"])
    sent = []

    async def fake_send(mod_id, owner, msg):
        sent.append((mod_id, owner, msg))
        return {"push": 1}
    monkeypatch.setattr(notify, "send", fake_send)
    r = client.post("/api/mods/v1/notify", headers=bearer(tok),
                    json={"level": "error", "title": "Print failed", "body": "Spaghetti", "url": "/mods/test-mod/",
                          "tag": "job-1", "user": "bob"})
    assert r.status_code == 200 and r.json()["channels"] == {"push": 1}
    mod_id, owner, msg = sent[0]
    # bob may not use an admin-only mod, so it goes to the primary admin instead.
    assert mod_id == "test-mod" and owner != "bob" and msg["level"] == "error"
    bad = client.post("/api/mods/v1/notify", headers=bearer(tok), json={"title": "x", "url": "https://evil/"})
    assert bad.status_code == 400


def test_notify_is_rate_limited(client, monkeypatch):
    _, tok = add_mod(permissions=["notify"])

    async def fake_send(*a):
        return {}
    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setitem(routes.RATE_LIMITS, "notify", (2, 60))
    codes = [client.post("/api/mods/v1/notify", headers=bearer(tok), json={"title": "x"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_events_between_mods(client, monkeypatch):
    _, emitter = add_mod({"events": {"emits": ["print.finished"]}}, permissions=["events.emit"], mod_id="print-hub")
    _, listener = add_mod({"events": {"subscribes": ["print-hub/print.*"]}}, permissions=["events.subscribe"],
                          mod_id="listener")
    add_mod({"events": {"subscribes": ["*.finished"]}}, permissions=["events.subscribe"], mod_id="off-mod",
            enabled=False)
    delivered = []

    async def fake_deliver(mod_id, path, event):
        delivered.append((mod_id, path, event["type"], event["source"]))
    monkeypatch.setattr(events, "_deliver", fake_deliver)
    r = client.post("/api/mods/v1/events", headers=bearer(emitter),
                    json={"type": "print.finished", "data": {"job": 7}})
    assert r.status_code == 200 and r.json()["delivered_to"] == ["listener"]
    assert client.post("/api/mods/v1/events", headers=bearer(emitter), json={"type": "print.other"}).status_code == 400
    # A run-time subscription, to a path on the listener's own server only.
    assert client.post("/api/mods/v1/events/subscriptions", headers=bearer(listener),
                       json={"pattern": "*/*", "path": "http://evil/"}).status_code == 400
    assert client.post("/api/mods/v1/events/subscriptions", headers=bearer(listener),
                       json={"pattern": "*/print.finished", "path": "/hooks/done"}).status_code == 200
    subs = client.get("/api/mods/v1/events/subscriptions", headers=bearer(listener)).json()["subscriptions"]
    assert {s["path"] for s in subs} == {"/mod/events", "/hooks/done"}


def test_event_patterns():
    assert events.matches("print-hub/print.*", "print-hub", "print.failed")
    assert not events.matches("print-hub/print.*", "rook", "print.failed")
    assert events.matches("*.failed", "rook", "game.failed")
    assert not events.matches("*.failed", "rook", "game.done")


def test_storage_and_settings(client):
    _, tok = add_mod(permissions=["storage"])
    h = bearer(tok)
    assert client.put("/api/mods/v1/kv/last.job", headers=h, json={"value": {"id": 3}}).status_code == 200
    assert client.get("/api/mods/v1/kv/last.job", headers=h).json()["value"] == {"id": 3}
    assert client.get("/api/mods/v1/kv", headers=h).json()["keys"] == ["last.job"]
    assert client.put("/api/mods/v1/kv/bad key", headers=h, json={"value": 1}).status_code in (400, 404)
    assert client.delete("/api/mods/v1/kv/last.job", headers=h).json()["existed"] is True
    assert client.get("/api/mods/v1/kv/last.job", headers=h).status_code == 404
    assert client.put("/api/mods/v1/settings", headers=h, json={"settings": {"camera": "on"}}).status_code == 200
    assert client.get("/api/mods/v1/settings", headers=h).json()["settings"] == {"camera": "on"}


def test_one_mod_cannot_read_another_mods_storage(client):
    _, a = add_mod(permissions=["storage"], mod_id="mod-a")
    _, b = add_mod(permissions=["storage"], mod_id="mod-b")
    client.put("/api/mods/v1/kv/secret", headers=bearer(a), json={"value": "a's"})
    assert client.get("/api/mods/v1/kv/secret", headers=bearer(b)).status_code == 404


def test_admin_routes_need_an_admin(client):
    assert client.get("/api/mods", headers={"x-test-user": "bob"}).status_code == 403
    assert client.get("/api/mods", headers={"x-test-user": "alice"}).status_code == 200
    assert client.post("/api/mods/install/prepare", headers={"x-test-user": "bob"},
                       json={"source": "/tmp"}).status_code == 403
    # A mod's own token is not an admin login.
    _, tok = add_mod()
    assert client.get("/api/mods", headers=bearer(tok)).status_code in (401, 403)


def test_ui_list_gives_frame_urls_only_for_mods_the_user_may_use(client):
    add_mod({"ui": {"tabs": [{"id": "main", "path": "/"}], "home_cards": [{"id": "c", "path": "/card"}]}})
    add_mod({"ui": {"tabs": [{"id": "main", "path": "/"}]}}, mod_id="shared-mod", access="users")
    alice = client.get("/api/mods/ui", headers={"x-test-user": "alice"}).json()["mods"]
    bob = client.get("/api/mods/ui", headers={"x-test-user": "bob"}).json()["mods"]
    assert {m["id"] for m in alice} == {"test-mod", "shared-mod"}
    assert {m["id"] for m in bob} == {"shared-mod"}
    tab = next(m for m in alice if m["id"] == "test-mod")["tabs"][0]
    assert tab["url"].startswith("/mods/test-mod/~") and tab["url"].endswith("/")


# ── Read-only core APIs: stats (stats.read) and machines (machines.read) ──

def test_stats_reads_the_devops_snapshot(client, monkeypatch):
    from src import devops_stats
    seen = {}

    def snap(hours=24, owner=""):
        seen["hours"] = hours
        return {"now": 1.0, "hours": hours, "windows": [1, 24, 168, 720], "live": {"replies": []},
                "chat": {"models": []}, "coders": {"runs": 0}}
    monkeypatch.setattr(devops_stats, "snapshot", snap)
    _, tok = add_mod(permissions=["stats.read"])
    r = client.get("/api/mods/v1/stats?scope=devops&hours=168", headers=bearer(tok))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["scope"] == "devops" and d["hours"] == 168 and seen["hours"] == 168 and "live" in d
    assert client.get("/api/mods/v1/stats?scope=email", headers=bearer(tok)).status_code == 400
    assert client.get("/api/mods/v1/stats", headers=bearer(tok)).status_code == 400


def test_stats_are_only_for_a_mod_only_admins_may_use(client, monkeypatch):
    from src import devops_stats
    monkeypatch.setattr(devops_stats, "snapshot", lambda hours=24, owner="": {"hours": hours})
    _, tok = add_mod(permissions=["stats.read"], access="users")
    r = client.get("/api/mods/v1/stats?scope=devops", headers=bearer(tok))
    assert r.status_code == 403 and "admins" in r.json()["detail"]
    assert client.get("/api/mods/v1/stats/transcripts/0123abcd", headers=bearer(tok)).status_code == 403


def test_stats_are_rate_limited(client, monkeypatch):
    from src import devops_stats
    monkeypatch.setattr(devops_stats, "snapshot", lambda hours=24, owner="": {"hours": hours})
    monkeypatch.setitem(routes.RATE_LIMITS, "stats.read", (2, 60))
    _, tok = add_mod(permissions=["stats.read"])
    codes = [client.get("/api/mods/v1/stats?scope=devops", headers=bearer(tok)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_stats_transcript_is_the_core_viewers_detail(client, monkeypatch):
    import routes.transcript_routes as tr
    rec = {"id": "0123abcd", "owner": "bob", "engine": "claude", "model": "opus", "action": "fix",
           "status": "done", "started": 1.0, "finished": 2.0, "chat_session_id": "s1", "chat_name": "Chat"}
    monkeypatch.setattr(tr, "_record_for", lambda rid: rec if rid == "0123abcd" else None)
    monkeypatch.setattr(tr.summaries, "get_summary", lambda d: {
        "engine": "claude", "counts": {"messages": 3}, "blocks": [{"kind": "text", "text": "hi"}],
        "final_text": "done", "resume_command": "claude --resume x", "preview": "hi"})
    _, tok = add_mod(permissions=["stats.read"])
    r = client.get("/api/mods/v1/stats/transcripts/0123abcd", headers=bearer(tok))
    assert r.status_code == 200, r.text
    d = r.json()
    # Server-wide: another user's run is readable by an admin-only mod.
    assert d["id"] == "0123abcd" and d["counts"] == {"messages": 3} and d["blocks"][0]["text"] == "hi"
    assert d["resume_command"] == "claude --resume x" and d["chat_session_id"] == "s1"
    assert client.get("/api/mods/v1/stats/transcripts/ffffffff", headers=bearer(tok)).status_code == 404
    assert client.get("/api/mods/v1/stats/transcripts/..%2Fetc", headers=bearer(tok)).status_code == 404
    assert tr.detail_for("../../x") is None


def test_machines_lists_tailnet_peers_with_their_ssh_user(client, monkeypatch):
    from src import machines
    calls = []
    peers = [{"host": "desktop-jaron", "name": "DESKTOP-JARON", "dns": "desktop-jaron.tail.ts.net",
              "ips": ["100.102.86.125"], "os": "windows", "online": True, "last_seen": "", "age_days": 0.1,
              "is_self": False, "shared": False},
             {"host": "jaron-dev-server", "name": "jaron-dev-server", "dns": "jaron-dev-server.tail.ts.net",
              "ips": ["100.121.62.9"], "os": "linux", "online": True, "last_seen": "", "age_days": 0.0,
              "is_self": True, "shared": False}]
    monkeypatch.setattr(machines, "peers", lambda refresh=False: calls.append(refresh) or peers)
    monkeypatch.setattr(machines, "load_prefs", lambda: {"desktop-jaron": {"ssh_user": "jaron", "gpu": True}})
    _, tok = add_mod(permissions=["machines.read"])
    r = client.get("/api/mods/v1/machines", headers=bearer(tok))
    assert r.status_code == 200, r.text
    ms = r.json()["machines"]
    assert [m["host"] for m in ms] == ["desktop-jaron", "jaron-dev-server"]
    assert ms[0]["ssh_user"] == "jaron" and ms[1]["ssh_user"] == "" and ms[0]["ips"] == ["100.102.86.125"]
    assert set(ms[0]) == {"host", "name", "dns", "ips", "os", "online", "last_seen", "is_self", "shared", "ssh_user"}
    client.get("/api/mods/v1/machines?refresh=1", headers=bearer(tok))
    assert calls == [False, True]


def test_an_admin_only_mod_is_never_for_other_users(client):
    """`admin_only` in the manifest (a mod that gives a shell, say): even with
    its access set to "users" by hand, only admins get its pages and tools,
    and the access setting can't be changed to "users"."""
    from src.mods import access, tools
    entry, tok = add_mod({"admin_only": True, "ui": {"tabs": [{"id": "main", "path": "/"}]}},
                         mod_id="shell-mod", access="users")
    assert access.may_use(entry, "alice") and not access.may_use(entry, "bob")
    assert not tools._user_allowed(entry, "bob") and tools._user_allowed(entry, "alice")
    bob = client.get("/api/mods/ui", headers={"x-test-user": "bob"}).json()["mods"]
    assert "shell-mod" not in {m["id"] for m in bob}
    alice = client.get("/api/mods/ui", headers={"x-test-user": "alice"}).json()["mods"]
    assert "shell-mod" in {m["id"] for m in alice}
    r = client.post("/api/mods/shell-mod/access", headers={"x-test-user": "alice"}, json={"access": "users"})
    assert r.status_code == 400 and "only for admins" in r.text
    listed = next(m for m in client.get("/api/mods", headers={"x-test-user": "alice"}).json()["mods"]
                  if m["id"] == "shell-mod")
    assert listed["admin_only"] is True and listed["access"] == "admins"
    me = client.get("/api/mods/v1/me", headers=bearer(tok)).json()
    assert me["admin_only"] is True and me["access"] == "admins"
    # Without the flag the same setting opens it to everyone.
    plain, _ = add_mod(mod_id="plain-mod", access="users")
    assert access.may_use(plain, "bob")
