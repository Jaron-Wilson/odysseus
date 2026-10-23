"""Mod styles (src/mods/style.py): the ui.style permission gate, what the sanitizer refuses, theme tokens,
stylesheet order, removal when a mod is disabled or removed, the size cap, safe mode, and the routes."""
import asyncio
import http.server
import threading

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from src.mods import manifest as mf, registry, routes, runtime, style
from tests.mods_helpers import add_mod, mods_dir  # noqa: F401

STYLE_UI = {"ui": {"style": {"path": "/mod/style.css"}}}


# ── the manifest and the permission ──

def test_permission_is_described_for_the_install_review():
    assert "ui.style" in mf.PERMISSIONS
    text = mf.describe_permissions(["ui.style"])[0]["description"]
    assert "how Odysseus looks" in text and "CSS only" in text and "?safe=1" in text


def test_style_in_the_manifest_needs_the_permission():
    base = {"id": "paint", "name": "Paint", "version": "1.0.0",
            "runtime": {"type": "process", "command": ["python", "-m", "paint"]}}
    with pytest.raises(mf.ManifestError, match="needs the ui.style permission"):
        mf.validate({**base, **STYLE_UI})
    out = mf.validate({**base, **STYLE_UI, "permissions": ["ui.style"]})
    assert out["ui"]["style"] == {"path": "/mod/style.css"}
    assert mf.validate(base)["ui"]["style"] is None


@pytest.mark.parametrize("bad", ["style.css", "/a/../b.css", "//x.com/a.css", "/a b.css", 5])
def test_style_path_must_be_a_plain_path(bad):
    base = {"id": "paint", "name": "Paint", "version": "1.0.0", "permissions": ["ui.style"],
            "runtime": {"type": "process", "command": ["python", "-m", "paint"]}, "ui": {"style": {"path": bad}}}
    with pytest.raises(mf.ManifestError):
        mf.validate(base)


# ── the sanitizer ──

REFUSED = {
    "@import": "@import url('https://evil.example/x.css');",
    "@import bare": "@import 'x.css';",
    "off-origin url": "a { background: url(https://evil.example/p.png) }",
    "protocol-relative url": "a { background: url(//evil.example/p.png) }",
    "quoted off-origin url": 'a { background: url("http://evil.example/p.png") }',
    "relative url": "a { background: url(p.png) }",
    "another mod's file": "a { background: url(/mods/other/p.png) }",
    "path escape": "a { background: url(/mods/paint/../other/p.png) }",
    "the core's api": "a { background: url(/api/mods) }",
    "data: that is not an image": "a { background: url(data:text/html;base64,PGI+) }",
    "escaped url": "a { background: u\\72l(https://evil.example/p.png) }",
    "escaped import": "\\40import 'x.css';",
    "comment split": "a { background: url/**/(https://evil.example/p.png) }",
    "image-set": 'a { background: image-set("https://evil.example/p.png" 1x) }',
    "src()": 'a { background: src("https://evil.example/p.png") }',
    "cross-fade": "a { background: cross-fade(url(data:image/png;base64,AA) 50%, white) }",
    "expression": "a { width: expression(alert(1)) }",
    "behavior": "a { behavior: url(x.htc) }",
    "-moz-binding": "a { -moz-binding: url(x.xml#y) }",
    "javascript:": "a { background: url(javascript:alert(1)) }",
    "closing tag": "a { color: red } </style><script>alert(1)</script>",
    "NUL": "a { color: red }\x00",
    "malformed url": "a { background: url(\"data:image/png;base64,AA }",
}


@pytest.mark.parametrize("why", sorted(REFUSED))
def test_sanitizer_refuses(why):
    with pytest.raises(style.StyleError):
        style.sanitize(REFUSED[why], "paint")


