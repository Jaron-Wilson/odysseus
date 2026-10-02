"""The cloud browser: one browser on this server that the agent drives and
the user can watch live and take over.

Asked for on 2026-09-29: "can we add a cloud browser like chatgpt does and
manus ai?" The agent already had a browser (the built-in Playwright MCP), but
it was headless and private to the MCP: nobody could see what it was doing,
or step in to log in or get past a CAPTCHA.

- This module starts a browser itself, with remote debugging on loopback
  only and a profile under DATA_DIR/cloud_browser, so logins stay between
  runs and restarts (the browser outlives a server restart and is picked up
  again).
- A real, visible Google Chrome on a virtual display (Xvfb) is preferred
  over Playwright's bundled, headless Chromium: Google refuses to sign in
  ("This browser or app may not be secure") or accept cookies imported from
  another browser into a `--headless=new` Chromium, but it is fine with an
  ordinary Chrome window, even one nobody's sitting in front of (confirmed
  2026-10-02: the sign-in form renders normally, and navigator.webdriver
  reads false with no CDP override needed). See _find_browser_executable()
  and _ensure_xvfb() below, and ODYSSEUS_CLOUD_BROWSER_HEADFUL to turn this
  off on a server with no real Chrome or Xvfb installed.
- The Browser MCP connects to it (--cdp-endpoint, src/builtin_mcp.py), so
  the agent's browser_* tools act on this very browser.
- The viewer attaches over CDP too (Playwright for Python): it streams the
  active tab as JPEG frames (Page.startScreencast) to whoever is watching.
  When the headful real-Chrome path is up, a take-over's clicks/scrolling/
  typing are replayed as real X11 input (xdotool against the Xvfb display,
  see x11_display_for_input()) instead of through CDP: Google blocks sign-in
  specifically on Input.dispatchKeyEvent/dispatchMouseEvent traffic, even in
  a real, visible Chrome (confirmed 2026-10-02: a screencast-only CDP
  session with X11-driven input takes an email with no "may not be secure"
  warning; the same input replayed through Playwright's page.mouse/keyboard
  does trigger it). The headless fallback has no real X11 display to inject
  into and falls back to the old CDP input path (routes/cloud_browser_routes.py,
  static/js/cloudBrowser.js). This and the Meet bot (src/meet/browser.py)
  still connect over CDP the same way whichever
  browser is actually running underneath.
"""
import asyncio
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("ODYSSEUS_CLOUD_BROWSER_PORT", "9333"))
ENDPOINT = f"http://127.0.0.1:{PORT}"
WIDTH, HEIGHT = 1280, 800
IDLE_STOP_S = 20            # stop the screencast this long after the last viewer leaves

# Real browsers to look for on PATH, in order, then these common install
# paths. google-chrome-stable first: it is the one Google treats as a real
# user's browser, so sign-in and cookie import work.
_REAL_BROWSER_NAMES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
_REAL_BROWSER_PATHS = ("/opt/google/chrome/chrome", "/usr/bin/google-chrome-stable",
                       "/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium")


def enabled() -> bool:
    return os.environ.get("ODYSSEUS_CLOUD_BROWSER", "1").lower() not in ("0", "false", "no")


def headful_enabled() -> bool:
    """Whether to prefer a real, visible Chrome on Xvfb over Playwright's
    bundled, headless Chromium. Off (e.g. a server with no Xvfb and no real
    Chrome) falls back to the old headless path."""
    return os.environ.get("ODYSSEUS_CLOUD_BROWSER_HEADFUL", "1").lower() not in ("0", "false", "no")


def _profile_dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "cloud_browser", "profile")


def _find_browser_executable() -> str:
    """A real browser installed on this machine: $ODYSSEUS_BROWSER_EXECUTABLE
    first, then google-chrome-stable/google-chrome/chromium/chromium-browser
    on PATH, then a few common install paths. "" if none is found, which
    falls back to Playwright's own Chromium (chromium_path(), below)."""
    env = os.environ.get("ODYSSEUS_BROWSER_EXECUTABLE", "")
    if env:
        return env
    for name in _REAL_BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in _REAL_BROWSER_PATHS:
        if os.path.exists(path):
            return path
    return ""


