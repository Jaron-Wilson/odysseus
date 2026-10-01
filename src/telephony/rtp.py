"""RTP for the free SIP line: G.711 audio in 20 ms packets, both ways.

    pack / unpack     RFC 3550 headers (CSRCs, extensions and padding read)
    JitterBuffer      puts packets back in order, drops duplicates and late
                      ones, and fills a lost packet with silence, so the call's
                      endpointing (which counts frames, not wall time) sees
                      a steady 20 ms stream even over a jumpy Wi-Fi link
    dtmf_event        RFC 4733 telephone-event payloads (keypad presses)
    RtpStream         one call's socket: decodes what comes in to mu-law for
                      src/telephony/call.py, and sends what it is given at
                      exactly 20 ms a packet on an absolute clock (silence
                      in between), reporting each playback mark once its
                      last packet is out

Only packets from the peer's address are accepted; anything else on the port
is dropped.
"""

import asyncio
import collections
import logging
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Tuple

from src.telephony import codec, sip

logger = logging.getLogger(__name__)

FRAME_S = codec.FRAME_MS / 1000
SAMPLES = codec.FRAME_BYTES               # 160 samples (and bytes) per 20 ms at 8 kHz
DTMF_EVENTS = "0123456789*#ABCD"
SILENCE_PCMA = b"\xd5"
MAX_LOST_FILL = 10                        # at most 200 ms of filled-in silence per gap
GAP_FILL_S = 0.25                         # no audio for this long: count it as silence


@dataclass
class RtpPacket:
    pt: int
    seq: int
    ts: int
    ssrc: int
    marker: bool
    payload: bytes


def pack(pt: int, seq: int, ts: int, ssrc: int, payload: bytes, marker: bool = False) -> bytes:
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | (pt & 0x7F),
                       seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF) + payload


def unpack(data: bytes) -> Optional[RtpPacket]:
    if len(data) < 12:
        return None
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
    if b0 >> 6 != 2:
        return None
    pos = 12 + 4 * (b0 & 0x0F)
    if b0 & 0x10:                                  # header extension
        if len(data) < pos + 4:
            return None
        pos += 4 + 4 * struct.unpack("!H", data[pos + 2:pos + 4])[0]
    end = len(data)
    if b0 & 0x20:                                  # padding
        if end == 0 or data[-1] > end - pos:
            return None
        end -= data[-1]
    if pos > end:
        return None
    return RtpPacket(b1 & 0x7F, seq, ts, ssrc, bool(b1 & 0x80), data[pos:end])


def seq_diff(a: int, b: int) -> int:
    """a - b for 16-bit sequence numbers, across the wrap."""
    d = (a - b) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


def dtmf_event(payload: bytes) -> Optional[Tuple[str, bool, int]]:
    """(key, end of event, duration in samples) from a telephone-event payload."""
    if len(payload) < 4:
        return None
    event, flags, duration = payload[0], payload[1], struct.unpack("!H", payload[2:4])[0]
    if event >= len(DTMF_EVENTS):
        return None
    return DTMF_EVENTS[event], bool(flags & 0x80), duration


def dtmf_payload(key: str, end: bool, duration: int, volume: int = 10) -> bytes:
    return struct.pack("!BBH", DTMF_EVENTS.index(key), (0x80 if end else 0) | (volume & 0x3F), duration)


