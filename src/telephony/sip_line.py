"""The free SIP line: call Odysseus from a softphone over the tailnet.

No phone number and no provider: a SIP app on the phone (Linphone, Zoiper)
calls sip:odysseus@<this server's Tailscale address>, and
src/telephony/sip_server.py answers it inside the Odysseus process. The call
then runs through the same PhoneCall as a Twilio call (src/telephony/call.py):
greeting, "Hears with" / "Speaks with", sentence by sentence replies,
barge-in, "*" to interrupt, "bye" to hang up, the 30 minute cap, and a chat
per call.

Settings are per user, in the Phone calls prefs (config.PREF_KEY) under
"sip"; the password is encrypted like the Twilio token and never sent back.
The listener itself is one per server, started while any user has the line
on, on the Tailscale addresses only:

    ODYSSEUS_SIP_PORT          SIP port, UDP and TCP (default 5060)
    ODYSSEUS_SIP_RTP_PORTS     RTP port range (default 10000-10100)
    ODYSSEUS_SIP_ADDRESSES     which Tailscale addresses (default: all of them)
    ODYSSEUS_SIP_TEST_LOOPBACK =1 listens on 127.0.0.1 only and lets loopback
                               in: for tests and a local preview, never for use
"""

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from src.telephony import agent, call as call_mod, codec, config, sip
from src.telephony.sip_server import Account, SipCall, SipServer

logger = logging.getLogger(__name__)

SIP_KEY = "sip"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MIN_PASSWORD = 10
MAX_DEVICES = 10
PROVISION_TTL = 600.0
PIN_TRIES = 3
PIN_WAIT_S = 20.0


# ── server-wide settings ──────────────────────────────────────────────

def test_mode() -> bool:
    return os.environ.get("ODYSSEUS_SIP_TEST_LOOPBACK", "").strip().lower() in ("1", "true", "yes", "on")


def sip_port() -> int:
    try:
        p = int(os.environ.get("ODYSSEUS_SIP_PORT", "5060"))
    except ValueError:
        p = 5060
    return p if 1 <= p <= 65535 else 5060


def rtp_ports() -> Tuple[int, int]:
    raw = os.environ.get("ODYSSEUS_SIP_RTP_PORTS", "10000-10100")
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", raw)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 1024 <= lo < hi <= 65535:
            return lo, hi
    return 10000, 10100


def _run(args: List[str], timeout: float = 4.0) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def tailnet_addresses() -> List[str]:
    """This server's Tailscale addresses: from `tailscale ip`, else from the
    interfaces (anything in 100.64.0.0/10 or fd7a:115c:a1e0::/48)."""
    found: List[str] = []
    if shutil.which("tailscale"):
        found = [ln.strip() for ln in _run(["tailscale", "ip"]).splitlines() if sip.is_tailnet(ln.strip())]
    if not found and shutil.which("ip"):
        for ln in _run(["ip", "-o", "addr", "show"]).splitlines():
            m = re.search(r"\binet6?\s+([0-9a-fA-F:.]+)/", ln)
            if m and sip.is_tailnet(m.group(1)):
                found.append(m.group(1))
    out: List[str] = []
    for a in found:
        if a not in out:
            out.append(a)
    # IPv4 first: it is the one to type into a phone.
    return sorted(out, key=lambda a: ":" in a)


def bind_addresses() -> List[str]:
    """Where the listener goes. Each passes sip.check_bind_address."""
    if test_mode():
        return ["127.0.0.1"]
    raw = os.environ.get("ODYSSEUS_SIP_ADDRESSES", "").strip()
    addrs = [a.strip() for a in raw.split(",") if a.strip()] if raw else tailnet_addresses()
    out = []
    for a in addrs:
        try:
            out.append(sip.check_bind_address(a))
        except sip.SipError as e:
            logger.warning("[sip] %s", e)
    return out