def chromium_path() -> str:
    """Playwright's own Chromium (the newest one installed), or
    ODYSSEUS_CLOUD_BROWSER_CHROME. The fallback when no real browser is
    found, or ODYSSEUS_CLOUD_BROWSER_HEADFUL=0."""
    own = os.environ.get("ODYSSEUS_CLOUD_BROWSER_CHROME", "")
    if own:
        return own
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
    found = sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome")),
                   key=lambda p: int((p.split("chromium-")[1].split(os.sep)[0] or "0")))
    return found[-1] if found else ""


def _xvfb_display() -> str:
    return os.environ.get("ODYSSEUS_CLOUD_BROWSER_DISPLAY", ":99")


def _xvfb_socket(display: str) -> str:
    # ":99" -> /tmp/.X11-unix/X99 (also ":99.0", which Xvfb accepts too).
    num = display.split(":", 1)[-1].split(".", 1)[0]
    return f"/tmp/.X11-unix/X{num}"


def _xvfb_running(display: str) -> bool:
    return os.path.exists(_xvfb_socket(display))


# Google refuses to sign in over CDP-driven input ("This browser or app may
# not be secure") even in a real, visible Chrome: it is specifically the
# Input.dispatchKeyEvent/dispatchMouseEvent traffic that trips it, not the
# CDP connection itself (confirmed 2026-10-02: a screencast-only CDP session
# with zero Input.* calls, driven instead by real X11 events via xdotool,
# reaches the sign-in form and takes an email with no warning at all, same
# as a human sitting at the real display; the same input over Playwright's
# page.mouse/page.keyboard does not). So when a real Chrome is up on Xvfb,
# the viewer replays clicks/keys/typing through xdotool against that X
# display instead of through CDP, and only falls back to CDP input in the
# headless path (no real X11 display to inject into, and headless was
# already known-blocked for sign-in regardless of input method).
def x11_display_for_input() -> str:
    """The X11 display to inject real input into, or "" to use CDP input
    instead (headless fallback, Xvfb never came up, or no xdotool: without
    it every input call would silently no-op rather than fall back)."""
    if not headful_enabled() or not shutil.which("xdotool"):
        return ""
    display = _xvfb_display()
    return display if _xvfb_running(display) else ""


async def _xdotool(display: str, *args: str) -> bool:
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            xdotool, *args, env=dict(os.environ, DISPLAY=display),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        return proc.returncode == 0
    except Exception:
        logger.debug("[cloud browser] xdotool %s failed", args, exc_info=True)
        return False


_X11_BUTTON = {"left": "1", "middle": "2", "right": "3"}

# JS KeyboardEvent.key names that are not themselves valid X11 keysym names.
# Printable characters (letters, digits, and most punctuation) either match
# their own keysym name already or arrive as a "text" event (insert_text)
# instead of a "key" press, so they are not listed here.
_KEY_TO_X11 = {
    "Enter": "Return", "Escape": "Escape", "Backspace": "BackSpace", "Tab": "Tab",
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "Delete": "Delete", "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    " ": "space", "Space": "space", "Shift": "Shift_L", "Control": "Control_L",
    "Alt": "Alt_L", "Meta": "Super_L", "CapsLock": "Caps_Lock",
}


async def _x11_mouse(display: str, kind: str, x: float, y: float, y_offset: float, button: str) -> None:
    ix, iy = str(int(x)), str(int(y + y_offset))
    btn = _X11_BUTTON.get(button, "1")
    if kind == "move":
        await _xdotool(display, "mousemove", "--sync", ix, iy)
    elif kind == "down":
        await _xdotool(display, "mousemove", "--sync", ix, iy)
        await _xdotool(display, "mousedown", btn)
    elif kind == "up":
        await _xdotool(display, "mousemove", "--sync", ix, iy)
        await _xdotool(display, "mouseup", btn)
    elif kind == "click":
        await _xdotool(display, "mousemove", "--sync", ix, iy)
        await _xdotool(display, "click", btn)
    elif kind == "dblclick":
        await _xdotool(display, "mousemove", "--sync", ix, iy)
        await _xdotool(display, "click", "--repeat", "2", "--delay", "60", btn)


