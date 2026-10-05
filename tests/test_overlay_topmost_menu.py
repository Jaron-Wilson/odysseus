"""Tests for the music overlay topmost robustness and custom popup menu.

Verifies:
1. Safe parsing of Tkinter's -topmost attribute (preventing bool("0") == True).
2. Win32 SetWindowPos helper logic for HWND_TOPMOST / HWND_NOTOPMOST reassertion.
3. Custom OverlayMenu dispatch and structure replacing fragile native tk.Menu.
4. Periodic 500ms topmost reassertion on the player, alert, and plan reader windows.
5. "Put alerts back above the player" repositioning, lifting, and topmost reset.
"""
import ast
import os
import sys
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERLAY_PY = os.path.join(HERE, "tools", "music_overlay", "music_overlay.py")
SRC = open(OVERLAY_PY, encoding="utf-8").read()


def _load_helpers():
    """Extract helper functions and OverlayMenu class from music_overlay.py."""
    tree = ast.parse(SRC)
    targets = ("parse_topmost", "_get_user32", "_get_hwnd", "set_window_topmost", "OverlayMenu")
    code_parts = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in targets:
            code_parts.append(ast.get_source_segment(SRC, node))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ("HWND_TOPMOST", "HWND_NOTOPMOST", "SWP_NOSIZE", "SWP_NOMOVE", "SWP_NOACTIVATE", "IS_WINDOWS"):
                    code_parts.append(ast.get_source_segment(SRC, node))
    ns = {
        "sys": sys,
        "IS_WINDOWS": False,
        "ctypes": None,
        "log": lambda m: None,
        "HWND_TOPMOST": -1,
        "HWND_NOTOPMOST": -2,
        "SWP_NOSIZE": 0x0001,
        "SWP_NOMOVE": 0x0002,
        "SWP_NOACTIVATE": 0x0010,
    }
    exec("\n\n".join(code_parts), ns)
    return ns


def test_parse_topmost_safely_handles_strings_and_bools():
    ns = _load_helpers()
    parse_topmost = ns["parse_topmost"]

    # The bug: Tk can return "0" as a string, where standard bool("0") is True.
    assert bool("0") is True  # demonstrating python's default behavior
    assert parse_topmost("0") is False  # our helper correctly evaluates "0" as False
    assert parse_topmost("1") is True
    assert parse_topmost("false") is False
    assert parse_topmost("true") is True
    assert parse_topmost("no") is False
    assert parse_topmost("yes") is True
    assert parse_topmost("off") is False
    assert parse_topmost("on") is True
    assert parse_topmost("") is False

    # Standard types
    assert parse_topmost(0) is False
    assert parse_topmost(1) is True
    assert parse_topmost(False) is False
    assert parse_topmost(True) is True
    assert parse_topmost(None) is False


def test_set_window_topmost_uses_win32_setwindowpos(monkeypatch):
    ns = _load_helpers()
    set_window_topmost = ns["set_window_topmost"]

    calls = []

    class FakeUser32:
        def GetParent(self, hwnd):
            return hwnd

        def SetWindowPos(self, hwnd, after, x, y, cx, cy, flags):
            calls.append({"hwnd": hwnd, "after": after, "x": x, "y": y, "cx": cx, "cy": cy, "flags": flags})
            return 1

    fake_u32 = FakeUser32()
    monkeypatch.setitem(ns, "_get_user32", lambda: fake_u32)

    class FakeTkWin:
        def __init__(self, hwnd=12345):
            self._hwnd = hwnd
            self.attrs = {}

        def winfo_id(self):
            return self._hwnd

        def attributes(self, name, val=None):
            if val is not None:
                self.attrs[name] = val
            return self.attrs.get(name)

    win = FakeTkWin(5555)

    # Set topmost True
    assert set_window_topmost(win, True) is True
    assert win.attributes("-topmost") is True
    assert len(calls) == 1
    assert calls[0]["hwnd"] == 5555
    assert calls[0]["after"] == -1  # HWND_TOPMOST
    assert calls[0]["flags"] == (0x0002 | 0x0001 | 0x0010)  # SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE

    # Set topmost False
    assert set_window_topmost(win, False) is True
    assert win.attributes("-topmost") is False
    assert len(calls) == 2
    assert calls[1]["hwnd"] == 5555
    assert calls[1]["after"] == -2  # HWND_NOTOPMOST

    # Graceful handling of None window
    assert set_window_topmost(None, True) is False


