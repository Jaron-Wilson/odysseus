"""SIP messages, digest authentication and SDP, for the free SIP line.

Just enough of RFC 3261 (SIP), RFC 2617/7616 (digest), RFC 4566 (SDP) and
RFC 3264 (offer/answer) for one softphone on the tailnet to call Odysseus,
register with it, and be called back. No third-party library: pyVoIP is a
thread-based client that registers to a PBX (it cannot challenge a caller,
act as a registrar or listen on TCP), and the asyncio SIP stacks on PyPI are
unmaintained. src/telephony/sip_server.py is the transport and dialog side;
this file is pure functions and small classes, tested on their own.
"""

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

MAX_MESSAGE = 65535
MAX_HEADERS = 120
SERVER_NAME = "Odysseus"
ALLOW = "INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER, INFO, UPDATE"

# RFC 3261 7.3.3 compact header names.
_COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
            "c": "content-type", "k": "supported", "s": "subject", "e": "content-encoding", "o": "event"}
# Headers whose values may be joined with commas into one line.
_LISTS = {"via", "contact", "route", "record-route", "allow", "supported"}

REASONS = {
    100: "Trying", 180: "Ringing", 183: "Session Progress", 200: "OK", 202: "Accepted",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 408: "Request Timeout", 415: "Unsupported Media Type",
    480: "Temporarily Unavailable", 481: "Call/Transaction Does Not Exist", 486: "Busy Here",
    487: "Request Terminated", 488: "Not Acceptable Here", 489: "Bad Event", 500: "Server Internal Error",
    501: "Not Implemented", 503: "Service Unavailable", 603: "Decline",
}


class SipError(ValueError):
    pass


# ── Messages ────────────────────────────────────────────────────────────