async def _x11_wheel(display: str, x: float, y: float, y_offset: float, dy: float) -> None:
    await _xdotool(display, "mousemove", "--sync", str(int(x)), str(int(y + y_offset)))
    clicks = max(1, min(8, int(abs(dy) // 60) or 1))
    button = "5" if dy > 0 else "4"
    await _xdotool(display, "click", "--repeat", str(clicks), button)


async def _x11_key(display: str, key: str) -> None:
    mapped = _KEY_TO_X11.get(key, key if len(key) == 1 else "")
    if mapped:
        await _xdotool(display, "key", "--clearmodifiers", mapped)


async def _x11_text(display: str, text: str) -> None:
    if text:
        await _xdotool(display, "type", "--clearmodifiers", "--", text)


def _ensure_xvfb(display: str, wait_s: float = 5.0) -> bool:
    """A virtual display for a real, visible Chrome to run on. Reuses one
    already listening on `display` (another run of this, or anything else);
    otherwise starts its own, in its own session so it outlives this process
    same as the browser does. False (no Xvfb on PATH, or it never came up)
    means fall back to headless."""
    if _xvfb_running(display):
        return True
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return False
    log = open(os.path.join(os.path.dirname(_profile_dir()), "xvfb.log"), "ab")
    subprocess.Popen([xvfb, display, "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _xvfb_running(display):
            return True
        time.sleep(0.1)
    return False


def _version() -> Optional[Dict]:
    """The browser answering on our port, if it is a browser."""
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/json/version", timeout=1.5) as r:
            d = json.loads(r.read().decode())
        return d if "Browser" in d else None
    except Exception:
        return None


def running() -> bool:
    return _version() is not None


def ensure(wait_s: float = 12.0) -> str:
    """Start the browser if it is not up. Returns its CDP endpoint, or "" if
    it could not start. Blocking: call from a thread."""
    if not enabled():
        return ""
    if running():
        return ENDPOINT
    os.makedirs(_profile_dir(), exist_ok=True)
    real_exe = _find_browser_executable() if headful_enabled() else ""
    if real_exe and not os.path.exists(real_exe):
        logger.warning("[cloud browser] browser executable not found: %s", real_exe)
        real_exe = ""
    display = _xvfb_display()
    env = None
    if real_exe and _ensure_xvfb(display):
        exe = real_exe
        env = dict(os.environ, DISPLAY=display)
        # No --headless: a real, visible Chrome on the virtual display is
        # the one Google lets sign in (see the module docstring). --disable-
        # gpu: this is a KVM guest with no GPU; confirmed stable under Xvfb.
        # --disable-blink-features=AutomationControlled: fewer automation
        # tells (navigator.webdriver already reads false on this combo).
        mode_args = ["--disable-gpu", "--disable-blink-features=AutomationControlled"]
    else:
        if real_exe:
            logger.warning("[cloud browser] Xvfb did not come up on %s, falling back to headless", display)
        exe = chromium_path()
        mode_args = ["--headless=new", "--hide-scrollbars"]
    if not exe or not os.path.exists(exe):
        logger.warning("[cloud browser] no browser found (set ODYSSEUS_BROWSER_EXECUTABLE, install "
                       "google-chrome-stable, or run: npx playwright install chromium)")
        return ""
    args = [exe, "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={PORT}",
            f"--user-data-dir={_profile_dir()}", f"--window-size={WIDTH},{HEIGHT}",
            "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
            "--mute-audio",
            # Ubuntu 24.04 blocks the user namespaces Chromium's sandbox needs
            # ("No usable sandbox!"). Playwright launches without it by
            # default too, as the MCP's own headless browser did.
            "--no-sandbox"] + mode_args + ["about:blank"]
    log = open(os.path.join(os.path.dirname(_profile_dir()), "chromium.log"), "ab")
    # Its own session: it outlives a server restart, and the next server
    # finds it on the port and keeps using it (tabs and logins intact).
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True, env=env)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if running():
            logger.info("[cloud browser] started on %s (%s)", ENDPOINT, "headful" if env else "headless")
            return ENDPOINT
        time.sleep(0.3)
    logger.warning("[cloud browser] browser did not come up on %s", ENDPOINT)
    return ""


def _tabs_json() -> List[Dict]:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/json/list", timeout=2) as r:
            return [t for t in json.loads(r.read().decode()) if t.get("type") == "page"]
    except Exception:
        return []


_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.I)
_HOST_RE = re.compile(r"^(?P<host>[\w.-]+)(:\d+)?(/\S*)?$")
_LOCAL_RE = re.compile(r"^(localhost|127\.|10\.|192\.168\.|100\.|[\w-]+\.ts\.net$)")


def address(text: str) -> str:
    """What the address bar means: a URL as it is, a bare host as a site
    (http for this machine, the LAN and the tailnet), anything else a search."""
    text = (text or "").strip()
    if not text:
        return ""
    if _SCHEME_RE.match(text):
        return text
    m = _HOST_RE.match(text)
    if m and ("." in m.group("host") or m.group("host") == "localhost"):
        return ("http://" if _LOCAL_RE.match(m.group("host")) else "https://") + text
    return "https://duckduckgo.com/?q=" + urllib.parse.quote(text)


# Some sites (Google first) refuse to sign in a browser that is driven over
# CDP: "This browser or app may not be secure". The way round is to sign in
# on an ordinary browser and bring the login over as cookies, exported by an
# extension as cookies.txt (Netscape format) or Cookie-Editor's JSON.
MAX_COOKIES = 3000
_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}


def parse_cookies(text: str) -> List[Dict]:
    """Playwright cookies from cookies.txt or a JSON export. ValueError if
    neither format yields any."""
    text = (text or "").strip()
    out: List[Dict] = []
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError(f"That JSON does not parse: {e}") from None
        if isinstance(data, dict):
            data = data.get("cookies") or []
        for c in data if isinstance(data, list) else []:
            if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
                continue
            ck = {"name": str(c["name"]), "value": str(c.get("value") or ""),
                  "domain": str(c["domain"]), "path": str(c.get("path") or "/"),
                  "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly"))}
            exp = c.get("expirationDate", c.get("expires"))
            if isinstance(exp, (int, float)) and exp > 0 and not c.get("session"):
                ck["expires"] = float(exp)
            ss = _SAME_SITE.get(str(c.get("sameSite") or "").lower())
            if ss:
                ck["sameSite"] = ss
            out.append(ck)
    else:
        for line in text.splitlines():
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _sub, path, secure, expires, name, value = parts[:7]
            ck = {"name": name, "value": value.rstrip("\r\n"), "domain": domain, "path": path or "/",
                  "secure": secure.upper() == "TRUE", "httpOnly": http_only}
            try:
                if int(float(expires)) > 0:
                    ck["expires"] = float(expires)
            except ValueError:
                pass
            out.append(ck)
    for ck in out:
        if ck.get("sameSite") == "None":
            ck["secure"] = True     # Chrome drops SameSite=None without Secure
    if not out:
        raise ValueError("No cookies found. Export them as cookies.txt or as JSON.")
    return out[:MAX_COOKIES]