def test_sanitizer_allows_what_a_theme_needs():
    css = """
    /* a comment */
    :root { --bg: #080C1C; --accent: #00BCFC }
    body { scroll-behavior: smooth; background: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg'/>") }
    .logo { background-image: url(/mods/paint/logo.png) }
    .f { filter: url(#blur) }
    @media (max-width: 600px) { .x { color: red } }
    @font-face { font-family: Own; src: url(/mods/paint/own.woff2) }
    """
    out = style.sanitize(css, "paint")
    assert "a comment" not in out and "url(/mods/paint/logo.png)" in out and "url(#blur)" in out


def test_size_cap():
    style.sanitize("a{color:red}\n" + "/*" + "x" * (style.MAX_BYTES - 100) + "*/", "paint")
    with pytest.raises(style.StyleError, match="64 KB"):
        style.sanitize("a{color:red}" + " " * style.MAX_BYTES, "paint")


def test_color_tokens_win_over_the_users_theme_and_layout_tokens_get_rules():
    out = style.sanitize(":root { --bg: #000; --accent: red !important; --ody-radius: 12px; --ody-density: 0.7 }", "paint")
    assert "--bg: #000 !important" in out and out.count("!important") >= 2
    assert "--accent: red !important" in out and "!important !important" not in out
    assert "border-radius: var(--ody-radius)" in out and "var(--ody-density)" in out
    assert "var(--ody-font-scale)" not in out        # a token it did not set gets no rules
    assert "--ody-radius" not in style.sanitize(":root { --bg: #000 }", "paint")


def test_workspace_and_studio_variables_follow_the_color_tokens():
    out = style.sanitize(":root { --border: #888; --fg-muted: #ccc; --accent: #fd0; --accent-ink: #000 }", "paint")
    rule = out[out.index("html.ui-studio"):]
    for want in ("--st-line: var(--border)", "--st-line-2: var(--border)", "--st-text-2: var(--fg-muted)",
                 "--st-accent: var(--accent)", "--st-accent-fg: var(--accent-ink)"):
        assert want in rule
    assert "html.ui-studio" not in style.sanitize(":root { --bg: #000 }", "paint")


def test_documented_tokens_are_all_described():
    for tok in ("bg", "fg", "panel", "border", "accent", "accent-ink", "fg-muted", "red", "font-family"):
        assert f"--{tok}" in style.TOKEN_DOCS
    for tok in style.LAYOUT_TOKENS:
        assert f"--{tok}" in style.TOKEN_DOCS and tok in style.DERIVED


# ── gating, order, removal ──

def give_style(mod_id, css="a{color:red}\n", **kw):
    """A mod with ui.style approved, and its checked stylesheet already stored."""
    entry, token = add_mod({**STYLE_UI}, permissions=["ui.style"], mod_id=mod_id, **kw)
    style._store(mod_id, css, entry)
    return entry, token


def test_no_style_without_the_approved_permission(mods_dir):
    entry, _ = add_mod({**STYLE_UI}, permissions=["ui.style"], mod_id="paint")
    style._store("paint", "a{color:red}\n", entry)
    assert [s["id"] for s in style.listing()] == ["paint"]
    with registry._lock:                                   # the admin never approved it
        data = registry._load()
        data["mods"]["paint"]["granted_permissions"] = []
        registry._save(data)
    assert style.listing() == [] and not style.eligible(registry.get("paint"))
    assert asyncio.run(style.refresh("paint"))["active"] is False
    assert style.read("paint") is None                      # and the stored copy is dropped


def test_a_mod_without_a_style_section_never_has_one(mods_dir):
    add_mod(permissions=[], mod_id="plain")
    assert not style.eligible(registry.get("plain")) and style.listing() == []


def test_ordered_by_mod_id_and_gone_when_disabled_or_removed(mods_dir):
    for mid in ("zeta", "alpha", "mid"):
        give_style(mid)
    assert [s["id"] for s in style.listing()] == ["alpha", "mid", "zeta"]
    registry.set_enabled("mid", False)
    assert [s["id"] for s in style.listing()] == ["alpha", "zeta"]
    registry.set_enabled("mid", True)
    assert [s["id"] for s in style.listing()] == ["alpha", "mid", "zeta"]
    registry.remove("zeta", keep_data=True)
    assert [s["id"] for s in style.listing()] == ["alpha", "mid"]
    style.clear("alpha")
    assert [s["id"] for s in style.listing()] == ["mid"] and style.read("alpha") is None


