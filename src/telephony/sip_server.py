"""The SIP user agent behind the free SIP line: answers calls, takes
registrations, and rings a registered softphone back.

It listens on UDP and TCP, on the server's Tailscale addresses only (see
sip.check_bind_address: never 0.0.0.0 or ::, never a LAN or public address),
and every request passes three gates before anything reaches the agent:

    1. source address   a tailnet address (or, in test mode, loopback), and
                        one of the account's devices when it lists any;
                        Linux delivers a packet for the Tailscale address
                        that came in on the LAN card too, so the bind alone
                        is not enough
    2. digest auth      username and password from the settings card, with
                        signed, expiring nonces and replay protection;
                        repeated failures lock the address out for a while
    3. an enabled line  only accounts whose owner turned the line on exist

A call that passes is a SipCall: a dialog plus an rtp.RtpStream, and it is
also the Transport src/telephony/call.py plays into, so the same PhoneCall
that runs a Twilio call runs this one (src/telephony/sip_line.py wires it).
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from src.telephony import rtp, sip

logger = logging.getLogger(__name__)

T1 = 0.5
T2 = 4.0
TX_LIFETIME = 32.0
MAX_CALLS = 4
# Registrations live in memory, so they are kept short: after a restart the
# phone is back within REG_MAX seconds and "Call me" works again.
REG_MIN, REG_MAX, REG_DEFAULT = 60, 600, 600
MAX_BINDINGS = 3
FAIL_WINDOW_S, FAIL_LIMIT, LOCKOUT_S = 600.0, 10, 600.0
MEDIA_TIMEOUT_S = 120.0
RING_S = 40.0


@dataclass
class Account:
    owner: Optional[str]
    username: str
    password: str
    devices: List[str] = field(default_factory=list)   # allowed source IPs; empty: the whole tailnet
    display: str = "Odysseus"


class Link:
    """Where a message came from and how to answer it."""

    def __init__(self, kind: str, local: Tuple[str, int], peer: Tuple[str, int],
                 send: Callable[[bytes], None], conn: Optional["_TcpConn"] = None):
        self.kind = kind            # "udp" | "tcp"
        self.local = local
        self.peer = peer
        self._send = send
        self.conn = conn

    @property
    def alive(self) -> bool:
        return self.conn.alive if self.conn else True

    def send(self, data: bytes) -> None:
        try:
            self._send(data)
        except Exception as e:
            logger.debug("[sip] send failed: %s", type(e).__name__)


@dataclass
class Registration:
    username: str
    owner: Optional[str]
    contact: str
    link: Link
    expires_at: float
    at: float


class SipCall:
    """One call: a SIP dialog and its RTP stream. Also call.Transport."""

    def __init__(self, server: "SipServer", *, call_id: str, local_hdr: str, remote_hdr: str,
                 local_tag: str, remote_tag: str, remote_target: str, link: Link, stream: rtp.RtpStream,
                 account: Account, direction: str, local_cseq: int = 0, session_id: int = 0):
        self.server = server
        self.call_id = call_id
        self.local_hdr = local_hdr
        self.remote_hdr = remote_hdr
        self.local_tag = local_tag
        self.remote_tag = remote_tag
        self.remote_target = remote_target
        self.link = link
        self.rtp = stream
        self.account = account
        self.direction = direction
        self.local_cseq = local_cseq
        self.session_id = session_id or secrets.randbelow(2 ** 31)
        self.sdp_version = 1
        self.proto = "RTP/AVP"
        self.started = time.time()
        self.ended = asyncio.Event()
        self.end_reason = ""
        self.closing = False
        self.ack_bytes = b""            # outbound: the ACK, resent if the 200 OK is
        self.on_audio: Optional[Callable[[bytes], None]] = None
        self.on_dtmf: Optional[Callable[[str], None]] = None
        self.on_played: Optional[Callable[[str], None]] = None
        stream.on_audio = lambda b: self.on_audio and self.on_audio(b)
        stream.on_dtmf = lambda d: self.on_dtmf and self.on_dtmf(d)
        stream.on_played = lambda m: self.on_played and self.on_played(m)

    @property
    def peer_ip(self) -> str:
        return self.link.peer[0]

    @property
    def is_ended(self) -> bool:
        return self.ended.is_set()

    # call.Transport
    async def play(self, ulaw: bytes, mark: str) -> None:
        if not self.is_ended:
            self.rtp.play(ulaw, mark)

    async def clear(self) -> None:
        self.rtp.clear()

    async def hangup(self) -> None:
        await self.server.bye(self, "agent hung up")

    def _finish(self, reason: str) -> None:
        if self.is_ended:
            return
        self.end_reason = reason
        self.rtp.close()
        self.ended.set()
        self.server._forget(self)


class _UdpListener(asyncio.DatagramProtocol):
    def __init__(self, server: "SipServer"):
        self.server = server
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.local: Tuple[str, int] = ("", 0)

    def connection_made(self, transport) -> None:
        self.transport = transport
        s = transport.get_extra_info("sockname")
        self.local = (s[0], s[1])

    def datagram_received(self, data: bytes, addr) -> None:
        peer = (addr[0], addr[1])
        link = Link("udp", self.local, peer, lambda b, p=peer: self.transport and self.transport.sendto(b, p))
        self.server._on_data(data, link)

    def error_received(self, exc) -> None:
        logger.debug("[sip] udp error: %s", type(exc).__name__)


class _TcpConn:
    def __init__(self, server: "SipServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.alive = True
        s, p = writer.get_extra_info("sockname"), writer.get_extra_info("peername")
        self.link = Link("tcp", (s[0], s[1]), (p[0], p[1]), self._write, self)

    def _write(self, data: bytes) -> None:
        if self.alive:
            self.writer.write(data)

    async def run(self) -> None:
        framer = sip.StreamFramer()
        if not self.server._source_ok(self.link.peer[0]):
            self.server._refused(self.link.peer[0])
            self.close()
            return
        try:
            while True:
                data = await self.reader.read(65536)
                if not data:
                    break
                for raw in framer.feed(data):
                    if raw == b"\r\n\r\n":
                        self._write(b"\r\n")            # RFC 5626 keep-alive pong
                        continue
                    self.server._on_data(raw, self.link)
        except (sip.SipError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        if self.alive:
            self.alive = False
            try:
                self.writer.close()
            except Exception:
                pass


class _ClientTx:
    def __init__(self, method: str):
        self.method = method
        self.q: "asyncio.Queue[sip.SipMessage]" = asyncio.Queue()
        self.got_response = asyncio.Event()


class SipServer:
    def __init__(self, addresses: List[str], port: int, rtp_ports: Tuple[int, int],
                 accounts: Callable[[], List[Account]],
                 on_call: Callable[[SipCall], Awaitable[None]], *,
                 precheck: Optional[Callable[[Account], Optional[Tuple[int, str]]]] = None,
                 test_mode: bool = False, realm: str = "odysseus", frame_s: float = rtp.FRAME_S):
        self.addresses = [sip.check_bind_address(a, test_mode) for a in addresses]
        if not self.addresses:
            raise sip.SipError("no address to listen on")
        self.port = int(port)
        self.rtp_ports = rtp_ports
        self.accounts = accounts
        self.on_call = on_call
        self.precheck = precheck
        self.test_mode = test_mode
        self.frame_s = frame_s
        self.auth = sip.DigestAuth(realm)
        self.listeners: List[Tuple[str, str, int]] = []      # (kind, ip, port) actually bound
        self.calls: Dict[str, SipCall] = {}
        self.registrations: Dict[str, List[Registration]] = {}
        self.refused = 0
        self._udp: Dict[str, _UdpListener] = {}
        self._tcp_servers: List[asyncio.base_events.Server] = []
        self._conns: set = set()
        self._server_tx: Dict[tuple, Dict] = {}
        self._invite_tx: Dict[Tuple[str, int], Dict] = {}
        self._client_tx: Dict[str, _ClientTx] = {}
        self._fails: Dict[str, List[float]] = {}
        self._locked: Dict[str, float] = {}
        self._rtp_used: set = set()
        self._tasks: set = set()
        self._janitor: Optional[asyncio.Task] = None
        self._last_refusal_log = 0.0
        self.running = False

    # ── lifecycle ──

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            for ip in self.addresses:
                ip = sip.check_bind_address(ip, self.test_mode)        # again, right at the bind
                lst = _UdpListener(self)
                await loop.create_datagram_endpoint(lambda lst=lst: lst, local_addr=(ip, self.port))
                self._udp[ip] = lst
                self.listeners.append(("udp", ip, self.port))
                srv = await asyncio.start_server(self._on_tcp, host=ip, port=self.port, reuse_address=True)
                self._tcp_servers.append(srv)
                self.listeners.append(("tcp", ip, self.port))
        except Exception:
            await self.stop()
            raise
        self.running = True
        self._janitor = asyncio.create_task(self._janitor_loop())
        logger.info("[sip] listening on %s", ", ".join(f"{k}:{sip.host_for_uri(i)}:{p}" for k, i, p in self.listeners))

    async def stop(self) -> None:
        self.running = False
        for c in list(self.calls.values()):
            try:
                await asyncio.wait_for(self.bye(c, "server stopping"), 2.0)
            except Exception:
                c._finish("server stopping")
        if self._janitor:
            self._janitor.cancel()
        for t in list(self._tasks):
            t.cancel()
        for e in list(self._server_tx.values()) + list(self._invite_tx.values()):
            if e.get("task"):
                e["task"].cancel()
        for lst in self._udp.values():
            if lst.transport:
                lst.transport.close()
        for srv in self._tcp_servers:
            srv.close()
        for conn in list(self._conns):
            conn.close()
        for srv in self._tcp_servers:
            try:
                await asyncio.wait_for(srv.wait_closed(), 1.0)
            except Exception:
                pass
        self._udp.clear()
        self._tcp_servers.clear()
        self.listeners.clear()

    def _bg(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _on_tcp(self, reader, writer) -> None:
        conn = _TcpConn(self, reader, writer)
        self._conns.add(conn)
        try:
            await conn.run()
        finally:
            self._conns.discard(conn)

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for k in [k for k, e in self._server_tx.items() if now - e["at"] > TX_LIFETIME * 2]:
                self._server_tx.pop(k, None)
            for k in [k for k, e in self._invite_tx.items() if now - e["at"] > TX_LIFETIME * 2]:
                self._invite_tx.pop(k, None)
            for user in list(self.registrations):
                regs = [r for r in self.registrations[user] if r.expires_at > now and r.link.alive]
                if regs:
                    self.registrations[user] = regs
                else:
                    self.registrations.pop(user, None)
            for ip in [ip for ip, until in self._locked.items() if until < now]:
                self._locked.pop(ip, None)
            mono = time.monotonic()
            for c in list(self.calls.values()):
                if not c.rtp.paused and mono - c.rtp.last_rx > MEDIA_TIMEOUT_S:
                    logger.info("[sip] call %s: no audio from the phone for %ds, hanging up",
                                c.call_id[:10], int(MEDIA_TIMEOUT_S))
                    self._bg(self.bye(c, "media timeout"))

    # ── gates ──

    def _account(self, username: str) -> Optional[Account]:
        for a in self.accounts():
            if a.username == username and a.password:
                return a
        return None

    def _source_ok(self, ip: str) -> bool:
        """Gate 1, before anything is parsed: tailnet (or test loopback), and
        if every account names its devices, one of those."""
        ok = sip.is_tailnet(ip)
        if not ok and self.test_mode:
            import ipaddress
            try:
                a = ipaddress.ip_address(ip.split("%", 1)[0])
                ok = a.is_loopback or bool(getattr(a, "ipv4_mapped", None) and a.ipv4_mapped.is_loopback)
            except ValueError:
                ok = False
        if not ok:
            return False
        accts = self.accounts()
        if accts and all(a.devices for a in accts):
            return any(sip.same_ip(ip, d) for a in accts for d in a.devices)
        return True

    def _refused(self, ip: str) -> None:
        self.refused += 1
        now = time.time()
        if now - self._last_refusal_log > 30:
            self._last_refusal_log = now
            logger.warning("[sip] refused a connection from %s (not an allowed tailnet device)", ip)

    def _fail(self, ip: str) -> None:
        now = time.time()
        fails = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW_S] + [now]
        self._fails[ip] = fails
        if len(fails) >= FAIL_LIMIT:
            self._locked[ip] = now + LOCKOUT_S
            self._fails.pop(ip, None)
            logger.warning("[sip] %s failed to sign in %d times: locked out for %d minutes",
                           ip, FAIL_LIMIT, int(LOCKOUT_S // 60))

    def _authenticate(self, req: sip.SipMessage, link: Link) -> Optional[Account]:
        """Gate 2. Answers 401/403 itself and returns None when it fails."""
        ip = link.peer[0]
        if self._locked.get(ip, 0) > time.time():
            self._respond(req, link, 403, "Too many failed sign-ins")
            return None
        found: Dict[str, Account] = {}

        def password_for(user: str) -> Optional[str]:
            a = self._account(user)
            if a:
                found["a"] = a
                return a.password
            return None

        header = req.get("Authorization") or req.get("Proxy-Authorization")
        status, user = self.auth.verify(req.method, header, password_for)
        if status == "ok":
            acct = found["a"]
            if acct.devices and not any(sip.same_ip(ip, d) for d in acct.devices):
                logger.info("[sip] %s signed in as %s from %s, which is not one of its devices: refused",
                            req.method, user, ip)
                self._respond(req, link, 403, "Not an allowed device")
                return None
            self._fails.pop(ip, None)
            return acct
        if status in ("missing", "stale"):
            self._respond(req, link, 401, headers=[("WWW-Authenticate", self.auth.challenge(stale=status == "stale"))])
            return None
        self._fail(ip)
        logger.info("[sip] %s from %s: sign-in failed (%s)", req.method, ip, status)
        if status == "replay":
            self._respond(req, link, 401, headers=[("WWW-Authenticate", self.auth.challenge())])
        else:
            self._respond(req, link, 403, "Wrong username or password")
        return None

    # ── messages in ──

    def _on_data(self, data: bytes, link: Link) -> None:
        if not data.strip():
            return
        if not self._source_ok(link.peer[0]):
            self._refused(link.peer[0])
            # Say no to a request (not to ACKs, not to responses).
            try:
                msg = sip.parse(data)
                if msg.is_request and msg.method != "ACK":
                    self._respond(msg, link, 403, "Forbidden", cache=False)
            except sip.SipError:
                pass
            return
        try:
            msg = sip.parse(data)
        except sip.SipError as e:
            logger.debug("[sip] unparseable message from %s: %s", link.peer[0], e)
            return
        try:
            if msg.is_request:
                self._on_request(msg, link)
            else:
                self._on_response(msg, link)
        except sip.SipError as e:
            if msg.is_request and msg.method != "ACK":
                self._respond(msg, link, 400, str(e)[:60], cache=False)
        except Exception:
            logger.exception("[sip] handling %s failed", msg.method or msg.status)
            if msg.is_request and msg.method != "ACK":
                self._respond(msg, link, 500, cache=False)

    def _respond(self, req: sip.SipMessage, link: Link, status: int, reason: str = "", *, to_tag: str = "",
                 headers: Optional[List[Tuple[str, str]]] = None, body: bytes = b"", cache: bool = True) -> None:
        resp = sip.response(req, status, reason, to_tag=to_tag, headers=headers, body=body, received=link.peer)
        data = resp.encode()
        link.send(data)
        if not cache:
            return
        try:
            num, method = req.cseq
        except sip.SipError:
            return
        key = (req.call_id, num, method, req.branch)
        entry = self._server_tx.get(key) or {"at": time.time()}
        entry.update({"resp": data, "link": link, "status": status})
        self._server_tx[key] = entry
        if method == "INVITE" and status >= 200:
            entry["acked"] = False
            self._invite_tx[(req.call_id, num)] = entry
            if link.kind == "udp":
                if entry.get("task"):
                    entry["task"].cancel()
                entry["task"] = self._bg(self._retransmit(entry))

    async def _retransmit(self, entry: Dict) -> None:
        """A final answer to an INVITE over UDP, again until the ACK."""
        wait, waited = T1, 0.0
        while waited < TX_LIFETIME and not entry.get("acked"):
            await asyncio.sleep(wait)
            waited += wait
            if entry.get("acked"):
                return
            entry["link"].send(entry["resp"])
            wait = min(wait * 2, T2)
        if not entry.get("acked") and entry.get("status", 0) < 300:
            call = self.calls.get(entry.get("call_id", ""))
            if call:
                logger.info("[sip] call %s: the phone never sent ACK, hanging up", call.call_id[:10])
                await self.bye(call, "no ACK")

    def _on_request(self, req: sip.SipMessage, link: Link) -> None:
        num, method = req.cseq
        if method != req.method:
            raise sip.SipError("CSeq method does not match")
        if req.method == "ACK":
            entry = self._invite_tx.get((req.call_id, num))
            if entry:
                entry["acked"] = True
            call = self.calls.get(req.call_id)
            if call and req.body and call.direction == "inbound":
                pass        # an answer in ACK only follows an offerless INVITE, which we refuse
            return
        key = (req.call_id, num, req.method, req.branch)
        prev = self._server_tx.get(key)
        if prev:
            # A retransmission: the same answer again (or, while the first
            # copy is still being handled, nothing yet). Never handled twice,
            # so a resent INVITE is not mistaken for a replayed digest.
            if prev.get("resp"):
                prev["link"].send(prev["resp"])
            return
        self._server_tx[key] = {"at": time.time(), "link": link}
        if req.method == "CANCEL":
            # Calls are answered at once, so there is never an INVITE left to cancel.
            self._respond(req, link, 481)
            return
        if req.method == "OPTIONS":
            self._respond(req, link, 200, headers=[("Allow", sip.ALLOW), ("Accept", "application/sdp")])
            return
        if req.method == "REGISTER":
            self._register(req, link)
            return
        if req.to_tag():
            self._in_dialog(req, link)
            return
        if req.method == "INVITE":
            self._bg(self._invite(req, link))
            return
        if req.method in ("BYE", "INFO", "UPDATE"):
            self._respond(req, link, 481)
            return
        self._respond(req, link, 405, headers=[("Allow", sip.ALLOW)])

    # ── REGISTER ──

    def _register(self, req: sip.SipMessage, link: Link) -> None:
        acct = self._authenticate(req, link)
        if not acct:
            return
        to_uri = sip.parse_name_addr(req.get("To"))[1]
        try:
            if sip.parse_uri(to_uri).user != acct.username:
                self._respond(req, link, 403, "Register your own username")
                return
        except sip.SipError:
            self._respond(req, link, 400, "Bad To")
            return
        try:
            default_exp = int(req.get("Expires") or REG_DEFAULT)
        except ValueError:
            default_exp = REG_DEFAULT
        now = time.time()
        regs = [r for r in self.registrations.get(acct.username, []) if r.expires_at > now]
        contacts = req.get_all("Contact")
        if contacts == ["*"]:
            regs = []
        for c in contacts:
            if c == "*":
                continue
            _, uri, params = sip.parse_name_addr(c)
            try:
                exp = int(params.get("expires", default_exp))
            except ValueError:
                exp = default_exp
            regs = [r for r in regs if r.contact != uri]
            if exp <= 0:
                continue
            exp = max(REG_MIN, min(REG_MAX, exp))
            regs.insert(0, Registration(acct.username, acct.owner, uri, link, now + exp, now))
        regs = regs[:MAX_BINDINGS]
        if regs:
            self.registrations[acct.username] = regs
        else:
            self.registrations.pop(acct.username, None)
        hdrs = [(("Contact"), f"<{r.contact}>;expires={max(0, int(r.expires_at - now))}") for r in regs]
        hdrs.append(("Date", time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())))
        if regs:
            logger.info("[sip] %s registered from %s (%s, %ds)", acct.username, link.peer[0], link.kind,
                        int(regs[0].expires_at - now))
        self._respond(req, link, 200, to_tag=sip.new_tag(), headers=hdrs)

    def registered(self, username: str) -> List[Registration]:
        now = time.time()
        return [r for r in self.registrations.get(username, []) if r.expires_at > now and r.link.alive]

    # ── INVITE (a call coming in) ──

    def _contact(self, link: Link, user: str = "odysseus") -> str:
        ip, port = link.local
        return f"<sip:{user}@{sip.host_for_uri(ip)}:{port};transport={link.kind}>"

    def _media_target(self, offer: sip.Sdp, link: Link) -> Tuple[str, int]:
        addr = offer.addr
        if not addr or addr in ("0.0.0.0", "::") or not (sip.same_ip(addr, link.peer[0]) or self._source_ok(addr)):
            addr = link.peer[0]          # the phone's real address, never an outside one
        return addr, offer.port

    async def _new_stream(self, local_ip: str, pt: int, name: str, dtmf: Optional[int],
                          remote: Tuple[str, int], peer_ip: str) -> rtp.RtpStream:
        allowed = [peer_ip] + ([remote[0]] if not sip.same_ip(remote[0], peer_ip) else [])
        stream = rtp.RtpStream(pt, name, dtmf, remote, allowed_ips=allowed, frame_s=self.frame_s)
        await rtp.open_stream(local_ip, self.rtp_ports, stream, self._rtp_used)
        return stream

    async def _invite(self, req: sip.SipMessage, link: Link) -> None:
        acct = self._authenticate(req, link)
        if not acct:
            return
        if len(self.calls) >= MAX_CALLS or any(c.account.username == acct.username for c in self.calls.values()):
            self._respond(req, link, 486)
            return
        if self.precheck:
            problem = self.precheck(acct)
            if problem:
                logger.info("[sip] call from %s refused: %s", acct.username, problem[1])
                self._respond(req, link, problem[0], problem[1])
                return
        if not req.body:
            self._respond(req, link, 488, "Send an SDP offer")
            return
        try:
            offer = sip.parse_sdp(req.body)
        except sip.SipError:
            self._respond(req, link, 488, "Bad SDP")
            return
        chosen = sip.negotiate(offer)
        if not chosen or not offer.port:
            self._respond(req, link, 488, "Enable PCMU (G.711 mu-law) or PCMA")
            return
        pt, name, dtmf = chosen
        self._respond(req, link, 100)
        try:
            stream = await self._new_stream(link.local[0], pt, name, dtmf, self._media_target(offer, link), link.peer[0])
        except OSError:
            self._respond(req, link, 503, "No free RTP port")
            return
        local_tag = sip.new_tag()
        remote_target = sip.parse_name_addr(req.get("Contact"))[1] or sip.parse_name_addr(req.get("From"))[1]
        call = SipCall(self, call_id=req.call_id, local_hdr=sip.with_tag(req.get("To"), local_tag),
                       remote_hdr=req.get("From"), local_tag=local_tag, remote_tag=req.from_tag(),
                       remote_target=remote_target, link=link, stream=stream, account=acct, direction="inbound")
        stream.paused = sip.answer_direction(offer.direction) in ("recvonly", "inactive")
        self.calls[call.call_id] = call
        call.proto = offer.proto
        body = sip.build_sdp(link.local[0], stream.local[1], [(pt, name)], dtmf, call.session_id,
                             call.sdp_version, sip.answer_direction(offer.direction), offer.proto)
        self._respond(req, link, 200, to_tag=local_tag, body=body,
                      headers=[("Contact", self._contact(link)), ("Allow", sip.ALLOW),
                               ("Content-Type", "application/sdp")])
        self._invite_tx[(req.call_id, req.cseq[0])]["call_id"] = call.call_id
        logger.info("[sip] call from %s (%s) answered, %s", acct.username, link.peer[0], name)
        stream.start()
        self._bg(self.run_call(call))

    async def run_call(self, call: SipCall, handler: Optional[Callable[[SipCall], Awaitable[None]]] = None) -> None:
        """Run `handler` (default: on_call) for an answered call, and hang up
        when it returns."""
        try:
            await (handler or self.on_call)(call)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[sip] call handler failed")
        finally:
            if not call.is_ended:
                await self.bye(call, "call over")

    # ── requests inside a call ──

    def _dialog_for(self, req: sip.SipMessage, link: Link) -> Optional[SipCall]:
        call = self.calls.get(req.call_id)
        if not call or req.to_tag() != call.local_tag or req.from_tag() != call.remote_tag:
            return None
        if not sip.same_ip(link.peer[0], call.peer_ip):
            return None
        return call

    def _in_dialog(self, req: sip.SipMessage, link: Link) -> None:
        call = self._dialog_for(req, link)
        if not call:
            self._respond(req, link, 481)
            return
        if req.method == "BYE":
            self._respond(req, link, 200)
            logger.info("[sip] call %s: the phone hung up", call.call_id[:10])
            call._finish("hangup")
        elif req.method in ("INVITE", "UPDATE"):
            body = b""
            hdrs = [("Contact", self._contact(call.link))]
            if req.body:
                try:
                    offer = sip.parse_sdp(req.body)
                except sip.SipError:
                    self._respond(req, link, 488)
                    return
                chosen = sip.negotiate(offer)
                if not chosen:
                    self._respond(req, link, 488)
                    return
                pt, name, dtmf = chosen
                remote = self._media_target(offer, link)
                if remote != call.rtp.remote:
                    call.rtp.remote = remote
                    call.rtp._latched = False
                    if not any(sip.same_ip(remote[0], a) for a in call.rtp.allowed_ips):
                        call.rtp.allowed_ips.append(remote[0])
                direction = sip.answer_direction(offer.direction)
                call.rtp.paused = direction in ("recvonly", "inactive")
                call.rtp.last_rx = time.monotonic()
                call.sdp_version += 1
                body = sip.build_sdp(call.link.local[0], call.rtp.local[1], [(call.rtp.pt, call.rtp.codec)],
                                     call.rtp.dtmf_pt, call.session_id, call.sdp_version, direction, offer.proto)
                hdrs.append(("Content-Type", "application/sdp"))
            self._respond(req, link, 200, headers=hdrs, body=body)
        elif req.method == "INFO":
            digit = _info_digit(req)
            self._respond(req, link, 200)
            if digit and call.on_dtmf:
                call.on_dtmf(digit)
        else:
            self._respond(req, link, 405, headers=[("Allow", sip.ALLOW)])

    # ── requests we send ──

    def _via(self, link: Link, branch: str) -> str:
        ip, port = link.local
        return f"SIP/2.0/{link.kind.upper()} {sip.host_for_uri(ip)}:{port};branch={branch};rport"

    def _request(self, method: str, uri: str, link: Link, *, call_id: str, cseq: int, from_hdr: str,
                 to_hdr: str, branch: str, body: bytes = b"", headers: Optional[List[Tuple[str, str]]] = None,
                 cseq_method: str = "") -> sip.SipMessage:
        m = sip.SipMessage(method=method, uri=uri)
        m.add("Via", self._via(link, branch))
        m.add("Max-Forwards", "70")
        m.add("From", from_hdr)
        m.add("To", to_hdr)
        m.add("Call-ID", call_id)
        m.add("CSeq", f"{cseq} {cseq_method or method}")
        m.add("User-Agent", sip.SERVER_NAME)
        for k, v in headers or []:
            m.add(k, v)
        m.body = body
        return m

    async def _transact(self, msg: sip.SipMessage, link: Link, timeout: float,
                        on_provisional: Optional[Callable[[sip.SipMessage], None]] = None) -> Optional[sip.SipMessage]:
        """Send a request and wait for its final response (None on timeout).
        Over UDP it is sent again (T1, doubling) until anything comes back."""
        branch = msg.branch
        tx = _ClientTx(msg.method)
        self._client_tx[branch] = tx
        data = msg.encode()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        wait = T1
        try:
            link.send(data)
            next_send = loop.time() + wait
            while True:
                now = loop.time()
                if now >= deadline:
                    return None
                until = min(deadline, next_send) if (link.kind == "udp" and not tx.got_response.is_set()) else deadline
                try:
                    resp = await asyncio.wait_for(tx.q.get(), max(0.01, until - now))
                except asyncio.TimeoutError:
                    if link.kind == "udp" and not tx.got_response.is_set() and loop.time() >= next_send:
                        link.send(data)
                        wait = min(wait * 2, T2)
                        next_send = loop.time() + wait
                    continue
                if resp.status < 200:
                    if on_provisional:
                        on_provisional(resp)
                    continue
                return resp
        finally:
            self._client_tx.pop(branch, None)

    def _on_response(self, resp: sip.SipMessage, link: Link) -> None:
        tx = self._client_tx.get(resp.branch)
        if tx:
            tx.got_response.set()
            tx.q.put_nowait(resp)
            return
        # A 200 OK to our INVITE sent again: our ACK got lost.
        try:
            num, method = resp.cseq
        except sip.SipError:
            return
        call = self.calls.get(resp.call_id)
        if call and method == "INVITE" and 200 <= resp.status < 300 and call.ack_bytes:
            call.link.send(call.ack_bytes)

    async def bye(self, call: SipCall, reason: str = "") -> None:
        """Hang up from our side."""
        if call.is_ended or call.closing:
            return
        call.closing = True
        call.rtp.close()
        if call.link.alive:
            call.local_cseq += 1
            msg = self._request("BYE", call.remote_target or sip.parse_name_addr(call.remote_hdr)[1], call.link,
                                call_id=call.call_id, cseq=call.local_cseq, from_hdr=call.local_hdr,
                                to_hdr=call.remote_hdr, branch=sip.new_branch())
            try:
                await self._transact(msg, call.link, 4.0)
            except Exception:
                pass
        logger.info("[sip] call %s ended (%s)", call.call_id[:10], reason or "hung up")
        call._finish(reason or "hung up")

    def _forget(self, call: SipCall) -> None:
        self.calls.pop(call.call_id, None)
        self._rtp_used.discard((call.link.local[0], call.rtp.local[1]))

    # ── calling a registered phone ──

    async def call_out(self, username: str, ring_s: float = RING_S) -> SipCall:
        """Ring `username`'s registered softphone. Returns the answered call;
        raises LookupError (not registered) or RuntimeError (no answer)."""
        acct = self._account(username)
        if not acct:
            raise LookupError("The SIP line is off or has no account.")
        regs = self.registered(username)
        if not regs:
            raise LookupError("No softphone is registered right now. Open it and check that it shows Registered.")
        if len(self.calls) >= MAX_CALLS or any(c.account.username == username for c in self.calls.values()):
            raise RuntimeError("A call is already on the line.")
        reg = regs[0]
        link = reg.link
        if link.kind == "udp":
            lst = self._udp.get(link.local[0])
            if not lst or not lst.transport:
                raise LookupError("The SIP listener for that phone is closed.")
        local_ip = link.local[0]
        stream = await self._new_stream(local_ip, 0, "PCMU", sip.DTMF_PT, (link.peer[0], 0), link.peer[0])
        call_id = sip.new_call_id(local_ip)
        local_tag = sip.new_tag()
        host = sip.host_for_uri(local_ip)
        from_hdr = f'"{acct.display}" <sip:odysseus@{host}>;tag={local_tag}'
        to_hdr = f"<sip:{username}@{host}>"
        session_id = secrets.randbelow(2 ** 31)
        offer = sip.build_sdp(local_ip, stream.local[1], [(0, "PCMU"), (8, "PCMA")], sip.DTMF_PT, session_id, 1)
        branch = sip.new_branch()
        invite = self._request("INVITE", reg.contact, link, call_id=call_id, cseq=1, from_hdr=from_hdr,
                               to_hdr=to_hdr, branch=branch, body=offer,
                               headers=[("Contact", self._contact(link)), ("Allow", sip.ALLOW),
                                        ("Content-Type", "application/sdp")])
        rang = []
        try:
            resp = await self._transact(invite, link, ring_s, on_provisional=lambda r: rang.append(r.status))
        except BaseException:
            stream.close()
            self._rtp_used.discard((local_ip, stream.local[1]))
            raise
        if resp is None:
            cancel = self._request("CANCEL", reg.contact, link, call_id=call_id, cseq=1, from_hdr=from_hdr,
                                   to_hdr=to_hdr, branch=branch, cseq_method="CANCEL")
            self._client_tx.pop(branch, None)
            cancel_tx = _ClientTx("CANCEL")
            self._client_tx[branch] = cancel_tx
            link.send(cancel.encode())
            try:
                # The 200 to CANCEL and the 487 to the INVITE share the branch.
                end = asyncio.get_running_loop().time() + 4
                while asyncio.get_running_loop().time() < end:
                    r = await asyncio.wait_for(cancel_tx.q.get(), end - asyncio.get_running_loop().time())
                    if r.cseq[1] == "INVITE" and r.status >= 300:
                        self._ack_failure(invite, r, link)
                        break
            except (asyncio.TimeoutError, sip.SipError):
                pass
            finally:
                self._client_tx.pop(branch, None)
            stream.close()
            self._rtp_used.discard((local_ip, stream.local[1]))
            raise RuntimeError("No answer." if rang else "The softphone did not respond.")
        if resp.status >= 300:
            self._ack_failure(invite, resp, link)
            stream.close()
            self._rtp_used.discard((local_ip, stream.local[1]))
            raise RuntimeError("Declined." if resp.status in (486, 603) else f"The softphone said {resp.status} {resp.reason}.")
        # Answered.
        remote_tag = resp.to_tag()
        remote_hdr = resp.get("To")
        target = sip.parse_name_addr(resp.get("Contact"))[1] or reg.contact
        ack = self._request("ACK", target, link, call_id=call_id, cseq=1, from_hdr=from_hdr, to_hdr=remote_hdr,
                            branch=sip.new_branch())
        call = SipCall(self, call_id=call_id, local_hdr=from_hdr, remote_hdr=remote_hdr, local_tag=local_tag,
                       remote_tag=remote_tag, remote_target=target, link=link, stream=stream, account=acct,
                       direction="outbound", local_cseq=1, session_id=session_id)
        call.ack_bytes = ack.encode()
        link.send(call.ack_bytes)
        try:
            answer = sip.parse_sdp(resp.body)
            chosen = sip.negotiate(answer)
        except sip.SipError:
            chosen = None
        self.calls[call_id] = call
        if not chosen or not answer.port:
            await self.bye(call, "no common codec")
            raise RuntimeError("The softphone has neither PCMU nor PCMA turned on.")
        stream.pt, stream.codec, stream.dtmf_pt = chosen
        stream.silence = rtp.SILENCE_PCMA if stream.codec == "PCMA" else rtp.codec.SILENCE
        stream.jitter.silence = stream.silence
        remote = self._media_target(answer, link)
        stream.remote = remote
        if not any(sip.same_ip(remote[0], a) for a in stream.allowed_ips):
            stream.allowed_ips.append(remote[0])
        stream.last_rx = time.monotonic()
        stream.start()
        logger.info("[sip] %s answered the call, %s", username, stream.codec)
        return call

    def _ack_failure(self, invite: sip.SipMessage, resp: sip.SipMessage, link: Link) -> None:
        """ACK for a non-2xx answer: same branch, the To of the response."""
        ack = sip.SipMessage(method="ACK", uri=invite.uri)
        ack.add("Via", invite.get("Via"))
        ack.add("Max-Forwards", "70")
        ack.add("From", invite.get("From"))
        ack.add("To", resp.get("To"))
        ack.add("Call-ID", invite.call_id)
        ack.add("CSeq", f"{invite.cseq[0]} ACK")
        link.send(ack.encode())


def _info_digit(req: sip.SipMessage) -> str:
    """A keypad press sent as SIP INFO (application/dtmf-relay or dtmf)."""
    ctype = req.get("Content-Type").lower()
    text = req.body.decode("utf-8", "replace").strip()
    if "dtmf-relay" in ctype:
        for ln in text.splitlines():
            k, _, v = ln.partition("=")
            if k.strip().lower() == "signal":
                v = v.strip()
                return v if len(v) == 1 and v in rtp.DTMF_EVENTS else ""
        return ""
    if ctype.startswith("application/dtmf"):
        return text if len(text) == 1 and text in rtp.DTMF_EVENTS else ""
    return ""
