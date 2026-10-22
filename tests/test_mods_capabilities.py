"""Mod capabilities (src/mods/capabilities.py): `provides`, `requires` and
`optional` in the manifest, calls brokered by the core at
/api/mods/v1/cap/<capability>/<action>, the messaging.sms rules (only the
user's own numbers unless they granted more, a rate limit, a log of every
send), its event, the install check for missing providers, and the warning
before disabling or removing a provider others depend on."""
import json
import os
import shutil

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from routes import prefs_routes, sms_routes
from src.mods import capabilities, catalog, events, manifest as mf, registry, routes, signing
from tests.mods_helpers import HELLO, UvicornThread, add_mod, mark_running, mods_dir  # noqa: F401

ALICE = "+15550102000"
OTHER = "+15550109999"


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
def env(mods_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    import src.event_bus
    monkeypatch.setattr(src.event_bus, "_resolve_event_owner", lambda owner: owner or "alice")
    monkeypatch.setattr(src.event_bus, "fire_event", lambda *a, **k: None)
    capabilities._calls.clear()
    routes._calls.clear()
    sent = []

    async def fake_post(url, payload):
        sent.append((url, payload))
        return 200
    monkeypatch.setattr(sms_routes, "_post_reply", fake_post)
    sms_routes.save_config("alice", {"numbers": [ALICE], "reply_url": "http://phone:8080/message"})
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(As)
    with TestClient(app) as client:
        yield {"client": client, "sent": sent}


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


ADMIN = {"x-test-user": "alice"}


def caller(perms=None, **over):
    raw = {"optional": ["messaging.sms"]}
    raw.update(over)
    return add_mod(raw, permissions=perms or [], mod_id=over.pop("mod_id", "notes-mod"))


# ── The manifest ─────────────────────────────────────────────────────────

def _man(**extra):
    raw = {"id": "x-mod", "name": "X", "version": "1.0.0", "runtime": {"type": "process", "command": ["x"]},
           "tutorial": {"steps": [{"title": "Hi", "body": "Hello"}]}}
    raw.update(extra)
    return raw


def test_manifest_fields_are_checked_and_kept():
    m = mf.validate(_man(provides=["home.lights"], requires=["messaging.sms"], optional=["storage.s3"]))
    assert m["provides"] == ["home.lights"] and m["requires"] == ["messaging.sms"] and m["optional"] == ["storage.s3"]
    assert mf.validate(_man())["requires"] == []


@pytest.mark.parametrize("bad", [["sms"], ["Messaging.SMS"], ["messaging..sms"], ["messaging.sms!"], "messaging.sms",
                                 [1], ["a." + "b" * 70]])
def test_bad_capability_ids_are_refused(bad):
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(_man(requires=bad))
    assert any("requires" in x for x in e.value.errors)


def test_a_capability_cannot_be_hard_and_soft():
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(_man(requires=["messaging.sms"], optional=["messaging.sms"]))
    assert any("both" in x for x in e.value.errors)


def test_only_a_provider_may_emit_a_capability_event():
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(_man(permissions=["events.emit"], events={"emits": ["messaging.sms.received"]},
                         optional=["messaging.sms"]))
    assert any("only a mod that provides" in x for x in e.value.errors)
    ok = mf.validate(_man(permissions=["events.emit"], events={"emits": ["messaging.sms.received"]},
                          provides=["messaging.sms"]))
    assert ok["provides"] == ["messaging.sms"]
    # Providing it without the event is allowed, with a note.
    warn = mf.validate(_man(provides=["messaging.sms"]))
    assert any("messaging.sms.received" in w for w in warn["warnings"])


# ── Calls ────────────────────────────────────────────────────────────────

def test_a_mod_may_only_call_what_it_declared(env):
    _, tok = add_mod({}, mod_id="plain")
    r = env["client"].post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok), json={"text": "hi"})
    assert r.status_code == 403 and "did not declare" in r.json()["detail"]
    assert env["client"].post("/api/mods/v1/cap/messaging.sms/send", json={"text": "hi"}).status_code == 401


def test_unknown_actions_and_bad_ids(env):
    _, tok = caller()
    c = env["client"]
    assert c.post("/api/mods/v1/cap/messaging.sms/delete_all", headers=bearer(tok), json={}).status_code == 404
    assert c.post("/api/mods/v1/cap/NOPE/send", headers=bearer(tok), json={}).status_code == 400


