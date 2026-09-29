"""The sidebar Devices panel: the Settings Devices tab and MCP servers in one place.

Asked for: "devices tab is a little hard to see and use can we use that on the
sidebar and put mcps there too? in settings is just tough to edit."
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def test_sidebar_entry_and_script_are_on_the_page():
    html = _read("static", "index.html")
    assert 'id="tool-devices-btn"' in html
    assert '<script type="module" src="/static/js/devicesPanel.js"></script>' in html
    # devicesSettings.js loads first: the panel borrows its element and handlers.
    assert html.index("js/devicesSettings.js") < html.index("js/devicesPanel.js")


def test_panel_borrows_the_settings_devices_tab_and_gives_it_back():
    js = _read("static", "js", "devicesPanel.js")
    assert '[data-settings-panel="devices"]' in js
    assert "insertBefore(el, _home.next)" in js           # put back where it was
    assert "window.devicesSettings?.load()" in js


def test_mcp_tab_uses_the_existing_server_routes():
    js = _read("static", "js", "devicesPanel.js")
    for route in ("/api/mcp/servers", "/reconnect", "/tools"):
        assert route in js
    routes = _read("routes", "mcp_routes.py")
    for r in ('@router.get("/servers")', '@router.post("/servers/{server_id}/reconnect")',
              '@router.patch("/servers/{server_id}")', '@router.delete("/servers/{server_id}")',
              '@router.get("/servers/{server_id}/tools")', '@router.patch("/servers/{server_id}/tools")'):
        assert r in routes


def test_new_text_has_no_em_dashes_or_british_spelling():
    js = _read("static", "js", "devicesPanel.js")
    assert "—" not in js
    assert not re.search(r"colour|organis", js)
