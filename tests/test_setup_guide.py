"""The guided setup checklist (src/setup_guide.py, routes/setup_routes.py) and
the setup steps a mod can add (src/mods/setup_steps.py).

The status of every step is computed from facts; these tests feed the step
builders each situation (nothing set up, partly set up, everything done) and
check the route end to end with the fact gatherers faked.
"""
import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src import setup_guide as sg
from src.mods import manifest as mf
from src.mods import setup_steps
from tests.mods_helpers import add_mod, mods_dir  # noqa: F401


def by_id(steps):
    return {s["id"]: s for s in steps}


# ── Essentials ──────────────────────────────────────────────────────────

def test_account_done_and_two_step_is_optional():
    s = by_id(sg.account_steps("alice", True, True, False))
    assert s["essentials.account"]["status"] == "done"
    assert s["essentials.account"]["title"] == "Your admin account"
    assert s["essentials.2fa"]["status"] == "optional" and s["essentials.2fa"]["dismissible"]
    assert s["essentials.2fa"]["action"]["target"] == "tab:account"
    s = by_id(sg.account_steps("alice", True, True, True))
    assert s["essentials.2fa"]["status"] == "done" and not s["essentials.2fa"]["dismissible"]


def test_account_not_applicable_when_sign_in_is_off():
    (only,) = sg.account_steps("", True, False, False)
    assert only["status"] == "not_applicable"


def test_model_needs_you_when_none_and_points_admin_at_add_models():
    (m,) = sg.model_steps(True, [])
    assert m["status"] == "needs_you" and m["action"]["target"] == "tab:services"
    (m,) = sg.model_steps(False, [])
    assert m["status"] == "needs_you" and m["action"] is None and "Ask an admin" in m["detail"]


def test_model_endpoint_that_does_not_answer_is_not_done():
    (m,) = sg.model_steps(True, [{"model_type": "llm", "models": [], "offline": True}])
    assert m["status"] == "needs_you" and "not answering" in m["detail"]


def test_model_done_counts_models_and_ignores_embedding_endpoints():
    items = [{"model_type": "llm", "models": ["a", "b"]},
             {"model_type": "llm", "models": []},
             {"model_type": "embedding", "models": ["e"]}]
    (m,) = sg.model_steps(True, items)
    assert m["status"] == "done" and "2 models ready from 1 connection" in m["detail"]
    (m,) = sg.model_steps(True, [{"model_type": "embedding", "models": ["e"]}])
    assert m["status"] == "needs_you"


@pytest.mark.parametrize("subs, here, status", [
    (0, None, "needs_you"), (2, None, "done"), (2, "off", "needs_you"), (0, "on", "done"),
    (0, "unsupported", "optional"),
])
def test_notifications_depend_on_this_browser(subs, here, status):
    (n,) = sg.push_steps(subs, here)
    assert n["status"] == status
    if status == "needs_you":
        assert n["action"]["kind"] == "enable-notifications" and n["dismissible"]


# ── Your devices ────────────────────────────────────────────────────────

def machine(host, os_name="windows", online=True, servers=None, phones=None, is_self=False, label=None):
    return {"host": host, "name": host, "label": label or host, "os": os_name, "online": online,
            "servers": servers or [], "phones": phones or [], "is_self": is_self}


def ov(machines, tailscale=True, network=None):
    return {"machines": machines, "others": [], "tailscale": tailscale, "network": network or {}}


def test_no_tailnet_needs_tailscale_and_hides_computers():
    out = by_id(sg.device_steps(ov([], tailscale=False), [], 0))
    assert out["devices.network"]["status"] == "needs_you"
    assert out["devices.network"]["action"]["kind"] == "browse-mods"
    assert out["devices.computers"]["status"] == "not_applicable"


def test_tailscale_addon_turned_off_points_at_mods():
    out = by_id(sg.device_steps(ov([], tailscale=False, network={"installed_off": True}), [], 0))
    assert "turned off" in out["devices.network"]["detail"]
    assert out["devices.network"]["action"]["target"] == "tab:mods"


def test_unlinked_computer_says_it_needs_the_helper_once():
    out = by_id(sg.device_steps(ov([machine("laptop", "linux", label="Laptop")]), [], 0))
    s = out["devices.computer.laptop"]
    assert s["status"] == "needs_you" and s["title"] == "Link Laptop"
    assert "installed once" in s["gives"] and "Add a computer" in s["gives"]
    assert s["action"]["target"] == "devices-add-card" and s["action"]["then"] == "enroll:computer"


