"""A softphone for testing the free SIP line without a phone.

Speaks to src/telephony/sip_server.py the way Linphone or Zoiper would:
REGISTER and INVITE with digest auth (answering the 401), an SDP offer,
ACK, RTP audio in 20 ms G.711 packets on a real clock, RFC 4733 keypad
presses, BYE, and it answers when the server rings it ("Call me"). It
records what the server sent back (audio, BYE). Used by
tests/test_sip_line.py, and by hand against a test-mode preview
(ODYSSEUS_SIP_TEST_LOOPBACK=1 ODYSSEUS_SIP_PORT=15060):

    ph = await Softphone(("127.0.0.1", 15060), "me", "password").open()
    await ph.register(); r = await ph.invite(); await ph.send_audio(...); await ph.bye()
"""

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from src.telephony import codec, rtp, sip


@dataclass
class Received:
    audio: bytearray = field(default_factory=bytearray)      # mu-law from the server
    packets: int = 0
    first_audio_at: float = 0.0
    last_audio_at: float = 0.0
    bye: bool = False
    dtmf: List[str] = field(default_factory=list)


class _SipSock(asyncio.DatagramProtocol):
    def __init__(self, phone: "Softphone"):
        self.phone = phone
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.phone._incoming(data, addr)


class _RtpSock(asyncio.DatagramProtocol):
    def __init__(self, phone: "Softphone"):
        self.phone = phone
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.phone._rtp_in(data, addr)