def test_an_updated_mod_needs_its_stylesheet_fetched_again(mods_dir):
    give_style("paint")
    assert style.listing()
    registry._update("paint", version="2.0.0")
    assert style.listing() == []                    # the old copy is not served for new code


def test_links_in_the_page_head_in_order_and_with_a_version(mods_dir):
    give_style("b-mod", "a{color:blue}\n")
    give_style("a-mod", "a{color:red}\n")
    req = StarletteRequest({"type": "http", "query_string": b"", "headers": []})
    html = style.with_links("<html><head><title>x</title></head><body></body></html>", req)
    a, b = html.index('data-mod="a-mod"'), html.index('data-mod="b-mod"')
    assert a < b < html.index("</head>")
    assert '<link rel="stylesheet" data-mod="a-mod" href="/api/mods/ui/style/a-mod.css?v=' in html
    assert "<script" not in html


def test_safe_mode_leaves_every_mod_style_out(mods_dir):
    give_style("paint")
    off_cookie = [(b"cookie", f"{style.SAFE_COOKIE}=off".encode())]
    for qs, headers in ((b"safe=1", []), (b"x=1&safe=true", []), (b"", off_cookie)):
        req = StarletteRequest({"type": "http", "query_string": qs, "headers": headers})
        assert style.is_safe(req)
        html = style.with_links("<head></head>", req)
        assert "data-mod" not in html and 'content="off"' in html
    req = StarletteRequest({"type": "http", "query_string": b"safe=0", "headers": []})
    assert not style.is_safe(req) and "data-mod=\"paint\"" in style.with_links("<head></head>", req)


# ── fetching from the mod ──

class Serve:
    def __init__(self):
        self.body = b"a{color:red}"
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/css")
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def mod_server(monkeypatch):
    s = Serve()

    async def running(_mod_id):
        return s.port

    monkeypatch.setattr(runtime, "ensure_running", running)
    yield s
    s.close()


def test_refresh_fetches_checks_and_keeps_it(mods_dir, mod_server):
    add_mod({**STYLE_UI}, permissions=["ui.style"], mod_id="paint")
    mod_server.body = b":root{--bg:#012;--ody-radius:9px}"
    st = asyncio.run(style.refresh("paint"))
    assert st["active"] and not st["error"] and st["bytes"] > 20
    css = style.read("paint")
    assert "--bg:#012 !important" in css and "--ody-radius:9px" in css
    v1 = style.listing()[0]["v"]
    mod_server.body = b":root{--bg:#345}"
    asyncio.run(style.refresh("paint"))
    assert style.listing()[0]["v"] != v1                  # a new ?v= so the page re-links it


def test_refresh_refuses_a_bad_sheet_and_the_page_goes_back_to_normal(mods_dir, mod_server):
    add_mod({**STYLE_UI}, permissions=["ui.style"], mod_id="paint")
    asyncio.run(style.refresh("paint"))
    assert style.listing()
    mod_server.body = b"@import url(https://evil.example/x.css); a{color:red}"
    st = asyncio.run(style.refresh("paint"))
    assert not st["active"] and "@import" in st["error"]
    assert style.listing() == [] and style.read("paint") is None


def test_refresh_refuses_a_sheet_over_the_cap(mods_dir, mod_server):
    add_mod({**STYLE_UI}, permissions=["ui.style"], mod_id="paint")
    mod_server.body = b"a{color:red}" + b" " * (style.MAX_BYTES + 10)
    st = asyncio.run(style.refresh("paint"))
    assert not st["active"] and "64 KB" in st["error"] and style.listing() == []
    mod_server.body = b"a{color:red}" + b" " * 100_000
    assert "64 KB" in asyncio.run(style.refresh("paint"))["error"]      # also while streaming