def test_linked_computer_missing_features_gets_install_with_its_server():
    m = machine("pc", servers=[{"id": "srv1", "status": "connected"}], label="PC")
    tool = {"host": "pc", "server_id": "srv1", "name": "pc-desktop", "online": True, "connected": True,
            "missing": ["Music overlay (Pop out)"], "outdated": [], "up_to_date": False}
    s = by_id(sg.device_steps(ov([m]), [tool], 0))["devices.computer.pc"]
    assert s["status"] == "needs_you" and "Music overlay" in s["detail"]
    assert s["action"] == {"kind": "install-tools", "label": "Install", "server_id": "srv1"}
    tool.update(outdated=["desktop_mcp_server.py"])
    assert by_id(sg.device_steps(ov([m]), [tool], 0))["devices.computer.pc"]["action"]["label"] == "Install and update"
    tool.update(missing=[])
    assert by_id(sg.device_steps(ov([m]), [tool], 0))["devices.computer.pc"]["action"]["label"] == "Update"


def test_linked_computer_that_is_current_is_done():
    m = machine("pc", servers=[{"id": "srv1", "status": "connected"}])
    tool = {"host": "pc", "server_id": "srv1", "name": "pc-desktop", "online": True, "connected": True,
            "missing": [], "outdated": [], "up_to_date": True}
    assert by_id(sg.device_steps(ov([m]), [tool], 0))["devices.computer.pc"]["status"] == "done"


def test_offline_linked_computer_cannot_install_from_here():
    m = machine("pc", online=False, servers=[{"id": "srv1"}])
    tool = {"host": "pc", "server_id": "srv1", "online": False, "connected": True, "missing": ["x"],
            "outdated": [], "up_to_date": False}
    s = by_id(sg.device_steps(ov([m]), [tool], 0))["devices.computer.pc"]
    assert s["action"]["kind"] == "settings" and "offline" in s["detail"]


def test_linked_but_unreachable_computer_says_reconnect():
    m = machine("pc", servers=[{"id": "srv1"}])
    s = by_id(sg.device_steps(ov([m]), [], 0))["devices.computer.pc"]
    assert s["status"] == "needs_you" and s["title"].startswith("Reconnect")


def test_no_other_computer_is_an_optional_nudge_and_self_is_skipped():
    out = by_id(sg.device_steps(ov([machine("server", is_self=True)]), [], 0))
    assert out["devices.computers"]["status"] == "optional"
    assert not any(k.startswith("devices.computer.") for k in out)


def test_phones_pair_or_done():
    out = by_id(sg.device_steps(ov([machine("pixel", "android", phones=[{"name": "pixel"}]),
                                    machine("iphone", "ios")]), [], 0))
    assert out["devices.phone.pixel"]["status"] == "done"
    assert out["devices.phone.iphone"]["status"] == "needs_you"
    assert out["devices.phone.iphone"]["action"]["then"] == "enroll:phone"
    out = by_id(sg.device_steps(ov([]), [], 0))
    assert out["devices.phone"]["status"] == "optional"
    out = by_id(sg.device_steps(ov([]), [], 1))
    assert out["devices.phone"]["status"] == "done"


# ── Accounts and mods ───────────────────────────────────────────────────

def test_accounts_google_states():
    def go(google, admin=True, email=0, sms=0):
        return by_id(sg.account_connect_steps("alice", admin, email, google, sms))
    assert go({"configured": False})["accounts.google"]["title"] == "Set up Google"
    assert "accounts.google" not in go({"configured": False}, admin=False)
    assert go({"configured": True, "connected": False})["accounts.google"]["status"] == "optional"
    s = go({"configured": True, "connected": True, "email": "a@x.org", "products": []})["accounts.google"]
    assert s["status"] == "done" and "a@x.org" in s["detail"]
    s = go({"configured": True, "connected": True, "products": [{"needs_reconnect": True}]})["accounts.google"]
    assert s["status"] == "needs_you"
    assert go({}, email=2, sms=1)["accounts.email"]["status"] == "done"
    assert go({}, sms=1)["accounts.phone"]["status"] == "done"
    assert go({})["accounts.phone"]["action"]["target"] == "sms-numbers"


