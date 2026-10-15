"""Game streaming (src/games.py, routes/games_routes.py, src/agent_tools/game_tools.py).

Sunshine's API, the SSH to the PC, Steam's store and CDN and Moonlight Web are
all fakes here: nothing leaves the test process except one local TLS server
in the certificate test.
"""
import ast
import asyncio
import base64
import json
import re
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware
from routes import games_routes
from src import games
from src.agent_tools import game_tools

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "hunter2-very-secret"
FILE_PASSWORD = "from-the-file-s3cret"
LAN = "https://192.168.100.82:47990"
TAILNET = "https://100.102.86.125:47990"

needs_magick = pytest.mark.skipif(not shutil.which("convert"), reason="ImageMagick not installed")


def _image(fmt="jpg", size="200x300", src="plasma:fractal") -> bytes:
    return subprocess.run(["convert", "-size", size, src, f"{fmt}:-"], capture_output=True, check=True).stdout


APPS = [
    {"name": "Desktop", "image-path": "desktop.png"},
    {"name": "Steam Big Picture", "cmd": "steam://open/bigpicture", "image-path": "steam.png",
     "auto-detach": "true", "wait-all": "true"},
    {"name": "Hades", "cmd": "steam://rungameid/1145360", "image-path": "C:\\Users\\jaron\\sunshine-covers\\1145360.png",
     "auto-detach": "true", "wait-all": "true", "exit-timeout": "5", "prep-cmd": [{"do": "secret.exe"}]},
    {"name": "Old Game", "cmd": "steam://rungameid/400", "image-path": "C:\\Users\\jaron\\sunshine-covers\\400.png"},
]

SERVERINFO_BUSY = """<?xml version="1.0" encoding="utf-8"?>
<root status_code="200"><hostname>DESKTOP-JARON</hostname><currentgame>1234567</currentgame>
<state>SUNSHINE_SERVER_BUSY</state><PairStatus>0</PairStatus></root>"""
SERVERINFO_FREE = SERVERINFO_BUSY.replace("1234567", "0").replace("BUSY", "FREE")


class Fake:
    """Sunshine, its GameStream /serverinfo, Steam's store and CDN, Moonlight Web."""

    def __init__(self):
        self.apps = [dict(a) for a in APPS]
        self.calls = []
        self.posted = []
        self.closed = 0
        self.serverinfo = SERVERINFO_FREE
        self.down = set()
        self.cdn = {}          # appid -> bytes for library_600x900*.jpg
        self.types = {}        # appid -> store type
        self.moonlight_up = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.calls.append((request.method, str(url), request.headers.get("authorization")))
        host = url.host
        if host in self.down:
            raise httpx.ConnectError("down", request=request)
        if url.port == 47990:
            want = "Basic " + base64.b64encode(f"sunuser:{PASSWORD}".encode()).decode()
            want_file = "Basic " + base64.b64encode(f"fileuser:{FILE_PASSWORD}".encode()).decode()
            if request.headers.get("authorization") not in (want, want_file):
                return httpx.Response(401)
            if url.path == "/api/apps" and request.method == "GET":
                return httpx.Response(200, json={"apps": self.apps, "env": {"PATH": "x"}})
            if url.path == "/api/apps" and request.method == "POST":
                body = json.loads(request.content)
                self.posted.append(body)
                self.apps.append({k: v for k, v in body.items() if k != "index"})
                return httpx.Response(200, json={"status": True})
            if url.path == "/api/apps/close":
                self.closed += 1
                return httpx.Response(200, json={"status": True})
            if url.path == "/api/pin" and request.method == "GET":
                return httpx.Response(200, json={"pairings": [{"id": "p1", "name": "moonlight-web", "address": "100.121.62.9"}]})
            if url.path == "/api/pin":
                self.posted.append(json.loads(request.content))
                return httpx.Response(200, json={"status": True})
            if url.path == "/api/clients/list":
                return httpx.Response(200, json={"named_certs": [{"name": "moonlight-web", "cert": "PEM"}]})
            return httpx.Response(404)
        if url.port == 47989 and url.path == "/serverinfo":
            return httpx.Response(200, text=self.serverinfo)
        if host == "store.steampowered.com":
            appid = request.url.params.get("appids")
            t = self.types.get(int(appid), "game")
            return httpx.Response(200, json={appid: {"success": True, "data": {"type": t, "name": "x"}}})
        if host.endswith("steamstatic.com"):
            m = re.search(r"/apps/(\d+)/", url.path)
            data = self.cdn.get(int(m.group(1))) if m else None
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, content=data, headers={"content-type": "image/jpeg"})
        if host == "127.0.0.1" and url.port == 8090:
            if not self.moonlight_up:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, text="<html>moonlight</html>")
        return httpx.Response(404)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import src.constants
    from src import machines
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ODYSSEUS_SUNSHINE_URLS", raising=False)
    monkeypatch.setattr(machines, "peers", lambda refresh=False: [
        {"host": "desktop-jaron", "name": "DESKTOP-JARON", "dns": "desktop-jaron.tail.ts.net",
         "ips": ["100.102.86.125"], "os": "windows", "online": True, "is_self": False, "shared": False}])
    monkeypatch.setattr(machines, "load_prefs", lambda: {})
    games._status_cache.update(at=0.0, data=None)
    games._good_url.update(url=None, at=0.0)
    games._discovered.update(at=0.0, urls=[])
    games._cover_misses.clear()
    games._cover_locks.clear()
    games._sync_job.update(running=False, step="", started_at=None, result=None, error=None)
    # No real credentials file: point the default at a path that isn't there.
    monkeypatch.setattr(games, "DEFAULT_CREDENTIALS_FILE", str(tmp_path / "no-such.env"))
    return tmp_path


