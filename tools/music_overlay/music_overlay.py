"""Music overlay: a small frameless player that stays on top of everything.

The browser's pop-out (Document Picture-in-Picture) always has Chrome's own
title bar, with "back to tab" and a close button. This is the frameless
version, a tiny native window on the Windows desktop: no title bar, always
on top, semi-transparent, drag it anywhere, right-click for options. It sits
over games in borderless fullscreen (Factorio's default).

It talks to Windows directly for the music:
- the current media session (title, artist, the album art the player itself
  publishes, play/pause/skip on that player) through winsdk, and
- the system volume and mute, and the playing app's own level in the
  Windows mixer, through pycaw, as desktop_mcp_server.py does. The +/-
  buttons drive one or the other (right-click to choose); the mouse wheel
  moves the app, Shift+wheel the computer.

And to Odysseus for messages: what the AI said while you were elsewhere (a
reply ready, a question) shows here over the game, with a box to answer.
Windows holds its own notifications back during games (Focus Assist); this is
an ordinary window, so it is not held back. Needs "token" (an Odysseus API
token with the "overlay" scope) and "url" in settings.json.

Run it with pythonw (no console). Odysseus starts it through the
"MusicOverlay" scheduled task, which runs on the logged-in desktop; named so
the Odysseus* services auto-start does not start it on its own.

Needs: python -m pip install --user winsdk pillow pycaw comtypes
"""

import asyncio
import ctypes
import re
import urllib.request
import io
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import webbrowser

try:
    from PIL import Image, ImageTk
except ImportError:                                    # art is optional
    Image = ImageTk = None

IS_WINDOWS = sys.platform == "win32"
# Linux too (asked for: "on my linux device opened the website tried popup but
# it didn't work"): media players through MPRIS (gdbus), the volume through
# wpctl. Settings live in ~/.config there.
APP_DIR = (os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "odysseus-music-overlay")
           if IS_WINDOWS else
           os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "odysseus-music-overlay"))
SETTINGS = os.path.join(APP_DIR, "settings.json")
LOG = os.path.join(APP_DIR, "overlay.log")
ODYSSEUS_URL = os.environ.get("ODYSSEUS_URL", "https://jaron-dev-server.tail90b62a.ts.net/")
LOCK_PORT = 47831                                      # single instance
POLL_S = 1.5
INBOX_S = 3.0
# "Close with Odysseus": gone once no Odysseus page on this machine has
# talked to the server for this long (the server reports it with the inbox).
CLOSE_AFTER_S = 150
W, H = 400, 64
MSG_H = 150
BG, FG, DIM, ACCENT = "#1b1b20", "#f2f2f2", "#9a9aa3", "#e0b341"


def log(msg: str) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def load_settings() -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s: dict) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except OSError:
        pass


# ── Topmost & Win32 window helpers ──────────────────────────────────────────
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010


def parse_topmost(val) -> bool:
    """Parse Tkinter's -topmost attribute value safely.

    Tkinter on some platforms/versions returns '0' or '1' as strings.
    In Python, bool('0') is True, which causes false-positives if not checked.
    """
    if isinstance(val, str):
        val = val.strip().lower()
        if val in ("0", "false", "no", "off", ""):
            return False
        if val in ("1", "true", "yes", "on"):
            return True
    return bool(val)


def _get_user32():
    if IS_WINDOWS and hasattr(ctypes, "windll"):
        try:
            return ctypes.windll.user32
        except Exception:
            return None
    return None


def _get_hwnd(win):
    """Get the top-level Win32 HWND for a Tk window."""
    user32 = _get_user32()
    if user32 is None or win is None:
        return None
    try:
        wid = win.winfo_id() if hasattr(win, "winfo_id") else getattr(win, "_hwnd", None)
        if not wid:
            return None
        return user32.GetParent(wid) or wid
    except Exception:
        return None


def set_window_topmost(win, on: bool = True) -> bool:
    """Set or re-assert topmost for a Tk window using Win32 SetWindowPos on Windows,
    and Tk's -topmost attribute on all platforms."""
    if win is None:
        return False
    try:
        if hasattr(win, "attributes"):
            win.attributes("-topmost", bool(on))
    except Exception:
        pass
    user32 = _get_user32()
    if user32 is None:
        return False
    try:
        hwnd = _get_hwnd(win)
        if not hwnd:
            return False
        target = HWND_TOPMOST if on else HWND_NOTOPMOST
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        return bool(user32.SetWindowPos(hwnd, target, 0, 0, 0, 0, flags))
    except Exception:
        return False


class OverlayMenu:
    """A frameless, topmost Tk popup menu designed for borderless windows.

    Native tk.Menu (Win32 TrackPopupMenu) drops command dispatch on Windows
    when attached to borderless (overrideredirect) windows that lack standard
    WS_CAPTION window activation. This Tk-native popup guarantees that every
    click fires directly within Tk's event system, stays topmost, and matches
    the overlay's dark palette.
    """

    MENU_BG = "#1f1f24"
    MENU_BORDER = "#33333b"
    ITEM_HOVER = "#2d2d36"
    ITEM_FG = "#f2f2f2"
    ITEM_DIM = "#9a9aa3"
    ITEM_ACCENT = "#e0b341"

    def __init__(self, parent, x: int, y: int, on_close=None):
        self.parent = parent
        self.on_close = on_close
        self.top = tk.Toplevel(parent)
        self.top.overrideredirect(True)
        set_window_topmost(self.top, True)
        self.top.configure(bg=self.MENU_BORDER)

        self.inner = tk.Frame(self.top, bg=self.MENU_BG, padx=2, pady=4)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)

        self._x = x
        self._y = y
        self._closed = False

        self.top.bind("<Escape>", lambda e: self.close())
        self.top.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, event=None):
        try:
            self.top.after(150, self._check_focus_and_close)
        except Exception:
            pass

    def _check_focus_and_close(self):
        if self._closed:
            return
        try:
            focus = self.top.focus_get()
            if focus is None or focus.winfo_toplevel() != self.top:
                self.close()
        except Exception:
            self.close()

    def add_command(self, label: str, command):
        self._add_row(label, command, prefix="   ")

    def add_checkbutton(self, label: str, checked: bool, command):
        check_symbol = "✓  " if checked else "   "
        self._add_row(label, command, prefix=check_symbol, active_color=self.ITEM_ACCENT if checked else None)

    def add_radiobutton(self, label: str, selected: bool, command):
        radio_symbol = "●  " if selected else "○  "
        self._add_row(label, command, prefix=radio_symbol, active_color=self.ITEM_ACCENT if selected else None)

    def add_separator(self):
        sep = tk.Frame(self.inner, bg=self.MENU_BORDER, height=1)
        sep.pack(fill="x", padx=6, pady=4)

    def _add_row(self, label: str, command, prefix: str = "", active_color=None):
        row = tk.Frame(self.inner, bg=self.MENU_BG, cursor="hand2")
        row.pack(fill="x", padx=2, pady=1)

        fg = active_color or self.ITEM_FG
        lbl = tk.Label(row, bg=self.MENU_BG, fg=fg, anchor="w",
                       font=("Segoe UI", 9), text=f"{prefix}{label}")
        lbl.pack(fill="x", padx=8, pady=3)

        def on_enter(e):
            row.configure(bg=self.ITEM_HOVER)
            lbl.configure(bg=self.ITEM_HOVER)

        def on_leave(e):
            row.configure(bg=self.MENU_BG)
            lbl.configure(bg=self.MENU_BG)

        def on_click(e):
            self.close()
            if command:
                try:
                    command()
                except Exception as ex:
                    log(f"menu command '{label}' error: {ex}")

        for w in (row, lbl):
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            w.bind("<Button-1>", on_click)

    def show(self):
        self.top.update_idletasks()
        w = max(220, self.top.winfo_reqwidth())
        h = self.top.winfo_reqheight()

        try:
            sw = self.top.winfo_screenwidth()
            sh = self.top.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080

        x = max(8, min(self._x, sw - w - 8))
        y = max(8, min(self._y, sh - h - 8))

        self.top.geometry(f"{w}x{h}+{x}+{y}")
        self.top.deiconify()
        set_window_topmost(self.top, True)
        try:
            self.top.focus_force()
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.top.destroy()
        except Exception:
            pass
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass


