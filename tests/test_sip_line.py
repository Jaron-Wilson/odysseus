"""The free SIP line: a softphone on the tailnet calls Odysseus directly
(src/telephony/sip.py, rtp.py, sip_server.py, sip_line.py, routes/sip_routes.py).

The line must never be reachable from outside the tailnet, so a good part of
this is about who gets nothing: the wildcard and LAN addresses are refused at
bind time, a packet from a non-tailnet source is refused before it is
parsed, a wrong password or a replayed digest gets 403, and a device not on
the account's list gets 403 even with the right password. The rest drives
whole calls over loopback (test mode) with src/telephony/sip_simulator.py,
through the same PhoneCall the Twilio line uses, with fake speech engines
and a fake agent.
"""

import asyncio
import logging
import re
import socket
import time
from pathlib import Path

import numpy as np
import pytest

from core.models import Session
from routes import prefs_routes
from src.telephony import call as call_mod, codec, config, rtp, simulator, sip, sip_line, sip_server
from src.telephony.sip_simulator import Softphone

PW = "correct-horse-battery"
ROOT = Path(__file__).resolve().parents[1]


# ── SIP messages ─────────────────────────────────────────────────────────

LINPHONE_INVITE = (
    "INVITE sip:odysseus@100.121.62.9 SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 100.101.1.2:49512;branch=z9hG4bK.abc123;rport\r\n"
    "v: SIP/2.0/UDP 10.0.0.9:5060;branch=z9hG4bK.proxy\r\n"
    "From: \"Jaron\" <sip:jaron@100.121.62.9>;tag=f00d\r\n"
    "To: sip:odysseus@100.121.62.9\r\n"
    "CSeq: 20 INVITE\r\n"
    "i: 7nQ1@100.101.1.2\r\n"
    "Max-Forwards: 70\r\n"
    "Supported: replaces, outbound,\r\n gruu\r\n"
    "Contact: <sip:jaron@100.101.1.2:49512;transport=udp>;+sip.instance=\"<urn:uuid:1>\"\r\n"
    "c: application/sdp\r\n"
    "l: 4\r\n"
    "\r\n"
    "v=0\r\nEXTRA"
).encode()


def test_parse_a_linphone_style_invite():
    m = sip.parse(LINPHONE_INVITE)
    assert (m.method, m.uri) == ("INVITE", "sip:odysseus@100.121.62.9")
    assert m.call_id == "7nQ1@100.101.1.2"                 # compact "i:"
    assert m.cseq == (20, "INVITE")
    assert len(m.get_all("Via")) == 2 and m.branch == "z9hG4bK.abc123"
    assert m.from_tag() == "f00d" and m.to_tag() == ""
    assert m.get_all("Supported") == ["replaces", "outbound", "gruu"]   # folded line joined
    assert m.body == b"v=0\r"                                # Content-Length wins
    display, uri, params = sip.parse_name_addr(m.get("From"))
    assert (display, uri, params["tag"]) == ("Jaron", "sip:jaron@100.121.62.9", "f00d")
    u = sip.parse_uri(sip.parse_name_addr(m.get("Contact"))[1])
    assert (u.user, u.host, u.port, u.params["transport"]) == ("jaron", "100.101.1.2", 49512, "udp")
    assert sip.parse_uri("sip:odysseus@[fd7a:115c:a1e0::1]:5070").host == "fd7a:115c:a1e0::1"


def test_response_copies_the_transaction_and_fills_in_received():
    req = sip.parse(LINPHONE_INVITE)
    r = sip.response(req, 200, to_tag="abc", received=("100.101.1.2", 40000), body=b"x")
    back = sip.parse(r.encode())
    assert back.status == 200 and back.reason == "OK"
    vias = back.get_all("Via")
    assert len(vias) == 2 and "received=100.101.1.2" in vias[0] and "rport=40000" in vias[0]
    assert back.to_tag() == "abc" and back.from_tag() == "f00d"
    assert back.call_id == req.call_id and back.cseq == (20, "INVITE") and back.body == b"x"
    # 100 Trying carries no To tag.
    assert sip.response(req, 100, to_tag="abc").to_tag() == ""


@pytest.mark.parametrize("raw", [
    b"INVITE sip:a@b SIP/2.0\r\nVia: SIP/2.0/UDP x;branch=z9hG4bK1\r\nFrom: <sip:a@b>;tag=1\r\nTo: <sip:b@b>\r\nCSeq: 1 INVITE\r\n\r\n",
    b"INVITE sip:a@b SIP/2.0\r\nVia: x\r\nFrom: a\r\nTo: b\r\nCall-ID: c\r\nCSeq: one INVITE\r\n\r\n",
    b"INVITE sip:a@b SIP/2.0\r\nVia: x\r\nFrom: a\r\nTo: b\r\nCall-ID: c\r\nCSeq: 1 INVITE\r\nContent-Length: 50\r\n\r\nshort",
    b"HELLO\r\n\r\n",
    b"",
])
def test_malformed_messages_are_refused(raw):
    with pytest.raises(sip.SipError):
        sip.parse(raw)


def test_tcp_framing_splits_messages_and_answers_keepalives():
    a = sip.SipMessage(method="OPTIONS", uri="sip:x", headers=[("Via", "SIP/2.0/TCP h;branch=z9hG4bK1"),
                       ("From", "<sip:a@h>;tag=1"), ("To", "<sip:b@h>"), ("Call-ID", "1"), ("CSeq", "1 OPTIONS")],
                       body=b"hello").encode()
    stream = b"\r\n\r\n" + a + a
    f = sip.StreamFramer()
    out = []
    for i in range(0, len(stream), 7):          # dribbled in, 7 bytes at a time
        out += f.feed(stream[i:i + 7])
    assert out[0] == b"\r\n\r\n" and out[1:] == [a, a]
    assert sip.parse(out[1]).body == b"hello"


# ── Digest ───────────────────────────────────────────────────────────────

def test_digest_matches_rfc_2617_example():
    assert sip.digest_response("Mufasa", "testrealm@host.com", "Circle Of Life", "GET", "/dir/index.html",
                               "dcd98b7102dd2f0e8b11d0f600bfb0c093", "auth", "00000001", "0a4f113b") \
        == "6629fae49393a05397450978507c4ef1"