@pytest.fixture
def fake(data_dir, monkeypatch):
    f = Fake()
    monkeypatch.setattr(games, "_http_transport", httpx.MockTransport(f))
    return f


@pytest.fixture
def setup(fake):
    games.save_config({"sunshine_urls": [LAN, TAILNET], "username": "sunuser", "password": PASSWORD})
    return fake


class _Auth:
    is_configured = True

    def __init__(self, admins):
        self.admins = admins

    def is_admin(self, user):
        return user in self.admins


def _client(user="jaron", admins=("jaron",)):
    app = FastAPI()
    app.state.auth_manager = _Auth(set(admins))

    @app.middleware("http")
    async def _who(request, call_next):
        request.state.current_user = user
        return await call_next(request)

    app.include_router(games_routes.setup_games_routes())
    return TestClient(app)


def run(coro):
    return asyncio.run(coro)


# ── /play ───────────────────────────────────────────────────────────────

def _app_source():
    return (ROOT / "app.py").read_text()


def test_bare_play_redirects_to_moonlight_web_with_the_slash():
    tree = ast.parse(_app_source())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "play_redirect")
    deco = ast.unparse(fn.decorator_list[0])
    fn.decorator_list = []
    assert deco.startswith("app.get('/play'")
    ns = {}
    from fastapi import Request
    from starlette.responses import RedirectResponse
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"),
         {"RedirectResponse": RedirectResponse, "Request": Request}, ns)
    app = FastAPI()
    app.get("/play")(ns["play_redirect"])

    @app.get("/{path:path}")          # a catch-all after it must not take /play
    async def spa(path: str):
        return {"spa": path}
    c = TestClient(app)
    r = c.get("/play", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/play/"
    r = c.get("/play?x=1", follow_redirects=False)
    assert r.headers["location"] == "/play/?x=1"


def test_bare_play_skips_the_login_redirect():
    tree = ast.parse(_app_source())
    exact = next(n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "AUTH_EXEMPT_EXACT" for t in n.targets))
    paths = ast.literal_eval(exact)
    assert "/play" in paths
    # Only the bare path: nothing else under /play is exempt.
    assert not any(p.startswith("/play/") for p in paths)
    src = _app_source()
    assert src.index('@app.get("/play"') < src.index('@app.get("/notes")')


