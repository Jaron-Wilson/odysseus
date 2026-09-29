"""Adding a device with one command or one scan, and checking one.

Adding the PC and the laptop took copying files, writing launchers with their
Tailscale addresses, registering tasks/units, adding SSH keys and typing the
servers into Agent Tools. Now a one-time code does it: the install script
fetches what it needs and registers the machine; a phone scans a QR code.
The code is the only credential on those paths, so these tests lean on it.
"""
import asyncio
import os
import re
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import enroll_routes
from src import devices, enrollment, machines, webpush

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def data(tmp_path, monkeypatch):
    for mod, attr, name in ((enrollment, "CODES_FILE", "enroll_codes.json"),
                            (devices, "DEVICES_FILE", "devices.json"),
                            (machines, "MACHINES_FILE", "machines.json")):
        monkeypatch.setattr(mod, attr, str(tmp_path / name))
        monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
    return tmp_path


class _Mgr:
    def __init__(self):
        self.reconnects = []

    async def _reconnect_configured(self, sid):
        self.reconnects.append(sid)
        return True

    def get_server_status(self, sid):
        return {"status": "connected"}

    def get_all_tools(self):
        return []


@pytest.fixture
def client(data, monkeypatch):
    mgr = _Mgr()
    monkeypatch.setattr(enroll_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(enroll_routes, "server_pubkey", lambda: "ssh-ed25519 AAAAtest odysseus")
    app = FastAPI()
    app.include_router(enroll_routes.setup_enroll_routes(get_mcp_manager=lambda: mgr))
    return TestClient(app, base_url="https://jaron-dev-server.tail0.ts.net"), mgr


@pytest.fixture
def db(monkeypatch):
    """A real, empty mcp_servers table in memory."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import core.database as cdb
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    cdb.McpServer.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", Session)
    return Session


# ── Codes ──────────────────────────────────────────────────────────────────

def test_codes_are_single_use_expiring_and_kind_bound(data):
    rec = enrollment.create("computer", now=1000.0)
    code = rec["code"]
    assert len(code) == enrollment.CODE_LEN
    assert enrollment.valid(code, "computer", now=1001.0)
    assert enrollment.valid(code, "phone", now=1001.0) is None           # wrong kind
    assert enrollment.valid(code, "computer", now=1000.0 + 21 * 60) is None   # expired
    assert enrollment.use(code, {"ok": True}, now=1002.0)
    assert enrollment.use(code, {"ok": True}, now=1003.0) is False       # once
    assert enrollment.valid(code, "computer", now=1004.0) is None
    for junk in ("", "short", "A" * 16, "../../etc/passwd", code.upper()):
        assert enrollment.valid(junk) is None


def test_codes_file_is_private(data):
    enrollment.create("computer")
    assert oct(os.stat(enrollment.CODES_FILE).st_mode & 0o777) == "0o600"


# ── Computer: script, files, register ──────────────────────────────────────

def test_computer_code_gives_commands_and_rendered_scripts(client):
    c, _ = client
    r = c.post("/api/devices/enroll", json={"kind": "computer"}).json()
    code = r["code"]
    assert r["commands"]["unix"] == f"curl -fsSL https://jaron-dev-server.tail0.ts.net/enroll/{code}/install.sh | bash"
    assert r["commands"]["windows"] == f"iwr -useb https://jaron-dev-server.tail0.ts.net/enroll/{code}/install.ps1 | iex"
    for name in ("install.sh", "install.ps1"):
        text = c.get(f"/enroll/{code}/{name}").text
        assert "__ODYSSEUS" not in text and "__ENROLL_CODE__" not in text
        assert code in text and "ssh-ed25519 AAAAtest odysseus" in text
    ps1 = c.get(f"/enroll/{code}/install.ps1").text
    assert "'mcp>=1.10,<1.13'" in ps1 and "OdysseusDesktopMCP" in ps1 and "100.64.0.0/10" in ps1


def test_rendered_install_sh_is_valid_bash(client, tmp_path):
    c, _ = client
    code = c.post("/api/devices/enroll", json={"kind": "computer"}).json()["code"]
    script = tmp_path / "install.sh"
    script.write_text(c.get(f"/enroll/{code}/install.sh").text)
    p = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_files_are_allowlisted_and_code_gated(client):
    c, _ = client
    code = c.post("/api/devices/enroll", json={"kind": "computer"}).json()["code"]
    assert "FastMCP" in c.get(f"/enroll/{code}/file/desktop_mcp_server.py").text
    for bad in ("app.py", "..%2Fapp.py", "devices.json"):
        assert c.get(f"/enroll/{code}/file/{bad}").status_code == 404
    assert c.get("/enroll/aaaaaaaaaaaaaaaa/install.sh").status_code == 404
    phone = c.post("/api/devices/enroll", json={"kind": "phone", "device": "p"}).json()["code"]
    assert c.get(f"/enroll/{phone}/install.sh").status_code == 404        # phone code, not computer


def test_register_adds_servers_once_and_records_the_machine(client, db):
    c, mgr = client
    code = c.post("/api/devices/enroll", json={"kind": "computer"}).json()["code"]
    body = {"dns": "gaming-pc.tail0.ts.net", "ip": "100.70.1.2", "os": "windows", "user": "jaron",
            "gpu": "NVIDIA GeForce RTX 4080", "ssh": True,
            "servers": [{"kind": "desktop", "port": 8931}, {"kind": "resolve", "port": 8930},
                        {"kind": "evil", "port": 22}]}
    r = c.post(f"/enroll/{code}/register", json=body).json()
    assert r["machine"] == "gaming-pc" and [s["name"] for s in r["servers"]] == ["gaming-pc-desktop", "gaming-pc-resolve"]
    assert len(mgr.reconnects) == 2
    assert machines.load_prefs()["gaming-pc"] == {"gpu": True, "ssh_user": "jaron"}
    assert c.post(f"/enroll/{code}/register", json=body).status_code == 404   # used up

    # Re-running on the same machine with a new code updates, not duplicates.
    code2 = c.post("/api/devices/enroll", json={"kind": "computer"}).json()["code"]
    c.post(f"/enroll/{code2}/register", json=body)
    import core.database as cdb
    s = db()
    assert s.query(cdb.McpServer).count() == 2
    s.close()
    st = c.get(f"/api/devices/enroll/{code}").json()
    assert st["used"] and st["result"]["machine"] == "gaming-pc"


def test_register_refuses_addresses_off_the_tailnet(client, db):
    c, _ = client
    for bad in ({"dns": "x.tail0.ts.net", "ip": "8.8.8.8"},
                {"dns": "evil.example.com", "ip": "100.70.1.2"},
                {"dns": "x.tail0.ts.net", "ip": "not-an-ip"}):
        code = c.post("/api/devices/enroll", json={"kind": "computer"}).json()["code"]
        assert c.post(f"/enroll/{code}/register", json=bad).status_code == 400


# ── Phone ──────────────────────────────────────────────────────────────────

def test_phone_page_registers_and_subscribes_under_its_own_name(client, monkeypatch):
    c, _ = client
    saved, sent = [], []
    monkeypatch.setattr(webpush, "save_subscription",
                        lambda sub, device="", owner="": saved.append((device, sub)) or {"device": device, "endpoint": "e"})
    monkeypatch.setattr(webpush, "public_key", lambda: "BKEY")

    async def fake_send(title, body, device="", url=""):
        sent.append(device)
        return {"sent": 1}

    monkeypatch.setattr(webpush, "send", fake_send)
    r = c.post("/api/devices/enroll", json={"kind": "phone", "device": "pixel-9"}).json()
    assert r["url"].endswith(f"/enroll/{r['code']}/phone") and r["qr_svg"].startswith("<")
    page = c.get(f"/enroll/{r['code']}/phone").text
    tok = devices.get("pixel-9")["token"]
    assert tok in page and "Turn on notifications" in page
    assert c.get(f"/enroll/{r['code']}/push-key").json() == {"public_key": "BKEY"}
    assert c.post(f"/enroll/{r['code']}/push", json={"subscription": {"endpoint": "https://fcm/x"}}).json()["sent"] == 1
    assert saved[0][0] == "pixel-9" and sent == ["pixel-9"]     # no alias mix-up possible
    assert c.get(f"/enroll/{r['code']}/phone").status_code == 404        # used up


def test_phone_and_check_codes_need_a_device(client):
    c, _ = client
    assert c.post("/api/devices/enroll", json={"kind": "phone"}).status_code == 400
    assert c.post("/api/devices/enroll", json={"kind": "check"}).status_code == 400


# ── Check ──────────────────────────────────────────────────────────────────

def test_check_page_reports_what_odysseus_can_reach(client, monkeypatch):
    c, _ = client
    devices.register("pixel-8a", kind="phone", commands=["notify", "open_url"],
                     endpoint="http://pixel-8a.tail0.ts.net:8778")
    peers = [{"host": "pixel-8a", "name": "Pixel 8a", "dns": "pixel-8a.tail0.ts.net", "ips": ["100.96.1.1"],
              "os": "android", "online": True, "is_self": False, "shared": False, "age_days": 0, "last_seen": ""}]
    monkeypatch.setattr(machines, "peers", lambda refresh=False: peers)

    async def listener_ok(device, command, params=None, timeout=10.0):
        return {"ok": True}

    monkeypatch.setattr(devices, "send_command", listener_ok)
    monkeypatch.setattr(webpush, "load_subscriptions", lambda: [{"device": "pixel-8a", "endpoint": "e"}])
    import routes.device_routes as dr
    monkeypatch.setattr(dr, "_configured_servers", lambda: [])
    r = c.post("/api/devices/enroll", json={"kind": "check", "device": "pixel-8a"}).json()
    assert "Is pixel-8a connected?" in c.get(f"/enroll/{r['code']}/check").text
    status = c.get(f"/enroll/{r['code']}/check/status").json()
    assert status["info"] == {"name": "Pixel 8a", "os": "android", "ip": "100.96.1.1", "online": True, "last_seen": ""}
    checks = status["checks"]
    assert [x["ok"] for x in checks] == [True, True, True]
    assert "online" in checks[0]["text"] and "Modes listener answered" in checks[1]["text"]
    # A check code can be reloaded: it expires rather than being used up.
    assert c.get(f"/enroll/{r['code']}/check/status").status_code == 200


# ── Login exemption ────────────────────────────────────────────────────────

def test_only_enroll_paths_skip_login():
    src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    pat = re.search(r'_re\.compile\(r"(\^/enroll/[^"]+)"\)', src).group(1)
    rx = re.compile(pat)
    code = "abcdefghjkmnpqrs"
    for ok in (f"/enroll/{code}/install.sh", f"/enroll/{code}/file/desktop_mcp_server.py",
               f"/enroll/{code}/check/status", f"/enroll/{code}/phone"):
        assert rx.match(ok), ok
    for no in ("/enroll/short/install.sh", f"/enroll/{code}/../../api/devices", "/api/devices/enroll",
               f"/enroll/{code}/a/b/c"):
        assert not rx.match(no), no


# ── Pages must work under the site's Content-Security-Policy ───────────────

def test_pages_have_no_inline_script(client, monkeypatch):
    """The first version used inline <script>; the CSP (script-src 'self'
    plus a nonce) blocked it, so every button on the phone and check pages
    silently did nothing. Behaviour must come from a static file."""
    c, _ = client
    monkeypatch.setattr(machines, "peers", lambda refresh=False: [])
    for kind in ("phone", "check"):
        r = c.post("/api/devices/enroll", json={"kind": kind, "device": "pixel-8a"}).json()
        page = c.get(r["url"].replace("https://jaron-dev-server.tail0.ts.net", "")).text
        scripts = re.findall(r"<script\b[^>]*>", page)
        assert scripts == ['<script src="/static/js/enroll-page.js" defer>'], (kind, scripts)
        assert f'data-code="{r["code"]}"' in page and 'data-page="' + kind + '"' in page
    assert os.path.exists(os.path.join(HERE, "static", "js", "enroll-page.js"))
    assert os.path.exists(os.path.join(HERE, "static", "css", "enroll-page.css"))


def test_check_page_can_turn_notifications_on_under_the_device_name(client, monkeypatch):
    c, _ = client
    saved, sent = [], []
    monkeypatch.setattr(webpush, "save_subscription",
                        lambda sub, device="", owner="": saved.append(device) or {"device": device, "endpoint": "e"})
    monkeypatch.setattr(webpush, "public_key", lambda: "BKEY")

    async def fake_send(title, body, device="", url=""):
        sent.append(device)
        return {"sent": 1}

    monkeypatch.setattr(webpush, "send", fake_send)
    r = c.post("/api/devices/enroll", json={"kind": "check", "device": "pixel-8a"}).json()
    assert c.get(f"/enroll/{r['code']}/check/push-key").json() == {"public_key": "BKEY"}
    for _ in range(2):                       # a check code is not used up
        assert c.post(f"/enroll/{r['code']}/check/subscribe",
                      json={"subscription": {"endpoint": "https://fcm/x"}}).json()["sent"] == 1
    assert saved == ["pixel-8a", "pixel-8a"] and sent == ["pixel-8a", "pixel-8a"]
    t = c.post(f"/enroll/{r['code']}/check/push").json()
    assert t["sent"] == 1


# ── Every Odysseus tool on every linked computer ───────────────────────────
# Asked for on 2026-09-29: "lets say I get my device linked, ask to install all
# MCPs available on that server or PC that my PC has? cause I know my laptop
# does not have music on it, so that popup would be nice to have".

def test_the_scripts_install_the_overlay_its_packages_and_token(client, tmp_path):
    c, _ = client
    code = c.post("/api/devices/enroll", json={"kind": "computer"}).json()["code"]
    sh = c.get(f"/enroll/{code}/install.sh").text
    assert "file/music_overlay.py" in sh and "odysseus-music-overlay.service" in sh
    assert 'tok = r.pop("overlay_token", "")' in sh and "odysseus-music-overlay" in sh
    (tmp_path / "i.sh").write_text(sh)
    assert subprocess.run(["bash", "-n", str(tmp_path / "i.sh")]).returncode == 0
    ps1 = c.get(f"/enroll/{code}/install.ps1").text
    assert "winsdk pyaudiowpatch" in ps1 and "Fetch 'music_overlay.py'" in ps1
    assert "Register-ScheduledTask -TaskName 'MusicOverlay'" in ps1 and "$r.overlay_token" in ps1
    assert "ConvertFrom-Json -AsHashtable" not in ps1                               # Windows PowerShell 5.1-safe
    assert "def main():" in c.get(f"/enroll/{code}/file/music_overlay.py").text   # served from its own folder


def test_register_mints_an_overlay_only_token_per_machine(client, db, monkeypatch):
    import core.database as cdb
    engine = db.kw["bind"]
    cdb.ApiToken.__table__.create(bind=engine)
    c, _ = client
    body = {"dns": "jaron-laptop.tail0.ts.net", "ip": "100.70.1.3", "os": "linux", "user": "jaron",
            "ssh": True, "servers": [{"kind": "desktop", "port": 8932}]}
    first = c.post(f"/enroll/{c.post('/api/devices/enroll', json={'kind': 'computer'}).json()['code']}/register",
                   json=body).json()["overlay_token"]
    second = c.post(f"/enroll/{c.post('/api/devices/enroll', json={'kind': 'computer'}).json()['code']}/register",
                    json=body).json()["overlay_token"]
    assert first.startswith("ody_") and first != second
    s = db()
    rows = s.query(cdb.ApiToken).filter(cdb.ApiToken.name == "overlay · jaron-laptop").all()
    assert sorted(r.is_active for r in rows) == [False, True] and {r.scopes for r in rows} == {"overlay"}
    s.close()


class _ToolsMgr:
    def __init__(self, tools, versions):
        self._tools, self.versions = tools, versions

    def get_server_status(self, sid):
        return {"status": "connected"}

    async def call_tool(self, name, args):
        import json
        return {"stdout": json.dumps({"ok": True, "files": self.versions[name.split("__")[1]]})}


def test_the_tools_check_names_what_each_computer_lacks(monkeypatch):
    from routes import device_routes as dr
    monkeypatch.setattr(dr, "_configured_servers", lambda: [
        {"id": "win", "name": "desktop-jaron-desktop", "url": "http://100.70.1.2:8931/sse", "enabled": True},
        {"id": "lap", "name": "jaron-laptop-desktop", "url": "http://100.70.1.3:8932/sse", "enabled": True},
        {"id": "res", "name": "desktop-jaron-resolve", "url": "http://100.70.1.2:8930/sse", "enabled": True}])
    peers = [{"host": "desktop-jaron", "os": "windows", "online": True, "dns": "desktop-jaron.tail0.ts.net", "ips": ["100.70.1.2"]},
             {"host": "jaron-laptop", "os": "linux", "online": True, "dns": "jaron-laptop.tail0.ts.net", "ips": ["100.70.1.3"]}]
    monkeypatch.setattr(machines, "peers", lambda refresh=False: peers)
    monkeypatch.setattr(machines, "find_peer", lambda ps, h: next((p for p in ps if h in p["ips"]), None))
    here = enroll_routes._sha_file(os.path.join(HERE, "tools", "mcp", "desktop_mcp_server.py"))
    ov = enroll_routes._sha_file(os.path.join(HERE, "tools", "music_overlay", "music_overlay.py"))
    win_tools = [{"name": t} for t in enroll_routes.FEATURES["windows"].values()] + [{"name": "tools_version"}]
    mgr = _ToolsMgr({"win": win_tools, "lap": [{"name": "now_playing"}]},
                    {"win": {"desktop_mcp_server.py": here, "music_overlay.py": ov}})
    rows = {m["server_id"]: m for m in asyncio.run(enroll_routes.machine_tools(mgr))}
    assert set(rows) == {"win", "lap"}                                   # the Resolve server is not a desktop MCP
    assert rows["win"]["up_to_date"] and rows["win"]["missing"] == []
    assert rows["lap"]["missing"] == ["Music overlay (Pop out)", "Play another computer's sound"]
    assert rows["lap"]["outdated"] == ["linux_desktop_mcp_server.py"]   # no tools_version: too old to say
    mgr.versions["win"]["music_overlay.py"] = "old"
    rows = {m["server_id"]: m for m in asyncio.run(enroll_routes.machine_tools(mgr))}
    assert rows["win"]["outdated"] == ["music_overlay.py"] and not rows["win"]["up_to_date"]


def test_update_runs_the_install_command_over_ssh(monkeypatch):
    ran = []

    async def fake_run(argv, timeout):
        ran.append(argv)
        return {"rc": 0, "out": "==> done", "err": ""}
    monkeypatch.setattr(machines, "_run", fake_run)
    monkeypatch.setattr(machines, "_ssh_keys", lambda: ["/k"])
    monkeypatch.setattr(machines, "load_prefs", lambda: {"jaron-laptop": {"ssh_user": "jaron"}})
    lap = {"name": "jaron-laptop-desktop", "os": "linux",
           "_peer": {"host": "jaron-laptop", "dns": "jaron-laptop.tail0.ts.net", "ips": ["100.70.1.3"]}}
    r = asyncio.run(enroll_routes.run_installer(lap, "https://ody.tail0.ts.net", "abcdefghjkmnpqrs"))
    assert r["ok"] and "jaron@jaron-laptop.tail0.ts.net" in ran[-1]
    assert "curl -fsSL https://ody.tail0.ts.net/enroll/abcdefghjkmnpqrs/install.sh | bash" in ran[-1][-1]
    win = dict(lap, name="desktop-jaron-desktop", os="windows",
               _peer={"host": "desktop-jaron", "dns": "desktop-jaron.tail0.ts.net", "ips": []})
    asyncio.run(enroll_routes.run_installer(win, "https://ody.tail0.ts.net", "abcdefghjkmnpqrs"))
    assert ran[-1][-1].startswith("powershell -NoProfile -EncodedCommand ")


def test_both_desktop_mcps_report_their_version_and_start_the_overlay():
    for name in ("desktop_mcp_server.py", "linux_desktop_mcp_server.py"):
        src = open(os.path.join(HERE, "tools", "mcp", name), encoding="utf-8").read()
        assert "def tools_version()" in src and "def start_music_overlay()" in src, name
    media = open(os.path.join(HERE, "routes", "media_routes.py"), encoding="utf-8").read()
    assert 'if "start_music_overlay" in set(target.get("tools") or []):' in media
    ui = open(os.path.join(HERE, "static", "js", "devicesSettings.js"), encoding="utf-8").read()
    assert "/api/devices/tools/${encodeURIComponent(b.dataset.mUpdate)}/update" in ui
    assert "import './deviceToolsNudge.js';" in open(os.path.join(HERE, "static", "js", "chat.js")).read()
