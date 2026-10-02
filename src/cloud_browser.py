"""The cloud browser: one Chromium on this server that the agent drives and
the user can watch live and take over.

Asked for on 2026-09-29: "can we add a cloud browser like chatgpt does and
manus ai?" The agent already had a browser (the built-in Playwright MCP), but
it was headless and private to the MCP: nobody could see what it was doing,
or step in to log in or get past a CAPTCHA.

- This module starts Playwright's Chromium itself, with remote debugging on
  loopback only and a profile under DATA_DIR/cloud_browser, so logins stay
  between runs and restarts (the browser outlives a server restart and is
  picked up again).
- The Browser MCP connects to it (--cdp-endpoint, src/builtin_mcp.py), so
  the agent's browser_* tools act on this very browser.
- The viewer attaches over CDP too (Playwright for Python): it streams the
  active tab as JPEG frames (Page.startScreencast) to whoever is watching,
  and replays the user's clicks, scrolling and typing when they take over
  (routes/cloud_browser_routes.py, static/js/cloudBrowser.js).
"""
import asyncio
import glob
import json
import logging
import os
import re
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


def enabled() -> bool:
    return os.environ.get("ODYSSEUS_CLOUD_BROWSER", "1").lower() not in ("0", "false", "no")


def _profile_dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "cloud_browser", "profile")


def chromium_path() -> str:
    """Playwright's own Chromium (the newest one installed), or
    ODYSSEUS_CLOUD_BROWSER_CHROME."""
    own = os.environ.get("ODYSSEUS_CLOUD_BROWSER_CHROME", "")
    if own:
        return own
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
    found = sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome")),
                   key=lambda p: int((p.split("chromium-")[1].split(os.sep)[0] or "0")))
    return found[-1] if found else ""


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
    exe = chromium_path()
    if not exe or not os.path.exists(exe):
        logger.warning("[cloud browser] no Chromium found (run: npx playwright install chromium)")
        return ""
    os.makedirs(_profile_dir(), exist_ok=True)
    args = [exe, "--headless=new", "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={PORT}", f"--user-data-dir={_profile_dir()}",
            f"--window-size={WIDTH},{HEIGHT}", "--no-first-run", "--no-default-browser-check",
            "--disable-dev-shm-usage", "--hide-scrollbars", "--mute-audio",
            # Ubuntu 24.04 blocks the user namespaces Chromium's sandbox needs
            # ("No usable sandbox!"). Playwright launches without it by
            # default too, as the MCP's own headless browser did.
            "--no-sandbox", "about:blank"]
    log = open(os.path.join(os.path.dirname(_profile_dir()), "chromium.log"), "ab")
    # Its own session: it outlives a server restart, and the next server
    # finds it on the port and keeps using it (tabs and logins intact).
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if running():
            logger.info("[cloud browser] started on %s", ENDPOINT)
            return ENDPOINT
        time.sleep(0.3)
    logger.warning("[cloud browser] Chromium did not come up on %s", ENDPOINT)
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
        return {"enabled": enabled(), "running": up, "endpoint_port": PORT,
                "chromium": bool(chromium_path()),
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
        if kind in ("click", "down", "up", "move", "dblclick"):
            x, y = self._xy(ev)
            button = ev.get("button") if ev.get("button") in ("left", "right", "middle") else "left"
            if kind == "click":
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
            await page.mouse.move(x, y)
            await page.mouse.wheel(float(ev.get("dx") or 0), float(ev.get("dy") or 0))
        elif kind == "key":
            key = str(ev.get("key") or "")[:40]
            if key:
                await page.keyboard.press(key)
        elif kind == "text":
            text = str(ev.get("text") or "")[:5000]
            if text:
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