# ── the routes ──

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


ALICE = {"x-test-user": "alice"}
BOB = {"x-test-user": "bob"}


def test_routes_serve_the_checked_sheet_to_signed_in_users_only(client):
    give_style("paint", ":root{--bg:#012 !important}\n")
    assert client.get("/api/mods/ui/styles").status_code == 401
    assert client.get("/api/mods/ui/style/paint.css").status_code == 401
    for who in (ALICE, BOB):                                  # the look is for everyone
        r = client.get("/api/mods/ui/style/paint.css", headers=who)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
        assert r.headers["x-content-type-options"] == "nosniff" and "--bg:#012" in r.text
    got = client.get("/api/mods/ui/styles", headers=BOB).json()
    assert got["safe"] is False and got["styles"][0]["id"] == "paint" and got["styles"][0]["url"].startswith("/api/mods/ui/style/paint.css?v=")
    assert client.get("/api/mods/ui/style/nope.css", headers=ALICE).status_code == 404
    assert client.get("/api/mods/ui/style/..%2Fregistry.css", headers=ALICE).status_code == 404


def test_routes_in_safe_mode_and_when_disabled(client):
    give_style("paint")
    assert client.get("/api/mods/ui/styles?safe=1", headers=ALICE).json() == {"safe": True, "styles": []}
    client.cookies.set(style.SAFE_COOKIE, "off")
    assert client.get("/api/mods/ui/styles", headers=ALICE).json()["safe"] is True
    client.cookies.clear()
    registry.set_enabled("paint", False)
    assert client.get("/api/mods/ui/styles", headers=ALICE).json()["styles"] == []
    assert client.get("/api/mods/ui/style/paint.css", headers=ALICE).status_code == 404


def test_status_is_for_admins(client):
    give_style("paint")
    assert client.get("/api/mods/ui/style-status", headers=BOB).status_code == 403
    out = client.get("/api/mods/ui/style-status", headers=ALICE).json()
    assert out["mods"]["paint"]["active"] is True and out["max_bytes"] == 64 * 1024 and "--accent" in out["tokens"]


def test_mod_api_refresh_needs_the_permission(client, mod_server):
    _, tok = add_mod({**STYLE_UI}, permissions=["ui.style"], mod_id="paint")
    _, tok2 = add_mod(permissions=["notify"], mod_id="other")
    h = {"Authorization": f"Bearer {tok}"}
    assert client.post("/api/mods/v1/ui/style/refresh").status_code == 401
    assert client.post("/api/mods/v1/ui/style/refresh", headers={"Authorization": f"Bearer {tok2}"}).status_code == 403
    r = client.post("/api/mods/v1/ui/style/refresh", headers=h)
    assert r.status_code == 200 and r.json()["style"]["active"] is True
    assert [s["id"] for s in style.listing()] == ["paint"]


def test_turning_a_mod_off_and_removing_it_over_the_api(client, mod_server, monkeypatch):
    async def noop(*a, **k):
        return None
    monkeypatch.setattr(runtime, "stop", noop)
    monkeypatch.setattr(runtime, "remove_docker", noop)
    monkeypatch.setattr(routes, "_after_enable", noop)
    add_mod({**STYLE_UI}, permissions=["ui.style"], mod_id="paint", enabled=False)
    assert client.post("/api/mods/paint/enable", headers=ALICE).status_code == 200   # fetches the sheet on enabling
    assert [s["id"] for s in style.listing()] == ["paint"]
    assert client.post("/api/mods/paint/disable", headers=ALICE).status_code == 200
    assert style.listing() == []
    client.post("/api/mods/paint/enable", headers=ALICE)
    assert client.delete("/api/mods/paint?keep_data=true", headers=ALICE).status_code == 200
    assert style.listing() == [] and style.read("paint") is None