def test_send_defaults_to_the_users_own_number_and_is_logged(env):
    _, tok = caller()
    r = env["client"].post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok),
                           json={"text": "Your print finished", "user": "alice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "core" and body["parts"] == 1 and body["ok"]
    assert env["sent"] == [("http://phone:8080/message", {"textMessage": {"text": "Your print finished"},
                                                         "phoneNumbers": [ALICE]})]
    log = capabilities.read_log()
    assert log[0]["caller"] == "notes-mod" and log[0]["to"] == ALICE and log[0]["chars"] == 19 and log[0]["ok"]
    # The words of a text are never logged.
    with open(capabilities.log_file()) as f:
        assert "print finished" not in f.read()


def test_other_numbers_need_the_users_grant(env):
    _, tok = caller()
    c = env["client"]
    r = c.post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok), json={"to": OTHER, "text": "hi"})
    assert r.status_code == 403 and not env["sent"]
    assert capabilities.read_log()[0]["ok"] is False
    # Alice (an admin) grants it one more number, in Settings > Mods.
    g = c.post("/api/mods/notes-mod/grants", headers=ADMIN,
               json={"capability": "messaging.sms", "numbers": ["(555) 010-9999"]})
    assert g.status_code == 200 and g.json()["mod"]["grants"] == {"messaging.sms": {"numbers": [OTHER]}}
    r = c.post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok), json={"to": OTHER, "text": "hi"})
    assert r.status_code == 200 and env["sent"][-1][1]["phoneNumbers"] == [OTHER]
    # The grant is Alice's: a call for Bob does not get it.
    registry.set_access("notes-mod", "users")
    r = c.post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok),
               json={"to": OTHER, "text": "hi", "user": "bob"})
    assert r.status_code == 403
    # Clearing it takes it away again; a capability the mod did not declare has nothing to grant.
    c.post("/api/mods/notes-mod/grants", headers=ADMIN, json={"capability": "messaging.sms", "numbers": []})
    assert registry.get("notes-mod").get("capability_grants") == {}
    assert c.post("/api/mods/notes-mod/grants", headers=ADMIN,
                  json={"capability": "home.lights", "numbers": []}).status_code == 400


def test_send_is_rate_limited(env, monkeypatch):
    _, tok = caller()
    monkeypatch.setitem(capabilities.KNOWN["messaging.sms"]["rate_limits"], "send", [(2, 60), (100, 86400)])
    codes = [env["client"].post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok),
                                json={"text": "x"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429] and len(env["sent"]) == 2


def test_status_masks_numbers(env):
    _, tok = caller()
    r = env["client"].post("/api/mods/v1/cap/messaging.sms/status", headers=bearer(tok), json={})
    res = r.json()
    assert r.status_code == 200 and res["available"] and res["can_send"] and res["targets"] == ["********2000"]


def test_send_without_a_gateway_says_so(env):
    sms_routes.save_config("alice", {"numbers": [ALICE], "reply_url": ""})
    _, tok = caller()
    r = env["client"].post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok), json={"text": "x"})
    assert r.status_code == 409 and "reply URL" in r.json()["detail"]


def test_nothing_provides_an_unknown_capability(env):
    _, tok = caller(optional=["home.lights"])
    r = env["client"].post("/api/mods/v1/cap/home.lights/toggle", headers=bearer(tok), json={})
    assert r.status_code == 424 and r.json()["missing"] == "home.lights"


def test_listing_what_a_mod_declared(env):
    _, tok = caller(requires=["home.lights"], optional=["messaging.sms"])
    d = env["client"].get("/api/mods/v1/cap", headers=bearer(tok)).json()
    assert d["missing"] == ["home.lights"] and d["optional"][0]["available"] is True
    assert d["optional"][0]["provider"]["id"] == "core"


# ── A mod that provides it takes over, with the same rules ───────────────

def _provider_app(token, seen):
    app = FastAPI()

    @app.post("/mod/cap/{cap}/{action}")
    async def handle(cap: str, action: str, request: Request):
        user = signing.verify_identity(token, request.headers)
        seen.append({"cap": cap, "action": action, "user": user, "caller": request.headers.get("x-odysseus-caller"),
                     "body": await request.json()})
        return {"ok": True, "via": "phone-mod"}
    return app