def test_mod_steps_updates_imports_and_recommendations():
    out = by_id(sg.mod_steps([], [{"id": "alpha", "name": "Alpha", "summary": "Does a thing"}],
                             [{"id": "beta", "name": "Beta"}],
                             [{"mod_id": "phone", "mod_name": "Phone", "slice": "sms", "title": "Texting settings"}]))
    assert out["mods.browse"]["status"] == "optional"
    assert out["mods.recommended.alpha"]["status"] == "optional"
    assert out["mods.updates"]["status"] == "needs_you" and "Beta" in out["mods.updates"]["detail"]
    imp = out["mods.import.phone.sms"]
    assert imp["status"] == "needs_you" and "texting settings" in imp["title"]
    out = by_id(sg.mod_steps([{"id": "x"}], [], [], []))
    assert out["mods.browse"]["status"] == "done"


def test_mod_contributed_steps_use_the_mods_answer():
    found = [
        {"mod_id": "p", "mod_name": "Printer", "action": {"kind": "mod", "link": "#mod/p/main"},
         "answer": {"done": True, "detail": "2 printers"},
         "step": {"id": "a", "title": "Connect a printer", "description": "d", "optional": True,
                  "status_path": "/s"}},
        {"mod_id": "p", "mod_name": "Printer", "action": None, "answer": {"done": False, "detail": ""},
         "step": {"id": "b", "title": "Pick a bed", "description": "", "optional": False, "status_path": "/s"}},
        {"mod_id": "p", "mod_name": "Printer", "action": None, "answer": None,
         "step": {"id": "c", "title": "Not asked", "description": "", "optional": True, "status_path": "/s"}},
        {"mod_id": "p", "mod_name": "Printer", "action": None, "answer": None,
         "step": {"id": "d", "title": "Manual", "description": "", "optional": True, "status_path": None}},
    ]
    out = by_id(sg.from_mod_steps(found))
    assert out["mods.step.p.a"]["status"] == "done" and out["mods.step.p.a"]["detail"] == "2 printers"
    assert out["mods.step.p.b"]["status"] == "needs_you"
    assert out["mods.step.p.c"]["status"] == "optional" and "Not checked yet" in out["mods.step.p.c"]["detail"]
    assert out["mods.step.p.d"]["detail"] == "" and out["mods.step.p.d"]["dismissible"]
    assert out["mods.step.p.a"]["mod"] == "Printer"


# ── Counting, dismissing, grouping ──────────────────────────────────────

def sample_steps():
    return [
        sg.step("essentials.model", "essentials", "Model", "g", "needs_you"),
        sg.step("essentials.notifications", "essentials", "Notify", "g", "needs_you", dismissible=True),
        sg.step("accounts.email", "accounts", "Mail", "g", "optional", dismissible=True),
        sg.step("essentials.account", "essentials", "Account", "g", "done"),
        sg.step("mods.x", "mods", "NA", "g", "not_applicable"),
    ]


def test_assemble_counts_only_what_needs_the_person():
    out = sg.assemble(sample_steps(), {"dismissed": []})
    assert out["summary"]["left"] == 2 and out["summary"]["optional_left"] == 1
    assert out["summary"]["done"] == 1 and not out["summary"]["complete"]
    assert [g["id"] for g in out["groups"]] == ["essentials", "accounts", "mods"]


def test_dismissed_optional_steps_stop_counting_and_done_steps_cannot_be_dismissed():
    out = sg.assemble(sample_steps(), {"dismissed": ["essentials.notifications", "accounts.email",
                                                     "essentials.model", "essentials.account"]})
    s = by_id([s for g in out["groups"] for s in g["steps"]])
    assert s["essentials.notifications"]["dismissed"] and s["accounts.email"]["dismissed"]
    assert not s["essentials.model"]["dismissed"]            # not dismissible: it is required
    assert not s["essentials.account"]["dismissed"]
    assert out["summary"]["left"] == 1 and out["summary"]["optional_left"] == 0


def test_card_stays_hidden_until_a_new_step_needs_the_person():
    st = sample_steps()
    hidden = ["essentials.model", "essentials.notifications"]
    assert sg.assemble(st, {"card_hidden": hidden})["card_hidden"] is True
    st.append(sg.step("devices.computer.new", "devices", "Link", "g", "needs_you"))
    assert sg.assemble(st, {"card_hidden": hidden})["card_hidden"] is False
    assert sg.assemble(st, {"card_hidden": None})["card_hidden"] is False


