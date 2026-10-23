"""Settings > Calls & Meetings, after the phone features moved to a mod.

Texting the agent, phone calls (the free SIP line and Twilio), call_me and
Google Meet are the Phone & Messages mod (Jaron-Wilson/odysseus-phone) since
2026-10-22. What matters here:

  * the core has none of their code, routes, tools or startup any more;
  * the tab points at the mod ("phone" is a page that moved to a mod, opened
    through modLinks.openLegacy) and keeps the core's own "Text messages"
    card, the built-in way to send a text when no such mod is installed;
  * the tab is still for everyone, and loads only its own cards.
"""
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "static"
_INDEX = (_STATIC / "index.html").read_text()


def _panel(name):
    """The markup of one settings panel, up to the next one, without comments."""
    start = _INDEX.index(f'<div data-settings-panel="{name}"')
    nxt = _INDEX.find("<div data-settings-panel=", start + 10)
    return re.sub(r"<!--.*?-->", "", _INDEX[start:nxt], flags=re.S)


MOVED_IDS = ("phone-card", "sip-section", "meet-card", "sms-generate", "sms-disable", "sms-secret-state",
             "phone-enabled", "sip-enabled", "meet-enabled", "meet-google-connect")


def test_the_tab_points_at_the_mod_and_keeps_text_messages():
    calls = _panel("calls")
    for i in ("phone-mod-card", "phone-mod-open", "phone-mod-state", "sms-card", "sms-numbers",
              "sms-reply-url", "sms-save", "sms-test"):
        assert _INDEX.count(f'id="{i}"') == 1 and f'id="{i}"' in calls, i
    for i in MOVED_IDS:
        assert f'id="{i}"' not in _INDEX, i
    assert "<h2>Text messages</h2>" in calls and "<h2>Phone &amp; Messages</h2>" in calls


def test_the_moved_code_is_gone_from_the_core():
    for f in ("routes/meet_routes.py", "routes/telephony_routes.py", "routes/sip_routes.py",
              "src/telephony", "src/meet", "src/agent_tools/call_me_tool.py", "scripts/phone_call_simulator.py",
              "docs/phone-calls.md", "docs/sip-line.md", "docs/google-meet.md"):
        assert not (_ROOT / f).exists(), f
    app = (_ROOT / "app.py").read_text()
    for s in ("setup_telephony_routes", "setup_sip_routes", "setup_meet_routes", "sip_line", "/api/sms/inbound"):
        assert s not in app, s
    for f in ("src/tool_schemas.py", "src/tool_execution.py", "src/tool_index.py", "src/agent_tools/__init__.py",
              "src/agent_loop.py", "src/google_write_gate.py"):
        text = (_ROOT / f).read_text()
        assert '"google_meet"' not in text and '"call_me"' not in text, f
    # Making a Meet stays with the Google connection, for the mod to ask for.
    from src.google_workspace import meet as gmeet
    assert gmeet.RECONNECT == "Connect Google Calendar again in Settings > Google."
    assert gmeet.meet_url("abc-defg-hij") == "https://meet.google.com/abc-defg-hij"
    assert gmeet.meet_url("https://evil.example/?meet.google.com/abc-defg-hij") == ""


def test_phone_is_a_page_that_moved_to_a_mod():
    from src.mods import placement
    from src import tool_pages
    assert placement.LEGACY_PAGES["phone"] == "Phone & Messages"
    core_js = (_STATIC / "js" / "modLinksCore.js").read_text()
    assert "'phone': 'Phone & Messages'," in core_js
    moved = {p["key"]: p for p in tool_pages.MOVED}
    assert moved["phone"]["group"] == "organize"
    assert "google meet" in moved["phone"]["aliases"] and "sms" in moved["phone"]["aliases"]
    assert tool_pages.page("sip line")["key"] == "phone"
    shell = (_STATIC / "js" / "workspace" / "shell.js").read_text()
    assert re.search(r"^\s+phone: svg\(", shell, re.M), "the mod tab's icon"


def test_the_tab_is_in_the_nav_for_everyone():
    btn = re.search(r'<button[^>]*data-settings-tab="calls"[^>]*>.*?</button>', _INDEX, re.S).group(0)
    assert "<span>Calls &amp; Meetings</span>" in btn and "<svg" in btn
    assert "admin-only" not in btn
    assert _INDEX.index('data-settings-tab="devices"') < _INDEX.index('data-settings-tab="calls"') \
        < _INDEX.index('data-settings-tab="appearance"')
    js = (_STATIC / "js" / "settings.js").read_text()
    admin_tabs = re.search(r"const ADMIN_TABS = new Set\(\[([^\]]*)\]\)", js).group(1)
    assert "'calls'" not in admin_tabs and "'devices'" not in admin_tabs


