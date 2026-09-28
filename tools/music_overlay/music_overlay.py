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

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "odysseus-music-overlay")
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


# ── system volume (pycaw) ─────────────────────────────────────────────────
def volume_endpoint():
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
        self.media = Media()
        self.q: "queue.Queue" = queue.Queue()
        self.last_key = None
        self.art_img = None
        self.source = ""                               # the playing app's media id
        root = self.root = tk.Tk()
        root.title("Odysseus music")
        root.overrideredirect(True)                    # no title bar, no X
        root.attributes("-topmost", self.settings.get("topmost", True))
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
        self._build()
        self._build_messages()
        root.after(50, self._round_corners)
        threading.Thread(target=self._poll, daemon=True).start()
        self.url = (self.settings.get("url") or ODYSSEUS_URL).rstrip("/") + "/"
        self.token = self.settings.get("token") or ""
        self.current = None                            # the message on show
        self.msg_open = False
        self.msg_above = True
        self.dismissed_plan = None
        if self.token:
            threading.Thread(target=self._poll_inbox, daemon=True).start()
        root.after(200, self._drain)

    def _font_ok(self, name):
        try:
            import tkinter.font as tkfont
            return name in tkfont.families(self.root)
        except Exception:
            return False

    def _round_corners(self):
        # Windows 11: ask DWM for rounded corners (harmless elsewhere).
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            pref = ctypes.c_int(2)                      # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

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
        # Relative to the window's top-left, which is known from the anchor.
        self._dx, self._dy = e.x_root - self.ax, e.y_root - (self.ay - self._msg_offset())

    def _drag(self, e):
        self.ax, self.ay = e.x_root - self._dx, e.y_root - self._dy + self._msg_offset()
        self.root.geometry(f"+{self.ax}+{self.ay - self._msg_offset()}")

    def _drag_end(self, e):
        # Saved as the player's own position, whether or not a message is open.
        self.settings.update(x=self.ax, y=self.ay)
        save_settings(self.settings)

    # right-click menu
    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        for pct in (100, 86, 70, 55):
            m.add_command(label=f"Opacity {pct}%", command=lambda p=pct: self._alpha(p / 100))
        top = tk.BooleanVar(value=bool(self.root.attributes("-topmost")))
        m.add_checkbutton(label="Always on top", variable=top, command=lambda: self._topmost(top.get()))
        label, _ = self._app()
        target = tk.StringVar(value=self.settings.get("vol_target", "pc"))
        m.add_radiobutton(label="Volume buttons: computer", value="pc", variable=target,
                          command=lambda: self._set("vol_target", "pc"))
        m.add_radiobutton(label=f"Volume buttons: {label or 'playing app'} only", value="app",
                          variable=target, command=lambda: self._set("vol_target", "app"))
        cwo = tk.BooleanVar(value=bool(self.settings.get("close_with_odysseus", True)))
        m.add_checkbutton(label="Close with Odysseus", variable=cwo,
                          command=lambda: self._set("close_with_odysseus", cwo.get()))
        m.add_separator()
        m.add_command(label="Open Odysseus", command=lambda: webbrowser.open(ODYSSEUS_URL))
        m.add_command(label="Close", command=self.root.destroy)
        m.tk_popup(e.x_root, e.y_root)

    def _set(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)

    def _alpha(self, a):
        self.root.attributes("-alpha", a)
        self.settings["alpha"] = a
        save_settings(self.settings)

    def _topmost(self, on):
        self.root.attributes("-topmost", on)
        self.settings["topmost"] = on
        save_settings(self.settings)

    # controls
    def _flash(self, text):
        self.badge.configure(text=text)
        self.root.after(1800, lambda: self.badge.configure(text=""))

    def _click(self, name):
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
        # Over the player row only, not the message box below it.
        if str(e.widget).startswith(str(self.msg)):
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
        while True:
            try:
                self.q.put(self.media.info())
            except Exception as e:
                log(f"media info failed: {e}")
                self.q.put(None)
            time.sleep(POLL_S)

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, dict) and item.get("_quit"):
                    self.root.destroy()
                    return
                if isinstance(item, dict) and "_plans" in item:
                    self._sync_plans(item["_plans"])
                elif isinstance(item, dict) and item.get("_message"):
                    self._show_message(item)
                elif isinstance(item, dict) and item.get("_status"):
                    self.msg_status.configure(text=item["_status"])
                    if item.get("collapse"):
                        self.root.after(1800, self._hide_message)
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
        r = self.root
        f = self.msg = tk.Frame(r, bg=BG)
        self.msg_head = tk.Label(f, bg=BG, fg=ACCENT, anchor="w", font=("Segoe UI Semibold", 9))
        self.msg_head.place(x=10, y=2, width=W - 60, height=18)
        close = tk.Label(f, bg=BG, fg=DIM, text="\u00d7", font=("Segoe UI", 12), cursor="hand2")
        close.place(x=W - 28, y=0, width=22, height=20)
        close.bind("<Button-1>", lambda e: self._hide_message(dismissed=True))
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

    def _show_message(self, ev):
        self.current = ev
        self.msg_head.configure(text=(({"question": "Question", "plan": "Plan waiting"}.get(ev.get("kind"), "Odysseus"))
                                      + (f" \u00b7 {ev['chat']}" if ev.get("chat") else "")))
        self.msg_body.configure(text=ev.get("body") or ev.get("heading") or "")
        for w in self.msg_opts.winfo_children():
            w.destroy()
        for label in (ev.get("options") or [])[:4]:
            b = tk.Label(self.msg_opts, bg="#34343c", fg=FG, text=label[:28], cursor="hand2",
                         font=("Segoe UI", 8), padx=6)
            b.pack(side="left", padx=(0, 6))
            if ev.get("kind") == "plan":
                b.bind("<Button-1>", lambda e, v=label.lower(): self._answer_plan(v))
            else:
                b.bind("<Button-1>", lambda e, t=label: self._send(t))
        self.msg_status.configure(text={"question": "Type a reply and press Enter",
                                        "plan": "Approve, Deny, or type the changes you want"}.get(
                                            ev.get("kind"), "Reply, or Open the chat"))
        self.entry.delete(0, "end")
        if not self.msg_open:
            # Open above the player (asked for: "above the music, not below
            # it"), unless the player sits at the very top of the screen.
            self.msg_above = self.ay >= MSG_H
            self.root.geometry(f"{W}x{H + MSG_H}+{self.ax}+{self.ay - MSG_H if self.msg_above else self.ay}")
            self.msg_open = True
        if self.msg_above:
            self.msg.place(x=0, y=0, width=W, height=MSG_H)
            self.player.place(x=0, y=MSG_H, width=W, height=H)
        else:
            self.player.place(x=0, y=0, width=W, height=H)
            self.msg.place(x=0, y=H, width=W, height=MSG_H)
        # Back on top, over a game that took focus, and a gentle chime.
        self.root.attributes("-topmost", False)
        self.root.attributes("-topmost", True)
        self.root.lift()
        try:
            import winsound
            winsound.MessageBeep(0x40)
        except Exception:
            pass
        log(f"message: {ev.get('kind')} {ev.get('heading')}")

    def _msg_offset(self):
        return MSG_H if self.msg_open and self.msg_above else 0

    def _hide_message(self, dismissed=False):
        if dismissed and self.current and self.current.get("kind") == "plan":
            self.dismissed_plan = self.current.get("plan_id")    # do not bring it back
        self.current = None
        if not self.msg_open:
            return
        self.msg.place_forget()
        self.player.place(x=0, y=0, width=W, height=H)
        self.msg_open = False
        self.root.geometry(f"{W}x{H}+{self.ax}+{self.ay}")

    # ── plans waiting on an answer ───────────────────────────────────────
    def _sync_plans(self, plans):
        """Show the newest pending plan while there is one; hide it after."""
        cur = self.current
        if plans:
            p = plans[0]
            if p["id"] == self.dismissed_plan:
                return
            if cur is None or (cur.get("kind") == "plan" and cur.get("plan_id") != p["id"]):
                first = " ".join(ln.strip("#*- ").strip() for ln in (p.get("plan") or "").splitlines()
                                 if ln.strip())[:220]
                self._show_message({"kind": "plan", "plan_id": p["id"], "session_id": p["session_id"],
                                    "chat": p.get("chat", ""), "heading": "Plan waiting",
                                    "body": f"Runs on {p.get('runs_on', '')}. {first}",
                                    "options": ["Approve", "Deny"]})
        elif cur and cur.get("kind") == "plan":
            self._hide_message()

    def _answer_plan(self, verb):
        ev = self.current
        if not ev:
            return
        self.msg_status.configure(text="Approving\u2026" if verb == "approve" else "Denying\u2026")

        def go():
            try:
                d = self._api("POST", f"api/overlay/plan/{ev['plan_id']}/{verb}")
                if verb == "approve":
                    msg = (f"Approved \u2713 run {d.get('run_id', '')} starting in the chat"
                           if d.get("resuming") else f"Approved \u2713 run {d.get('run_id', '')}")
                else:
                    msg = "Denied \u2713"
                self.q.put({"_status": msg, "collapse": True})
            except Exception as e:
                log(f"plan {verb} failed: {e}")
                self.q.put({"_status": f"Could not {verb}: {e}"[:80]})
        threading.Thread(target=go, daemon=True).start()

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
                            if d.get("queued_behind_reply") else "Sent \u2713", "collapse": True})
            except Exception as e:
                log(f"reply failed: {e}")
                self.q.put({"_status": f"Could not send: {e}"[:80]})
        threading.Thread(target=go, daemon=True).start()
        self.entry.delete(0, "end")

    def _show(self, info):
        if not info:
            self.title.configure(text="Nothing playing")
            self.artist.configure(text="")
            self.buttons["play"].configure(text=self.GLYPH["play"])
            return
        self.source = info.get("source", "")
        self.title.configure(text=info["title"] or "Unknown")
        self.artist.configure(text=info["artist"])
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