@dataclass
class SipMessage:
    method: str = ""                 # requests
    uri: str = ""
    status: int = 0                  # responses
    reason: str = ""
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    @property
    def is_request(self) -> bool:
        return bool(self.method)

    def get(self, name: str, default: str = "") -> str:
        vals = self.get_all(name)
        return vals[0] if vals else default

    def get_all(self, name: str) -> List[str]:
        want = _canon(name)
        out: List[str] = []
        for k, v in self.headers:
            if _canon(k) == want:
                out.extend(split_list(v) if want in _LISTS else [v])
        return out

    def set(self, name: str, value: str) -> None:
        want = _canon(name)
        self.headers = [(k, v) for k, v in self.headers if _canon(k) != want]
        self.headers.append((name, value))

    def add(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    @property
    def call_id(self) -> str:
        return self.get("Call-ID")

    @property
    def cseq(self) -> Tuple[int, str]:
        parts = self.get("CSeq").split()
        try:
            return int(parts[0]), (parts[1].upper() if len(parts) > 1 else "")
        except (ValueError, IndexError):
            raise SipError("bad CSeq")

    @property
    def branch(self) -> str:
        via = self.get("Via")
        return header_params(via).get("branch", "") if via else ""

    def from_tag(self) -> str:
        return parse_name_addr(self.get("From"))[2].get("tag", "")

    def to_tag(self) -> str:
        return parse_name_addr(self.get("To"))[2].get("tag", "")

    def encode(self) -> bytes:
        if self.is_request:
            first = f"{self.method} {self.uri} SIP/2.0"
        else:
            first = f"SIP/2.0 {self.status} {self.reason or REASONS.get(self.status, '')}"
        lines = [first]
        for k, v in self.headers:
            if _canon(k) == "content-length":
                continue
            lines.append(f"{k}: {v}")
        lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + self.body


def _canon(name: str) -> str:
    n = name.strip().lower()
    return _COMPACT.get(n, n)


def split_list(value: str) -> List[str]:
    """A comma-separated header value, split outside quotes and <>."""
    out, cur, quote, angle = [], [], False, 0
    for ch in value:
        if ch == '"':
            quote = not quote
        elif not quote and ch == "<":
            angle += 1
        elif not quote and ch == ">":
            angle = max(0, angle - 1)
        if ch == "," and not quote and not angle:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def parse(data: bytes) -> SipMessage:
    """One SIP message (a UDP datagram, or one framed off a TCP stream)."""
    if len(data) > MAX_MESSAGE:
        raise SipError("message too large")
    sep = data.find(b"\r\n\r\n")
    if sep >= 0:
        head, body = data[:sep], data[sep + 4:]
    else:
        sep = data.find(b"\n\n")
        head, body = (data[:sep], data[sep + 2:]) if sep >= 0 else (data, b"")
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        raise SipError("not UTF-8")
    raw_lines = text.replace("\r\n", "\n").split("\n")
    while raw_lines and not raw_lines[0].strip():
        raw_lines.pop(0)
    if not raw_lines:
        raise SipError("empty message")
    first, rest = raw_lines[0].strip(), raw_lines[1:]
    lines: List[str] = []
    for ln in rest:
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += " " + ln.strip()          # header folding
        elif ln.strip():
            lines.append(ln)
    if len(lines) > MAX_HEADERS:
        raise SipError("too many headers")
    msg = SipMessage()
    if first.startswith("SIP/2.0 "):
        parts = first.split(" ", 2)
        try:
            msg.status = int(parts[1])
        except (ValueError, IndexError):
            raise SipError("bad status line")
        if not 100 <= msg.status <= 699:
            raise SipError("bad status code")
        msg.reason = parts[2] if len(parts) > 2 else ""
    else:
        parts = first.split(" ")
        if len(parts) != 3 or parts[2] != "SIP/2.0" or not re.fullmatch(r"[A-Za-z]+", parts[0]):
            raise SipError("bad request line")
        msg.method, msg.uri = parts[0].upper(), parts[1]
    for ln in lines:
        if ":" not in ln:
            raise SipError("bad header line")
        k, v = ln.split(":", 1)
        msg.headers.append((k.strip(), v.strip()))
    clen = msg.get("Content-Length")
    if clen:
        try:
            n = int(clen)
        except ValueError:
            raise SipError("bad Content-Length")
        if n < 0 or n > len(body):
            raise SipError("body shorter than Content-Length")
        body = body[:n]
    msg.body = body
    for need in ("Via", "From", "To", "Call-ID", "CSeq"):
        if not msg.get(need):
            raise SipError(f"missing {need}")
    msg.cseq  # validates
    return msg


class StreamFramer:
    """SIP over TCP: bytes in, whole messages out (framed by Content-Length).
    Keep-alive CRLFs between messages (RFC 5626) come out as b"\\r\\n\\r\\n"."""

    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> List[bytes]:
        self.buf += data
        out: List[bytes] = []
        while self.buf:
            if self.buf.startswith(b"\r\n\r\n"):
                self.buf = self.buf[4:]
                out.append(b"\r\n\r\n")
                continue
            if self.buf[:1] in (b"\r", b"\n"):
                if len(self.buf) < 4 and b"\r\n\r\n".startswith(self.buf):
                    return out              # maybe the start of a ping
                self.buf = self.buf[1:]     # a pong, or stray line ends
                continue
            end = self.buf.find(b"\r\n\r\n")
            if end < 0:
                if len(self.buf) > MAX_MESSAGE:
                    raise SipError("header too large")
                return out
            m = re.search(rb"(?im)^(?:content-length|l)[ \t]*:[ \t]*(\d+)[ \t]*\r?$", self.buf[:end])
            n = int(m.group(1)) if m else 0
            if n > MAX_MESSAGE:
                raise SipError("body too large")
            total = end + 4 + n
            if len(self.buf) < total:
                return out
            out.append(self.buf[:total])
            self.buf = self.buf[total:]
        return out


# ── URIs and header values ─────────────────────────────────────────────

def header_params(value: str) -> Dict[str, str]:
    """The ;params after a header value (outside any <...>)."""
    tail = value
    if ">" in value:
        tail = value[value.rfind(">") + 1:]
    elif ";" in value:
        tail = value[value.find(";"):]
    else:
        return {}
    out = {}
    for part in tail.split(";")[1:]:
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        out[k.strip().lower()] = v.strip().strip('"')
    return out


def parse_name_addr(value: str) -> Tuple[str, str, Dict[str, str]]:
    """("display", "sip:user@host", {"tag": ...}) from a From/To/Contact value."""
    value = (value or "").strip()
    m = re.match(r'^\s*(?:"((?:[^"\\]|\\.)*)"|([^<"]*?))\s*<([^>]*)>(.*)$', value)
    if m:
        display = (m.group(1) if m.group(1) is not None else (m.group(2) or "")).strip()
        return display, m.group(3).strip(), header_params(">" + m.group(4))
    uri, _, rest = value.partition(";")
    return "", uri.strip(), header_params(";" + rest) if rest else {}


@dataclass
class SipUri:
    scheme: str = "sip"
    user: str = ""
    host: str = ""
    port: int = 0
    params: Dict[str, str] = field(default_factory=dict)


def parse_uri(uri: str) -> SipUri:
    m = re.match(r"^(sips?):(?:([^@;]*)@)?(\[[0-9A-Fa-f:.]+\]|[^:;?]+)(?::(\d+))?([^?]*)", (uri or "").strip())
    if not m:
        raise SipError("bad SIP URI")
    params = {}
    for part in (m.group(5) or "").split(";"):
        if part:
            k, _, v = part.partition("=")
            params[k.lower()] = v
    host = m.group(3)
    if host.startswith("["):
        host = host[1:-1]
    user = (m.group(2) or "").split(":", 1)[0]
    return SipUri(m.group(1).lower(), user, host, int(m.group(4) or 0), params)


def host_for_uri(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


def new_tag() -> str:
    return secrets.token_hex(6)


def new_branch() -> str:
    return "z9hG4bK" + secrets.token_hex(10)


def new_call_id(host: str) -> str:
    return f"{secrets.token_hex(12)}@{host_for_uri(host)}"


def with_tag(value: str, tag: str) -> str:
    if parse_name_addr(value)[2].get("tag"):
        return value
    v = value.strip()
    if "<" not in v:
        v = f"<{v}>"
    return f"{v};tag={tag}"


def response(req: SipMessage, status: int, reason: str = "", *, to_tag: str = "",
             headers: Optional[List[Tuple[str, str]]] = None, body: bytes = b"",
             received: Optional[Tuple[str, int]] = None) -> SipMessage:
    """A response to `req`: Via (with received/rport filled in, RFC 3581),
    From, To (tagged), Call-ID and CSeq copied, as RFC 3261 8.2.6 says."""
    out = SipMessage(status=status, reason=reason or REASONS.get(status, ""))
    vias = req.get_all("Via")
    for i, via in enumerate(vias):
        if i == 0 and received:
            via = _via_received(via, received)
        out.add("Via", via)
    out.add("From", req.get("From"))
    to = req.get("To")
    out.add("To", with_tag(to, to_tag) if to_tag and status > 100 else to)
    out.add("Call-ID", req.call_id)
    out.add("CSeq", req.get("CSeq"))
    out.add("Server", SERVER_NAME)
    for k, v in headers or []:
        out.add(k, v)
    out.body = body
    return out


def _via_received(via: str, addr: Tuple[str, int]) -> str:
    ip, port = addr[0], addr[1]
    parts = via.split(";")
    out = [parts[0]]
    has_received = False
    for p in parts[1:]:
        k = p.split("=", 1)[0].strip().lower()
        if k == "rport":
            out.append(f"rport={port}")
        elif k == "received":
            out.append(f"received={ip}")
            has_received = True
        else:
            out.append(p)
    if not has_received:
        out.append(f"received={ip}")
    return ";".join(out)


# ── Digest authentication ──────────────────────────────────────────────

def parse_auth(value: str) -> Dict[str, str]:
    """The fields of an Authorization: Digest ... header."""
    value = (value or "").strip()
    if not value.lower().startswith("digest "):
        return {}
    out = {}
    for m in re.finditer(r'([A-Za-z0-9_-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))', value[7:]):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def _h(algo: str, s: str) -> str:
    fn = hashlib.sha256 if algo.upper().startswith("SHA-256") else hashlib.md5
    return fn(s.encode("utf-8")).hexdigest()


def digest_response(username: str, realm: str, password: str, method: str, uri: str, nonce: str,
                    qop: str = "", nc: str = "", cnonce: str = "", algorithm: str = "MD5") -> str:
    """RFC 2617 / 7616 response value (the client side, and what we compare to)."""
    ha1 = _h(algorithm, f"{username}:{realm}:{password}")
    ha2 = _h(algorithm, f"{method}:{uri}")
    if qop:
        return _h(algorithm, f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return _h(algorithm, f"{ha1}:{nonce}:{ha2}")


class DigestAuth:
    """Server side of digest auth with stateless, expiring, HMAC-signed
    nonces and replay protection: with qop=auth each nonce count may be used
    once and must go up; without qop a nonce is good for one request."""

    NONCE_TTL = 300.0

    def __init__(self, realm: str = "odysseus", secret: Optional[bytes] = None,
                 clock: Callable[[], float] = time.time):
        self.realm = realm
        self.secret = secret or secrets.token_bytes(32)
        self.clock = clock
        self._seen: Dict[str, int] = {}          # nonce -> highest nc used (0: no-qop, used)

    def _sign(self, raw: bytes) -> bytes:
        return hmac.new(self.secret, raw, hashlib.sha256).digest()[:12]

    def nonce(self) -> str:
        raw = int(self.clock()).to_bytes(8, "big") + secrets.token_bytes(8)
        return base64.urlsafe_b64encode(raw + self._sign(raw)).decode("ascii").rstrip("=")

    def _nonce_age(self, nonce: str) -> Optional[float]:
        try:
            blob = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4))
        except Exception:
            return None
        if len(blob) != 28 or not hmac.compare_digest(blob[16:], self._sign(blob[:16])):
            return None
        return self.clock() - int.from_bytes(blob[:8], "big")

    def challenge(self, stale: bool = False) -> str:
        v = f'Digest realm="{self.realm}", nonce="{self.nonce()}", algorithm=MD5, qop="auth"'
        return v + (", stale=true" if stale else "")

    def _sweep(self) -> None:
        if len(self._seen) > 2000:
            for n in [n for n in self._seen if (self._nonce_age(n) or 1e9) > self.NONCE_TTL]:
                self._seen.pop(n, None)

    def verify(self, method: str, header: str, password_for: Callable[[str], Optional[str]]) -> Tuple[str, str]:
        """("ok" | "stale" | "missing" | "bad" | "replay", username)."""
        f = parse_auth(header)
        if not f:
            return "missing", ""
        username = f.get("username", "")
        nonce, resp = f.get("nonce", ""), f.get("response", "")
        algo = f.get("algorithm", "MD5") or "MD5"
        if algo.upper() not in ("MD5", "SHA-256"):
            return "bad", username
        if f.get("realm", "") != self.realm or not (username and nonce and resp and f.get("uri")):
            return "bad", username
        age = self._nonce_age(nonce)
        if age is None or age < -5:
            return "stale", username        # not ours, or from before a restart: ask again
        password = password_for(username)
        if password is None:
            # Same work either way, so an unknown name looks like a wrong password.
            digest_response(username, self.realm, secrets.token_hex(8), method, f["uri"], nonce)
            return "bad", username
        qop = f.get("qop", "")
        nc_s, cnonce = f.get("nc", ""), f.get("cnonce", "")
        if qop and (qop != "auth" or not re.fullmatch(r"[0-9a-fA-F]{8}", nc_s) or not cnonce):
            return "bad", username
        want = digest_response(username, self.realm, password, method, f["uri"], nonce, qop, nc_s, cnonce, algo)
        if not hmac.compare_digest(want, resp.lower()):
            return "bad", username
        if age > self.NONCE_TTL:
            return "stale", username
        self._sweep()
        nc = int(nc_s, 16) if qop else 0
        last = self._seen.get(nonce)
        if last is not None and (not qop or nc <= last):
            return "replay", username
        self._seen[nonce] = nc
        return "ok", username


# ── SDP ─────────────────────────────────────────────────────────────────

STATIC_PT = {0: ("PCMU", 8000), 8: ("PCMA", 8000)}
CODECS = ("PCMU", "PCMA")
DTMF_PT = 101


@dataclass
class Sdp:
    addr: str = ""
    port: int = 0
    payloads: List[int] = field(default_factory=list)
    rtpmap: Dict[int, Tuple[str, int]] = field(default_factory=dict)
    ptime: int = 20
    direction: str = "sendrecv"
    proto: str = "RTP/AVP"


def parse_sdp(body: bytes) -> Sdp:
    """The first audio stream of an SDP body."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise SipError("SDP is not UTF-8")
    sdp = Sdp()
    session_addr, media_addr, in_audio, seen_audio = "", "", False, False
    session_dir, media_dir = "", ""
    for ln in text.replace("\r\n", "\n").split("\n"):
        ln = ln.strip()
        if len(ln) < 2 or ln[1] != "=":
            continue
        k, v = ln[0], ln[2:]
        if k == "m":
            parts = v.split()
            if in_audio:
                break                       # only the first audio stream
            if parts and parts[0] == "audio" and not seen_audio and len(parts) >= 4:
                in_audio = seen_audio = True
                try:
                    sdp.port = int(parts[1].split("/")[0])
                    sdp.payloads = [int(p) for p in parts[3:] if p.isdigit()]
                except ValueError:
                    raise SipError("bad m= line")
                sdp.proto = parts[2].upper()
                if sdp.proto not in ("RTP/AVP", "RTP/AVPF"):
                    sdp.payloads = []          # SRTP or anything else: not spoken here
            continue
        if k == "c":
            parts = v.split()
            if len(parts) >= 3:
                addr = parts[2].split("/")[0]
                if in_audio:
                    media_addr = addr
                elif not seen_audio:
                    session_addr = addr
        elif k == "a":
            if v in ("sendrecv", "sendonly", "recvonly", "inactive"):
                if in_audio:
                    media_dir = v
                elif not seen_audio:
                    session_dir = v
            elif in_audio and v.startswith("rtpmap:"):
                m = re.match(r"rtpmap:(\d+)\s+([^/\s]+)/(\d+)", v)
                if m:
                    sdp.rtpmap[int(m.group(1))] = (m.group(2), int(m.group(3)))
            elif in_audio and v.startswith("ptime:"):
                try:
                    sdp.ptime = int(float(v[6:]))
                except ValueError:
                    pass
    if not seen_audio:
        raise SipError("no audio stream")
    sdp.addr = media_addr or session_addr
    sdp.direction = media_dir or session_dir or "sendrecv"
    return sdp


def codec_name(sdp: Sdp, pt: int) -> Tuple[str, int]:
    if pt in sdp.rtpmap:
        name, rate = sdp.rtpmap[pt]
        return name.upper(), rate
    return STATIC_PT.get(pt, ("", 0))


def negotiate(offer: Sdp) -> Optional[Tuple[int, str, Optional[int]]]:
    """(payload type, "PCMU" | "PCMA", telephone-event payload type or None)
    for an offer, or None when it has neither G.711 codec. PCMU is preferred,
    then whichever the phone listed first."""
    found: Dict[str, int] = {}
    for pt in offer.payloads:
        name, rate = codec_name(offer, pt)
        if name in CODECS and rate == 8000 and name not in found:
            found[name] = pt
    if not found:
        return None
    name = "PCMU" if "PCMU" in found else "PCMA"
    dtmf = None
    for pt in offer.payloads:
        n, rate = codec_name(offer, pt)
        if n == "TELEPHONE-EVENT" and rate == 8000:
            dtmf = pt
            break
    return found[name], name, dtmf


def build_sdp(ip: str, port: int, codecs: List[Tuple[int, str]], dtmf_pt: Optional[int],
              session_id: int, version: int, direction: str = "sendrecv", proto: str = "RTP/AVP") -> bytes:
    fam = "IP6" if ":" in ip else "IP4"
    pts = [str(pt) for pt, _ in codecs] + ([str(dtmf_pt)] if dtmf_pt is not None else [])
    lines = ["v=0", f"o=odysseus {session_id} {version} IN {fam} {ip}", "s=Odysseus",
             f"c=IN {fam} {ip}", "t=0 0", f"m=audio {port} {proto} {' '.join(pts)}"]
    for pt, name in codecs:
        lines.append(f"a=rtpmap:{pt} {name}/8000")
    if dtmf_pt is not None:
        lines += [f"a=rtpmap:{dtmf_pt} telephone-event/8000", f"a=fmtp:{dtmf_pt} 0-16"]
    lines += ["a=ptime:20", f"a={direction}"]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def answer_direction(offer_dir: str) -> str:
    return {"sendonly": "recvonly", "recvonly": "sendonly", "inactive": "inactive"}.get(offer_dir, "sendrecv")


# ── Who may connect ────────────────────────────────────────────────────

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def is_tailnet(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a in (TAILNET_V4 if a.version == 4 else TAILNET_V6)


def same_ip(a: str, b: str) -> bool:
    try:
        x, y = ipaddress.ip_address(a.split("%", 1)[0]), ipaddress.ip_address(b.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(x, ipaddress.IPv6Address) and x.ipv4_mapped:
        x = x.ipv4_mapped
    if isinstance(y, ipaddress.IPv6Address) and y.ipv4_mapped:
        y = y.ipv4_mapped
    return x == y


def check_bind_address(ip: str, test_mode: bool = False) -> str:
    """The address, if the SIP line may listen on it: a Tailscale address
    (100.64.0.0/10 or fd7a:115c:a1e0::/48), or loopback in test mode.
    Never the wildcard (0.0.0.0, ::), never a LAN or public address."""
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        raise SipError(f"not an IP address: {ip!r}")
    if a.is_unspecified:
        raise SipError("refusing to listen on every interface")
    if test_mode and a.is_loopback:
        return str(a)
    if not is_tailnet(str(a)):
        raise SipError(f"refusing to listen on {a}: not a Tailscale address")
    return str(a)