def test_a_provider_mod_takes_over_and_the_core_rules_still_apply(env):
    _, ptok = add_mod({"provides": ["messaging.sms"]}, mod_id="phone")
    _, tok = caller()
    seen = []
    with UvicornThread(_provider_app(ptok, seen)) as srv:
        mark_running("phone", srv.port)
        c = env["client"]
        r = c.post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok), json={"text": "hello"})
        assert r.status_code == 200 and r.json()["provider"] == "phone"
        assert seen == [{"cap": "messaging.sms", "action": "send", "user": "alice", "caller": "notes-mod",
                         "body": {"to": ALICE, "text": "hello"}}]
        assert not env["sent"]          # the built-in gateway was not used
        # The number rule is the core's, whoever provides it.
        r = c.post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok), json={"to": OTHER, "text": "x"})
        assert r.status_code == 403 and len(seen) == 1
        assert capabilities.read_log()[1]["provider"] == "phone"


def test_the_user_must_be_allowed_to_use_the_provider(env):
    add_mod({"provides": ["messaging.sms"]}, mod_id="phone")        # admins only
    _, tok = add_mod({"optional": ["messaging.sms"]}, mod_id="notes-mod", access="users")
    sms_routes.save_config("bob", {"numbers": ["+15550103000"], "reply_url": "http://phone/x"})
    r = env["client"].post("/api/mods/v1/cap/messaging.sms/send", headers=bearer(tok),
                           json={"text": "x", "user": "bob"})
    assert r.status_code == 403 and "phone" in r.json()["detail"]


# ── Events ───────────────────────────────────────────────────────────────

def test_a_received_text_only_reaches_mods_that_declared_messaging(env, monkeypatch):
    add_mod({"optional": ["messaging.sms"], "events": {"subscribes": ["messaging.sms.received"]}},
            permissions=["events.subscribe"], mod_id="texter")
    add_mod({"events": {"subscribes": ["*.received"]}}, permissions=["events.subscribe"], mod_id="snoop")
    add_mod({"optional": ["messaging.sms"], "events": {"subscribes": ["core/messaging.sms.*"]}},
            permissions=["events.subscribe"], mod_id="bobs-mod", access="admins")
    delivered = []

    async def fake_deliver(mod_id, path, event):
        delivered.append((mod_id, event["type"], event["data"]))
    monkeypatch.setattr(events, "_deliver", fake_deliver)

    async def go():
        capabilities.sms_received("alice", ALICE, "pick up milk")
        import asyncio
        await asyncio.sleep(0.05)
    env["client"].portal.call(go)
    got = sorted(d[0] for d in delivered)
    assert got == ["bobs-mod", "texter"]            # never "snoop"
    assert delivered[0][2]["text"] == "pick up milk" and delivered[0][2]["from"] == ALICE
    # The Mods page's event history keeps the size of a text, not its words.
    hist = env["client"].get("/api/mods/events", headers=ADMIN).json()["events"][0]
    assert hist["type"] == "messaging.sms.received" and "milk" not in json.dumps(hist)
    # Bob's text is not delivered to an admins-only mod.
    delivered.clear()

    async def go_bob():
        capabilities.sms_received("bob", "+15550103000", "hi")
        import asyncio
        await asyncio.sleep(0.05)
    env["client"].portal.call(go_bob)
    assert delivered == []


# ── Install, disable and remove ──────────────────────────────────────────

def _folder(tmp_path, name, **extra):
    d = tmp_path / name
    shutil.copytree(HELLO, d)
    raw = json.loads((d / "odysseus-mod.json").read_text())
    raw.update(extra)
    (d / "odysseus-mod.json").write_text(json.dumps(raw))
    return str(d)


def test_install_review_names_missing_providers_and_asks_before_installing(env, tmp_path):
    c = env["client"]
    src = _folder(tmp_path, "needs", requires=["home.lights"], optional=["messaging.sms", "storage.s3"])
    staged = c.post("/api/mods/install/prepare", headers=ADMIN, json={"source": src}).json()
    caps = staged["capabilities"]
    assert caps["missing"] == ["home.lights"] and caps["missing_optional"] == ["storage.s3"]
    assert caps["optional"][0]["provider"]["id"] == "core"
    perms = [p["id"] for p in staged["permissions"]]
    r = c.post("/api/mods/install/confirm", headers=ADMIN, json={"install_id": staged["install_id"],
                                                                 "permissions": perms, "enable": False})
    assert r.status_code == 409 and r.json()["missing_requires"] == ["home.lights"]
    # The staged install is still there: install it anyway.
    r = c.post("/api/mods/install/confirm", headers=ADMIN, json={"install_id": staged["install_id"],
                                                                 "permissions": perms, "enable": False,
                                                                 "allow_missing": True})
    assert r.status_code == 200 and r.json()["mod"]["capabilities"]["missing"] == ["home.lights"]


