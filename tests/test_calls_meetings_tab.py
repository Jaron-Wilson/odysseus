"""Settings > Calls & Meetings: the phone and meeting cards, out of Devices.

Asked for 2026-10-02: "why is all these settings in devices tab it shouldnt
be". Phone SMS, Phone calls (with the free SIP line) and Google Meet are ways
to reach the agent, not devices, so they moved to their own tab. What matters:

  * the four cards are on the new tab, with the same element ids, and the
    Devices tab keeps only the device cards;
  * the tab is for everyone, like Devices (the routes are per user);
  * opening the tab loads those cards, and opening Devices no longer does;
  * every pointer in the app and the docs names the new tab.
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


CALL_IDS = ("sms-card", "phone-card", "sip-section", "meet-card",
            "sms-save", "phone-enabled", "sip-enabled", "meet-enabled", "meet-google-connect")


def test_the_cards_moved_to_their_own_tab():
    calls, devices = _panel("calls"), _panel("devices")
    for i in CALL_IDS:
        assert _INDEX.count(f'id="{i}"') == 1, i
        assert f'id="{i}"' in calls and f'id="{i}"' not in devices, i
    for h in ("Phone SMS", "Phone calls", "Free SIP line (tailnet)", "Google Meet"):
        assert h in calls and h not in devices, h
    # The device cards stay.
    for i in ("devices-machines", "devices-others", "devices-subs", "devices-add-btn", "devices-add-commands"):
        assert f'id="{i}"' in devices and f'id="{i}"' not in calls, i


def test_the_tab_is_in_the_nav_for_everyone():
    btn = re.search(r'<button[^>]*data-settings-tab="calls"[^>]*>.*?</button>', _INDEX, re.S).group(0)
    assert "<span>Calls &amp; Meetings</span>" in btn and "<svg" in btn
    assert "admin-only" not in btn
    # Right after Devices, in the same group.
    assert _INDEX.index('data-settings-tab="devices"') < _INDEX.index('data-settings-tab="calls"') \
        < _INDEX.index('data-settings-tab="appearance"')
    js = (_STATIC / "js" / "settings.js").read_text()
    admin_tabs = re.search(r"const ADMIN_TABS = new Set\(\[([^\]]*)\]\)", js).group(1)
    assert "'calls'" not in admin_tabs and "'devices'" not in admin_tabs


def test_each_tab_loads_its_own_cards():
    js = (_STATIC / "js" / "settings.js").read_text()
    assert "if (tab === 'calls') window.devicesSettings?.loadCalls?.();" in js
    assert "if (tab === 'devices') window.devicesSettings?.load();" in js
    ds = (_STATIC / "js" / "devicesSettings.js").read_text()
    load = ds[ds.index("async function load(refresh = false) {"):ds.index("// ── Adding a device")]
    for fn in ("loadSms()", "loadPhone()", "loadSip()", "loadMeet()"):
        assert fn not in load, fn
    calls = ds[ds.index("function loadCalls() {"):ds.index("async function load(refresh = false) {")]
    for fn in ("loadSms()", "loadPhone()", "loadSip()", "loadMeet()"):
        assert fn in calls, fn
    assert "window.devicesSettings = { load, loadCalls };" in ds
    # The cards' handlers listen on the panel the cards are in.
    init = ds[ds.index("function init() {"):]
    assert "[data-settings-panel=\"calls\"]" in init
    for h in ("onSmsClick", "onPhoneClick", "onSipClick", "onMeetClick", "onCallsCopyClick"):
        assert f"calls.addEventListener('click', {h})" in init, h


_POINTER_FILES = ("routes/meet_routes.py", "routes/sms_routes.py", "routes/telephony_routes.py",
                  "routes/sip_routes.py", "src/meet/tool.py", "src/meet/google_calendar.py",
                  "src/meet/dialin.py", "src/agent_tools/call_me_tool.py", "src/tool_schemas.py",
                  "src/telephony/sip_line.py", "docs/phone-calls.md", "docs/sip-line.md",
                  "docs/sms-gateway.md", "docs/google-meet.md")


def test_pointers_name_the_new_tab():
    for f in _POINTER_FILES:
        text = (_ROOT / f).read_text()
        # Google Voice's own "Settings > Devices and numbers" is not ours.
        assert not re.search(r"Settings > Devices(?! and numbers)", text), f
        assert "Settings, Devices" not in text, f
        assert "—" not in text or f == "src/tool_schemas.py", f
    from routes import meet_routes
    from src.meet import google_calendar
    assert "Settings > Calls & Meetings" in meet_routes.TURN_ON
    assert google_calendar.RECONNECT == "Connect Google Calendar again in Settings > Calls & Meetings > Google Meet."


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
  // Stand-in for settings.js's tab switching, calling the same loaders.
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


def test_opening_each_tab_asks_for_its_own_cards(browser):
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
        assert {"/api/sms/config", "/api/telephony/config", "/api/telephony/sip", "/api/meet/config"} <= paths
        assert not any(p.startswith("/api/devices") for p in paths)
        assert pg.is_visible("#sms-card") and pg.is_visible("#meet-card")

        asked.clear()
        pg.click('[data-settings-tab="devices"]')
        pg.wait_for_timeout(500)
        paths = {p for _, p in asked}
        assert "/api/devices" in paths
        assert not paths & {"/api/sms/config", "/api/telephony/config", "/api/telephony/sip", "/api/meet/config"}
        assert not pg.is_visible("#sms-card")

        # The cards' buttons still work from their new panel.
        pg.click('[data-settings-tab="calls"]')
        pg.wait_for_timeout(300)
        asked.clear()
        pg.click("#sms-save")
        pg.wait_for_timeout(300)
        assert ("PUT", "/api/sms/config") in asked
        assert not errors, errors
    finally:
        ctx.close()