# ── Settings and secrets ────────────────────────────────────────────────

def test_addresses_are_normalized():
    assert games.normalize_sunshine_url("192.168.100.82") == "https://192.168.100.82:47990"
    assert games.normalize_sunshine_url("https://pc.ts.net:47990/") == "https://pc.ts.net:47990"
    for bad in ("ftp://pc", "https://u:p@pc", "https://pc/api", "https://pc?x=1"):
        with pytest.raises(ValueError):
            games.normalize_sunshine_url(bad)
    assert games.normalize_moonlight_url("") == "/play/"
    assert games.normalize_moonlight_url("/play") == "/play/"
    assert games.normalize_moonlight_url("https://pc.ts.net/play") == "https://pc.ts.net/play/"
    for bad in ("//evil/", "javascript:alert(1)", "/play/?a=1"):
        with pytest.raises(ValueError):
            games.normalize_moonlight_url(bad)


def test_the_password_is_encrypted_and_never_returned(fake, data_dir):
    c = _client()
    r = c.put("/api/games/config", json={"sunshine_urls": f"{LAN}\n{TAILNET}", "username": "sunuser",
                                         "password": PASSWORD})
    assert r.status_code == 200
    assert PASSWORD not in r.text and r.json()["has_password"] is True
    stored = (data_dir / "games.json").read_text()
    assert PASSWORD not in stored
    assert json.loads(stored)["password"].startswith("enc:")
    assert games.credentials()[:2] == ("sunuser", PASSWORD)
    # Saving other fields keeps the password; a blank one doesn't clear it.
    c.put("/api/games/config", json={"play_in": "new_tab", "password": ""})
    assert games.credentials()[1] == PASSWORD
    for path in ("/api/games/config", "/api/games/status", "/api/games/apps", "/api/games/health",
                 "/api/games/pairings"):
        body = c.get(path).text
        assert PASSWORD not in body, path
        assert "enc:" not in body, path
    assert c.put("/api/games/config", json={"clear_password": True}).json()["has_password"] is False
    assert games.credentials()[2] == ""


def test_credentials_can_come_from_an_env_file_and_stay_on_the_server(fake, data_dir):
    env = data_dir / "sunshine.env"
    env.write_text(f'# Sunshine\nexport SUNSHINE_USER=fileuser\nSUNSHINE_PASS="{FILE_PASSWORD}"\nOTHER=nope\n')
    assert games.read_env_file(str(env)) == {"SUNSHINE_USER": "fileuser", "SUNSHINE_PASS": FILE_PASSWORD}
    c = _client()
    cfg = c.put("/api/games/config", json={"sunshine_urls": [LAN], "credentials_file": str(env)}).json()
    assert cfg["credentials_from"] == "file" and cfg["credentials_file_exists"] is True
    apps = c.get("/api/games/apps").json()["apps"]
    assert [a["name"] for a in apps][:3] == ["Desktop", "Steam Big Picture", "Hades"]
    for path in ("/api/games/config", "/api/games/apps", "/api/games/status", "/api/games/health"):
        body = c.get(path).text
        assert FILE_PASSWORD not in body and "fileuser" not in body, path
    # Settings win over the file once both are set.
    games.save_config({"username": "sunuser", "password": PASSWORD})
    assert games.credentials()[2] == "settings"


def test_bad_settings_are_refused(fake):
    c = _client()
    assert c.put("/api/games/config", json={"sunshine_urls": "ftp://pc"}).status_code == 400
    assert c.put("/api/games/config", json={"pc_covers_dir": "C:\\x\";rm"}).status_code == 400
    assert c.put("/api/games/config", json={"steam_libraries": "relative\\path"}).status_code == 400
    assert c.put("/api/games/config", json={"play_in": "popup"}).status_code == 400
    assert c.put("/api/games/config", content=b"nope").status_code == 400