def test_helper_missing_message_names_the_place_to_fix_it():
    msg = sg.helper_missing("Laptop", "play another computer's sound")
    assert "Laptop" in msg and "Settings > Devices" in msg and "Install" in msg
    assert chr(0x2014) not in msg


# ── The route ───────────────────────────────────────────────────────────

class FakeAuth:
    users = {"alice": {}, "bob": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"

    def totp_enabled(self, u):
        return False


class As(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        u = request.headers.get("x-test-user")
        if u:
            request.state.current_user = u
            request.state.api_token = False
        return await call_next(request)


@pytest.fixture
def client(tmp_path, monkeypatch, mods_dir):
    from routes import prefs_routes
    from routes.setup_routes import setup_setup_routes
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    monkeypatch.setattr(sg, "_models", lambda user, admin: [])
    monkeypatch.setattr(sg, "_email_accounts", lambda user: 0)
    monkeypatch.setattr(sg, "_google", lambda user: {"configured": False})
    monkeypatch.setattr(sg, "_sms_numbers", lambda user: 0)
    monkeypatch.setattr(sg, "_push_count", lambda user: 0)
    monkeypatch.setattr(sg, "_mods_facts", lambda user: {"installed": [], "recommended": [], "updates": [],
                                                        "imports": []})

    async def devices(mgr):
        return {"ov": ov([machine("laptop", "linux")]), "tools": [], "registered": 0}
    monkeypatch.setattr(sg, "_devices", devices)
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(setup_setup_routes())
    app.add_middleware(As)
    return TestClient(app)


A = {"x-test-user": "alice"}
B = {"x-test-user": "bob"}


def ids(body):
    return {s["id"]: s for g in body["groups"] for s in g["steps"]}


def test_status_needs_a_signed_in_user(client):
    assert client.get("/api/setup/status").status_code == 401


def test_fresh_install_for_an_admin_lists_all_four_groups(client):
    body = client.get("/api/setup/status", headers=A).json()
    assert [g["id"] for g in body["groups"]] == ["essentials", "devices", "accounts", "mods"]
    s = ids(body)
    assert s["essentials.model"]["status"] == "needs_you"
    assert s["devices.computer.laptop"]["status"] == "needs_you"
    assert s["essentials.notifications"]["status"] == "needs_you"
    assert body["is_admin"] and not body["seen"] and not body["summary"]["complete"]
    assert body["summary"]["left"] == sum(1 for x in s.values() if x["status"] == "needs_you")


def test_non_admin_sees_only_steps_they_can_do(client):
    body = client.get("/api/setup/status", headers=B).json()
    assert not body["is_admin"]
    assert [g["id"] for g in body["groups"]] == ["essentials", "accounts"]
    s = ids(body)
    assert "Ask an admin" in s["essentials.model"]["detail"] and s["essentials.model"]["action"] is None
    assert not any(k.startswith(("devices.", "mods.")) for k in s)
    assert "accounts.google" not in s            # an admin has to set Google up first


def test_here_param_changes_the_notification_step(client):
    s = ids(client.get("/api/setup/status?here=on", headers=A).json())
    assert s["essentials.notifications"]["status"] == "done"
    s = ids(client.get("/api/setup/status?here=bogus", headers=A).json())
    assert s["essentials.notifications"]["status"] == "needs_you"


def test_dismissals_persist_per_user_and_can_be_undone(client):
    assert client.post("/api/setup/dismiss", headers=A, json={"id": "accounts.email"}).json()["ok"]
    assert ids(client.get("/api/setup/status", headers=A).json())["accounts.email"]["dismissed"] is True
    assert ids(client.get("/api/setup/status", headers=B).json())["accounts.email"]["dismissed"] is False
    client.post("/api/setup/dismiss", headers=A, json={"id": "accounts.email", "dismissed": False})
    assert ids(client.get("/api/setup/status", headers=A).json())["accounts.email"]["dismissed"] is False


def test_dismiss_rejects_ids_that_are_not_step_ids(client):
    for bad in ("", "../x", "A B", "x" * 200):
        assert client.post("/api/setup/dismiss", headers=A, json={"id": bad}).status_code == 400


def test_a_required_step_cannot_be_hidden_even_if_asked(client):
    client.post("/api/setup/dismiss", headers=A, json={"id": "essentials.model"})
    body = client.get("/api/setup/status", headers=A).json()
    assert ids(body)["essentials.model"]["dismissed"] is False
    assert "essentials.model" in body["summary"]["needs"]


def test_seen_is_remembered_for_that_user_only(client):
    client.post("/api/setup/seen", headers=A)
    assert client.get("/api/setup/status", headers=A).json()["seen"] is True
    assert client.get("/api/setup/status", headers=B).json()["seen"] is False


def test_card_hide_returns_when_something_new_needs_them(client):
    body = client.get("/api/setup/status", headers=A).json()
    client.post("/api/setup/card", headers=A, json={"hidden": True, "ids": body["summary"]["needs"]})
    assert client.get("/api/setup/status", headers=A).json()["card_hidden"] is True
    client.post("/api/setup/card", headers=A, json={"hidden": True, "ids": body["summary"]["needs"][:-1]})
    assert client.get("/api/setup/status", headers=A).json()["card_hidden"] is False
    client.post("/api/setup/card", headers=A, json={"hidden": False})
    assert client.get("/api/setup/status", headers=A).json()["card_hidden"] is False


def test_a_broken_fact_does_not_break_the_checklist(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is down")
    monkeypatch.setattr(sg, "_models", boom)
    monkeypatch.setattr(sg, "_email_accounts", boom)

    async def devices_boom(mgr):
        raise RuntimeError("tailnet is down")
    monkeypatch.setattr(sg, "_devices", devices_boom)
    body = client.get("/api/setup/status", headers=A).json()
    s = ids(body)
    assert s["essentials.model"]["status"] == "needs_you"      # unknown counts as not set up
    assert "accounts.email" in s and "devices.network" not in s


def test_complete_when_every_required_step_is_done(client, monkeypatch):
    monkeypatch.setattr(sg, "_models", lambda u, a: [{"model_type": "llm", "models": ["m"]}])
    monkeypatch.setattr(sg, "_push_count", lambda u: 1)

    async def devices(mgr):
        return {"ov": ov([]), "tools": [], "registered": 1}
    monkeypatch.setattr(sg, "_devices", devices)
    body = client.get("/api/setup/status", headers=A).json()
    assert body["summary"]["complete"] and body["summary"]["left"] == 0
    assert body["summary"]["optional_left"] >= 1


# ── Mod-contributed steps ───────────────────────────────────────────────

STEPS = {"steps": [
    {"id": "connect", "title": "Connect it", "description": "Lets it do a thing.",
     "status_path": "/setup/connect", "action": {"label": "Open", "path": "/connect"}},
    {"id": "tune", "title": "Tune it", "optional": False, "action": {"label": "Settings", "settings": True}},
]}


def manifest(setup, **kw):
    raw = {"id": "demo", "name": "Demo", "version": "1.0.0",
           "runtime": {"type": "process", "command": ["python", "-m", "x"]},
           "ui": {"tabs": [{"id": "main", "title": "Demo", "path": "/"}], "settings_path": "/settings"}}
    raw.update(kw)
    if setup is not None:
        raw["setup"] = setup
    return raw


def test_manifest_accepts_setup_steps_and_normalizes_them():
    out = mf.validate(manifest(STEPS))
    steps = out["setup"]["steps"]
    assert [s["id"] for s in steps] == ["connect", "tune"]
    assert steps[0]["optional"] is True and steps[1]["optional"] is False
    assert steps[0]["action"] == {"label": "Open", "path": "/connect", "settings": False}
    assert steps[1]["action"]["settings"] is True and steps[1]["status_path"] is None
    assert mf.validate(manifest(None))["setup"] is None


@pytest.mark.parametrize("bad, why", [
    ({"steps": "x"}, "steps"),
    ({"steps": [{"id": "A b", "title": "t"}]}, "id"),
    ({"steps": [{"id": "a", "title": ""}]}, "title"),
    ({"steps": [{"id": "a", "title": "t", "description": "x" * 400}]}, "description"),
    ({"steps": [{"id": "a", "title": "t", "optional": "yes"}]}, "optional"),
    ({"steps": [{"id": "a", "title": "t", "status_path": "http://evil"}]}, "status_path"),
    ({"steps": [{"id": "a", "title": "t", "action": {"label": "x"}}]}, "action"),
    ({"steps": [{"id": "a", "title": "t", "action": {"label": "x", "path": "/a", "settings": True}}]}, "action"),
    ({"steps": [{"id": "a", "title": "t"}, {"id": "a", "title": "u"}]}, "twice"),
    ({"steps": [{"id": f"s{i}", "title": "t"} for i in range(7)]}, "at most"),
])
def test_manifest_rejects_bad_setup_steps(bad, why):
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(manifest(bad))
    assert why in str(e.value)


def test_settings_action_needs_a_settings_page():
    raw = manifest({"steps": [{"id": "a", "title": "t", "action": {"label": "x", "settings": True}}]})
    raw["ui"].pop("settings_path")
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(raw)
    assert "settings_path" in str(e.value)


def test_collect_lists_enabled_mods_the_user_may_use_and_links_into_them(mods_dir, monkeypatch):
    from src.mods import access
    add_mod(manifest(STEPS), mod_id="demo", access="users")
    add_mod(manifest(STEPS, id="hidden"), mod_id="hidden", access="admins")
    add_mod(manifest(STEPS, id="off"), mod_id="off", enabled=False, access="users")
    access.set_auth_manager(FakeAuth())
    monkeypatch.setenv("AUTH_ENABLED", "true")

    async def ask(entry, step, user):
        return {"done": True, "detail": "ok"} if step["id"] == "connect" else None
    monkeypatch.setattr(setup_steps, "_ask", ask)
    found = asyncio.run(setup_steps.collect("bob"))
    assert {(f["mod_id"], f["step"]["id"]) for f in found} == {("demo", "connect"), ("demo", "tune")}
    connect = next(f for f in found if f["step"]["id"] == "connect")
    assert connect["answer"]["done"] is True
    assert connect["action"]["link"] == "#mod/demo/main?path=%2Fconnect"
    tune = next(f for f in found if f["step"]["id"] == "tune")
    assert tune["action"]["link"] == "#mod/demo/main?path=%2Fsettings"
    admin_found = asyncio.run(setup_steps.collect("alice"))
    assert {f["mod_id"] for f in admin_found} == {"demo", "hidden"}


def test_ask_never_starts_a_mod_that_is_not_running(mods_dir):
    entry, _ = add_mod(manifest(STEPS), mod_id="demo")
    step = entry["manifest"]["setup"]["steps"][0]
    assert asyncio.run(setup_steps._ask(entry, step, "alice")) is None


def test_ask_reads_done_and_detail_from_a_running_mod(mods_dir):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from tests.mods_helpers import free_port, mark_running

    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["user"] = self.headers.get("X-Odysseus-User")
            seen["sig"] = bool(self.headers.get("X-Odysseus-Sig"))
            body = json.dumps({"done": False, "detail": "two printers found, none chosen"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        entry, _ = add_mod(manifest(STEPS), mod_id="demo")
        mark_running("demo", port)
        step = entry["manifest"]["setup"]["steps"][0]
        ans = asyncio.run(setup_steps._ask(entry, step, "alice"))
        assert ans == {"done": False, "detail": "two printers found, none chosen"}
        assert seen == {"user": "alice", "sig": True}
    finally:
        srv.shutdown()


# ── The page is wired in ────────────────────────────────────────────────

def test_setup_tab_panel_script_and_devices_hooks_are_in_the_page():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    assert 'data-settings-tab="setup"' in html and 'data-settings-panel="setup"' in html
    assert 'id="setup-root"' in html and 'id="setup-nav-badge"' in html
    assert "/static/js/setupGuide.js" in html and "/static/css/setupGuide.css" in html
    assert 'id="devices-add-card"' in html            # the "Add a computer" step lands here
    js = open(os.path.join(root, "static", "js", "setupGuide.js"), encoding="utf-8").read()
    for kind in ("settings", "mod", "browse-mods", "enable-notifications", "install-tools"):
        assert f"'{kind}'" in js, kind                # every action kind the server sends is handled
    dev = open(os.path.join(root, "static", "js", "devicesSettings.js"), encoding="utf-8").read()
    assert "data-devices-empty-help" in dev and "Each computer needs it installed once" in dev
    home = open(os.path.join(root, "static", "js", "workspace", "home.js"), encoding="utf-8").read()
    assert "SetupGuide.mountCard" in home