def _auth_header(auth, user, pw, method="REGISTER", uri="sip:h", nc=1, nonce=None, qop=True, realm=None):
    nonce = nonce or sip.parse_auth(auth.challenge())["nonce"]
    realm = realm or auth.realm
    if qop:
        resp = sip.digest_response(user, realm, pw, method, uri, nonce, "auth", f"{nc:08x}", "cn")
        return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
                f'response="{resp}", qop=auth, nc={nc:08x}, cnonce="cn"'), nonce
    resp = sip.digest_response(user, realm, pw, method, uri, nonce)
    return f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}"', nonce


def test_digest_right_wrong_unknown_and_replay():
    auth = sip.DigestAuth()
    pw = {"alice": PW}.get
    h, nonce = _auth_header(auth, "alice", PW)
    assert auth.verify("REGISTER", h, pw) == ("ok", "alice")
    assert auth.verify("REGISTER", h, pw)[0] == "replay"           # same nc again
    h2, _ = _auth_header(auth, "alice", PW, nc=2, nonce=nonce)
    assert auth.verify("REGISTER", h2, pw)[0] == "ok"               # next nc, same nonce: fine
    assert auth.verify("INVITE", h2, pw)[0] == "bad"                # bound to the method
    bad, _ = _auth_header(auth, "alice", "wrong-password-1")
    assert auth.verify("REGISTER", bad, pw)[0] == "bad"
    who, _ = _auth_header(auth, "mallory", PW)
    assert auth.verify("REGISTER", who, pw)[0] == "bad"            # unknown user looks the same
    other_realm, _ = _auth_header(auth, "alice", PW, realm="elsewhere")
    assert auth.verify("REGISTER", other_realm, pw)[0] == "bad"
    assert auth.verify("REGISTER", "", pw)[0] == "missing"
    no_qop, _ = _auth_header(auth, "alice", PW, qop=False)
    assert auth.verify("REGISTER", no_qop, pw)[0] == "ok"
    assert auth.verify("REGISTER", no_qop, pw)[0] == "replay"      # without qop: one use


def test_digest_nonces_expire_and_cannot_be_forged():
    now = [1_000_000.0]
    auth = sip.DigestAuth(clock=lambda: now[0])
    pw = {"alice": PW}.get
    h, _ = _auth_header(auth, "alice", PW)
    now[0] += sip.DigestAuth.NONCE_TTL + 1
    assert auth.verify("REGISTER", h, pw)[0] == "stale"
    other = sip.DigestAuth()                                       # another server's secret
    forged, _ = _auth_header(auth, "alice", PW, nonce=sip.parse_auth(other.challenge())["nonce"])
    assert auth.verify("REGISTER", forged, pw)[0] == "stale"
    assert "stale=true" in auth.challenge(stale=True)
    assert 'qop="auth"' in auth.challenge() and "algorithm=MD5" in auth.challenge()


# ── Where it listens, who it lets in ─────────────────────────────────────

@pytest.mark.parametrize("addr", ["0.0.0.0", "::", "192.168.100.203", "10.0.0.1", "8.8.8.8", "172.17.0.1",
                                  "127.0.0.1", "100.63.255.255", "100.128.0.1", "fd7a:115c:a1e1::1", "nonsense"])
def test_never_listens_outside_the_tailnet(addr):
    with pytest.raises(sip.SipError):
        sip.check_bind_address(addr)
    with pytest.raises(sip.SipError):
        sip_server.SipServer([addr], 5060, (10000, 10100), lambda: [], None)


def test_tailnet_addresses_are_fine_and_loopback_only_in_test_mode():
    assert sip.check_bind_address("100.121.62.9") == "100.121.62.9"
    assert sip.check_bind_address("fd7a:115c:a1e0::ed01:3e0a") == "fd7a:115c:a1e0::ed01:3e0a"
    assert sip.check_bind_address("127.0.0.1", test_mode=True) == "127.0.0.1"
    with pytest.raises(sip.SipError):
        sip.check_bind_address("0.0.0.0", test_mode=True)
    with pytest.raises(sip.SipError):
        sip.check_bind_address("192.168.1.10", test_mode=True)