# ── Windows media session (winsdk), on its own asyncio thread ─────────────
class Media:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=6):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _session(self):
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager)
        mgr = await Manager.request_async()
        return mgr.get_current_session()

    async def _info(self):
        s = await self._session()
        if s is None:
            return None
        props = await s.try_get_media_properties_async()
        status = s.get_playback_info().playback_status
        art = b""
        if props.thumbnail is not None:
            try:
                from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions
                stream = await props.thumbnail.open_read_async()
                size = int(stream.size)
                if 0 < size < 8_000_000:
                    buf = Buffer(size)
                    await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
                    data = bytearray(buf.length)
                    DataReader.from_buffer(buf).read_bytes(data)
                    art = bytes(data)
            except Exception as e:                     # art is a nicety
                log(f"art read failed: {e}")
        return {"title": props.title or "", "artist": props.artist or "",
                "playing": int(status) == 4, "art": art,
                "source": s.source_app_user_model_id or ""}

    def info(self):
        return self.run(self._info())

    async def _do(self, what):
        s = await self._session()
        if s is None:
            return False
        op = {"play_pause": s.try_toggle_play_pause_async,
              "next": s.try_skip_next_async,
              "previous": s.try_skip_previous_async}[what]
        return bool(await op())

    def do(self, what):
        return self.run(self._do(what))


# ── Linux: media players over MPRIS (D-Bus, through gdbus) ───────────────
class MprisMedia:
    """The same info() / do() as Media, for Linux. Uses the gdbus tool, so
    nothing needs installing: every desktop with a session bus has it."""

    _BUS = ["gdbus", "call", "--session"]
    _PATH = "/org/mpris/MediaPlayer2"

    def __init__(self):
        self._art_cache = {}
        self._name = ""

    def _call(self, *args, timeout=3):
        import subprocess
        r = subprocess.run(self._BUS + list(args), capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""

    def _players(self):
        out = self._call("--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
                         "--method", "org.freedesktop.DBus.ListNames")
        return [n for n in re.findall(r"org\.mpris\.MediaPlayer2\.[\w.\-]+", out) if not n.endswith("playerctld")]

    def _prop(self, name, prop):
        return self._call("--dest", name, "--object-path", self._PATH, "--method",
                          "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player", prop)

    @staticmethod
    def _str(meta, key):
        m = re.search(r"'" + re.escape(key) + r"': <\[?(['\"])(.*?)(?<!\\)\1", meta)
        return m.group(2).replace("\\'", "'").replace('\\"', '"') if m else ""

    def _art(self, url):
        if not url:
            return b""
        if url in self._art_cache:
            return self._art_cache[url]
        data = b""
        try:
            if url.startswith("file://"):
                import urllib.parse
                with open(urllib.parse.unquote(url[7:]), "rb") as f:
                    data = f.read(8_000_000)
            elif url.startswith(("http://", "https://")):
                with urllib.request.urlopen(url, timeout=5) as r:
                    data = r.read(8_000_000)
        except Exception as e:
            log(f"art read failed: {e}")
        self._art_cache = {url: data}                    # one song's art at a time
        return data

    def info(self):
        best = None
        for name in self._players():
            status = self._prop(name, "PlaybackStatus")
            meta = self._prop(name, "Metadata")
            title = self._str(meta, "xesam:title")
            if not title:
                continue
            item = (name, "'Playing'" in status, title, meta)
            if item[1]:
                best = item
                break
            best = best or item
        if not best:
            self._name = ""
            return None
        name, playing, title, meta = best
        self._name = name
        source = name.split(".")[3] if name.count(".") >= 3 else name
        return {"title": title, "artist": self._str(meta, "xesam:artist"), "playing": playing,
                "art": self._art(self._str(meta, "mpris:artUrl")), "source": source}

    def do(self, what):
        name = self._name or next(iter(self._players()), "")
        method = {"play_pause": "PlayPause", "next": "Next", "previous": "Previous"}[what]
        return bool(name) and self._call("--dest", name, "--object-path", self._PATH, "--method",
                                         f"org.mpris.MediaPlayer2.Player.{method}") != ""


class _WpctlVolume:
    """The four calls the overlay makes on the Windows volume endpoint, for
    Linux (PipeWire's wpctl on the default output)."""
    SINK = "@DEFAULT_AUDIO_SINK@"

    def _run(self, *args):
        import subprocess
        return subprocess.run(["wpctl", *args], capture_output=True, text=True, timeout=3).stdout

    def GetMasterVolumeLevelScalar(self):
        m = re.search(r"Volume:\s*([0-9.]+)", self._run("get-volume", self.SINK))
        return float(m.group(1)) if m else 0.0

    def SetMasterVolumeLevelScalar(self, v, _ctx=None):
        self._run("set-volume", "-l", "1.0", self.SINK, f"{max(0.0, min(1.0, v)):.2f}")

    def GetMute(self):
        return "[MUTED]" in self._run("get-volume", self.SINK)

    def SetMute(self, m, _ctx=None):
        self._run("set-mute", self.SINK, "1" if m else "0")


# ── system volume (pycaw) ─────────────────────────────────────────────────
def volume_endpoint():
    if not IS_WINDOWS:
        return _WpctlVolume()
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    speakers = AudioUtilities.GetSpeakers()
    endpoint = getattr(speakers, "EndpointVolume", None)
    if endpoint is not None:
        return endpoint
    raw = getattr(speakers, "_dev", speakers)
    iface = raw.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def app_sessions(source):
    """(label, [ISimpleAudioVolume]) for the app behind the media session
    `source` (its AppUserModelId: "Chrome", "Spotify.exe", ...)."""
    from pycaw.pycaw import AudioUtilities
    want = _norm(source)
    if "308046b0af4a39cb" in want:                   # Firefox's id
        want = "firefox"
    found, active = {}, {}
    for s in AudioUtilities.GetAllSessions():
        proc, vol = getattr(s, "Process", None), getattr(s, "SimpleAudioVolume", None)
        if proc is None or vol is None:
            continue
        try:
            name = proc.name()
        except Exception:
            continue
        stem = _norm(name.rsplit(".", 1)[0])
        if want and stem and (stem in want or want in stem):
            found.setdefault(name, []).append(vol)
        if getattr(s, "State", 0) == 1:
            active.setdefault(name, []).append(vol)
    pick = found or (active if len(active) == 1 else {})
    if not pick:
        return None, []
    name, vols = next(iter(pick.items()))
    stem = name.rsplit(".", 1)[0]
    label = {"chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
             "spotify": "Spotify"}.get(stem.lower(), stem)
    return label, vols


# ── bringing up an existing browser tab (UI Automation) ───────────────────
def _bring_front(hwnd):
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                   # SW_RESTORE
    # Windows only lets the foreground process move focus; a tap of Alt
    # (the documented workaround) lifts that for this call.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)


def focus_marked_tab(marker):
    """Select the Chrome/Edge tab whose title contains `marker` (set by the
    Odysseus page when asked) and bring its window forward. True if found."""
    import comtypes.client
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as U
    uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
    windows = uia.GetRootElement().FindAll(
        U.TreeScope_Children, uia.CreatePropertyCondition(U.UIA_ClassNamePropertyId, "Chrome_WidgetWin_1"))
    # The active tab's title is the window's title: no tab search needed.
    for i in range(windows.Length):
        w = windows.GetElement(i)
        if marker in (w.CurrentName or ""):
            _bring_front(w.CurrentNativeWindowHandle)
            return True
    tab_item = uia.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_TabItemControlTypeId)
    for i in range(windows.Length):
        w = windows.GetElement(i)
        tabs = w.FindAll(U.TreeScope_Descendants, tab_item)
        for j in range(tabs.Length):
            t = tabs.GetElement(j)
            if marker not in (t.CurrentName or ""):
                continue
            _bring_front(w.CurrentNativeWindowHandle)
            try:
                t.GetCurrentPattern(U.UIA_SelectionItemPatternId).QueryInterface(
                    U.IUIAutomationSelectionItemPattern).Select()
            except Exception:
                t.GetCurrentPattern(U.UIA_LegacyIAccessiblePatternId).QueryInterface(
                    U.IUIAutomationLegacyIAccessiblePattern).DoDefaultAction()
            return True
    return False