class JitterBuffer:
    """Reorders audio packets within `depth` packets. push() returns the
    payloads now ready, oldest first; a gap that is still open once `depth`
    later packets are in counts as lost and becomes silence."""

    def __init__(self, depth: int = 3, silence: bytes = codec.SILENCE):
        self.depth = depth
        self.silence = silence
        self.next_seq: Optional[int] = None
        self.held: dict = {}
        self.lost = 0
        self.late = 0
        self.dupes = 0

    def push(self, seq: int, payload: bytes) -> List[bytes]:
        if self.next_seq is None:
            self.next_seq = seq
        d = seq_diff(seq, self.next_seq)
        if abs(d) > 1000:                  # the phone restarted its sequence
            out = self.flush()
            self.next_seq = seq
            self.held[seq] = payload
            return out + self._drain()
        if d < 0:
            self.late += 1
            return []
        if seq in self.held:
            self.dupes += 1
            return []
        self.held[seq] = payload
        return self._drain()

    def _drain(self) -> List[bytes]:
        out: List[bytes] = []
        while self.held:
            if self.next_seq in self.held:
                out.append(self.held.pop(self.next_seq))
                self.next_seq = (self.next_seq + 1) & 0xFFFF
                continue
            if len(self.held) < self.depth:
                break
            # The packet we wait for is not coming: skip to the oldest held.
            oldest = min(self.held, key=lambda s: seq_diff(s, self.next_seq))
            gap = seq_diff(oldest, self.next_seq)
            self.lost += gap
            size = len(self.held[oldest]) or SAMPLES
            out.extend([self.silence * size] * min(gap, MAX_LOST_FILL))
            self.next_seq = oldest
        return out

    def flush(self) -> List[bytes]:
        out = []
        for s in sorted(self.held, key=lambda s: seq_diff(s, self.next_seq or s)):
            out.append(self.held[s])
        self.held.clear()
        return out