def test_disabling_or_removing_a_provider_warns_about_the_mods_that_need_it(env):
    add_mod({"provides": ["home.lights"]}, mod_id="lights")
    add_mod({"requires": ["home.lights"]}, mod_id="scenes")
    add_mod({"optional": ["home.lights"]}, mod_id="wakeup")
    c = env["client"]
    deps = c.get("/api/mods/lights/dependents", headers=ADMIN).json()["dependents"]
    assert {(d["id"], d["kind"]) for d in deps} == {("scenes", "requires"), ("wakeup", "optional")}
    r = c.post("/api/mods/lights/disable", headers=ADMIN)
    assert r.status_code == 409 and "Test Mod stops working" in r.json()["detail"]
    assert registry.get("lights")["enabled"]
    r = c.delete("/api/mods/lights", headers=ADMIN)
    assert r.status_code == 409 and registry.get("lights")
    assert c.post("/api/mods/lights/disable?force=true", headers=ADMIN).status_code == 200
    assert not registry.get("lights")["enabled"]
    # Another provider means nobody depends on this one alone.
    registry.set_enabled("lights", True)
    add_mod({"provides": ["home.lights"]}, mod_id="lights-two")
    assert capabilities.dependents("lights") == []
    assert c.delete("/api/mods/lights", headers=ADMIN).status_code == 200


def test_the_installed_card_shows_capabilities(env):
    add_mod({"requires": ["home.lights"], "optional": ["messaging.sms"]}, mod_id="scenes")
    m = env["client"].get("/api/mods", headers=ADMIN).json()
    card = next(x for x in m["mods"] if x["id"] == "scenes")
    assert card["capabilities"]["missing"] == ["home.lights"]
    assert m["capabilities"][0]["id"] == "messaging.sms" and m["capabilities"][0]["providers"][0]["id"] == "core"


# ── Browse ───────────────────────────────────────────────────────────────

def test_catalog_summary_index_and_capability_search(monkeypatch):
    s = catalog.summarize(_man(provides=["messaging.sms"], optional=["storage.s3"]))
    assert s["provides"] == ["messaging.sms"] and s["optional"] == ["storage.s3"] and s["requires"] == []
    ix, problems = catalog.parse_index({"mods": [
        {"repo": "https://github.com/a/phone", "id": "phone", "provides": ["messaging.sms"]},
        {"repo": "https://github.com/a/bad", "id": "bad", "provides": ["Nope"]},
    ]}, "test")
    assert ix["a/phone"]["provides"] == ["messaging.sms"] and problems and "bad" not in str(ix["a/bad"]["provides"])

    def entry(name, provides):
        return {"listed": True, "full_name": f"a/{name}", "repo": name, "owner": "a", "stars": 0,
                "manifest": {"id": name, "name": name, "valid": True, "compatible": True, "provides": provides}}
    fake = {"entries": {"a/phone": entry("phone", ["messaging.sms"]), "a/notes": entry("notes", [])}}
    monkeypatch.setattr(catalog, "build", lambda force=False: fake)
    monkeypatch.setattr(catalog, "_installed_github", lambda: {})
    monkeypatch.setattr(catalog, "settings", lambda: {})
    from src.mods import registry as reg
    monkeypatch.setattr(reg, "list_mods", lambda: [])
    got = catalog.query(capability="messaging.sms")
    assert [r["id"] for r in got["results"]] == ["phone"] and got["capability"] == "messaging.sms"
    assert [r["id"] for r in catalog.query(q="messaging.sms")["results"]] == ["phone"]