def test_configured_addresses_are_filtered_too(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_SIP_TEST_LOOPBACK", raising=False)
    monkeypatch.setenv("ODYSSEUS_SIP_ADDRESSES", "0.0.0.0, 192.168.100.203, ::, 100.121.62.9")
    assert sip_line.bind_addresses() == ["100.121.62.9"]
    monkeypatch.setenv("ODYSSEUS_SIP_TEST_LOOPBACK", "1")
    assert sip_line.bind_addresses() == ["127.0.0.1"]


def test_detects_the_tailscale_addresses(monkeypatch):
    monkeypatch.setattr(sip_line.shutil, "which", lambda name: "/usr/bin/" + name)
    outputs = {"tailscale": "fd7a:115c:a1e0::ed01:3e0a\n100.121.62.9\n",
               "ip": "2: ens18 inet 192.168.100.203/24 ...\n5: tailscale0 inet 100.121.62.9/32 scope global\n"}
    monkeypatch.setattr(sip_line, "_run", lambda args, timeout=4.0: outputs[args[0]])
    assert sip_line.tailnet_addresses() == ["100.121.62.9", "fd7a:115c:a1e0::ed01:3e0a"]
    outputs["tailscale"] = ""                                     # no CLI answer: read the interfaces
    assert sip_line.tailnet_addresses() == ["100.121.62.9"]


class _Sent:
    def __init__(self):
        self.data = []

    def __call__(self, b):
        self.data.append(b)


def _server(accounts, test_mode=False, **kw):
    addr = "127.0.0.1" if test_mode else "100.121.62.9"
    calls = []

    async def on_call(c):
        calls.append(c)

    srv = sip_server.SipServer([addr], 5060, (10000, 10100), lambda: accounts, on_call, test_mode=test_mode, **kw)
    return srv, calls


def _invite_bytes(src="100.101.1.2"):
    return LINPHONE_INVITE.replace(b"100.101.1.2", src.encode())


def test_requests_from_outside_the_tailnet_get_403_and_go_nowhere():
    asyncio.run(_outside_the_tailnet())


async def _outside_the_tailnet():
    srv, calls = _server([sip_server.Account("alice", "alice", PW)])
    for src in ("192.168.100.50", "8.8.8.8", "127.0.0.1", "100.200.0.1"):
        out = _Sent()
        link = sip_server.Link("udp", ("100.121.62.9", 5060), (src, 40000), out)
        srv._on_data(_invite_bytes(src), link)
        assert len(out.data) == 1 and sip.parse(out.data[0]).status == 403
        assert not srv.calls and not calls
    assert srv.refused == 4
    # A tailnet source gets a digest challenge instead.
    out = _Sent()
    srv._on_data(_invite_bytes(), sip_server.Link("udp", ("100.121.62.9", 5060), ("100.101.1.2", 40000), out))
    await asyncio.sleep(0.05)
    r = sip.parse(out.data[0])
    assert r.status == 401 and r.get("WWW-Authenticate").startswith("Digest realm=\"odysseus\"")


def test_device_list_narrows_who_may_even_ask():
    srv, _ = _server([sip_server.Account("alice", "alice", PW, devices=["100.101.1.2"])])
    assert srv._source_ok("100.101.1.2")
    assert not srv._source_ok("100.101.1.3")
    assert not srv._source_ok("192.168.1.2")
    # With another account open to the whole tailnet, anyone on it may ask
    # (and still has to sign in as an account that allows them).
    srv2, _ = _server([sip_server.Account("alice", "alice", PW, devices=["100.101.1.2"]),
                       sip_server.Account("bob", "bob", PW)])
    assert srv2._source_ok("100.101.1.3") and not srv2._source_ok("192.168.1.2")


def test_options_and_unknown_methods_need_no_account_but_say_nothing():
    srv, _ = _server([sip_server.Account("alice", "alice", PW)])
    raw = LINPHONE_INVITE.replace(b"INVITE sip:", b"OPTIONS sip:").replace(b"20 INVITE", b"20 OPTIONS")
    out = _Sent()
    srv._on_data(raw, sip_server.Link("udp", ("100.121.62.9", 5060), ("100.101.1.2", 40000), out))
    r = sip.parse(out.data[0])
    assert r.status == 200 and "INVITE" in r.get("Allow")
    raw = LINPHONE_INVITE.replace(b"INVITE sip:", b"SUBSCRIBE sip:").replace(b"20 INVITE", b"20 SUBSCRIBE")
    out = _Sent()
    srv._on_data(raw, sip_server.Link("udp", ("100.121.62.9", 5060), ("100.101.1.2", 40000), out))
    assert sip.parse(out.data[0]).status == 405


# ── SDP ──────────────────────────────────────────────────────────────────

LINPHONE_SDP = (b"v=0\r\no=jaron 123 456 IN IP4 100.101.1.2\r\ns=Talk\r\nc=IN IP4 100.101.1.2\r\nt=0 0\r\n"
                b"m=audio 7078 RTP/AVP 96 97 98 0 8 101\r\na=rtpmap:96 opus/48000/2\r\na=rtpmap:97 speex/16000\r\n"
                b"a=rtpmap:98 speex/8000\r\na=rtpmap:101 telephone-event/8000\r\na=ptime:20\r\n"
                b"m=video 9078 RTP/AVP 96\r\na=rtpmap:96 VP8/90000\r\n")


def test_sdp_picks_pcmu_and_telephone_event_from_a_linphone_offer():
    s = sip.parse_sdp(LINPHONE_SDP)
    assert (s.addr, s.port, s.payloads, s.direction) == ("100.101.1.2", 7078, [96, 97, 98, 0, 8, 101], "sendrecv")
    assert sip.negotiate(s) == (0, "PCMU", 101)


def test_sdp_falls_back_to_pcma_and_refuses_neither():
    pcma = LINPHONE_SDP.replace(b"96 97 98 0 8 101", b"96 8 101")
    assert sip.negotiate(sip.parse_sdp(pcma)) == (8, "PCMA", 101)
    opus = LINPHONE_SDP.replace(b"96 97 98 0 8 101", b"96 97")
    assert sip.negotiate(sip.parse_sdp(opus)) is None
    dyn = (b"v=0\r\nc=IN IP4 100.1.1.1\r\nm=audio 4000 RTP/AVP 110\r\na=rtpmap:110 PCMU/8000\r\na=sendonly\r\n")
    s = sip.parse_sdp(dyn)
    assert sip.negotiate(s) == (110, "PCMU", None) and s.direction == "sendonly"
    assert sip.answer_direction("sendonly") == "recvonly"
    with pytest.raises(sip.SipError):
        sip.parse_sdp(b"v=0\r\nm=video 1 RTP/AVP 96\r\n")
    avpf = sip.parse_sdp(LINPHONE_SDP.replace(b"7078 RTP/AVP", b"7078 RTP/AVPF"))
    assert avpf.proto == "RTP/AVPF" and sip.negotiate(avpf) == (0, "PCMU", 101)
    assert b"m=audio 9 RTP/AVPF 0" in sip.build_sdp("100.1.1.1", 9, [(0, "PCMU")], None, 1, 1, proto="RTP/AVPF")
    srtp = sip.parse_sdp(LINPHONE_SDP.replace(b"7078 RTP/AVP", b"7078 RTP/SAVP"))
    assert sip.negotiate(srtp) is None


def test_sdp_answer_round_trips():
    body = sip.build_sdp("100.121.62.9", 10002, [(0, "PCMU")], 101, 42, 3)
    s = sip.parse_sdp(body)
    assert (s.addr, s.port, s.payloads, s.ptime) == ("100.121.62.9", 10002, [0, 101], 20)
    assert sip.negotiate(s) == (0, "PCMU", 101)
    v6 = sip.build_sdp("fd7a:115c:a1e0::1", 10002, [(0, "PCMU")], None, 1, 1).decode()
    assert "c=IN IP6 fd7a:115c:a1e0::1" in v6


# ── RTP ──────────────────────────────────────────────────────────────────

def test_rtp_pack_and_unpack():
    raw = rtp.pack(0, 65535, 0xFFFFFFF0, 0xDEADBEEF, b"\xff" * 160, marker=True)
    p = rtp.unpack(raw)
    assert (p.pt, p.seq, p.ts, p.ssrc, p.marker, len(p.payload)) == (0, 65535, 0xFFFFFFF0, 0xDEADBEEF, True, 160)
    # One CSRC, a header extension and padding, all skipped.
    hdr = bytes([0x80 | 0x20 | 0x10 | 1, 8]) + (7).to_bytes(2, "big") + (1).to_bytes(4, "big") + (2).to_bytes(4, "big")
    raw2 = hdr + b"CSRC" + b"\xbe\xde\x00\x01" + b"EXT!" + b"audio" + b"\x00\x00\x03"
    p2 = rtp.unpack(raw2)
    assert (p2.pt, p2.seq, p2.payload) == (8, 7, b"audio")
    assert rtp.unpack(b"\x40" + b"\x00" * 11) is None and rtp.unpack(b"short") is None
    assert rtp.seq_diff(2, 65534) == 4 and rtp.seq_diff(65534, 2) == -4


def test_jitter_buffer_reorders_drops_dupes_and_fills_loss():
    jb = rtp.JitterBuffer(depth=3)
    frames = {s: bytes([s % 256]) * 160 for s in range(65530, 65536)}
    frames.update({s: bytes([s]) * 160 for s in range(0, 10)})
    order = [65530, 65532, 65531, 65533, 65535, 65534, 0, 2, 2, 1, 3, 4, 65533, 6, 7, 8, 9]   # 5 is lost
    out = []
    for s in order:
        out += jb.push(s, frames[s])
    out += jb.flush()
    want = [frames[s] for s in list(range(65530, 65536)) + [0, 1, 2, 3, 4]] + [codec.SILENCE * 160] + \
           [frames[s] for s in (6, 7, 8, 9)]
    assert out == want
    assert jb.dupes == 1 and jb.late == 1 and jb.lost == 1


def test_jitter_buffer_follows_a_sequence_restart():
    jb = rtp.JitterBuffer()
    assert jb.push(100, b"a") == [b"a"]
    assert jb.push(40000, b"b") == [b"b"]
    assert jb.push(40001, b"c") == [b"c"]


def test_dtmf_events():
    assert rtp.dtmf_event(rtp.dtmf_payload("*", True, 640)) == ("*", True, 640)
    assert rtp.dtmf_event(rtp.dtmf_payload("9", False, 160)) == ("9", False, 160)
    assert rtp.dtmf_event(b"\x20\x80\x00\x10") is None        # not a keypad event
    req = sip.parse(LINPHONE_INVITE.replace(b"INVITE sip:", b"INFO sip:").replace(b"20 INVITE", b"21 INFO")
                    .replace(b"c: application/sdp", b"c: application/dtmf-relay").replace(b"l: 4", b"l: 22")
                    .replace(b"v=0\r\nEXTRA", b"Signal=#\r\nDuration=160"))
    assert sip_server._info_digit(req) == "#"


class _Collect(asyncio.DatagramProtocol):
    def __init__(self):
        self.got = []

    def datagram_received(self, data, addr):
        self.got.append((time.monotonic(), rtp.unpack(data)))


def test_rtp_stream_sends_every_20ms_marks_when_played_and_dedupes_keys():
    async def run():
        loop = asyncio.get_running_loop()
        sink = _Collect()
        st, _ = await loop.create_datagram_endpoint(lambda: sink, local_addr=("127.0.0.1", 0))
        sink_addr = st.get_extra_info("sockname")
        played, keys, heard = [], [], []
        stream = rtp.RtpStream(0, "PCMU", 101, sink_addr, on_played=played.append, on_dtmf=keys.append,
                               on_audio=heard.append)
        await rtp.open_stream("127.0.0.1", (31000, 31100), stream, set())
        assert stream.local[0] == "127.0.0.1"
        stream.start()
        await asyncio.sleep(0.1)
        tone = codec.pcm16_to_ulaw(simulator.tone(0.4))                       # 20 frames
        stream.play(tone, "m1")
        t0 = time.monotonic()
        while not played and time.monotonic() - t0 < 2:
            await asyncio.sleep(0.005)
        took = time.monotonic() - t0
        await asyncio.sleep(0.3)
        # Keypad: one press, its end packet sent three times, is one key.
        sender_t, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
        for end in (False, False, True, True, True):
            sender_t.sendto(rtp.pack(101, 5, 4242, 1, rtp.dtmf_payload("*", end, 160)), stream.local)
        sender_t.sendto(rtp.pack(0, 6, 5000, 1, b"\x00" * 160), stream.local)
        await asyncio.sleep(0.1)
        stream.close()
        st.close()
        sender_t.close()
        return sink.got, played, took, keys, heard

    got, played, took, keys, heard = asyncio.run(run())
    assert played == ["m1"] and 0.3 < took < 0.7          # 20 frames at 20 ms, not all at once
    pkts = [p for _, p in got]
    assert all(len(p.payload) == 160 and p.pt == 0 for p in pkts)
    seqs = [p.seq for p in pkts]
    assert all(rtp.seq_diff(b, a) == 1 for a, b in zip(seqs, seqs[1:]))
    tss = [p.ts for p in pkts]
    assert all((b - a) & 0xFFFFFFFF == 160 for a, b in zip(tss, tss[1:]))
    # Talkspurt start is marked once.
    starts = [i for i, p in enumerate(pkts) if p.marker]
    assert len(starts) == 1 and pkts[starts[0]].payload != codec.SILENCE * 160
    times = [t for t, _ in got]
    gaps = np.diff(times)
    assert abs(float(np.mean(gaps)) - 0.020) < 0.004
    assert len(pkts) >= 30
    assert keys == ["*"] and heard and heard[0] == b"\x00" * 160


def test_rtp_stream_ignores_strangers():
    stream = rtp.RtpStream(0, "PCMU", 101, ("100.101.1.2", 7078))
    heard = []
    stream.on_audio = heard.append
    stream.datagram_received(rtp.pack(0, 1, 160, 9, b"\x10" * 160), ("100.101.1.99", 7078))
    stream.datagram_received(rtp.pack(0, 1, 160, 9, b"\x10" * 160), ("192.168.1.2", 7078))
    assert heard == [] and stream.dropped == 2
    stream.datagram_received(rtp.pack(0, 1, 160, 9, b"\x10" * 160), ("100.101.1.2", 40002))
    assert heard == [b"\x10" * 160] and stream.remote == ("100.101.1.2", 40002)   # latched


def test_pcma_audio_is_converted_both_ways():
    stream = rtp.RtpStream(8, "PCMA", None, ("127.0.0.1", 9))
    heard = []
    stream.on_audio = heard.append
    tone = codec.pcm16_to_ulaw(simulator.tone(0.02))
    stream.datagram_received(rtp.pack(8, 1, 0, 1, codec.ulaw_to_alaw(tone)), ("127.0.0.1", 9))
    back = codec.ulaw_to_pcm16(heard[0]).astype(int)
    assert np.max(np.abs(back - codec.ulaw_to_pcm16(tone).astype(int))) < 600
    stream.play(tone, "x")
    assert stream._out[0][0] == codec.ulaw_to_alaw(tone)


def test_alaw_matches_g711():
    assert codec.alaw_to_pcm16(bytes([0xD5, 0x55, 0x2A, 0xAA])).tolist() == [8, -8, -32256, 32256]
    x = np.arange(-32768, 32768, 7, dtype=np.int16)
    back = codec.alaw_to_pcm16(codec.pcm16_to_alaw(x)).astype(int)
    assert np.max(np.abs(back - x.astype(int)) / (np.abs(x.astype(int)) + 64)) < 0.12
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop
    except ImportError:
        return
    assert codec.pcm16_to_alaw(x) == audioop.lin2alaw(x.tobytes(), 2)


# ── Whole calls over loopback ────────────────────────────────────────────

class _Mgr:
    def __init__(self):
        self.sessions = {}

    def get_sessions_for_user(self, username=None):
        return self.sessions

    def get_session(self, sid):
        return self.sessions.get(sid)

    def add_message(self, sid, msg):
        self.sessions[sid].history.append(msg)

    def save_sessions(self):
        pass


class _Stubs:
    def __init__(self):
        self.heard = ["what time is it"]
        self.stt_calls = []
        self.tts_calls = []
        self.replies = []
        self.reply_text = "It is noon. Anything else?"
        self.reply_delay = 0.0

    def stt(self, wav):
        self.stt_calls.append(wav)
        return self.heard.pop(0) if self.heard else "and another thing"

    def tts(self, text):
        self.tts_calls.append(text)
        n = int(24000 * min(1.0, 0.03 * len(text)))
        return codec.wav_bytes((np.sin(np.arange(n) / 3) * 6000).astype(np.int16), 24000)

    async def reply(self, sid, text):
        self.replies.append((sid, text))
        for word in self.reply_text.split(" "):
            if self.reply_delay:
                await asyncio.sleep(self.reply_delay)
            yield ("delta", word + " ")


def _free_port():
    for _ in range(50):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.bind(("127.0.0.1", 0))
        port = u.getsockname()[1]
        t = socket.socket()
        try:
            t.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            u.close()
            t.close()
    raise RuntimeError("no free port")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ODYSSEUS_SIP_TEST_LOOPBACK", "1")
    port = _free_port()
    rtp_lo = 32000 + (port % 200) * 100
    monkeypatch.setenv("ODYSSEUS_SIP_PORT", str(port))
    monkeypatch.setenv("ODYSSEUS_SIP_RTP_PORTS", f"{rtp_lo}-{rtp_lo + 60}")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    mgr = _Mgr()
    from src import ai_interaction
    import core.models as cm
    monkeypatch.setattr(ai_interaction, "_session_manager", mgr)
    monkeypatch.setattr(cm, "_SESSION_MANAGER_INSTANCE", mgr)
    made = []

    def fake_create(sm, owner, model, endpoint_id, name=""):
        sid = f"call{len(made) + 1}"
        mgr.sessions[sid] = Session(id=sid, name=name, endpoint_url="http://llm/v1", model=model, owner=owner)
        made.append((sid, owner, name))
        return sid, mgr.sessions[sid]

    import routes.session_routes as sr
    monkeypatch.setattr(sr, "create_direct_chat", fake_create)
    stubs = _Stubs()
    monkeypatch.setattr(call_mod, "default_stt", stubs.stt)
    monkeypatch.setattr(call_mod, "default_tts", stubs.tts)
    monkeypatch.setattr(call_mod, "engines_ready", lambda: [])
    from src.telephony import agent
    monkeypatch.setattr(agent, "reply", stubs.reply)
    line = sip_line.SipLine()
    monkeypatch.setattr(sip_line, "LINE", line)
    from routes import sip_routes, telephony_routes
    monkeypatch.setattr(sip_routes, "LINE", line)
    telephony_routes.ACTIVE.clear()

    def configure(user="alice", username="alice", password=PW, enabled=True, devices=None, **over):
        cfg = config.get_config(user)
        cfg.update({"model": "qwen", "endpoint_id": "ep-a"})
        cfg.update(over)
        s = {"enabled": enabled, "username": username, "devices": devices or [], "require_pin": False}
        s.update({k: v for k, v in over.items() if k == "require_pin"})
        sip_line.set_password(s, password)
        cfg["sip"] = s
        config.save_config(user, cfg)

    yield {"port": port, "line": line, "stubs": stubs, "made": made, "mgr": mgr, "configure": configure,
           "server": ("127.0.0.1", port)}


async def _greeted(ph):
    """Line noise while the greeting plays (it sets the noise floor), then
    wait for the greeting to finish."""
    feeder = asyncio.create_task(ph.send_audio(simulator.silence(1.3)))
    n = await ph.wait_quiet(0, 6)
    await feeder
    return n


async def _say(ph, seconds=1.0):
    await ph.send_audio(simulator.silence(0.3))
    await ph.send_audio(simulator.tone(seconds))
    await ph.send_audio(simulator.silence(1.0))


def test_a_call_greets_then_answers_through_the_call_pipeline(env):
    env["configure"](greeting="Hi, it's Odysseus.")
    st = env["stubs"]

    async def run():
        line = env["line"]
        await line.reconcile()
        assert line.running
        # Every socket it opened is on loopback (test mode), none on 0.0.0.0.
        assert {ip for _, ip, _ in line.server.listeners} == {"127.0.0.1"}
        ph = await Softphone(env["server"], "alice", PW).open()
        r = await ph.invite()
        assert r.status == 200 and r.get("Content-Type") == "application/sdp"
        answer = sip.parse_sdp(r.body)
        assert answer.addr == "127.0.0.1" and sip.negotiate(answer) == (0, "PCMU", 101)
        rtp_ports = sip_line.rtp_ports()
        assert rtp_ports[0] <= answer.port <= rtp_ports[1]
        n = await _greeted(ph)
        assert n > 0
        from routes.telephony_routes import ACTIVE
        assert [c["engine"] for c in ACTIVE.values()] == ["sip"]
        await _say(ph)
        await ph.wait_quiet(n, 10)
        r = await ph.bye()
        assert r.status == 200
        await asyncio.sleep(0.3)
        assert not line.server.calls and not ACTIVE
        await line.stop()
        ph.close()

    asyncio.run(run())
    assert st.tts_calls == ["Hi, it's Odysseus.", "It is noon.", "Anything else?"]
    assert [t for _, t in st.replies] == ["what time is it"]
    assert len(st.stt_calls) == 1 and codec.read_wav(st.stt_calls[0])[1] == 16000
    assert env["made"] == [("call1", "alice", env["made"][0][2])] and "SIP alice" in env["made"][0][2]


def test_talking_over_the_agent_and_star_both_cut_it_off(env, monkeypatch):
    env["configure"]()
    st = env["stubs"]
    st.heard = ["tell me a long story", "actually stop"]
    st.reply_text = "Once upon a time. There was a cat. It sat. It sat more. It sat even more. The end."
    st.reply_delay = 0.05
    reasons = []
    orig = call_mod.PhoneCall.interrupt

    def spy(self, reason):
        reasons.append(reason)
        return orig(self, reason)

    monkeypatch.setattr(call_mod.PhoneCall, "interrupt", spy)

    async def run():
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW).open()
        assert (await ph.invite()).status == 200
        await _greeted(ph)
        await _say(ph)
        # Wait for the reply to start playing, then talk over it.
        t0 = time.monotonic()
        before = len(ph.rx.audio)
        while len(ph.rx.audio) == before and time.monotonic() - t0 < 8:
            await asyncio.sleep(0.02)
        await ph.press("*")
        await asyncio.sleep(0.3)
        await ph.send_audio(simulator.tone(1.0))
        await ph.send_audio(simulator.silence(1.0))
        await ph.wait_quiet(len(ph.rx.audio), 10)
        await ph.bye()
        await line.stop()
        ph.close()

    asyncio.run(run())
    assert "keypad" in reasons
    assert [t for _, t in st.replies] == ["tell me a long story", "actually stop"]