def tailnet_info() -> Dict:
    """This machine's MagicDNS name and the other devices on the tailnet."""
    raw = _run(["tailscale", "status", "--json"]) if shutil.which("tailscale") else ""
    try:
        st = json.loads(raw) if raw else {}
    except ValueError:
        st = {}
    me = st.get("Self") or {}
    devices = []
    for p in (st.get("Peer") or {}).values():
        ips = [ip for ip in (p.get("TailscaleIPs") or []) if sip.is_tailnet(ip) and ":" not in ip]
        if ips:
            devices.append({"name": str(p.get("HostName") or p.get("DNSName") or "")[:60],
                            "ip": ips[0], "online": bool(p.get("Online")), "os": str(p.get("OS") or "")})
    devices.sort(key=lambda d: (not d["online"], d["name"].lower()))
    return {"dns_name": str(me.get("DNSName") or "").rstrip("."), "devices": devices[:50]}


# ── per-user settings ─────────────────────────────────────────────────

def sip_config(cfg: Dict) -> Dict:
    s = cfg.get(SIP_KEY)
    return dict(s) if isinstance(s, dict) else {}


def password(scfg: Dict) -> str:
    from src import secret_storage
    return secret_storage.decrypt(str(scfg.get("password") or ""))


def set_password(scfg: Dict, pw: str) -> None:
    from src import secret_storage
    scfg["password"] = secret_storage.encrypt(pw) if pw else ""


def valid_device(ip: str) -> str:
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        raise ValueError(f"Not an IP address: {str(ip)[:40]}")
    if not (sip.is_tailnet(str(a)) or (test_mode() and a.is_loopback)):
        raise ValueError(f"{a} is not a Tailscale address (100.x.y.z).")
    return str(a)


def accounts() -> List[Account]:
    out = []
    for owner, cfg in config._owners():
        s = sip_config(cfg)
        if not s.get("enabled") or not s.get("username") or not s.get("password"):
            continue
        pw = password(s)
        if pw:
            out.append(Account(owner, str(s["username"]), pw, [str(d) for d in s.get("devices") or []]))
    return out


def username_taken(username: str, owner: Optional[str]) -> bool:
    for u, cfg in config._owners():
        if u != owner and str(sip_config(cfg).get("username") or "").lower() == username.lower():
            return True
    return False


# ── the line ──────────────────────────────────────────────────────────