def test_set_window_topmost_graceful_without_win32(monkeypatch):
    ns = _load_helpers()
    set_window_topmost = ns["set_window_topmost"]

    monkeypatch.setitem(ns, "_get_user32", lambda: None)

    class FakeTkWin:
        def __init__(self):
            self.attrs = {}

        def winfo_id(self):
            return 1111

        def attributes(self, name, val=None):
            if val is not None:
                self.attrs[name] = val
            return self.attrs.get(name)

    win = FakeTkWin()
    # On non-Windows platforms or when user32 is unavailable, returns False for Win32
    # but still sets Tk's -topmost attribute.
    assert set_window_topmost(win, True) is False
    assert win.attributes("-topmost") is True


def test_overlay_code_wiring_for_topmost_and_menu():
    assert "class OverlayMenu:" in SRC
    assert "def parse_topmost(" in SRC
    assert "def set_window_topmost(" in SRC
    assert "root.after(500, self._reassert_topmost)" in SRC
    assert "set_window_topmost(root, self.topmost_on)" in SRC
    assert "OverlayMenu(self.root" in SRC
    assert "self._active_menu = m" in SRC
    assert "Put alerts back above the player" in SRC
    assert "Always on top" in SRC


def test_alert_reset_and_lift_wiring():
    # Verify _reset_alert repositions, lifts, and reasserts topmost
    assert "def _reset_alert(self):" in SRC
    assert "self.settings.pop(\"alert_x\", None)" in SRC
    assert "self.settings.pop(\"alert_y\", None)" in SRC
    assert "self._place_alert()" in SRC
    assert "self.alert.lift()" in SRC
    assert "set_window_topmost(self.alert, True)" in SRC
    assert 'self._flash("Alerts reset")' in SRC


def test_drag_reasserts_topmost():
    # Verify dragging does not drop topmost Z-order
    assert 'if getattr(self, "topmost_on", True):\n            set_window_topmost(self.root, True)' in SRC
    assert 'if getattr(self, "topmost_on", True):\n            set_window_topmost(self.alert, True)' in SRC


def test_overlay_menu_dispatches_commands(monkeypatch):
    ns = _load_helpers()

    created_labels = []

    class FakeWidget:
        def __init__(self, *args, **kwargs):
            self.bindings = {}
            self.text = kwargs.get("text", "")
            self._destroyed = False

        def pack(self, *args, **kwargs):
            pass

        def bind(self, event, handler):
            self.bindings[event] = handler

        def configure(self, **kwargs):
            pass

        def destroy(self):
            self._destroyed = True

        def overrideredirect(self, flag):
            pass

        def attributes(self, *args):
            pass

        def winfo_reqwidth(self):
            return 200

        def winfo_reqheight(self):
            return 150

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

        def geometry(self, g):
            self._geom = g

        def deiconify(self):
            pass

        def focus_force(self):
            pass

        def update_idletasks(self):
            pass

    class FakeLabel(FakeWidget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_labels.append(self)

    class FakeTk:
        Toplevel = FakeWidget
        Frame = FakeWidget
        Label = FakeLabel

    monkeypatch.setitem(ns, "tk", FakeTk)
    OverlayMenu = ns["OverlayMenu"]

    closed = []
    parent = FakeWidget()
    menu = OverlayMenu(parent, 100, 200, on_close=lambda: closed.append(True))

    executed = []
    menu.add_command("Open Browser", lambda: executed.append("open"))
    menu.add_checkbutton("Always on top", True, lambda: executed.append("topmost"))
    menu.add_radiobutton("Computer volume", True, lambda: executed.append("volume"))
    menu.add_separator()
    menu.show()

    assert menu.top._geom.startswith("220x150+")

    # Verify labels were created with checkmark and radio symbols
    texts = [lbl.text for lbl in created_labels]
    assert any("Open Browser" in t for t in texts)
    assert any("✓" in t and "Always on top" in t for t in texts)
    assert any("●" in t and "Computer volume" in t for t in texts)

    # Click the "Open Browser" label and verify execution + auto-close
    open_lbl = next(lbl for lbl in created_labels if "Open Browser" in lbl.text)
    assert "<Button-1>" in open_lbl.bindings
    open_lbl.bindings["<Button-1>"](None)

    assert executed == ["open"]
    assert closed == [True]
    assert menu.top._destroyed is True