def test_only_an_admin_gets_anything(setup, data_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    before = (data_dir / "games.json").read_text()
    for user in (None, "guest"):
        c = _client(user=user)
        for method, path in (("get", "/api/games/apps"), ("get", "/api/games/status"), ("get", "/api/games/config"),
                             ("get", "/api/games/cover/1145360"), ("post", "/api/games/stop"),
                             ("post", "/api/games/sync"), ("get", "/api/games/health"), ("post", "/api/games/test"),
                             ("get", "/api/games/pairings"), ("post", "/api/games/forget-certificates")):
            assert getattr(c, method)(path).status_code == 403, path
        assert c.put("/api/games/config", json={"username": "evil"}).status_code == 403
    assert (data_dir / "games.json").read_text() == before
    assert setup.closed == 0


# ── The Games page ──────────────────────────────────────────────────────

def test_apps_list_names_kinds_and_covers_without_commands_or_creds(setup):
    c = _client()
    d = c.get("/api/games/apps").json()
    by = {a["name"]: a for a in d["apps"]}
    assert by["Hades"]["kind"] == "steam" and by["Hades"]["steam_appid"] == 1145360
    assert by["Hades"]["cover"] == "/api/games/cover/1145360"
    assert by["Desktop"]["kind"] == "desktop" and by["Steam Big Picture"]["kind"] == "bigpicture"
    text = json.dumps(d)
    for leak in ("steam://", "secret.exe", "prep-cmd", "PATH", PASSWORD, "sunuser"):
        assert leak not in text
    # Basic auth went to Sunshine only.
    assert all(auth for (m, u, auth) in setup.calls if ":47990/" in u)


def test_covers_are_served_from_the_covers_folder(setup, data_dir):
    covers = data_dir / "covers"
    covers.mkdir()
    (covers / "1145360.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    games.save_config({"covers_dir": str(covers)})
    c = _client()
    r = c.get("/api/games/cover/1145360")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    hades = next(a for a in c.get("/api/games/apps").json()["apps"] if a["name"] == "Hades")
    assert hades["has_cover"] and hades["cover"].startswith("/api/games/cover/1145360?v=")
    for bad in ("..%2Fgames", ".hidden", "a b"):
        assert c.get(f"/api/games/cover/{bad}").status_code == 404


@needs_magick
def test_a_missing_cover_is_fetched_once_from_steam(setup, data_dir, monkeypatch):
    async def no_ssh(*a, **k):
        raise games.GamesError("no ssh in this test")
    monkeypatch.setattr(games, "run_powershell", no_ssh)
    setup.cdn[1145360] = _image()
    c = _client()
    r = c.get("/api/games/cover/1145360")
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert (data_dir / "game-covers" / "1145360.png").exists()
    n = len(setup.calls)
    assert c.get("/api/games/cover/1145360").status_code == 200
    assert len(setup.calls) == n          # served from the cache


def test_status_says_what_is_running(setup):
    setup.serverinfo = SERVERINFO_BUSY
    setup.apps[2]["id"] = "1234567"
    st = run(games.status(force=True))
    assert st["sunshine"]["ok"] and st["sunshine"]["url"] == LAN
    assert st["pc"]["name"] == "DESKTOP-JARON" and st["pc"]["online"] is True
    assert st["running"] == {"app_id": 1234567, "name": "Hades"}
    setup.serverinfo = SERVERINFO_FREE
    assert run(games.status(force=True))["running"] is None


def test_the_tailnet_address_is_used_when_the_lan_one_is_down(setup):
    setup.down.add("192.168.100.82")
    st = run(games.status(force=True))
    assert st["sunshine"]["ok"] and st["sunshine"]["url"] == TAILNET


def test_wrong_credentials_are_reported(setup):
    games.save_config({"password": "wrong"})
    st = run(games.status(force=True))
    assert not st["sunshine"]["ok"] and "refused the username or password" in st["sunshine"]["error"]


def test_stop_closes_the_running_app(setup):
    r = _client().post("/api/games/stop")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert setup.closed == 1


def test_pairing_pins_go_to_sunshine(setup):
    c = _client()
    d = c.get("/api/games/pairings").json()
    assert d["pairings"] == [{"id": "p1", "name": "moonlight-web", "address": "100.121.62.9"}]
    assert d["clients"] == [{"name": "moonlight-web"}]
    assert "PEM" not in json.dumps(d)
    assert c.post("/api/games/pair", json={"pairing_id": "p1", "pin": "12a4"}).status_code == 400
    assert c.post("/api/games/pair", json={"pairing_id": "p1", "pin": "1234", "name": "web"}).json() == {"ok": True}
    assert setup.posted[-1] == {"pin": "1234", "name": "web", "pairing_id": "p1"}


# ── Health ──────────────────────────────────────────────────────────────

def test_health_reports_sunshine_and_moonlight_web(setup):
    h = _client().get("/api/games/health").json()
    assert h["sunshine"]["ok"] is True and h["moonlight"] == {"ok": True, "url": "http://127.0.0.1:8090/play/",
                                                              "detail": "Answering"}
    setup.moonlight_up = False
    setup.down.update({"192.168.100.82", "100.102.86.125"})
    h = _client().get("/api/games/health").json()
    assert h["sunshine"]["ok"] is False and "not answering" in h["sunshine"]["error"]
    assert h["moonlight"]["ok"] is False


def test_test_button(setup):
    r = _client().post("/api/games/test").json()
    assert r == {"ok": True, "url": LAN, "apps": 4}


# ── Certificate ─────────────────────────────────────────────────────────

def _tls_server(tmp: Path, name: str):
    key, crt = tmp / f"{name}.key", tmp / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(crt), "-days", "30", "-subj", "/CN=Sunshine Gamestream Host"], capture_output=True, check=True)
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("authorization"))
            body = json.dumps({"apps": [{"name": "Desktop"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_sunshines_certificate_is_recorded_and_a_different_one_refused(data_dir, tmp_path):
    a, seen_a = _tls_server(tmp_path, "a")
    url = f"https://127.0.0.1:{a.server_address[1]}"
    try:
        games.save_config({"sunshine_urls": [url], "username": "sunuser", "password": PASSWORD})
        assert run(games.test_connection())["ok"] is True
        assert seen_a and seen_a[0].startswith("Basic ")
        pins = json.loads((data_dir / "games.json").read_text())["pins"]
        assert list(pins) == [f"127.0.0.1:{a.server_address[1]}"]
        assert run(games.test_connection())["ok"] is True
    finally:
        a.shutdown()
    # Something else answers on the same address with its own certificate.
    b, seen_b = _tls_server(tmp_path, "b")
    try:
        raw = json.loads((data_dir / "games.json").read_text())
        raw["pins"] = {f"127.0.0.1:{b.server_address[1]}": pins[f"127.0.0.1:{a.server_address[1]}"]}
        raw["sunshine_urls"] = [f"https://127.0.0.1:{b.server_address[1]}"]
        (data_dir / "games.json").write_text(json.dumps(raw))
        games._good_url.update(url=None, at=0.0)
        r = run(games.test_connection())
        assert r["ok"] is False and "different certificate" in r["error"]
        assert seen_b == []                       # the password never went to it
        assert games.public_config()["certificates"]
        games.forget_certificates()
        assert run(games.test_connection())["ok"] is True
    finally:
        b.shutdown()


# ── CSP ─────────────────────────────────────────────────────────────────

def _frame_src():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}
    csp = TestClient(app).get("/").headers["content-security-policy"]
    return next(d for d in csp.split("; ") if d.startswith("frame-src"))


def test_moonlight_web_here_needs_no_csp_change_and_elsewhere_gets_its_origin(data_dir):
    assert _frame_src() == "frame-src 'self' https://www.youtube-nocookie.com"
    games.save_config({"moonlight_url": "https://pc.tail.ts.net:8443/play"})
    assert _frame_src() == "frame-src 'self' https://www.youtube-nocookie.com https://pc.tail.ts.net:8443"
    games.save_config({"moonlight_url": "/play/"})
    assert _frame_src() == "frame-src 'self' https://www.youtube-nocookie.com"


# ── Steam sync ──────────────────────────────────────────────────────────

def _acf(appid, name, flags=4):
    return f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"StateFlags"\t\t"{flags}"\n' \
           f'\t"UserConfig"\n\t{{\n\t\t"name"\t\t"inner"\n\t}}\n}}\n'


def test_appmanifests_are_parsed():
    a = games.parse_acf(_acf(1145360, 'Hades \\"II\\"'))
    assert a["appid"] == "1145360" and a["name"] == 'Hades "II"' and a["stateflags"] == "4"


def test_powershell_scripts_quote_their_paths():
    s = games.manifests_script([r"C:\Program Files (x86)\Steam", r"D:\Jaron's Games\Steam"])
    assert "'C:\\Program Files (x86)\\Steam'" in s and "'D:\\Jaron''s Games\\Steam'" in s
    with pytest.raises(ValueError):
        games.manifests_script([r"C:\x'; Remove-Item C:\ -Recurse; '"])
    with pytest.raises(games.GamesError):
        games.upload_script(r"C:\covers", "../evil.png")


class FakePC:
    """SSH to the PC: the appmanifests, the Steam library cache, uploads."""

    def __init__(self, manifests, capsules=None):
        self.manifests = manifests
        self.capsules = capsules or {}
        self.uploads = {}
        self.scripts = []

    async def __call__(self, script, stdin=None, timeout=60.0):
        self.scripts.append(script)
        if "appmanifest_" in script:
            rows = [{"library": lib, "file": f"appmanifest_{i}.acf", "text": _acf(i, n, f)}
                    for (i, n, lib, f) in self.manifests]
            return {"rc": 0, "out": "\ufeff" + json.dumps(rows), "err": ""}
        if "librarycache" in script:
            appid = int(re.search(r"Join-Path \$lc '(\d+)'", script).group(1))
            data = self.capsules.get(appid)
            return {"rc": 0, "out": base64.b64encode(data).decode() + "\r\n" if data else "", "err": ""}
        if "WriteAllBytes" in script:
            name = re.search(r"Join-Path \$d '([^']+)'", script).group(1)
            self.uploads[name] = base64.b64decode(stdin)
            return {"rc": 0, "out": "ok\r\n", "err": ""}
        return {"rc": 0, "out": "", "err": ""}


@needs_magick
def test_sync_adds_missing_games_skips_non_games_and_falls_back_on_a_gray_cover(setup, data_dir, monkeypatch):
    C, D = r"C:\Program Files (x86)\Steam", r"D:\Program Files (x86)\Steam"
    pc = FakePC([
        (1145360, "Hades", C, 4),                              # already in Sunshine
        (228980, "Steamworks Common Redistributables", C, 4),
        (431960, "Wallpaper Engine", D, 4),
        (2200, "Some Game Soundtrack", D, 4),
        (3300, "A Music Pack", D, 4),                          # the store says "music"
        (1000, "New Game", D, 4),                              # good art on Steam's CDN
        (2000, "Brand New Game", D, 4),                        # gray placeholder on the CDN
        (5000, "Half Downloaded", D, 2),
    ], capsules={2000: _image(size="300x450", src="gradient:red-blue")})
    monkeypatch.setattr(games, "run_powershell", pc)
    setup.types[3300] = "music"
    setup.cdn[1000] = _image()
    setup.cdn[2000] = b"\xff\xd8" + b"\x00" * 2600             # Steam's ~2.6 KB placeholder
    r = run(games.sync_steam())
    assert r["added"] == ["Brand New Game", "New Game"]
    assert r["already"] == ["Hades"]
    assert r["removed"] == ["Old Game"]
    reasons = {s["name"]: s["reason"] for s in r["skipped"]}
    assert set(reasons) == {"Steamworks Common Redistributables", "Wallpaper Engine", "Some Game Soundtrack",
                            "A Music Pack", "Half Downloaded"}
    assert "music" in reasons["A Music Pack"]
    assert r["covers"] == {"New Game": "steam", "Brand New Game": "pc-library-cache"}
    assert r["errors"] == []
    # The new apps, shaped like the user's other Steam apps.
    added = {p["name"]: p for p in setup.posted}
    g = added["New Game"]
    assert g["cmd"] == "steam://rungameid/1000" and g["index"] == -1
    assert g["auto-detach"] == "true" and g["wait-all"] == "true"      # the template used strings
    assert g["image-path"] == "C:\\Users\\jaron\\sunshine-covers\\1000.png"
    assert g["prep-cmd"] == [] and "secret.exe" not in json.dumps(g)
    # Covers went to the PC as PNG and into the local cache.
    assert set(pc.uploads) == {"1000.png", "2000.png"}
    assert all(v.startswith(b"\x89PNG") for v in pc.uploads.values())
    assert (data_dir / "game-covers" / "2000.png").exists()
    # The store was asked only about candidates, not the known non-games.
    store = [u for (_, u, _) in setup.calls if "store.steampowered.com" in u]
    assert not any("228980" in u or "431960" in u or "1145360" in u for u in store)
    # A second run adds nothing.
    r2 = run(games.sync_steam())
    assert r2["added"] == [] and sorted(r2["already"]) == ["Brand New Game", "Hades", "New Game"]
    last = games.public_config()["last_sync"]
    assert last["added"] == [] and last["removed"] == ["Old Game"]


def test_placeholder_detection():
    assert games.is_placeholder(b"\xff\xd8" + b"\x00" * 2600)
    if shutil.which("convert") and shutil.which("identify"):
        flat = _image(fmt="bmp", size="300x450", src="xc:#2a2a2a")       # big, but one color
        assert len(flat) > games.PLACEHOLDER_MAX_BYTES and games.is_placeholder(flat)
        assert not games.is_placeholder(_image())


def test_sync_route_runs_in_the_background(setup, monkeypatch):
    async def fake_sync(progress=None):
        return {"added": ["X"], "already": [], "skipped": [], "removed": [], "covers": {}, "errors": [], "at": "t"}
    monkeypatch.setattr(games, "sync_steam", fake_sync)
    with _client() as c:
        s = c.post("/api/games/sync").json()
        assert s["running"] is True
        for _ in range(50):
            s = c.get("/api/games/sync").json()
            if not s["running"]:
                break
            import time
            time.sleep(0.05)
    assert s["result"]["added"] == ["X"] and s["error"] is None


# ── Chat tools ──────────────────────────────────────────────────────────

def test_tools_are_registered_admin_only_and_plan_mode_safe():
    import src.agent_tools as at
    from src import tool_index, tool_schemas, tool_security
    from src.agent_loop import TOOL_SECTIONS
    names = {t["function"]["name"] for t in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    assert game_tools.ALL_TOOLS <= names
    assert game_tools.ALL_TOOLS <= at.TOOL_TAGS
    assert game_tools.ALL_TOOLS <= set(tool_index.BUILTIN_TOOL_DESCRIPTIONS)
    assert game_tools.ALL_TOOLS <= set(TOOL_SECTIONS)
    assert all(tool_security.is_public_blocked_tool(t) for t in game_tools.ALL_TOOLS)
    blocked = tool_security.plan_mode_disabled_tools()
    assert {"game_stop", "game_sync_steam", "game_launch"} <= blocked
    assert not ({"games_list", "game_status"} & blocked)


def test_tools_hide_until_sunshine_has_a_login(data_dir):
    assert game_tools.hidden_tools("jaron") == set(game_tools.ALL_TOOLS)
    assert game_tools.relevant_tools("jaron", "launch hades on steam") == set()
    games.save_config({"username": "sunuser", "password": PASSWORD})
    assert game_tools.hidden_tools("jaron") == set()
    assert game_tools.relevant_tools("jaron", "what games do I have?") == set(game_tools.ALL_TOOLS)
    assert game_tools.relevant_tools("jaron", "what is the weather") == set()


def test_games_list_and_status_tools(setup):
    setup.serverinfo = SERVERINFO_BUSY
    setup.apps[2]["id"] = "1234567"
    out = run(game_tools.run_tool("games_list", "{}"))
    assert out["exit_code"] == 0
    assert "Hades (running)" in out["output"] and "Running: Hades." in out["output"]
    assert PASSWORD not in out["output"]
    out = run(game_tools.run_tool("games_list", '{"query": "steam"}'))
    assert "Steam Big Picture" in out["output"] and "Hades" not in out["output"].split("\n", 1)[1]
    st = run(game_tools.run_tool("game_status", "{}"))["output"]
    assert "DESKTOP-JARON - online" in st and "answering at https://192.168.100.82:47990" in st
    assert "Moonlight Web: up" in st


def test_game_launch_gives_a_link_that_starts_the_stream(setup):
    out = run(game_tools.run_tool("game_launch", '{"name": "hades"}'))
    assert out["exit_code"] == 0
    assert "[Play Hades](/#games?play=Hades)" in out["output"]
    assert "only launches a game for a Moonlight client" in out["output"]
    out = run(game_tools.run_tool("game_launch", '{"name": "Halo Infinite"}'))
    assert out["exit_code"] == 1 and "No app called" in out["error"]
    assert games.launch_link("Baldur's Gate 3") == "/#games?play=Baldur%27s%20Gate%203"


def test_game_stop_tool(setup):
    setup.serverinfo = SERVERINFO_BUSY
    setup.apps[2]["id"] = "1234567"
    out = run(game_tools.run_tool("game_stop", "{}"))
    assert out == {"output": "Closed Hades on the PC.", "exit_code": 0}
    assert setup.closed == 1


def test_tool_dispatch_reaches_the_game_tools_for_an_admin_only(setup, monkeypatch):
    from src import tool_execution
    from src.tool_parsing import ToolBlock
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: False)
    _, result = run(tool_execution.execute_tool_block(ToolBlock("game_stop", "{}"), owner="guest"))
    assert result["exit_code"] == 1 and "restricted to admin" in result["error"]
    assert setup.closed == 0
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: owner == "jaron")
    _, result = run(tool_execution.execute_tool_block(ToolBlock("games_list", "{}"), owner="jaron"))
    assert result["exit_code"] == 0 and "Hades" in result["output"], result