def test_each_tab_loads_its_own_cards():
    js = (_STATIC / "js" / "settings.js").read_text()
    assert "if (tab === 'calls') window.devicesSettings?.loadCalls?.();" in js
    ds = (_STATIC / "js" / "devicesSettings.js").read_text()
    calls = ds[ds.index("function loadCalls() {"):ds.index("async function load(refresh = false) {")]
    assert "loadSms()" in calls and "loadPhoneMod()" in calls
    for gone in ("loadPhone()", "loadSip()", "loadMeet()", "/api/meet", "/api/telephony"):
        assert gone not in ds, gone
    init = ds[ds.index("function init() {"):]
    for h in ("onSmsClick", "onPhoneModClick"):
        assert f"calls.addEventListener('click', {h})" in init, h
    assert "openLegacy('phone')" in ds


def test_the_meet_command_opens_the_mod():
    js = (_STATIC / "js" / "slashCommands.js").read_text()
    body = js[js.index("async function _cmdMeet("):]
    body = body[:body.index("\n}\n")]
    assert "/api/meet" not in body
    assert "openLegacy('phone')" in body and "#mod/phone/phone?path=" in body


# ── In a browser: the real devicesSettings.js ────────────────────────────

def _settings_markup():
    start = _INDEX.index('<div id="settings-modal"')
    end = _INDEX.index("<!-- Search overlay")
    return _INDEX[start:end]


_PAGE = """<!doctype html><html><head><link rel="stylesheet" href="/static/style.css"></head><body>
__SETTINGS__
<script>
  const m = document.getElementById('settings-modal');
  m.classList.remove('hidden');
  m.querySelectorAll('[data-settings-tab]').forEach(btn => btn.addEventListener('click', () => {
    const tab = btn.dataset.settingsTab;
    m.querySelectorAll('[data-settings-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.settingsPanel !== tab));
    if (tab === 'devices') window.devicesSettings?.load();
    if (tab === 'calls') window.devicesSettings?.loadCalls?.();
  }));
</script>
<script type="module" src="/static/js/devicesSettings.js"></script>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                      # no browser binary here
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def test_opening_the_tab_asks_for_its_own_cards(browser):
    ctx = browser.new_context(viewport={"width": 1300, "height": 900})
    pg = ctx.new_page()
    asked, errors = [], []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    html = _PAGE.replace("__SETTINGS__", _settings_markup())

    def route(r):
        path = r.request.url.split("example.test", 1)[-1].split("?", 1)[0]
        if path in ("", "/"):
            r.fulfill(body=html, content_type="text/html")
        elif path.startswith("/static/"):
            f = _STATIC / path[len("/static/"):]
            if f.suffix not in (".js", ".css") or not f.exists():
                r.fulfill(status=404, body="")
                return
            r.fulfill(body=f.read_text(), content_type="text/css" if f.suffix == ".css" else "text/javascript")
        elif path == "/api/mods/ui":
            asked.append((r.request.method, path))
            r.fulfill(body=json.dumps({"mods": [], "replaces": {"phone": {"mod": "phone", "tab": "phone",
                                                                            "title": "Phone & Messages"}}}),
                      content_type="application/json")
        elif path.startswith("/api/"):
            asked.append((r.request.method, path))
            r.fulfill(body=json.dumps({}), content_type="application/json")
        else:
            r.fulfill(status=404, body="")
    ctx.route("**/*", route)
    try:
        pg.goto("https://example.test/")
        pg.wait_for_function("() => window.devicesSettings && window.devicesSettings.loadCalls")
        pg.click('[data-settings-tab="calls"]')
        pg.wait_for_timeout(500)
        paths = {p for _, p in asked}
        assert {"/api/sms/config", "/api/mods/ui"} <= paths
        assert not any(p.startswith(("/api/devices", "/api/telephony", "/api/meet")) for p in paths)
        assert pg.is_visible("#sms-card") and pg.is_visible("#phone-mod-card")
        assert "Installed" in pg.text_content("#phone-mod-state")
        asked.clear()
        pg.click("#sms-save")
        pg.wait_for_timeout(300)
        assert ("PUT", "/api/sms/config") in asked
        assert not errors, errors
    finally:
        ctx.close()
