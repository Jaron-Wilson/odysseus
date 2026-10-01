"""Joining a Google Meet through the cloud browser.

The cloud browser (src/cloud_browser.py) is a Chromium on this server with
remote debugging on loopback. A meeting gets its own tab in it:

    guest       in a fresh browser context (no cookies, nothing of the
                profile): Meet asks for a name, the bot types the display
                name ("Odysseus (AI)") and asks to join; someone in the
                meeting lets it in. Nothing to sign in to.
    signed_in   in the browser's own profile, as whatever Google account is
                signed in there (sign in once through the cloud browser
                viewer, ideally a separate account named "Odysseus (AI)").
                Invited, it skips the lobby.

Either way the tab can be watched (and taken over) in the cloud browser
viewer. Before Meet's scripts run, inject.js replaces the microphone and
camera and taps WebRTC for the meeting's audio, which comes back here
through a Playwright binding as 16 kHz PCM; the agent's speech goes the
other way through page.evaluate. No sound card, PulseAudio or virtual
devices are needed.

Meet has no stable DOM API, so everything this reads off the page (the
join button, the lobby, "the call has ended") is in SELECTORS and
_STATE_JS below, matched on English text and ARIA labels. When Meet
changes, this is the one place to fix.
"""

import asyncio
import base64
import json
import logging
import os
import re
from typing import Callable, Dict, Iterable, Optional

from src.meet.links import MEET_HOST

logger = logging.getLogger(__name__)

BINDING = "__odysseusMeetEmit"
RATE = 16000
_JS = os.path.join(os.path.dirname(__file__), "inject.js")

SELECTORS = {
    "name_input": 'input[aria-label="Your name"], input[placeholder="Your name"], input[autocomplete="name"]',
    "join": re.compile(r"^\s*(ask to join|join now|join anyway|switch here|join)\s*$", re.I),
    "dismiss": re.compile(r"^\s*(got it|dismiss|close|ok|no thanks|not now)\s*$", re.I),
    "leave": re.compile(r"leave call", re.I),
    "chat_open": re.compile(r"chat with everyone|open chat|^chat$", re.I),
    "chat_input": 'textarea[aria-label*="Send a message"], textarea[aria-label*="message" i], textarea',
    "mic_off": re.compile(r"turn on microphone|unmute", re.I),
}

# What the page shows, as one word. Text first (the lobby and the end screens
# have no buttons worth matching), then the in-call controls.
_STATE_JS = r"""
() => {
  const txt = ((document.body && document.body.innerText) || '').slice(0, 30000);
  const labels = [...document.querySelectorAll('button,[role=button]')]
    .filter((b) => b.getClientRects().length > 0)
    .map((b) => ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).trim());
  const has = (re) => labels.some((l) => re.test(l));
  let state = 'loading';
  if (/you can.t join this (video )?call|denied your request|no one responded to your request|you.ve been removed|removed you from the (meeting|call)|check your meeting code|invalid video call name/i.test(txt)) state = 'denied';
  else if (/you left the meeting|you.ve left the (meeting|call)|the call has ended|meeting has ended|return to home screen|rejoin/i.test(txt) && !has(/leave call/i)) state = 'ended';
  else if (has(/leave call/i)) state = /you.re the only one here|no one else is here/i.test(txt) ? 'alone' : 'in';
  else if (/asking to (be let in|join)|please wait until a meeting host|someone in the (meeting|call) will let you in|waiting for the host|you.ll join the call when someone lets you in/i.test(txt)) state = 'lobby';
  else if (/sign in to (join|continue)|to join this (call|meeting), sign in|use your google account/i.test(txt) && !has(/ask to join|join now/i)) state = 'signin';
  else if (has(/^\s*(ask to join|join now|join anyway|switch here)\s*$/i)) state = 'prejoin';
  const odd = (window.__odyMeet && window.__odyMeet.state()) || {};
  return { state, remote: odd.remote || 0, audio: odd.ctx || 'none', title: document.title || '' };
}
"""


def script(hosts: Iterable[str], name: str, tagline: str) -> str:
    with open(_JS, "r", encoding="utf-8") as f:
        body = f.read()
    cfg = {"hosts": list(hosts), "binding": BINDING, "rate": RATE, "name": name, "tagline": tagline}
    return body.rstrip().rstrip(";") + "(" + json.dumps(cfg) + ");\n"


def _not_headless(ua: str) -> str:
    # Meet turns away a browser that calls itself HeadlessChrome; it is the
    # same Chromium either way.
    return (ua or "").replace("HeadlessChrome", "Chrome")