# ── the window ────────────────────────────────────────────────────────────
class Overlay:
    ICON_FONT = ("Segoe Fluent Icons", 12)
    GLYPH = {"previous": "", "play": "", "pause": "", "next": "",
             "vol_down": "", "vol_up": "", "vol": "", "mute": "",
             "note": ""}

    def __init__(self):
        self.settings = load_settings()
        self.media = Media() if IS_WINDOWS else MprisMedia()
        self.q: "queue.Queue" = queue.Queue()
        self.last_key = None
        self.art_img = None
        self.source = ""                               # the playing app's media id
        self.remote = None                             # a phone's server_id while its song is shown
        root = self.root = tk.Tk()
        root.title("Odysseus music")
        root.overrideredirect(True)                    # no title bar, no X
        self.topmost_on = parse_topmost(self.settings.get("topmost", True))
        root.attributes("-topmost", self.topmost_on)
        set_window_topmost(root, self.topmost_on)
        root.attributes("-alpha", float(self.settings.get("alpha", 0.86)))
        root.configure(bg=BG)
        sw = root.winfo_screenwidth()
        x = self.settings.get("x", sw - W - 24)
        y = self.settings.get("y", 64)
        # Where the player sits. Only a drag changes it: messages open above
        # it by growing the window upward, and the window is always placed
        # from this anchor, never from a read-back position. Seen live: with
        # display scaling the read-back was off, and the player crept a
        # little with every ask, question and completion.
        self.ax, self.ay = int(x), int(y)
        root.geometry(f"{W}x{H}+{self.ax}+{self.ay}")
        if not self._font_ok("Segoe Fluent Icons"):
            self.ICON_FONT = ("Segoe MDL2 Assets", 12)
        self._active_menu = None
        self._build()
        self._build_messages()
        root.after(50, self._round_corners)
        root.after(500, self._reassert_topmost)
        threading.Thread(target=self._poll, daemon=True).start()
        self.url = (self.settings.get("url") or ODYSSEUS_URL).rstrip("/") + "/"
        self.token = self.settings.get("token") or ""
        self.current = None                            # the message on show
        self.msg_open = False
        self.items = []                                # the message queue
        self.idx = 0
        self._seq = 0
        self.plans = {}
        self.dismissed_plans = set()
        self.reader = None
        if self.token:
            threading.Thread(target=self._poll_inbox, daemon=True).start()
        root.after(200, self._drain)

    def _font_ok(self, name):
        try:
            import tkinter.font as tkfont
            return name in tkfont.families(self.root)
        except Exception:
            return False

    def _round_corners(self, win=None):
        # Windows 11: ask DWM for rounded corners (harmless elsewhere).
        try:
            w = win or self.root
            hwnd = _get_hwnd(w)
            if hwnd:
                pref = ctypes.c_int(2)                      # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    def _reassert_topmost(self):
        try:
            if getattr(self, "topmost_on", True):
                set_window_topmost(self.root, True)
                if getattr(self, "msg_open", False) and getattr(self, "alert", None):
                    set_window_topmost(self.alert, True)
                if getattr(self, "reader", None) and getattr(self.reader, "top", None):
                    set_window_topmost(self.reader.top, True)
        except Exception:
            pass
        try:
            self.root.after(500, self._reassert_topmost)
        except Exception:
            pass

    def _close_menu_if_open(self):
        if getattr(self, "_active_menu", None):
            self._active_menu.close()
            self._active_menu = None

    def _build(self):
        # The player row lives in its own frame, so a message can open above
        # it (the window grows upward) without moving the music.
        r = self.player = tk.Frame(self.root, bg=BG)
        r.place(x=0, y=0, width=W, height=H)
        self.art = tk.Label(r, bg=BG, fg=DIM, text=self.GLYPH["note"], font=("Segoe Fluent Icons", 18),
                            width=3)
        self.art.place(x=6, y=6, width=52, height=52)
        self.title = tk.Label(r, bg=BG, fg=FG, anchor="w", font=("Segoe UI Semibold", 10), text="Nothing playing")
        self.title.place(x=66, y=9, width=150, height=22)
        self.artist = tk.Label(r, bg=BG, fg=DIM, anchor="w", font=("Segoe UI", 9), text="")
        self.artist.place(x=66, y=31, width=150, height=20)
        xs = 220
        self.buttons = {}
        for name in ("previous", "play", "next", "vol_down", "vol_up", "vol"):
            b = tk.Label(r, bg=BG, fg=FG, text=self.GLYPH[name], font=self.ICON_FONT, cursor="hand2")
            b.place(x=xs, y=18, width=28, height=28)
            b.bind("<Button-1>", lambda e, n=name: self._click(n))
            b.bind("<Enter>", lambda e, w=b: w.configure(fg=ACCENT))
            b.bind("<Leave>", lambda e, w=b: w.configure(fg=FG))
            self.buttons[name] = b
            xs += 29
        self.badge = tk.Label(r, bg=BG, fg=ACCENT, font=("Segoe UI", 8), text="")
        self.badge.place(x=W - 106, y=2, width=100, height=14)
        for w in (self.root, r, self.art, self.title, self.artist):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Button-3>", self._menu)
        for b in self.buttons.values():
            b.bind("<Button-3>", self._menu)
        r.bind_all("<MouseWheel>", self._wheel)

    # dragging
    def _drag_start(self, e):
        self._close_menu_if_open()
        # Relative to the window's top-left, which is known from the anchor.
        self._dx, self._dy = e.x_root - self.ax, e.y_root - self.ay

    def _drag(self, e):
        self.ax, self.ay = e.x_root - self._dx, e.y_root - self._dy
        self.root.geometry(f"+{self.ax}+{self.ay}")
        if self.current is not None and "alert_x" not in self.settings:
            self._place_alert()                        # it follows until given its own spot

    def _drag_end(self, e):
        # Saved as the player's own position, whether or not a message is open.
        self.settings.update(x=self.ax, y=self.ay)
        save_settings(self.settings)
        if getattr(self, "topmost_on", True):
            set_window_topmost(self.root, True)

    # right-click menu
    def _menu(self, e):
        self._close_menu_if_open()

        m = OverlayMenu(self.root, e.x_root, e.y_root, on_close=lambda: setattr(self, "_active_menu", None))
        self._active_menu = m

        current_alpha = float(self.settings.get("alpha", 0.86))
        for pct in (100, 86, 70, 55):
            val = pct / 100
            is_cur = abs(current_alpha - val) < 0.03
            m.add_radiobutton(f"Opacity {pct}%", is_cur, lambda p=pct: self._alpha(p / 100))

        m.add_separator()
        top_on = parse_topmost(self.root.attributes("-topmost"))
        m.add_checkbutton("Always on top", top_on, lambda: self._topmost(not top_on))

        label, _ = self._app()
        target = self.settings.get("vol_target", "pc")
        m.add_radiobutton("Volume buttons: computer", target == "pc",
                          lambda: self._set("vol_target", "pc"))
        m.add_radiobutton(f"Volume buttons: {label or 'playing app'} only", target == "app",
                          lambda: self._set("vol_target", "app"))

        cwo = bool(self.settings.get("close_with_odysseus", True))
        m.add_checkbutton("Close with Odysseus", cwo,
                          lambda: self._set("close_with_odysseus", not cwo))

        m.add_command("Put alerts back above the player", self._reset_alert)
        m.add_separator()
        m.add_command("Open Odysseus", lambda: webbrowser.open(ODYSSEUS_URL))
        m.add_command("Close", self.root.destroy)
        m.show()

    def _set(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)

    def _alpha(self, a):
        self.root.attributes("-alpha", a)
        self.alert.attributes("-alpha", a)
        self.settings["alpha"] = a
        save_settings(self.settings)

    def _topmost(self, on):
        self.topmost_on = bool(on)
        self.settings["topmost"] = self.topmost_on
        save_settings(self.settings)
        set_window_topmost(self.root, self.topmost_on)
        if getattr(self, "alert", None):
            set_window_topmost(self.alert, self.topmost_on)
        if getattr(self, "reader", None) and getattr(self.reader, "top", None):
            set_window_topmost(self.reader.top, self.topmost_on)
        self._flash("Always on top: on" if self.topmost_on else "Always on top: off")

    # controls
    def _flash(self, text):
        self.badge.configure(text=text)
        self.root.after(1800, lambda: self.badge.configure(text=""))

    def _click(self, name):
        if getattr(self, "remote", None) and name in ("previous", "next", "play"):
            # The phone's song: its own player does it.
            action = "play_pause" if name == "play" else name
            sid = self.remote

            def go():
                try:
                    self._api("POST", "api/overlay/remote_media/control", {"server_id": sid, "action": action})
                except Exception as e:
                    log(f"phone control failed: {e}")
            threading.Thread(target=go, daemon=True).start()
            self._flash("\U0001F4F1 " + {"play": "play/pause", "next": "next", "previous": "previous"}[name])
            return
        try:
            if name in ("previous", "next"):
                self.media.do(name)
            elif name == "play":
                self.media.do("play_pause")
            elif name in ("vol_up", "vol_down") and self.settings.get("vol_target") == "app":
                self._step_app(10 if name == "vol_up" else -10)
            elif name in ("vol_up", "vol_down"):
                v = volume_endpoint()
                cur = round(v.GetMasterVolumeLevelScalar() * 100)
                new = max(0, min(100, cur + (10 if name == "vol_up" else -10)))
                v.SetMasterVolumeLevelScalar(new / 100.0, None)
                if new > 0 and v.GetMute():
                    v.SetMute(0, None)
                self._flash(f"PC {new}%")
            elif name == "vol":
                v = volume_endpoint()
                muted = not bool(v.GetMute())
                v.SetMute(1 if muted else 0, None)
                self.buttons["vol"].configure(text=self.GLYPH["mute" if muted else "vol"])
                self._flash("muted" if muted else "")
        except Exception as e:
            log(f"{name} failed: {e}")
            self._flash("error")
        self.last_key = None                          # refresh right away

    def _app(self):
        try:
            return app_sessions(self.source)
        except Exception as e:
            log(f"app volume: {e}")
            return None, []

    def _step_app(self, delta):
        label, vols = self._app()
        if not vols:
            self._flash("no app")
            return
        cur = round(vols[0].GetMasterVolume() * 100)
        new = max(0, min(100, cur + delta))
        for v in vols:
            v.SetMasterVolume(new / 100.0, None)
            if new > 0 and v.GetMute():
                v.SetMute(0, None)
        self._flash(f"{label} {new}%")

    def _step_pc(self, delta):
        v = volume_endpoint()
        new = max(0, min(100, round(v.GetMasterVolumeLevelScalar() * 100) + delta))
        v.SetMasterVolumeLevelScalar(new / 100.0, None)
        if new > 0 and v.GetMute():
            v.SetMute(0, None)
        self._flash(f"PC {new}%")

    def _wheel(self, e):
        # Over the player row only: not the message box, not the plan reader.
        try:
            if e.widget.winfo_toplevel() is not self.root:
                return
        except Exception:
            return
        step = 5 if e.delta > 0 else -5
        try:
            if e.state & 0x0001:                     # Shift: the whole computer
                self._step_pc(step)
            else:
                self._step_app(step)
        except Exception as ex:
            log(f"wheel failed: {ex}")

    # polling (a thread; the window is updated on the Tk thread)
    def _poll(self):
        remote, remote_at = None, 0.0
        while True:
            try:
                info = self.media.info()
            except Exception as e:
                log(f"media info failed: {e}")
                info = None
            # Nothing playing on this PC: what the phone is playing, through
            # Odysseus. Asked for: "popup says nothing playing when it's from
            # my phone". The one that is actually playing wins.
            if not (info and info.get("playing")) and self.token and self.url:
                if time.time() - remote_at > POLL_S * 3:
                    remote_at = time.time()
                    try:
                        remote = self._api("GET", "api/overlay/remote_media").get("item")
                    except Exception as e:
                        log(f"remote media failed: {e}")
                        remote = None
                if remote and (remote.get("playing") or not (info and info.get("title"))):
                    import base64
                    try:
                        art = base64.b64decode(remote.get("art_jpeg_b64") or "")
                    except Exception:
                        art = b""
                    info = {"title": remote.get("title") or "", "artist": remote.get("artist") or "",
                            "playing": bool(remote.get("playing")), "art": art, "source": "",
                            "remote": remote.get("server_id"), "remote_name": remote.get("name") or "phone"}
            else:
                remote = None
            self.q.put(info)
            time.sleep(POLL_S)

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, dict) and item.get("_quit"):
                    self.root.destroy()
                    return
                if isinstance(item, dict) and item.get("_call"):
                    item["_call"]()
                elif isinstance(item, dict) and "_plans" in item:
                    self._sync_plans(item["_plans"])
                elif isinstance(item, dict) and item.get("_message"):
                    self._enqueue(item)
                elif isinstance(item, dict) and item.get("_status"):
                    if item.get("key") in (None, (self.current or {}).get("_key")):
                        self.msg_status.configure(text=item["_status"])
                    if item.get("done"):
                        # Answered: after a moment, on to the next message.
                        self.root.after(1500, lambda k=item["done"]: self._remove(k))
                else:
                    self._show(item)
        except queue.Empty:
            pass
        self.root.after(250, self._drain)

    # ── messages from Odysseus ────────────────────────────────────────────
    def _api(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path.lstrip("/"), data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode() or "{}")

    def _api_bytes(self, path):
        req = urllib.request.Request(self.url + path.lstrip("/"),
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def _poll_inbox(self):
        since = None
        while True:
            try:
                d = self._api("GET", f"api/overlay/inbox?since={since or 0}")
                seen = d.get("page_seen_ago")
                if (self.settings.get("close_with_odysseus", True) and seen is not None
                        and seen > CLOSE_AFTER_S):
                    log(f"no Odysseus page open here for {seen:.0f}s: closing")
                    self.q.put({"_quit": True})
                self.q.put({"_plans": d.get("plans") or []})
                if since is None:
                    since = d.get("now", time.time())   # only what arrives from now on
                else:
                    for ev in d.get("events", []):
                        since = max(since, ev.get("ts", since))
                        if ev.get("kind") != "plan":    # plans come from "plans" above
                            self.q.put(dict(ev, _message=True))
            except Exception as e:
                log(f"inbox poll failed: {e}")
            time.sleep(INBOX_S)

    def _build_messages(self):
        # Alerts live in their own window, not stacked onto the player's.
        # Asked for: "make the music and the alerts 2 separate ones so it's
        # not pushing one down farther than I want". It opens above the
        # player until dragged somewhere else, and keeps that spot.
        a = self.alert = tk.Toplevel(self.root)
        a.overrideredirect(True)
        a.attributes("-topmost", self.topmost_on)
        set_window_topmost(a, self.topmost_on)
        a.attributes("-alpha", float(self.settings.get("alpha", 0.86)))
        a.configure(bg=BG)
        a.withdraw()
        f = self.msg = tk.Frame(a, bg=BG)
        f.place(x=0, y=0, width=W, height=MSG_H)
        self.msg_head = tk.Label(f, bg=BG, fg=ACCENT, anchor="w", font=("Segoe UI Semibold", 9))
        self.msg_head.place(x=10, y=2, width=W - 110, height=18)
        close = tk.Label(f, bg=BG, fg=DIM, text="\u00d7", font=("Segoe UI", 12), cursor="hand2")
        close.place(x=W - 28, y=0, width=22, height=20)
        close.bind("<Button-1>", lambda e: self._close_current())
        self.msg_nav = tk.Label(f, bg=BG, fg=DIM, text="", font=("Segoe UI", 9), cursor="hand2")
        self.msg_nav.place(x=W - 92, y=1, width=60, height=18)
        # Left half of "‹ 1/2 ›" goes back, the right half forward.
        self.msg_nav.bind("<Button-1>", lambda e: self._step_msg(-1 if e.x < 30 else 1))
        self.msg_body = tk.Label(f, bg=BG, fg=FG, anchor="nw", justify="left", wraplength=W - 20,
                                 font=("Segoe UI", 9))
        self.msg_body.place(x=10, y=20, width=W - 20, height=52)
        self.msg_opts = tk.Frame(f, bg=BG)
        self.msg_opts.place(x=10, y=72, width=W - 20, height=24)
        self.entry = tk.Entry(f, bg="#2a2a31", fg=FG, insertbackground=FG, relief="flat",
                              font=("Segoe UI", 9))
        self.entry.place(x=10, y=100, width=W - 130, height=24)
        self.entry.bind("<Return>", lambda e: self._send(self.entry.get()))
        self.entry.bind("<Button-1>", lambda e: self.entry.focus_force())
        send = tk.Label(f, bg="#34343c", fg=FG, text="Send", cursor="hand2", font=("Segoe UI", 9))
        send.place(x=W - 114, y=100, width=48, height=24)
        send.bind("<Button-1>", lambda e: self._send(self.entry.get()))
        opn = tk.Label(f, bg="#34343c", fg=FG, text="Open", cursor="hand2", font=("Segoe UI", 9))
        opn.place(x=W - 60, y=100, width=50, height=24)
        opn.bind("<Button-1>", lambda e: self.current and self._open_chat(self.current["session_id"]))
        self.msg_status = tk.Label(f, bg=BG, fg=DIM, anchor="w", font=("Segoe UI", 8))
        self.msg_status.place(x=10, y=127, width=W - 20, height=16)
        for w in (f, self.msg_head, self.msg_body, self.msg_status):
            w.bind("<ButtonPress-1>", self._alert_drag_start)
            w.bind("<B1-Motion>", self._alert_drag)
            w.bind("<ButtonRelease-1>", self._alert_drag_end)
            w.bind("<Button-3>", self._menu)

    # the alert window's own position
    def _alert_pos(self):
        if "alert_x" in self.settings:
            return int(self.settings["alert_x"]), int(self.settings["alert_y"])
        if self.ay >= MSG_H + 6:
            return self.ax, self.ay - MSG_H - 6        # just above the player
        return self.ax, self.ay + H + 6                # no room above: below it

    def _place_alert(self):
        x, y = self._alert_pos()
        self.alert.geometry(f"{W}x{MSG_H}+{x}+{y}")

    def _alert_drag_start(self, e):
        self._close_menu_if_open()
        x, y = self._alert_pos()
        self._adx, self._ady = e.x_root - x, e.y_root - y

    def _alert_drag(self, e):
        self._alert_xy = (e.x_root - self._adx, e.y_root - self._ady)
        self.alert.geometry(f"+{self._alert_xy[0]}+{self._alert_xy[1]}")

    def _alert_drag_end(self, e):
        if getattr(self, "_alert_xy", None):
            self.settings.update(alert_x=self._alert_xy[0], alert_y=self._alert_xy[1])
            save_settings(self.settings)
            self._alert_xy = None
        if getattr(self, "topmost_on", True):
            set_window_topmost(self.alert, True)

    def _reset_alert(self):
        self.settings.pop("alert_x", None)
        self.settings.pop("alert_y", None)
        save_settings(self.settings)
        self._place_alert()
        if getattr(self, "alert", None):
            self.alert.lift()
            set_window_topmost(self.alert, True)
            if self.current is not None or getattr(self, "msg_open", False):
                if not self.msg_open:
                    self.alert.deiconify()
                    self.msg_open = True
                self.root.after(50, lambda: self._round_corners(self.alert))
        self._flash("Alerts reset")

    # ── the message queue ─────────────────────────────────────────────────
    # Everything waiting to be read, oldest first. Seen live: two replies
    # finishing together, the second replaced the first before it was read.
    # Now the head says "1/2", the arrows flip between them, and closing or
    # answering one shows the next.
    def _enqueue(self, ev):
        self._seq += 1
        ev = dict(ev)
        ev.setdefault("_key", f"m{self._seq}")
        if any(it["_key"] == ev["_key"] for it in self.items):
            return
        self.items.append(ev)
        if self.current is None:
            self.idx = len(self.items) - 1
            self._render()
        else:
            self._render_head()
        self._attention()
        log(f"message: {ev.get('kind')} {ev.get('heading')}")

    def _remove(self, key):
        keep = [it for it in self.items if it["_key"] != key]
        if len(keep) == len(self.items):
            return
        cur_key = (self.current or {}).get("_key")
        self.items = keep
        if not self.items:
            self._collapse()
            return
        if cur_key == key:
            self.idx = min(self.idx, len(self.items) - 1)
            self._render()
        else:
            self.idx = next((n for n, it in enumerate(self.items) if it["_key"] == cur_key), 0)
            self._render_head()

    def _step_msg(self, delta):
        if len(self.items) > 1:
            self.idx = (self.idx + delta) % len(self.items)
            self._render()

    def _close_current(self):
        ev = self.current
        if not ev:
            return
        if ev.get("kind") == "plan":
            self.dismissed_plans.add(ev.get("plan_id"))     # do not bring it back
        self._remove(ev["_key"])

    def _render_head(self):
        ev = self.current
        if not ev:
            return
        label = {"question": "Question", "plan": "Plan waiting"}.get(ev.get("kind"), "Odysseus")
        self.msg_head.configure(text=label + (f" \u00b7 {ev['chat']}" if ev.get("chat") else ""))
        n = len(self.items)
        self.msg_nav.configure(text=f"\u2039 {self.idx + 1}/{n} \u203a" if n > 1 else "")

    def _render(self):
        ev = self.current = self.items[self.idx]
        self._render_head()
        self.msg_body.configure(text=ev.get("body") or ev.get("heading") or "")
        for w in self.msg_opts.winfo_children():
            w.destroy()

        def button(text, cmd):
            b = tk.Label(self.msg_opts, bg="#34343c", fg=FG, text=text[:28], cursor="hand2",
                         font=("Segoe UI", 8), padx=6)
            b.pack(side="left", padx=(0, 6))
            b.bind("<Button-1>", lambda e: cmd())

        if ev.get("kind") == "plan":
            pid = ev["plan_id"]
            button("Approve", lambda: self._answer_plan(pid, "approve"))
            button("Deny", lambda: self._answer_plan(pid, "deny"))
            plans = [it["plan_id"] for it in self.items if it.get("kind") == "plan"]
            button(f"Read {len(plans)} plans" if len(plans) > 1 else "Read plan",
                   lambda: self._open_reader(plans if len(plans) > 1 else [pid]))
        else:
            for label in (ev.get("options") or [])[:4]:
                button(label, lambda t=label: self._send(t))
        self.msg_status.configure(text={"question": "Type a reply and press Enter",
                                        "plan": "Approve, Deny, Read it here, or type changes"}.get(
                                            ev.get("kind"), "Reply, or Open the chat"))
        self.entry.delete(0, "end")
        if not self.msg_open:
            self._place_alert()
            self.alert.deiconify()
            self.msg_open = True
            self.root.after(50, lambda: self._round_corners(self.alert))

    def _attention(self):
        # Back on top, over a game that took focus, and a gentle chime.
        for w in (self.root, self.alert):
            set_window_topmost(w, True)
            w.lift()
        try:
            if IS_WINDOWS:
                import winsound
                winsound.MessageBeep(0x40)
            else:
                self.root.bell()
        except Exception:
            pass

    def _collapse(self):
        self.current = None
        self.items = []
        self.idx = 0
        if not self.msg_open:
            return
        self.alert.withdraw()
        self.msg_open = False

    # ── plans waiting on an answer ───────────────────────────────────────
    def _sync_plans(self, plans):
        """Every pending plan is in the queue while it is pending, and leaves
        it once answered (here, in the chat, or anywhere else)."""
        self.plans = {p["id"]: p for p in plans}
        for key in [it["_key"] for it in self.items
                    if it.get("kind") == "plan" and it["plan_id"] not in self.plans]:
            self._remove(key)
        for p in reversed(plans):                          # oldest first into the queue
            if p["id"] in self.dismissed_plans:
                continue
            first = " ".join(ln.strip("#*- ").strip() for ln in (p.get("plan") or "").splitlines()
                             if ln.strip())[:220]
            self._enqueue({"kind": "plan", "_key": "plan:" + p["id"], "plan_id": p["id"],
                           "session_id": p.get("session_id", ""), "chat": p.get("chat", ""),
                           "heading": "Plan waiting",
                           "body": f"Runs on {p.get('runs_on', '')}. {first}"})
        if self.reader is not None:
            self.reader.sync(set(self.plans))

    def _answer_plan(self, plan_id, verb, on_done=None):
        key = "plan:" + plan_id
        if (self.current or {}).get("_key") == key:
            self.msg_status.configure(text="Approving\u2026" if verb == "approve" else "Denying\u2026")

        def go():
            try:
                d = self._api("POST", f"api/overlay/plan/{plan_id}/{verb}")
                if verb == "approve":
                    msg = (f"Approved \u2713 run {d.get('run_id', '')} starting in the chat"
                           if d.get("resuming") else f"Approved \u2713 run {d.get('run_id', '')}")
                else:
                    msg = "Denied \u2713"
                self.q.put({"_status": msg, "key": key, "done": key})
            except Exception as e:
                log(f"plan {verb} failed: {e}")
                msg = f"Could not {verb}: {e}"[:80]
                self.q.put({"_status": msg, "key": key})
            if on_done:
                self.q.put({"_call": lambda: on_done(msg)})
        threading.Thread(target=go, daemon=True).start()

    def _open_reader(self, plan_ids):
        if self.reader is not None:
            self.reader.close()
        self.reader = PlanReader(self, plan_ids)

    def _open_chat(self, sid):
        """Bring up the Odysseus tab already open on this machine, switched
        to the chat; open a new tab only when there is none."""
        if not sid:
            return
        self.msg_status.configure(text="Opening\u2026")

        def go():
            try:
                d = self._api("POST", "api/overlay/open", {"session_id": sid})
                acks = []
                for _ in range(16):                  # the pages poll every 1.5 s
                    time.sleep(0.25)
                    acks = self._api("GET", f"api/overlay/open/{d['nonce']}").get("acks") or []
                    if acks:
                        break
                if acks:
                    for _ in range(12):              # the marked title can take a moment
                        if focus_marked_tab(d["marker"]):
                            log("open: brought up the existing tab")
                            self.q.put({"_status": "Opened \u2713"})
                            return
                        time.sleep(0.25)
                    log("open: a tab answered but could not be found; opening a new one")
                else:
                    log("open: no Odysseus tab on this machine; opening a new one")
            except Exception as e:
                log(f"open failed: {e}")
            webbrowser.open(self.url + "#" + sid)
            self.q.put({"_status": "Opened in a new tab"})
        threading.Thread(target=go, daemon=True).start()

    def _send(self, text):
        text = (text or "").strip()
        ev = self.current
        if not text or not ev:
            return
        self.msg_status.configure(text="Sending\u2026")

        def go():
            try:
                d = self._api("POST", "api/overlay/reply", {"session_id": ev["session_id"], "text": text})
                self.q.put({"_status": "Sent \u2713 (queued behind the current reply)"
                            if d.get("queued_behind_reply") else "Sent \u2713",
                            "key": ev["_key"], "done": ev["_key"]})
            except Exception as e:
                log(f"reply failed: {e}")
                self.q.put({"_status": f"Could not send: {e}"[:80]})
        threading.Thread(target=go, daemon=True).start()
        self.entry.delete(0, "end")

    def _show(self, info):
        if not info:
            self.remote = None
            self.title.configure(text="Nothing playing")
            self.artist.configure(text="")
            self.buttons["play"].configure(text=self.GLYPH["play"])
            return
        self.source = info.get("source", "")
        self.remote = info.get("remote")               # a phone's song (see _poll)
        self.title.configure(text=info["title"] or "Unknown")
        self.artist.configure(text=(f"{info['artist']} \u00b7 \U0001F4F1 {info['remote_name']}"
                                    if self.remote else info["artist"]))
        self.buttons["play"].configure(text=self.GLYPH["pause" if info["playing"] else "play"])
        key = (info["title"], info["artist"], len(info["art"]))
        if key != self.last_key:
            self.last_key = key
            log(f"now playing: {info['title']} - {info['artist']}")
            if info["art"] and Image is not None:
                try:
                    img = Image.open(io.BytesIO(info["art"])).convert("RGB")
                    w, h = img.size
                    side = min(w, h)                  # YouTube art is often 16:9: crop square
                    img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
                    self.art_img = ImageTk.PhotoImage(img.resize((52, 52)))
                    self.art.configure(image=self.art_img, text="")
                except Exception as e:
                    log(f"art decode failed: {e}")
            else:
                self.art.configure(image="", text=self.GLYPH["note"])
        try:
            muted = bool(volume_endpoint().GetMute())
            self.buttons["vol"].configure(text=self.GLYPH["mute" if muted else "vol"])
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


class PlanReader:
    """Read plans over a game, and answer them there.

    A frameless window above the player with the plan's PDF pages, scrolled
    with the wheel, and Approve / Deny / PDF under each. Several plans sit
    side by side (asked for: "if it's 2 plans, open side by side")."""
    COL_W = 600
    PAD = 10
    HEAD_H = 30

    def __init__(self, ov, plan_ids):
        self.ov = ov
        self.ids = list(plan_ids)[:3]
        self.q: "queue.Queue" = queue.Queue()
        self.images = {}                              # plan id -> PhotoImages (kept alive)
        self.cols = {}
        n = len(self.ids)
        t = self.top = tk.Toplevel(ov.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        set_window_topmost(t, True)
        t.after(50, lambda: self.ov._round_corners(t))
        t.configure(bg=BG)
        w = n * self.COL_W + (n + 1) * self.PAD
        h = min(int(t.winfo_screenheight() * 0.78), 1100)
        # Right edge on the player's right edge, bottom just above the overlay
        # (never over it): shorter if need be, below it only if there is no
        # room above at all.
        x = max(0, ov.ax + W - w)
        # Above whichever of the two windows is higher on screen.
        top = ov.ay
        if ov.msg_open:
            top = min(top, ov._alert_pos()[1])
        room_above = top - 16
        if room_above >= 420:
            h = min(h, room_above)
            y = room_above - h + 8
        else:
            y = ov.ay + H + 8
            h = min(h, max(300, t.winfo_screenheight() - y - 8))
        t.geometry(f"{w}x{h}+{x}+{y}")
        self.rx, self.ry = x, y
        head = tk.Frame(t, bg=BG)
        head.place(x=0, y=0, width=w, height=self.HEAD_H)
        title = tk.Label(head, bg=BG, fg=ACCENT, anchor="w", font=("Segoe UI Semibold", 10),
                         text="Plan waiting" if n == 1 else f"{n} plans waiting")
        title.place(x=self.PAD, y=4, width=w - 60, height=22)
        close = tk.Label(head, bg=BG, fg=DIM, text="\u00d7", font=("Segoe UI", 13), cursor="hand2")
        close.place(x=w - 32, y=2, width=26, height=24)
        close.bind("<Button-1>", lambda e: self.close())
        for wd in (head, title):
            wd.bind("<ButtonPress-1>", self._drag_start)
            wd.bind("<B1-Motion>", self._drag)
            wd.bind("<ButtonRelease-1>", self._drag_end)
        body_h = h - self.HEAD_H - self.PAD
        for i, pid in enumerate(self.ids):
            self._column(pid, self.PAD + i * (self.COL_W + self.PAD), self.HEAD_H, body_h)
            threading.Thread(target=self._load, args=(pid,), daemon=True).start()
        t.after(100, self._pump)

    # layout
    def _column(self, pid, x, y, h):
        f = tk.Frame(self.top, bg="#232329")
        f.place(x=x, y=y, width=self.COL_W, height=h)
        head = tk.Label(f, bg="#232329", fg=FG, anchor="w", justify="left", font=("Segoe UI", 9),
                        text="Loading\u2026", wraplength=self.COL_W - 16)
        head.place(x=8, y=4, width=self.COL_W - 16, height=36)
        canvas = tk.Canvas(f, bg="#2b2b31", highlightthickness=0)
        canvas.place(x=8, y=44, width=self.COL_W - 16, height=h - 44 - 44)
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 40), "units"))
        canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
        canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
        bar = tk.Frame(f, bg="#232329")
        bar.place(x=8, y=h - 38, width=self.COL_W - 16, height=32)
        status = tk.Label(bar, bg="#232329", fg=DIM, anchor="e", font=("Segoe UI", 8), text="")
        buttons = []
        for label, cmd in (("Approve", lambda: self._answer(pid, "approve")),
                           ("Deny", lambda: self._answer(pid, "deny")),
                           ("PDF in browser", lambda: self._pdf(pid))):
            b = tk.Label(bar, bg="#34343c", fg=FG, text=label, cursor="hand2", font=("Segoe UI", 9), padx=10)
            b.pack(side="left", padx=(0, 8), ipady=4)
            b.bind("<Button-1>", lambda e, c=cmd: c())
            buttons.append(b)
        status.pack(side="right")
        self.cols[pid] = {"head": head, "canvas": canvas, "status": status, "buttons": buttons,
                          "doc": None, "answered": False}

    # data (threads) -> Tk thread through self.q
    def _load(self, pid):
        try:
            doc = self.ov._api("GET", f"api/overlay/plan/{pid}")
            self.q.put(("doc", pid, doc))
            for n in range(1, min(int(doc.get("pages") or 0), 30) + 1):
                data = self.ov._api_bytes(f"api/overlay/plan/{pid}/page/{n}.png?w={self.COL_W - 32}")
                self.q.put(("page", pid, data))
        except Exception as e:
            log(f"reader: could not load plan {pid}: {e}")
            self.q.put(("error", pid, str(e)))

    def _pump(self):
        try:
            while True:
                kind, pid, data = self.q.get_nowait()
                col = self.cols.get(pid)
                if not col:
                    continue
                if kind == "doc":
                    col["doc"] = data
                    col["head"].configure(text=(f"{data.get('chat') or 'Plan'}\n"
                                                f"Runs on {data.get('runs_on', '')} \u00b7 {data.get('cwd', '')}"))
                    if not data.get("pages"):
                        self._text(col, data.get("plan") or "(empty plan)")
                elif kind == "page":
                    self._add_page(pid, col, data)
                elif kind == "error":
                    col["head"].configure(text=f"Could not load this plan: {data}"[:120])
        except queue.Empty:
            pass
        except tk.TclError:
            return                                    # closed
        self.top.after(100, self._pump)

    def _add_page(self, pid, col, data):
        c = col["canvas"]
        if Image is None:
            self._text(col, (col["doc"] or {}).get("plan") or "")
            return
        img = ImageTk.PhotoImage(Image.open(io.BytesIO(data)))
        self.images.setdefault(pid, []).append(img)
        y = c.bbox("all")[3] + 8 if c.bbox("all") else 8
        c.create_image((self.COL_W - 16) // 2, y, image=img, anchor="n")
        c.configure(scrollregion=c.bbox("all"))

    def _text(self, col, text):
        c = col["canvas"]
        c.create_text(12, 10, text=text, anchor="nw", fill=FG, font=("Segoe UI", 10),
                      width=self.COL_W - 44)
        c.configure(scrollregion=c.bbox("all"))

    # actions
    def _answer(self, pid, verb):
        col = self.cols[pid]
        if col["answered"]:
            return
        col["status"].configure(text="Approving\u2026" if verb == "approve" else "Denying\u2026")
        self.ov._answer_plan(pid, verb, on_done=lambda msg, p=pid: self._answered(p, msg))

    def _answered(self, pid, msg):
        col = self.cols.get(pid)
        if not col:
            return
        col["status"].configure(text=msg)
        if msg.startswith(("Approved", "Denied")):
            col["answered"] = True
            for b in col["buttons"][:2]:
                b.configure(fg=DIM, cursor="arrow")
            if all(c["answered"] for c in self.cols.values()):
                self.top.after(1500, self.close)

    def _pdf(self, pid):
        doc = (self.cols.get(pid) or {}).get("doc") or {}
        if doc.get("pdf_url"):
            webbrowser.open(self.ov.url + doc["pdf_url"].lstrip("/"))
        elif doc.get("session_id"):
            self.ov._open_chat(doc["session_id"])

    def sync(self, pending):
        """A plan answered elsewhere (the chat, a phone) shows as such here."""
        for pid, col in self.cols.items():
            if not col["answered"] and col["doc"] is not None and pid not in pending:
                self._answered(pid, "Answered elsewhere")
                col["answered"] = True
        if self.cols and all(c["answered"] for c in self.cols.values()):
            self.top.after(1500, self.close)

    def close(self):
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        if self.ov.reader is self:
            self.ov.reader = None

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.rx, e.y_root - self.ry

    def _drag(self, e):
        self.rx, self.ry = e.x_root - self._dx, e.y_root - self._dy
        self.top.geometry(f"+{self.rx}+{self.ry}")

    def _drag_end(self, e):
        set_window_topmost(self.top, True)


def main():
    # One at a time: a second start just exits.
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log("already running")
        return
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    log("started")
    try:
        Overlay().run()
    except Exception as e:
        log(f"crashed: {e!r}")
        raise
    finally:
        log("closed")
        lock.close()


if __name__ == "__main__":
    main()