def test_saying_bye_hangs_up_from_our_side(env):
    env["configure"]()
    st = env["stubs"]
    st.heard = ["okay bye"]

    async def run():
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW).open()
        assert (await ph.invite()).status == 200
        await _greeted(ph)
        await _say(ph)
        await asyncio.wait_for(ph.ended.wait(), 10)
        await asyncio.sleep(0.2)
        assert not line.server.calls
        await line.stop()
        ph.close()
        return ph

    ph = asyncio.run(run())
    assert ph.rx.bye and st.replies == []
    assert st.tts_calls == [config.DEFAULT_GREETING, call_mod.GOODBYE]


def test_wrong_password_unknown_user_and_lockout(env, monkeypatch):
    env["configure"]()
    monkeypatch.setattr(sip_server, "FAIL_LIMIT", 3)

    async def run():
        line = env["line"]
        await line.reconcile()
        out = []
        for user, pw in (("alice", "not-the-password"), ("mallory", PW), ("alice", "nope-nope-nope")):
            ph = await Softphone(env["server"], user, pw).open()
            r = await ph.invite()
            out.append(r.status)
            ph.close()
        # Locked out now, even with the right password.
        ph = await Softphone(env["server"], "alice", PW).open()
        r = await ph.invite()
        out.append((r.status, r.reason))
        ph.close()
        n_calls = len(line.server.calls)
        await line.stop()
        return out, n_calls

    out, n_calls = asyncio.run(run())
    assert out[:3] == [403, 403, 403] and out[3] == (403, "Too many failed sign-ins")
    assert n_calls == 0 and env["made"] == [] and env["stubs"].stt_calls == []


