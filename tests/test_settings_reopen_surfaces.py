"""Opening Settings while it is already open brings it to the front.

Reported 2026-10-02: with Settings open, a link that opens Settings again
(the calendar's "set up CalDAV sync") left it behind the calendar, in
Workspace as a background tab and in Classic and Studio under the calendar
window, so none of its buttons took a click. The calendar also un-hid the
modal by hand (window.settingsModule does not exist), which skips both
Settings' own opener and the Workspace tab shell. Measured with real clicks
against a preview server before and after.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_settings_open_surfaces_an_already_open_window():
    js = _read("static/js/settings.js")
    body = js.split("export function open(tab) {", 1)[1].split("\nexport function close()", 1)[0]
    assert "const wasOpen = !modalEl.classList.contains('hidden');" in body
    assert "if (wasOpen) _surface();" in body
    surface = js.split("function _surface() {", 1)[1].split("\n}\n", 1)[0]
    assert "ws-away" in surface and "openTool('settings')" in surface      # Workspace: its tab
    assert "bringToFront('settings-modal')" in surface                    # Classic / Studio: on top


def test_modal_manager_can_raise_an_open_window():
    js = _read("static/js/modalManager.js")
    assert "export function bringToFront(id)" in js
    assert "bringToFront };" in js


def test_the_calendar_opens_settings_through_its_opener():
    js = _read("static/js/calendar.js")
    assert "modal.classList.remove('hidden');\n      const tab = modal.querySelector('[data-settings-tab=\"integrations\"]')" not in js
    assert "window.settingsModule" not in js
    assert js.count("_openIntegrations();") >= 4