def test_find_app_matches_loosely():
    apps = [{"name": n} for n in ("Hades", "Hades II", "Baldur's Gate 3", "ELDEN RING")]
    assert games.find_app(apps, "hades")["name"] == "Hades"
    assert games.find_app(apps, "baldurs gate 3")["name"] == "Baldur's Gate 3"
    assert games.find_app(apps, "elden")["name"] == "ELDEN RING"
    assert games.find_app(apps, "zzz") is None


# ── The page's markup ───────────────────────────────────────────────────

def test_the_page_and_settings_are_wired_in():
    html = (ROOT / "static" / "index.html").read_text()
    assert 'id="tool-games-btn"' in html and "/static/js/gamesPanel.js" in html
    assert 'id="games-settings-card"' in html and "/static/js/gamesSettings.js" in html
    assert 'type="password"' in html.split('id="games-pass"')[1][:80] or 'id="games-pass" class="settings-select" type="password"' in html
    shell = (ROOT / "static" / "js" / "workspace" / "shell.js").read_text()
    assert "key: 'games'" in shell and ":not(:has(> .games-panel))" in shell
    js = (ROOT / "static" / "js" / "gamesSettings.js").read_text()
    # The password field is only ever written, never filled from the server.
    assert "$('games-pass').value = ''" in js and "c.password" not in js