class Viewer:
    """Watches the browser's active tab and replays the user's input on it."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None
        self._cdp = None
        self._lock = asyncio.Lock()
        self.subscribers: Set[asyncio.Queue] = set()
        self._last_frame: Optional[Dict] = None
        self._size = (WIDTH, HEIGHT)
        self._idle_task: Optional[asyncio.Task] = None
        self.taken_over_by = ""
        self.taken_over_at = 0.0

    # -- connection ---------------------------------------------------------

    async def _connect(self):
        if self._browser and self._browser.is_connected():
            return self._browser
        endpoint = await asyncio.to_thread(ensure)
        if not endpoint:
            raise RuntimeError("The cloud browser is not running (no Chromium, or it is switched off).")
        from playwright.async_api import async_playwright
        if not self._pw:
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        for ctx in self._browser.contexts:
            self._watch_context(ctx)
        return self._browser

    def _watch_context(self, ctx) -> None:
        ctx.on("page", lambda p: asyncio.ensure_future(self._on_new_page(p)))
        for p in ctx.pages:
            self._watch_page(p)

    def _watch_page(self, page) -> None:
        def nav(frame):
            if frame == page.main_frame:
                if page is self._page:
                    self._broadcast({"type": "page", **self._page_info(page)})
                self._broadcast({"type": "tabs", "tabs": self.tabs()})
        page.on("framenavigated", nav)
        page.on("close", lambda *_: asyncio.ensure_future(self._on_close(page)))

    async def _on_new_page(self, page) -> None:
        self._watch_page(page)
        # A tab the agent (or a link) opens is where the action is: follow it.
        async with self._lock:
            await self._switch(page)

    async def _on_close(self, page) -> None:
        if page is self._page:
            async with self._lock:
                self._page = None
                self._cdp = None
                pages = self._pages()
                if pages:
                    await self._switch(pages[-1])
        self._broadcast({"type": "tabs", "tabs": self.tabs()})

    async def _new_page(self):
        ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        return await ctx.new_page()

    def _pages(self) -> list:
        if not self._browser:
            return []
        return [p for c in self._browser.contexts for p in c.pages if not p.is_closed()]

    @staticmethod
    def _page_info(page) -> Dict:
        try:
            url = page.url
        except Exception:
            url = ""
        return {"url": url}

    # -- screencast ---------------------------------------------------------

    async def _switch(self, page) -> None:
        if page is self._page and self._cdp:
            return
        await self._stop_cast()
        self._page = page
        if self.subscribers:
            await self._start_cast()
        self._broadcast({"type": "tabs", "tabs": self.tabs()})
        self._broadcast({"type": "page", **self._page_info(page)})

    async def _start_cast(self) -> None:
        if not self._page or self._cdp:
            return
        try:
            cdp = await self._page.context.new_cdp_session(self._page)
        except Exception as e:
            logger.debug("[cloud browser] no CDP session: %s", e)
            return
        self._cdp = cdp

        def frame(ev):
            meta = ev.get("metadata") or {}
            self._size = (int(meta.get("deviceWidth") or WIDTH), int(meta.get("deviceHeight") or HEIGHT))
            f = {"type": "frame", "data": ev.get("data", ""), "w": self._size[0], "h": self._size[1]}
            self._last_frame = f
            self._broadcast(f)
            asyncio.ensure_future(self._ack(cdp, ev.get("sessionId")))
        cdp.on("Page.screencastFrame", frame)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60,
                                                "maxWidth": WIDTH, "maxHeight": HEIGHT, "everyNthFrame": 1})

    @staticmethod
    async def _ack(cdp, sid) -> None:
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        except Exception:
            pass

    async def _stop_cast(self) -> None:
        cdp, self._cdp = self._cdp, None
        if cdp:
            try:
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
            except Exception:
                pass

    def _broadcast(self, msg: Dict) -> None:
        for q in list(self.subscribers):
            if msg.get("type") == "frame":
                # Only the newest frame matters: drop one a slow viewer has not taken.
                while q.qsize() and q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        async with self._lock:
            await self._connect()
            if self._idle_task:
                self._idle_task.cancel()
                self._idle_task = None
            self.subscribers.add(q)
            if not self._page or self._page.is_closed():
                pages = self._pages()
                self._page = pages[-1] if pages else await self._new_page()
            await self._start_cast()
        q.put_nowait({"type": "tabs", "tabs": self.tabs()})
        q.put_nowait({"type": "page", **self._page_info(self._page)})
        if self._last_frame:
            q.put_nowait(self._last_frame)
        # A still page sends no frames: ask for one so the viewer is not blank.
        asyncio.ensure_future(self._nudge())
        return q

    async def _nudge(self) -> None:
        try:
            if self._page and not self._last_frame:
                data = await self._page.screenshot(type="jpeg", quality=60)
                import base64
                f = {"type": "frame", "data": base64.b64encode(data).decode(), "w": self._size[0], "h": self._size[1]}
                self._last_frame = f
                self._broadcast(f)
        except Exception:
            pass

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        if not self.subscribers and not self._idle_task:
            self._idle_task = asyncio.ensure_future(self._stop_when_idle())

    async def _stop_when_idle(self) -> None:
        try:
            await asyncio.sleep(IDLE_STOP_S)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if not self.subscribers:
                await self._stop_cast()
                self.taken_over_by = ""
        self._idle_task = None

    # -- tabs and input -----------------------------------------------------

    def tabs(self) -> List[Dict]:
        out = []
        for i, p in enumerate(self._pages()):
            out.append({"index": i, "url": p.url, "active": p is self._page})
        return out

    async def status(self) -> Dict:
        up = await asyncio.to_thread(running)
        tabs = await asyncio.to_thread(_tabs_json) if up else []
        headful = headful_enabled() and bool(_find_browser_executable())
        return {"enabled": enabled(), "running": up, "endpoint_port": PORT,
                "chromium": bool(chromium_path() or _find_browser_executable()),
                "headful": headful,
                "tabs": [{"url": t.get("url", ""), "title": t.get("title", "")} for t in tabs],
                "watchers": len(self.subscribers), "taken_over_by": self.taken_over_by}

    async def import_cookies(self, text: str) -> Dict:
        """Add an exported login to the browser's own profile, where it is
        kept (and where a signed-in Meet join looks). Never logs values."""
        cookies = parse_cookies(text)
        async with self._lock:
            await self._connect()
            ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            await ctx.add_cookies(cookies)
        sites = sorted({c["domain"].lstrip(".") for c in cookies})
        logger.info("[cloud-browser] imported %d cookies for %d sites", len(cookies), len(sites))
        return {"ok": True, "count": len(cookies), "sites": sites[:20]}

    async def take_over(self, user: str, on: bool) -> Dict:
        self.taken_over_by = (user or "you") if on else ""
        self.taken_over_at = time.time() if on else 0.0
        self._broadcast({"type": "control", "taken_over_by": self.taken_over_by})
        return {"taken_over_by": self.taken_over_by}

    async def act(self, ev: Dict) -> Dict:
        """Replay one input event from the viewer on the active tab."""
        async with self._lock:
            await self._connect()
            page = self._page
            kind = str(ev.get("type") or "")
            if kind == "tab":
                pages = self._pages()
                i = int(ev.get("index", -1))
                if 0 <= i < len(pages):
                    await self._switch(pages[i])
                    try:
                        await pages[i].bring_to_front()
                    except Exception:
                        pass
                return {"ok": True}
            if kind == "close_tab":
                pages = self._pages()
                i = int(ev.get("index", -1))
                target = pages[i] if 0 <= i < len(pages) else page
                if target is None or target.is_closed():
                    return {"ok": True}
                if len(pages) <= 1:
                    # Keep one blank tab, so there is still a screen to show.
                    await self._switch(await self._new_page())
                elif target is self._page:
                    # Its neighbor, like closing a tab in a browser does.
                    k = pages.index(target)
                    await self._switch(pages[k + 1] if k + 1 < len(pages) else pages[k - 1])
                await target.close()
                return {"ok": True}
            if kind == "new_tab":
                p = await self._new_page()
                await self._switch(p)
                page = p
                kind, ev = "navigate", {"url": ev.get("url") or "about:blank"}
            if not page or page.is_closed():
                raise RuntimeError("No tab is open.")
        # Outside the lock: a navigation can take seconds, and frames must keep flowing.
        x11 = x11_display_for_input()
        if kind in ("click", "down", "up", "move", "dblclick"):
            x, y = self._xy(ev)
            button = ev.get("button") if ev.get("button") in ("left", "right", "middle") else "left"
            if x11:
                y_offset = max(0, HEIGHT - self._size[1])
                await _x11_mouse(x11, kind, x, y, y_offset, button)
            elif kind == "click":
                await page.mouse.click(x, y, button=button)
            elif kind == "dblclick":
                await page.mouse.dblclick(x, y, button=button)
            elif kind == "move":
                await page.mouse.move(x, y)
            elif kind == "down":
                await page.mouse.move(x, y)
                await page.mouse.down(button=button)
            else:
                await page.mouse.move(x, y)
                await page.mouse.up(button=button)
        elif kind == "wheel":
            x, y = self._xy(ev)
            dy = float(ev.get("dy") or 0)
            if x11:
                y_offset = max(0, HEIGHT - self._size[1])
                await _x11_wheel(x11, x, y, y_offset, dy)
            else:
                await page.mouse.move(x, y)
                await page.mouse.wheel(float(ev.get("dx") or 0), dy)
        elif kind == "key":
            key = str(ev.get("key") or "")[:40]
            if key:
                if x11:
                    await _x11_key(x11, key)
                else:
                    await page.keyboard.press(key)
        elif kind == "text":
            text = str(ev.get("text") or "")[:5000]
            if text:
                if x11:
                    await _x11_text(x11, text)
                else:
                    await page.keyboard.insert_text(text)
        elif kind == "navigate":
            url = address(str(ev.get("url") or ""))
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        elif kind == "back":
            await page.go_back(timeout=15000)
        elif kind == "forward":
            await page.go_forward(timeout=15000)
        elif kind == "reload":
            await page.reload(timeout=30000)
        else:
            raise ValueError(f"unknown input {kind!r}")
        return {"ok": True, "url": page.url}

    def _xy(self, ev: Dict):
        """The viewer sends where on the picture (0..1); the page wants CSS pixels."""
        w, h = self._size
        fx = min(1.0, max(0.0, float(ev.get("fx") or 0)))
        fy = min(1.0, max(0.0, float(ev.get("fy") or 0)))
        return fx * w, fy * h


viewer = Viewer()