def test_a_device_not_on_the_list_is_refused_even_with_the_password(env):
    env["configure"](devices=["127.0.0.2"])
    env2 = env

    async def run():
        line = env2["line"]
        await line.reconcile()
        ph = await Softphone(env2["server"], "alice", PW).open()       # from 127.0.0.1
        r = await asyncio.wait_for(ph.invite(), 6)
        ph.close()
        ok = await Softphone(env2["server"], "alice", PW, local_ip="127.0.0.2").open()
        r2 = await ok.register()
        ok.close()
        await line.stop()
        return r, r2

    r, r2 = asyncio.run(run())
    assert r.status == 403 and r2.status == 200
    assert env["made"] == []


def test_no_common_codec_and_engines_not_ready(env, monkeypatch):
    env["configure"]()

    async def run():
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW, codecs=[(96, "opus")]).open()
        r1 = await ph.invite()
        ph.close()
        monkeypatch.setattr(call_mod, "engines_ready", lambda: ["Speech to text: pick one."])
        ph = await Softphone(env["server"], "alice", PW).open()
        r2 = await ph.invite()
        ph.close()
        await line.stop()
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.status == 488 and r2.status == 503
    assert env["made"] == []


def test_pcma_phone_over_tcp(env):
    env["configure"]()

    async def run():
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW, transport="tcp", codecs=[(8, "PCMA")], dtmf_pt=None).open()
        r = await ph.invite()
        assert r.status == 200 and sip.negotiate(sip.parse_sdp(r.body))[1] == "PCMA"
        n = await _greeted(ph)
        await ph.bye()
        await line.stop()
        ph.close()
        return n

    assert asyncio.run(run()) > 0
    assert env["stubs"].tts_calls[0] == config.DEFAULT_GREETING