class RtpStream(asyncio.DatagramProtocol):
    """One call's RTP socket."""

    def __init__(self, pt: int, codec_name: str, dtmf_pt: Optional[int], remote: Tuple[str, int], *,
                 on_audio: Optional[Callable[[bytes], None]] = None,
                 on_dtmf: Optional[Callable[[str], None]] = None,
                 on_played: Optional[Callable[[str], None]] = None,
                 allowed_ips: Optional[List[str]] = None, frame_s: float = FRAME_S):
        self.pt = pt
        self.codec = codec_name
        self.dtmf_pt = dtmf_pt
        self.remote = remote
        self.allowed_ips = list(allowed_ips or [remote[0]])
        self.on_audio = on_audio
        self.on_dtmf = on_dtmf
        self.on_played = on_played
        self.frame_s = frame_s
        self.silence = SILENCE_PCMA if codec_name == "PCMA" else codec.SILENCE
        self.jitter = JitterBuffer(silence=self.silence)
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.local: Tuple[str, int] = ("", 0)
        self.ssrc = secrets.randbits(32)
        self.seq = secrets.randbits(16)
        self.ts = secrets.randbits(32)
        self._out: Deque[Tuple[bytes, Optional[str]]] = collections.deque()
        self._sender: Optional[asyncio.Task] = None
        self._talking = False
        self._latched = False
        self._dtmf_seen: Deque[int] = collections.deque(maxlen=16)
        self.sent = 0
        self.received = 0
        self.dropped = 0
        self.last_rx = time.monotonic()
        self.last_audio_rx = 0.0
        self.paused = False                 # on hold: keep the clock, send nothing

    # ── socket ──

    def connection_made(self, transport) -> None:
        self.transport = transport
        sock = transport.get_extra_info("sockname")
        if sock:
            self.local = (sock[0], sock[1])

    def datagram_received(self, data: bytes, addr) -> None:
        if not any(sip.same_ip(addr[0], ip) for ip in self.allowed_ips):
            self.dropped += 1
            return
        pkt = unpack(data)
        if pkt is None:
            return
        if not self._latched:
            # Symmetric RTP: answer to where the phone really sends from.
            self.remote = (addr[0], addr[1])
            self._latched = True
        self.last_rx = time.monotonic()
        self.received += 1
        if self.dtmf_pt is not None and pkt.pt == self.dtmf_pt:
            ev = dtmf_event(pkt.payload)
            # One press is several packets with the same timestamp.
            if ev and pkt.ts not in self._dtmf_seen:
                self._dtmf_seen.append(pkt.ts)
                if self.on_dtmf:
                    self.on_dtmf(ev[0])
            return
        if pkt.pt != self.pt:
            return
        self.last_audio_rx = self.last_rx
        for payload in self.jitter.push(pkt.seq, pkt.payload):
            if self.on_audio and payload:
                self.on_audio(codec.alaw_to_ulaw(payload) if self.codec == "PCMA" else payload)

    def error_received(self, exc) -> None:
        logger.debug("[sip] rtp socket error: %s", type(exc).__name__)

    def connection_lost(self, exc) -> None:
        self.transport = None

    # ── sending ──

    def start(self) -> None:
        if self._sender is None:
            self._sender = asyncio.create_task(self._send_loop())

    def play(self, ulaw: bytes, mark: str = "") -> None:
        """Queue mu-law audio; `mark` is reported once its last packet is sent."""
        frames = list(codec.frames(ulaw))
        for i, fr in enumerate(frames):
            payload = codec.ulaw_to_alaw(fr) if self.codec == "PCMA" else fr
            self._out.append((payload, mark if i == len(frames) - 1 else None))
        if not frames and mark and self.on_played:
            self.on_played(mark)

    def clear(self) -> None:
        self._out.clear()

    @property
    def queued_frames(self) -> int:
        return len(self._out)

    def _send(self, payload: bytes, marker: bool = False, pt: Optional[int] = None) -> None:
        if self.transport is None:
            return
        self.transport.sendto(pack(self.pt if pt is None else pt, self.seq, self.ts, self.ssrc, payload, marker),
                              self.remote)
        self.seq = (self.seq + 1) & 0xFFFF
        self.sent += 1

    async def _send_loop(self) -> None:
        """One packet every 20 ms on an absolute schedule (no drift). If the
        loop was held up for long, catch up by restarting the schedule rather
        than bursting a backlog at the phone."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        n = 0
        while self.transport is not None:
            n += 1
            due = start + n * self.frame_s
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -5 * self.frame_s:
                start, n = loop.time(), 0
            if self._out:
                payload, mark = self._out.popleft()
                if not self.paused:
                    self._send(payload, marker=not self._talking)
                self._talking = True
                if mark and self.on_played:
                    try:
                        self.on_played(mark)
                    except Exception:
                        logger.debug("[sip] mark callback failed", exc_info=True)
            else:
                if not self.paused:
                    self._send(self.silence * SAMPLES)
                self._talking = False
            self.ts = (self.ts + SAMPLES) & 0xFFFFFFFF
            # A phone with silence suppression sends nothing while you are
            # quiet; the endpointer counts frames, so it would never hear
            # the pause that ends your turn. Stand in for the missing frames.
            if (self.last_audio_rx and self.on_audio
                    and time.monotonic() - self.last_audio_rx > GAP_FILL_S):
                try:
                    self.on_audio(codec.SILENCE * SAMPLES)
                except Exception:
                    logger.debug("[sip] audio callback failed", exc_info=True)

    async def send_dtmf(self, key: str) -> None:
        """A keypad press as RFC 4733 events (for tests and outbound calls)."""
        if self.dtmf_pt is None:
            return
        ts = self.ts
        for i in range(1, 4):
            self._send_event(key, False, i * SAMPLES, ts, marker=(i == 1))
            await asyncio.sleep(self.frame_s)
        for _ in range(3):
            self._send_event(key, True, 4 * SAMPLES, ts)

    def _send_event(self, key: str, end: bool, duration: int, ts: int, marker: bool = False) -> None:
        if self.transport is None:
            return
        self.transport.sendto(pack(self.dtmf_pt, self.seq, ts, self.ssrc, dtmf_payload(key, end, duration), marker),
                              self.remote)
        self.seq = (self.seq + 1) & 0xFFFF

    def close(self) -> None:
        if self._sender and not self._sender.done():
            self._sender.cancel()
        if self.transport is not None:
            self.transport.close()
            self.transport = None


async def open_stream(local_ip: str, ports: Tuple[int, int], stream: RtpStream, used: set) -> RtpStream:
    """Bind `stream` to a free even port in `ports` on `local_ip`."""
    loop = asyncio.get_running_loop()
    lo, hi = ports
    first = lo + (lo & 1)
    candidates = list(range(first, hi + 1, 2))
    if not candidates:
        raise OSError("the RTP port range is empty")
    offset = secrets.randbelow(len(candidates))
    for i in range(len(candidates)):
        port = candidates[(offset + i) % len(candidates)]
        if (local_ip, port) in used:
            continue
        try:
            await loop.create_datagram_endpoint(lambda: stream, local_addr=(local_ip, port))
        except OSError:
            continue
        used.add((local_ip, port))
        return stream
    raise OSError("no free RTP port")