class SipLine:
    """Starts and stops the one listener as users turn the line on and off,
    and runs each call."""

    RECHECK_S = 30.0

    def __init__(self):
        self.server: Optional[SipServer] = None
        self.error = ""
        self.closed = False
        self.app = None
        self._lock: Optional[asyncio.Lock] = None
        self._accounts: List[Account] = []
        self._accounts_at = 0.0
        self._tasks: set = set()
        self._provision: Dict[str, Dict] = {}
        self._info: Dict = {}
        self._info_at = 0.0

    def _get_accounts(self) -> List[Account]:
        if time.monotonic() - self._accounts_at > 5:
            try:
                self._accounts = accounts()
            except Exception as e:
                logger.warning("[sip] could not read the SIP accounts: %s", type(e).__name__)
                self._accounts = []
            self._accounts_at = time.monotonic()
        return self._accounts

    def invalidate(self) -> None:
        self._accounts_at = 0.0

    def _bg(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    @property
    def running(self) -> bool:
        return bool(self.server and self.server.running)

    async def reconcile(self) -> None:
        """Listener on if any user has the line on, off otherwise."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self.invalidate()
            want = bool(self._get_accounts()) and not self.closed
            if want and not self.running:
                addrs = await asyncio.to_thread(bind_addresses)
                if not addrs:
                    self.error = ("No Tailscale address on this server. Is Tailscale up? "
                                  "(The SIP line never listens anywhere else.)")
                    return
                try:
                    srv = SipServer(addrs, sip_port(), rtp_ports(), self._get_accounts, self._on_call,
                                    precheck=self._precheck, test_mode=test_mode())
                    await srv.start()
                    self.server = srv
                    self.error = ""
                except (OSError, sip.SipError) as e:
                    self.server = None
                    self.error = f"Could not listen on port {sip_port()}: {e}"
                    logger.warning("[sip] %s", self.error)
            elif not want and self.server:
                await self.stop()

    async def shutdown(self) -> None:
        """App shutdown: stop, and stay stopped."""
        self.closed = True
        await self.stop()

    async def stop(self) -> None:
        srv, self.server = self.server, None
        if srv:
            await srv.stop()
            logger.info("[sip] listener stopped")

    async def run_forever(self) -> None:
        """Startup task: bring the line up, and keep trying while it is on
        but has nowhere to listen (Tailscale started after Odysseus)."""
        while True:
            try:
                await self.reconcile()
            except Exception:
                logger.exception("[sip] reconcile failed")
            await asyncio.sleep(self.RECHECK_S)

    def info(self) -> Dict:
        if time.monotonic() - self._info_at > 30:
            self._info = tailnet_info()
            self._info_at = time.monotonic()
        return self._info

    # ── calls ──

    def _precheck(self, acct: Account) -> Optional[Tuple[int, str]]:
        problems = call_mod.engines_ready()
        if problems:
            return 503, "Speech engines are not set up in Odysseus"
        return None

    def _is_admin(self, owner: Optional[str]) -> bool:
        try:
            from routes.telephony_routes import _is_admin
            return _is_admin(self.app, owner) if self.app is not None else False
        except Exception:
            return False

    async def _say(self, call: SipCall, text: str) -> None:
        """Speak one line outside the turn loop (PIN prompt, errors) and wait
        for it to go out."""
        try:
            audio = await asyncio.to_thread(call_mod.default_tts, text)
            ulaw = codec.to_phone(audio) if audio else b""
        except Exception as e:
            logger.info("[sip] could not speak: %s", type(e).__name__)
            return
        done = asyncio.Event()
        call.on_played = lambda m: done.set()
        await call.play(ulaw, "say")
        try:
            await asyncio.wait_for(done.wait(), len(ulaw) / codec.RATE + 3)
        except asyncio.TimeoutError:
            pass
        call.on_played = None

    async def _pin_gate(self, call: SipCall, cfg: Dict) -> bool:
        pin_len = int(cfg.get("pin_len") or 4)
        digits: asyncio.Queue = asyncio.Queue()
        call.on_dtmf = digits.put_nowait
        prompt = "Enter your PIN."
        for attempt in range(PIN_TRIES):
            await self._say(call, prompt)
            while not digits.empty():
                digits.get_nowait()
            entered = ""
            deadline = time.monotonic() + PIN_WAIT_S
            while len(entered) < pin_len and not call.is_ended:
                try:
                    d = await asyncio.wait_for(digits.get(), max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if d == "#":
                    break
                if d.isdigit():
                    entered += d
            if call.is_ended:
                return False
            if entered and config.check_pin(cfg, entered):
                call.on_dtmf = None
                return True
            logger.info("[sip] wrong PIN on a call from %s (try %d)", call.account.username, attempt + 1)
            prompt = "That is not right. Try again."
        await self._say(call, "That PIN is not right. Goodbye.")
        return False

    async def _on_call(self, call: SipCall, greeting: str = "") -> None:
        from routes import telephony_routes
        owner = call.account.owner
        cfg = config.get_config(owner)
        scfg = sip_config(cfg)
        label = f"SIP {call.account.username}"
        try:
            sid, _ = agent.new_call_chat(owner, cfg, label, call.direction, self._is_admin(owner))
        except Exception as e:
            logger.warning("[sip] no chat for the call: %s", e)
            await self._say(call, "Odysseus has no model to answer with. Pick one in Settings, Calls and Meetings, Phone calls. Goodbye.")
            return
        if scfg.get("require_pin") and cfg.get("pin_hash") and call.direction == "inbound":
            if not await self._pin_gate(call, cfg):
                return
        greeting = greeting or str(cfg.get("greeting") or "").strip() or config.DEFAULT_GREETING
        pc = call_mod.PhoneCall(call, sid, greeting=greeting)
        call.on_audio = pc.feed
        call.on_dtmf = pc.dtmf
        call.on_played = pc.played
        telephony_routes.ACTIVE[call.call_id] = {"owner": owner, "sid": sid, "caller": label,
                                                 "since": time.time(), "engine": "sip"}
        logger.info("[sip] call %s (%s) into chat %s", call.call_id[:10], call.direction, sid)
        try:
            await pc.start()
            while not call.is_ended and not pc.ended:
                try:
                    await asyncio.wait_for(call.ended.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
        finally:
            telephony_routes.ACTIVE.pop(call.call_id, None)
            await pc.end(call.end_reason or "hung up")
            logger.info("[sip] call %s ended after %d turn(s)", call.call_id[:10], pc.turns)

    async def call_me(self, owner: Optional[str], greeting: str = "") -> Dict:
        """Ring the owner's registered softphone. Checks right away (raises
        ValueError with a sentence), then rings in the background."""
        if not self.running:
            raise ValueError("The SIP line is not running. Turn it on and Save first.")
        scfg = sip_config(config.get_config(owner))
        user = str(scfg.get("username") or "")
        if not scfg.get("enabled") or not user:
            raise ValueError("Turn the SIP line on first.")
        regs = self.server.registered(user)
        if not regs:
            raise ValueError("Your softphone is not registered. Open it and check it says Registered "
                             "(Linphone: a green dot; Zoiper: a check mark).")
        problems = call_mod.engines_ready()
        if problems:
            raise ValueError(" ".join(problems))
        srv = self.server

        async def ring():
            try:
                c = await srv.call_out(user)
            except (LookupError, RuntimeError) as e:
                logger.info("[sip] calling %s: %s", user, e)
                return
            await srv.run_call(c, lambda cc: self._on_call(cc, greeting))

        self._bg(ring())
        return {"ok": True, "to": user, "device": regs[0].link.peer[0]}

    # ── Linphone remote provisioning (a QR code) ──

    def provision_token(self, owner: Optional[str]) -> str:
        now = time.time()
        for k in [k for k, v in self._provision.items() if now - v["at"] > PROVISION_TTL]:
            self._provision.pop(k, None)
        token = secrets.token_urlsafe(32)
        self._provision[token] = {"owner": owner, "at": now}
        return token

    def take_provision(self, token: str) -> Optional[Dict]:
        rec = self._provision.pop(token or "", None)
        if not rec or time.time() - rec["at"] > PROVISION_TTL:
            return None
        return rec


LINE = SipLine()


def dial_addresses(addrs: List[str], port: int, dns_name: str = "") -> List[str]:
    """What to dial, best first: the MagicDNS name, then each address."""
    suffix = "" if port == 5060 else f":{port}"
    out = []
    if dns_name:
        out.append(f"sip:odysseus@{dns_name}{suffix}")
    for a in addrs:
        out.append(f"sip:odysseus@{sip.host_for_uri(a)}{suffix}")
    return out


def linphone_xml(username: str, pw: str, host: str, port: int, transport: str = "udp",
                 realm: str = "odysseus") -> str:
    """A Linphone remote-provisioning file for this account. It carries the
    digest HA1 (md5 of user:realm:password), not the password itself, and
    tells Linphone not to fetch it again (the link works once)."""
    import hashlib
    ha1 = hashlib.md5(f"{username}:{realm}:{pw}".encode("utf-8")).hexdigest()
    h = sip.host_for_uri(host)
    e = html.escape
    proxy = e(f"<sip:{h}:{port};transport={transport}>")
    ident = e(f'"{username}" <sip:{username}@{h}>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<config xmlns="http://www.linphone.org/xsds/lpconfig.xsd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.linphone.org/xsds/lpconfig.xsd lpconfig.xsd">
  <section name="misc">
    <entry name="transient_provisioning" overwrite="true">1</entry>
  </section>
  <section name="sip">
    <entry name="default_proxy" overwrite="true">0</entry>
  </section>
  <section name="proxy_0" overwrite="true">
    <entry name="reg_proxy" overwrite="true">{proxy}</entry>
    <entry name="reg_identity" overwrite="true">{ident}</entry>
    <entry name="reg_expires" overwrite="true">600</entry>
    <entry name="reg_sendregister" overwrite="true">1</entry>
    <entry name="publish" overwrite="true">0</entry>
    <entry name="avpf" overwrite="true">0</entry>
    <entry name="dial_escape_plus" overwrite="true">0</entry>
    <entry name="realm" overwrite="true">{e(realm)}</entry>
  </section>
  <section name="auth_info_0" overwrite="true">
    <entry name="username" overwrite="true">{e(username)}</entry>
    <entry name="ha1" overwrite="true">{ha1}</entry>
    <entry name="realm" overwrite="true">{e(realm)}</entry>
    <entry name="domain" overwrite="true">{e(host)}</entry>
    <entry name="algorithm" overwrite="true">MD5</entry>
  </section>
</config>
"""