def test_200_ok_is_resent_until_ack_and_a_resent_invite_is_one_call(env):
    env["configure"]()

    async def run():
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW).open()
        ph.call_id = sip.new_call_id("127.0.0.1")
        host = "127.0.0.1"
        frm = f"<sip:alice@{host}>;tag=t1"
        to = f"<sip:odysseus@{host}>"
        first = ph._build("INVITE", f"sip:odysseus@{host}", call_id=ph.call_id, cseq=1, from_hdr=frm, to_hdr=to,
                          body=ph.offer(), extra=[("Content-Type", "application/sdp")])
        r = await ph.send(first)
        assert r.status == 401
        ph._ack_non2xx(first, r)
        invite = ph._authorize(ph._build("INVITE", f"sip:odysseus@{host}", call_id=ph.call_id, cseq=2,
                                         from_hdr=frm, to_hdr=to, body=ph.offer(),
                                         extra=[("Content-Type", "application/sdp")]), r.get("WWW-Authenticate"))
        q = asyncio.Queue()
        ph._waiters[invite.branch] = q
        raw = invite.encode()
        ph.send_raw(raw)
        ph.send_raw(raw)                     # a retransmission, same branch
        oks = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.8:
            try:
                m = await asyncio.wait_for(q.get(), 0.1)
                if m.status == 200:
                    oks.append(m)
            except asyncio.TimeoutError:
                pass
        calls = len(line.server.calls)
        tag = oks[0].to_tag()
        ack = ph._build("ACK", f"sip:odysseus@{host}", call_id=ph.call_id, cseq=2, from_hdr=frm,
                        to_hdr=sip.with_tag(to, tag))
        ph.send_raw(ack.encode())
        await asyncio.sleep(0.1)
        n = len(oks)
        await asyncio.sleep(1.2)
        while not q.empty():
            if q.get_nowait().status == 200:
                oks.append(1)
        later = len(oks) - n
        await line.stop()
        ph.close()
        return n, calls, later, {m.to_tag() for m in oks if hasattr(m, "to_tag")}

    n, calls, later, tags = asyncio.run(run())
    assert n >= 3 and calls == 1 and later == 0 and len(tags) == 1