class MeetBrowser:
    """One meeting's tab. `on_audio(pcm_bytes)` gets the meeting's audio,
    `on_mark(name)` hears when a played clip has finished."""

    def __init__(self, on_audio: Callable[[bytes], None], on_mark: Callable[[str], None],
                 endpoint_fn: Optional[Callable[[], str]] = None, hosts: Iterable[str] = (MEET_HOST,)):
        self.on_audio = on_audio
        self.on_mark = on_mark
        self.endpoint_fn = endpoint_fn
        self.hosts = tuple(hosts)
        self._pw = None
        self._browser = None
        self._ctx = None
        self._own_ctx = False
        self.page = None
        self.remote_tracks = 0

    # ── open and join ──

    async def open(self, url: str, name: str, join_as: str = "guest",
                   tagline: str = "AI assistant: listening and transcribing") -> None:
        if self.endpoint_fn is None:
            from src import cloud_browser
            if not cloud_browser.enabled():
                raise RuntimeError("The cloud browser is switched off (ODYSSEUS_CLOUD_BROWSER=0).")
            endpoint = await asyncio.to_thread(cloud_browser.ensure)
        else:
            endpoint = await asyncio.to_thread(self.endpoint_fn)
        if not endpoint:
            raise RuntimeError("The cloud browser is not running (no Chromium found).")
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        origin = re.match(r"^https?://[^/]+", url).group(0)
        if join_as == "signed_in" and self._browser.contexts:
            self._ctx = self._browser.contexts[0]
        else:
            self._ctx = await self._browser.new_context(viewport={"width": 1280, "height": 800})
            self._own_ctx = True
        try:
            await self._ctx.grant_permissions(["microphone", "camera"], origin=origin)
        except Exception as e:
            logger.debug("[meet] could not grant media permissions: %s", e)
        self.page = await self._ctx.new_page()
        try:
            cdp = await self._ctx.new_cdp_session(self.page)
            ua = (await cdp.send("Browser.getVersion")).get("userAgent", "")
            await cdp.send("Emulation.setUserAgentOverride", {"userAgent": _not_headless(ua)})
        except Exception as e:
            logger.debug("[meet] no user agent override: %s", e)
        await self.page.expose_binding(BINDING, self._on_msg)
        await self.page.add_init_script(script(self.hosts, name, tagline))
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)

    def _on_msg(self, _source, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = msg.get("t")
        if kind == "audio":
            try:
                self.on_audio(base64.b64decode(msg.get("d") or ""))
            except Exception as e:
                logger.debug("[meet] audio handler failed: %s", type(e).__name__)
        elif kind == "mark":
            self.on_mark(str(msg.get("name") or ""))
        elif kind == "track":
            self.remote_tracks = int(msg.get("n") or 0)

    async def status(self) -> Dict:
        if not self.page or self.page.is_closed():
            return {"state": "ended", "remote": 0, "audio": "none"}
        try:
            return await self.page.evaluate(_STATE_JS)
        except Exception as e:
            # Mid-navigation, or the tab went away.
            if self.page.is_closed():
                return {"state": "ended", "remote": 0, "audio": "none"}
            logger.debug("[meet] status check failed: %s", type(e).__name__)
            return {"state": "loading", "remote": 0, "audio": "none"}

    async def _click(self, pattern, timeout: float = 1500) -> bool:
        try:
            btn = self.page.get_by_role("button", name=pattern).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=timeout)
                return True
        except Exception:
            pass
        return False

    async def join(self, name: str) -> None:
        """On the pre-join screen: the name (as a guest), then Ask to join or
        Join now."""
        await self._click(SELECTORS["dismiss"])
        try:
            box = self.page.locator(SELECTORS["name_input"]).first
            if await box.count() and await box.is_visible():
                await box.fill(name, timeout=3000)
        except Exception as e:
            logger.debug("[meet] no name box: %s", type(e).__name__)
        if not await self._click(SELECTORS["join"], timeout=5000):
            raise RuntimeError("Could not find Meet's join button.")
        try:
            await self.page.evaluate("window.__odyMeet && window.__odyMeet.resume()")
        except Exception:
            pass

    # ── in the meeting ──

    async def unmute(self) -> None:
        await self._click(SELECTORS["mic_off"])

    async def send_chat(self, text: str) -> bool:
        """A message in the meeting's chat (best effort)."""
        try:
            box = self.page.locator(SELECTORS["chat_input"]).first
            if not (await box.count() and await box.is_visible()):
                await self._click(SELECTORS["chat_open"])
                await asyncio.sleep(1.0)
            box = self.page.locator(SELECTORS["chat_input"]).first
            if not await box.count():
                return False
            await box.fill(text, timeout=3000)
            await box.press("Enter")
            return True
        except Exception as e:
            logger.info("[meet] could not post in the meeting chat: %s", type(e).__name__)
            return False

    async def play(self, pcm: bytes, mark: str) -> None:
        if not self.page or self.page.is_closed():
            raise RuntimeError("the meeting tab is closed")
        await self.page.evaluate("([d, r, m]) => window.__odyMeet.play(d, r, m)",
                                 [base64.b64encode(pcm).decode("ascii"), RATE, mark])

    async def clear(self) -> None:
        if self.page and not self.page.is_closed():
            await self.page.evaluate("window.__odyMeet && window.__odyMeet.clear()")

    async def leave(self) -> None:
        if self.page and not self.page.is_closed():
            await self._click(SELECTORS["leave"])
            await asyncio.sleep(0.5)

    async def close(self) -> None:
        """Close the tab (and the guest context); the browser keeps running."""
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self._own_ctx and self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.page = None
        self._pw = None


class BrowserTransport:
    """call.Transport over a Meet tab: 16 kHz PCM out, marks back."""

    def __init__(self, browser: MeetBrowser, on_hangup: Callable[[], "asyncio.Future"]):
        self.b = browser
        self._on_hangup = on_hangup
        self._unmuted = False

    async def play(self, audio: bytes, mark: str) -> None:
        if not self._unmuted:
            self._unmuted = True
            await self.b.unmute()
        await self.b.play(audio, mark)

    async def clear(self) -> None:
        await self.b.clear()

    async def hangup(self) -> None:
        await self._on_hangup()