def test_the_index_validator_knows_the_capability_fields():
    import importlib.util
    p = os.path.join(os.path.dirname(HELLO), "..", "templates", "mods-index", "scripts", "validate_index.py")
    spec = importlib.util.spec_from_file_location("validate_index_t", p)
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    good = {"schema_version": 1, "mods": [{"repo": "https://github.com/a/phone", "id": "phone",
                                          "provides": ["messaging.sms"], "optional": ["storage.s3"]}]}
    assert v.check(good) == []
    bad = {"schema_version": 1, "mods": [{"repo": "https://github.com/a/phone", "id": "phone", "requires": ["SMS"]}]}
    assert any("requires" in x for x in v.check(bad))


def test_status_without_a_reply_url_is_not_available(env):
    """print-hub's adapter reads `available` at the top of the answer."""
    sms_routes.save_config("alice", {"numbers": [ALICE], "reply_url": ""})
    _, tok = caller()
    r = env["client"].post("/api/mods/v1/cap/messaging.sms/status", headers=bearer(tok), json={})
    assert r.status_code == 200 and r.json()["available"] is False


# ── A mod's own named API, and calling a provider by name ────────────────

def test_a_mods_own_api_is_served_only_by_that_mod(env):
    _, htok = add_mod({"provides": ["print-hub.jobs"]}, mod_id="print-hub")
    add_mod({"provides": ["print-hub.jobs"]}, mod_id="aaa-squatter")      # sorts first, but not its namespace
    _, tok = caller(optional=["print-hub.jobs"])
    assert [p["id"] for p in capabilities.providers("print-hub.jobs")] == ["print-hub"]
    seen = []
    with UvicornThread(_provider_app(htok, seen)) as srv:
        mark_running("print-hub", srv.port)
        r = env["client"].post("/api/mods/v1/cap/print-hub.jobs/list", headers=bearer(tok), json={"limit": 3})
        assert r.status_code == 200 and r.json()["provider"] == "print-hub" and r.json()["via"] == "phone-mod"
        assert seen[0]["action"] == "list" and seen[0]["body"] == {"limit": 3} and seen[0]["caller"] == "notes-mod"
    # The core's own capabilities belong to no mod, even one called "messaging".
    add_mod({"provides": ["messaging.sms"]}, mod_id="messaging", enabled=False)
    assert capabilities.owner_of("messaging.sms") is None


def test_calling_a_provider_by_name(env):
    _, ptok = add_mod({"provides": ["messaging.sms"]}, mod_id="phone")
    _, tok = caller()
    seen = []
    with UvicornThread(_provider_app(ptok, seen)) as srv:
        mark_running("phone", srv.port)
        c = env["client"]
        r = c.post("/api/mods/v1/cap/messaging.sms/send?provider=core", headers=bearer(tok), json={"text": "a"})
        assert r.status_code == 200 and r.json()["provider"] == "core" and not seen
        r = c.post("/api/mods/v1/cap/messaging.sms/send?provider=phone", headers=bearer(tok), json={"text": "b"})
        assert r.status_code == 200 and r.json()["provider"] == "phone" and len(seen) == 1
        r = c.post("/api/mods/v1/cap/messaging.sms/send?provider=nobody", headers=bearer(tok), json={"text": "c"})
        assert r.status_code == 424


# ── The proxy never lets a browser reach a provider's endpoint ───────────

@pytest.mark.parametrize("path, blocked", [
    ("/mod/cap/messaging.sms/send", True), ("//mod//cap/x/y", True), ("/mod%2Fcap/x/y", True),
    ("/./mod/cap/x", True), ("/mod/cap", True), ("/mod/capture", False), ("/mod/tools/x", False), ("/", False)])
def test_proxy_blocks_the_capability_endpoint(path, blocked):
    from src.mods import proxy
    assert proxy.core_only(path) is blocked


def test_sms_notifications_follow_the_provider_mod(env):
    """Mod notifications by text (src/mods/notify.py) go through the mod that
    provides messaging.sms, so they use its reply URL, not the core's."""
    import asyncio
    from src.mods import notify
    assert asyncio.run(notify._sms("alice", "built in")) == "reply_url" and len(env["sent"]) == 1
    _, ptok = add_mod({"provides": ["messaging.sms"]}, mod_id="phone")
    seen = []
    with UvicornThread(_provider_app(ptok, seen)) as srv:
        mark_running("phone", srv.port)
        assert asyncio.run(notify._sms("alice", "Print failed")) == "phone-mod"
    assert seen == [{"cap": "messaging.sms", "action": "send", "user": "alice", "caller": "core",
                     "body": {"to": ALICE, "text": "Print failed"}}]
    assert len(env["sent"]) == 1