def test_bye_for_an_unknown_call_is_481_and_from_another_ip_too(env):
    env["configure"]()

    async def run():
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW).open()
        assert (await ph.invite()).status == 200
        other = await Softphone(env["server"], "alice", PW, local_ip="127.0.0.3").open()
        other.call_id, other.local_hdr, other.remote_hdr, other.remote_target = (
            ph.call_id, ph.local_hdr, ph.remote_hdr, ph.remote_target)
        r_spoof = await other.bye()                  # right dialog, wrong address
        still = len(line.server.calls)
        ph2 = await Softphone(env["server"], "alice", PW).open()
        ph2.call_id, ph2.local_hdr, ph2.remote_hdr, ph2.remote_target = "nope@x", ph.local_hdr, ph.remote_hdr, ph.remote_target
        r_unknown = await ph2.bye()
        r = await ph.bye()
        await asyncio.sleep(0.2)
        after = len(line.server.calls)
        await line.stop()
        for p in (ph, ph2, other):
            p.close()
        return r_spoof.status, still, r_unknown.status, r.status, after

    assert asyncio.run(run()) == (481, 1, 481, 200, 0)


def test_pin_gate_on_the_keypad(env):
    env["configure"](require_pin=True)
    cfg = config.get_config("alice")
    config.set_pin(cfg, "1234")
    config.save_config("alice", cfg)
    st = env["stubs"]

    async def run(keys):
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW).open()
        assert (await ph.invite()).status == 200
        feeder = asyncio.create_task(ph.send_audio(simulator.silence(1.0)))
        await ph.wait_quiet(0, 6, quiet=0.3)
        for k in keys:
            await ph.press(k)
            await asyncio.sleep(0.05)
        await feeder
        await ph.wait_quiet(len(ph.rx.audio), 6, quiet=0.5)
        ended = ph.ended.is_set() or ph.rx.bye
        if not ended:
            await ph.bye()
        await line.stop()
        ph.close()
        return ended

    assert asyncio.run(run("1234")) is False
    assert st.tts_calls == ["Enter your PIN.", config.DEFAULT_GREETING]
    st.tts_calls.clear()
    asyncio.run(run("9999#"))
    assert st.tts_calls[:2] == ["Enter your PIN.", "That is not right. Try again."]


def test_register_then_call_me_rings_the_softphone(env):
    env["configure"]()
    st = env["stubs"]

    async def run():
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW).open()
        with pytest.raises(ValueError):
            await line.call_me("alice")                     # not registered yet
        r = await ph.register(expires=3600)
        assert r.status == 200 and "expires=600" in r.get("Contact")   # capped
        regs = line.server.registered("alice")
        assert len(regs) == 1 and regs[0].link.peer[1] == ph.sip_port
        res = await line.call_me("alice", "Your build finished.")
        assert res["ok"]
        await asyncio.wait_for(ph.answered.wait(), 5)
        invite = [m for m in ph.requests if m.method == "INVITE"][0]
        assert sip.negotiate(sip.parse_sdp(invite.body))[1] == "PCMU"
        n = await _greeted(ph)
        assert n > 0
        await ph.bye()
        await asyncio.sleep(0.2)
        assert not line.server.calls
        # Unregister: Expires 0.
        r = await ph.register(expires=0)
        assert r.status == 200 and not line.server.registered("alice")
        await line.stop()
        ph.close()

    asyncio.run(run())
    assert st.tts_calls[0] == "Your build finished."
    assert env["made"] and "SIP alice" in env["made"][0][2]


def test_call_me_tool_uses_the_sip_line_or_says_why_not(env):
    from src.agent_tools.call_me_tool import CallMeTool
    tool = CallMeTool()

    async def run():
        r1 = await tool.execute('{"message": "hi"}', {"owner": "alice"})
        env["configure"]()
        line = env["line"]
        await line.reconcile()
        ph = await Softphone(env["server"], "alice", PW).open()
        await ph.register()
        r2 = await tool.execute('{"message": "Done.", "via": "sip"}', {"owner": "alice"})
        await asyncio.wait_for(ph.answered.wait(), 5)
        await asyncio.sleep(0.3)
        await ph.bye()
        await line.stop()
        ph.close()
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1["exit_code"] == 1 and "SIP line" in r1["error"] and "Phone number" in r1["error"]
    assert r2["exit_code"] == 0 and "Ringing" in r2["output"]