class Softphone:
    def __init__(self, server: Tuple[str, int], username: str, password: str, *,
                 local_ip: str = "127.0.0.1", transport: str = "udp", codecs=((0, "PCMU"),),
                 dtmf_pt: Optional[int] = 101, realm_user: str = ""):
        self.server = server
        self.username = username
        self.password = password
        self.local_ip = local_ip
        self.kind = transport
        self.codecs = list(codecs)
        self.dtmf_pt = dtmf_pt
        self.rx = Received()
        self.sip_port = 0
        self.rtp_port = 0
        self._sip = None
        self._rtp = None
        self._reader = None
        self._writer = None
        self._framer = sip.StreamFramer()
        self._waiters: Dict[str, asyncio.Queue] = {}
        self.requests: List[sip.SipMessage] = []     # requests the server sent us
        self.call_id = ""
        self.local_tag = ""
        self.remote_tag = ""
        self.remote_hdr = ""
        self.remote_target = ""
        self.cseq = 0
        self.remote_media: Optional[Tuple[str, int]] = None
        self.pt = 0
        self.seq = secrets.randbits(16)
        self.ts = secrets.randbits(32)
        self.ssrc = secrets.randbits(32)
        self.answer: Optional[sip.SipMessage] = None
        self.last_request: Optional[sip.SipMessage] = None
        self.local_hdr = ""
        self.auto_answer = True
        self.ringing = asyncio.Event()
        self.answered = asyncio.Event()
        self.ended = asyncio.Event()
        self._reader_task = None

    # ── sockets ──

    async def open(self) -> "Softphone":
        loop = asyncio.get_running_loop()
        if self.kind == "udp":
            t, p = await loop.create_datagram_endpoint(lambda: _SipSock(self), local_addr=(self.local_ip, 0))
            self._sip = p
            self.sip_port = t.get_extra_info("sockname")[1]
        else:
            self._reader, self._writer = await asyncio.open_connection(self.server[0], self.server[1],
                                                                       local_addr=(self.local_ip, 0))
            self.sip_port = self._writer.get_extra_info("sockname")[1]
            self._reader_task = asyncio.create_task(self._read_tcp())
        t, p = await loop.create_datagram_endpoint(lambda: _RtpSock(self), local_addr=(self.local_ip, 0))
        self._rtp = p
        self.rtp_port = t.get_extra_info("sockname")[1]
        return self

    async def _read_tcp(self):
        try:
            while True:
                data = await self._reader.read(65536)
                if not data:
                    break
                for raw in self._framer.feed(data):
                    if raw != b"\r\n\r\n":
                        self._incoming(raw, self.server)
        except (ConnectionError, asyncio.CancelledError):
            pass

    def close(self):
        if self._sip and self._sip.transport:
            self._sip.transport.close()
        if self._rtp and self._rtp.transport:
            self._rtp.transport.close()
        if self._writer:
            self._writer.close()
        if self._reader_task:
            self._reader_task.cancel()

    def send_raw(self, data: bytes) -> None:
        if self.kind == "udp":
            self._sip.transport.sendto(data, self.server)
        else:
            self._writer.write(data)

    # ── SIP ──

    @property
    def host(self) -> str:
        return sip.host_for_uri(self.server[0])

    def _contact(self) -> str:
        return f"<sip:{self.username}@{sip.host_for_uri(self.local_ip)}:{self.sip_port};transport={self.kind}>"

    def _build(self, method: str, uri: str, *, call_id: str, cseq: int, from_hdr: str, to_hdr: str,
               body: bytes = b"", extra: Optional[List[Tuple[str, str]]] = None, branch: str = "") -> sip.SipMessage:
        m = sip.SipMessage(method=method, uri=uri)
        m.add("Via", f"SIP/2.0/{self.kind.upper()} {sip.host_for_uri(self.local_ip)}:{self.sip_port};"
                     f"branch={branch or sip.new_branch()};rport")
        m.add("Max-Forwards", "70")
        m.add("From", from_hdr)
        m.add("To", to_hdr)
        m.add("Call-ID", call_id)
        m.add("CSeq", f"{cseq} {method}")
        m.add("Contact", self._contact())
        m.add("User-Agent", "OdysseusTestPhone")
        for k, v in extra or []:
            m.add(k, v)
        m.body = body
        return m

    async def send(self, msg: sip.SipMessage, timeout: float = 5.0, want_final: bool = True) -> sip.SipMessage:
        """Send a request, return its final response."""
        q: asyncio.Queue = asyncio.Queue()
        self._waiters[msg.branch] = q
        self.send_raw(msg.encode())
        try:
            end = time.monotonic() + timeout
            while True:
                resp = await asyncio.wait_for(q.get(), max(0.01, end - time.monotonic()))
                if resp.status >= 200 or not want_final:
                    return resp
                if resp.status == 180:
                    self.ringing.set()
        finally:
            self._waiters.pop(msg.branch, None)

    def _authorize(self, msg: sip.SipMessage, challenge: str, nc: int = 1, password: str = "") -> sip.SipMessage:
        f = sip.parse_auth(challenge)
        cnonce = secrets.token_hex(8)
        nc_s = f"{nc:08x}"
        resp = sip.digest_response(self.username, f["realm"], password or self.password, msg.method, msg.uri,
                                   f["nonce"], "auth", nc_s, cnonce)
        msg.set("Authorization", f'Digest username="{self.username}", realm="{f["realm"]}", nonce="{f["nonce"]}", '
                                 f'uri="{msg.uri}", response="{resp}", algorithm=MD5, qop=auth, nc={nc_s}, '
                                 f'cnonce="{cnonce}"')
        return msg

    async def _with_auth(self, make, timeout: float = 5.0, password: str = "") -> sip.SipMessage:
        """make(cseq) -> request. Sends it, answers one 401 with credentials."""
        self.cseq += 1
        first = make(self.cseq)
        self.last_request = first
        r = await self.send(first, timeout)
        if r.status != 401:
            return r
        if first.method == "INVITE":
            self._ack_non2xx(first, r)
        self.cseq += 1
        again = self._authorize(make(self.cseq), r.get("WWW-Authenticate"), password=password)
        self.last_request = again
        return await self.send(again, timeout)

    def _ack_non2xx(self, invite: sip.SipMessage, resp: sip.SipMessage) -> None:
        ack = sip.SipMessage(method="ACK", uri=invite.uri)
        ack.add("Via", invite.get("Via"))
        ack.add("Max-Forwards", "70")
        ack.add("From", invite.get("From"))
        ack.add("To", resp.get("To"))
        ack.add("Call-ID", invite.call_id)
        ack.add("CSeq", f"{invite.cseq[0]} ACK")
        self.send_raw(ack.encode())

    async def register(self, expires: int = 600, password: str = "") -> sip.SipMessage:
        call_id = sip.new_call_id(self.local_ip)
        tag = sip.new_tag()
        aor = f"<sip:{self.username}@{self.host}>"
        return await self._with_auth(lambda n: self._build(
            "REGISTER", f"sip:{self.host}", call_id=call_id, cseq=n, from_hdr=f"{aor};tag={tag}", to_hdr=aor,
            extra=[("Expires", str(expires))]), password=password)

    async def options(self) -> sip.SipMessage:
        self.cseq += 1
        return await self.send(self._build("OPTIONS", f"sip:{self.host}", call_id=sip.new_call_id(self.local_ip),
                                           cseq=self.cseq, from_hdr=f"<sip:{self.username}@{self.host}>;tag={sip.new_tag()}",
                                           to_hdr=f"<sip:odysseus@{self.host}>"))

    def offer(self) -> bytes:
        return sip.build_sdp(self.local_ip, self.rtp_port, self.codecs, self.dtmf_pt, 1234, 1)

    async def invite(self, password: str = "", body: Optional[bytes] = None, timeout: float = 8.0) -> sip.SipMessage:
        """Call sip:odysseus@server. Returns the final response; on 200 the
        call is up (ACK sent, media flowing)."""
        self.call_id = sip.new_call_id(self.local_ip)
        self.local_tag = sip.new_tag()
        uri = f"sip:odysseus@{self.host}:{self.server[1]}"
        from_hdr = f'"Test" <sip:{self.username}@{self.host}>;tag={self.local_tag}'
        to_hdr = f"<sip:odysseus@{self.host}>"
        offer = self.offer() if body is None else body
        r = await self._with_auth(lambda n: self._build(
            "INVITE", uri, call_id=self.call_id, cseq=n, from_hdr=from_hdr, to_hdr=to_hdr, body=offer,
            extra=[("Content-Type", "application/sdp")]), timeout=timeout, password=password)
        self.answer = r
        if 200 <= r.status < 300:
            self.remote_tag = r.to_tag()
            self.remote_hdr = r.get("To")
            self.local_hdr = from_hdr
            self.remote_target = sip.parse_name_addr(r.get("Contact"))[1] or uri
            self._start_media(r.body)
            ack = self._build("ACK", self.remote_target, call_id=self.call_id, cseq=self.cseq,
                              from_hdr=from_hdr, to_hdr=self.remote_hdr)
            self.send_raw(ack.encode())
            self.answered.set()
        elif r.status >= 300:
            self._ack_non2xx(self.last_request, r)
        return r

    def _start_media(self, body: bytes) -> None:
        sdp = sip.parse_sdp(body)
        chosen = sip.negotiate(sdp)
        self.pt = chosen[0] if chosen else 0
        self.remote_media = (sdp.addr, sdp.port)

    async def bye(self) -> sip.SipMessage:
        self.cseq += 1
        m = self._build("BYE", self.remote_target, call_id=self.call_id, cseq=self.cseq,
                        from_hdr=self.local_hdr, to_hdr=self.remote_hdr)
        r = await self.send(m)
        self.ended.set()
        return r

    # ── incoming ──

    def _incoming(self, data: bytes, addr) -> None:
        try:
            msg = sip.parse(data)
        except sip.SipError:
            return
        if not msg.is_request:
            q = self._waiters.get(msg.branch)
            if q:
                q.put_nowait(msg)
            return
        self.requests.append(msg)
        if msg.method == "BYE":
            self.rx.bye = True
            self._reply(msg, 200)
            self.ended.set()
        elif msg.method == "INVITE" and not msg.to_tag():
            self._reply(msg, 180)
            self.ringing.set()
            if self.auto_answer:
                asyncio.get_running_loop().call_later(0.2, self._answer_incoming, msg)
        elif msg.method == "CANCEL":
            self._reply(msg, 200)
        elif msg.method == "ACK":
            pass
        else:
            self._reply(msg, 200)

    def _reply(self, req: sip.SipMessage, status: int, body: bytes = b"", extra=None) -> None:
        tag = self.local_tag or sip.new_tag()
        hdrs = [("Contact", self._contact())] + list(extra or [])
        resp = sip.response(req, status, to_tag=tag, headers=hdrs, body=body)
        self.send_raw(resp.encode())

    def _answer_incoming(self, invite: sip.SipMessage) -> None:
        self.call_id = invite.call_id
        self.local_tag = sip.new_tag()
        self.remote_tag = invite.from_tag()
        self.remote_hdr = invite.get("From")
        self.local_hdr = sip.with_tag(invite.get("To"), self.local_tag)
        self.remote_target = sip.parse_name_addr(invite.get("Contact"))[1]
        self._start_media(invite.body)
        self._reply(invite, 200, self.offer(), [("Content-Type", "application/sdp")])
        self.answered.set()

    def _rtp_in(self, data: bytes, addr) -> None:
        pkt = rtp.unpack(data)
        if not pkt:
            return
        if self.dtmf_pt is not None and pkt.pt == self.dtmf_pt:
            ev = rtp.dtmf_event(pkt.payload)
            if ev:
                self.rx.dtmf.append(ev[0])
            return
        self.rx.packets += 1
        payload = codec.alaw_to_ulaw(pkt.payload) if pkt.pt == 8 else pkt.payload
        if payload.strip(b"\xff\x7f"):
            now = time.monotonic()
            self.rx.first_audio_at = self.rx.first_audio_at or now
            self.rx.last_audio_at = now
            self.rx.audio.extend(payload)

    # ── media out ──

    def _rtp_send(self, payload: bytes, pt: Optional[int] = None, marker: bool = False, ts: Optional[int] = None):
        if not self.remote_media:
            return
        self._rtp.transport.sendto(rtp.pack(self.pt if pt is None else pt, self.seq,
                                            self.ts if ts is None else ts, self.ssrc, payload, marker),
                                   self.remote_media)
        self.seq = (self.seq + 1) & 0xFFFF

    async def send_audio(self, samples: np.ndarray, pace: float = 1.0) -> None:
        """int16 at 8 kHz, as 20 ms packets on a real clock (pace < 1 is faster)."""
        ulaw = codec.pcm16_to_ulaw(samples)
        loop = asyncio.get_running_loop()
        start = loop.time()
        for i, fr in enumerate(codec.frames(ulaw)):
            payload = codec.ulaw_to_alaw(fr) if self.pt == 8 else fr
            self._rtp_send(payload)
            self.ts = (self.ts + rtp.SAMPLES) & 0xFFFFFFFF
            delay = start + (i + 1) * 0.02 * pace - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    async def press(self, key: str) -> None:
        ts = self.ts
        for i in range(1, 4):
            self._rtp_send(rtp.dtmf_payload(key, False, i * 160), pt=self.dtmf_pt, marker=i == 1, ts=ts)
            await asyncio.sleep(0.02)
        for _ in range(3):
            self._rtp_send(rtp.dtmf_payload(key, True, 640), pt=self.dtmf_pt, ts=ts)
        self.ts = (self.ts + 640) & 0xFFFFFFFF

    async def wait_quiet(self, since: int = 0, timeout: float = 10.0, quiet: float = 0.6) -> int:
        """Wait until more than `since` mu-law bytes have come and the
        server has then been quiet for `quiet` seconds. Returns the total."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.ended.is_set():
            await asyncio.sleep(0.05)
            if len(self.rx.audio) > since and time.monotonic() - self.rx.last_audio_at > quiet:
                break
        return len(self.rx.audio)

    async def wait_reply(self, timeout: float = 10.0, quiet: float = 0.6) -> int:
        """Wait until the server has sent some audio and then gone quiet.
        Returns how many mu-law bytes came."""
        seen = len(self.rx.audio)
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.ended.is_set():
            await asyncio.sleep(0.05)
            if len(self.rx.audio) > seen and time.monotonic() - self.rx.last_audio_at > quiet:
                break
        return len(self.rx.audio) - seen