def test_turning_the_line_off_closes_the_port(env):
    env["configure"]()

    async def run():
        line = env["line"]
        await line.reconcile()
        assert line.running
        env["configure"](enabled=False)
        await line.reconcile()
        assert not line.running
        ph = await Softphone(env["server"], "alice", PW).open()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ph.options(), 1.5)
        ph.close()

    asyncio.run(run())


# ── Settings ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(env, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from core.middleware import FunnelGuardMiddleware
    from routes import sip_routes, telephony_routes

    async def no_reconcile():
        return None

    monkeypatch.setattr(env["line"], "reconcile", no_reconcile)
    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(telephony_routes.setup_telephony_routes())
    app.include_router(sip_routes.setup_sip_routes())
    app.add_middleware(FunnelGuardMiddleware)
    with TestClient(app) as c:
        yield c


def _put(client, user="alice", **body):
    return client.put("/api/telephony/sip", headers={"x-test-user": user}, json=body)


def test_settings_validation(client):
    assert _put(client, username="a b").status_code == 400
    assert _put(client, username="alice", password="short").status_code == 400
    assert _put(client, username="alice", password='has"quote-inside').status_code == 400
    assert _put(client, devices=["192.168.1.5"]).status_code == 400
    assert _put(client, devices=["0.0.0.0"]).status_code == 400
    assert _put(client, enabled=True).status_code == 400            # no account yet
    r = _put(client, username="alice", password=PW, enabled=True, devices=["127.0.0.1"])
    assert r.status_code == 200, r.text
    assert _put(client, user="bob", username="ALICE", password=PW).status_code == 400   # taken


def test_settings_never_echo_the_password(client, env, caplog):
    caplog.set_level(logging.DEBUG)
    r = _put(client, username="alice", password=PW, enabled=True)
    assert r.status_code == 200 and r.json()["has_password"] is True
    g = client.get("/api/telephony/sip", headers={"x-test-user": "alice"})
    assert PW not in r.text and PW not in g.text and "password" not in {k for k in g.json() if k != "has_password"}
    raw = Path(prefs_routes.PREFS_FILE).read_text()
    assert PW not in raw                                             # encrypted at rest
    assert sip_line.password(config.get_config("alice")["sip"]) == PW
    assert PW not in caplog.text
    # The generic prefs API does not show the phone settings at all.
    assert "phone_calls" in prefs_routes.PRIVATE_KEYS


def test_test_button(client, env):
    _put(client, username="alice", password=PW, enabled=True)
    r = client.post("/api/telephony/sip/test", headers={"x-test-user": "alice"}).json()
    names = {c["name"]: c for c in r["checks"]}
    assert names["Turned on"]["ok"] and names["Account"]["ok"]
    assert not names["Listening"]["ok"]                              # the listener is not up in this test
    assert names["Softphone registered"]["optional"]
    assert r["ok"] is False


def test_linphone_qr_works_once_and_carries_no_password(client, env, monkeypatch):
    _put(client, username="alice", password=PW, enabled=True)

    class FakeServer:
        running = True
        listeners = [("udp", "100.121.62.9", 5060), ("tcp", "100.121.62.9", 5060)]

        def registered(self, user):
            return []

    monkeypatch.setattr(env["line"], "server", FakeServer())
    r = client.post("/api/telephony/sip/provision", headers={"x-test-user": "alice", "host": "ody.tail1.ts.net"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qr_svg"].startswith("<") and "<svg" in body["qr_svg"] and not body["warning"]
    path = body["url"].split("ody.tail1.ts.net", 1)[1]
    funneled = client.get(path, headers={"Tailscale-Funnel-Request": "?1"})
    assert funneled.status_code == 404                               # never over Funnel
    x = client.get(path)
    assert x.status_code == 200 and x.headers["content-type"].startswith("application/xml")
    import hashlib
    assert PW not in x.text
    assert hashlib.md5(f"alice:odysseus:{PW}".encode()).hexdigest() in x.text
    assert f"&lt;sip:100.121.62.9:{env['port']};transport=udp&gt;" in x.text and "transient_provisioning" in x.text
    assert client.get(path).status_code == 404                       # once
    assert client.get("/api/telephony/sip/provision/" + "A" * 43 + ".xml").status_code == 404


def test_the_provisioning_path_is_the_only_new_login_exemption():
    src = (ROOT / "app.py").read_text()
    pat = re.search(r'_re\.compile\(r"(\^/api/telephony/sip/[^"]+)"\)', src).group(1)
    assert re.match(pat, "/api/telephony/sip/provision/" + "a" * 43 + ".xml")
    for p in ("/api/telephony/sip", "/api/telephony/sip/test", "/api/telephony/sip/call-me",
              "/api/telephony/sip/provision", "/api/telephony/sip/provision/short.xml"):
        assert not re.match(pat, p), p


def test_the_settings_card_has_every_field_its_script_uses():
    js = (ROOT / "static" / "js" / "devicesSettings.js").read_text()
    html = (ROOT / "static" / "index.html").read_text()
    ids = set(re.findall(r"\$\('(sip-[a-z-]+)'\)", js)) | set(re.findall(r"#(sip-[a-z-]+)", js))
    assert {"sip-enabled", "sip-password", "sip-test", "sip-call-me", "sip-qr", "sip-dial"} <= ids
    for i in sorted(ids):
        assert f'id="{i}"' in html, i
    assert "$('sip-password').value = ''" in js and "cfg.password" not in js
    assert "Free SIP line (tailnet)" in html
    sw = (ROOT / "static" / "sw.js").read_text()
    assert int(re.search(r"CACHE_NAME = 'odysseus-v(\d+)'", sw).group(1)) >= 354


def test_docs_cover_linphone_zoiper_and_troubleshooting():
    doc = (ROOT / "docs" / "sip-line.md").read_text()
    for word in ("Linphone", "Zoiper", "PCMU", "battery", "NAT", "Troubleshooting"):
        assert word in doc, word
    assert chr(0x2014) not in doc
    assert "sip-line.md" in (ROOT / "docs" / "phone-calls.md").read_text()
