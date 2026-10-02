"""Tests for outbound URL safety / SSRF hardening (src/url_safety.py).

A stub resolver is injected so the tests never touch real DNS.
"""

import asyncio
import http.server
import ipaddress
import socketserver
import threading

from src.url_safety import check_outbound_url, resolve_and_check, PinnedAsyncTransport


def _resolver(mapping):
    def resolve(host):
        if host in mapping:
            return mapping[host]
        raise OSError(f"unresolvable: {host}")
    return resolve


PUBLIC = _resolver({"example.com": ["93.184.216.34"]})
LOOPBACK = _resolver({"localhost": ["127.0.0.1"]})
LAN = _resolver({"nas.local": ["192.168.1.50"]})
METADATA = _resolver({"evil.example": ["169.254.169.254"]})
MAPPED_METADATA = _resolver({"evil6.example": ["::ffff:169.254.169.254"]})


def test_non_http_scheme_blocked():
    for url in ("file:///etc/passwd", "ftp://x/y", "gopher://h", "redis://h:6379"):
        ok, reason = check_outbound_url(url, resolver=PUBLIC)
        assert ok is False, url
        assert "scheme" in reason


def test_missing_host_or_empty_blocked():
    assert check_outbound_url("", resolver=PUBLIC)[0] is False
    assert check_outbound_url("http://", resolver=PUBLIC)[0] is False


def test_public_url_allowed():
    ok, reason = check_outbound_url("https://example.com/v1/embeddings", resolver=PUBLIC)
    assert ok is True, reason


def test_cloud_metadata_blocked_even_when_private_allowed():
    # The headline SSRF vector must be blocked regardless of block_private.
    ok, reason = check_outbound_url("http://evil.example/latest/meta-data/", resolver=METADATA)
    assert ok is False
    assert "link-local" in reason


def test_ipv4_mapped_metadata_blocked():
    ok, reason = check_outbound_url("http://evil6.example/", resolver=MAPPED_METADATA)
    assert ok is False
    assert "link-local" in reason


def test_loopback_and_lan_allowed_by_default_local_first():
    # Local-first: a localhost / LAN embedding server is a legitimate target.
    assert check_outbound_url("http://localhost:8080/v1", resolver=LOOPBACK)[0] is True
    assert check_outbound_url("http://nas.local:1234/v1", resolver=LAN)[0] is True


def test_strict_mode_blocks_private_and_loopback():
    ok, reason = check_outbound_url("http://localhost:8080", block_private=True, resolver=LOOPBACK)
    assert ok is False and "private" in reason
    ok, reason = check_outbound_url("http://nas.local", block_private=True, resolver=LAN)
    assert ok is False and "private" in reason


def test_unresolvable_host_blocked():
    ok, reason = check_outbound_url("http://does-not-resolve.invalid", resolver=PUBLIC)
    assert ok is False
    assert "resolve" in reason


# ---------------------------------------------------------------------------
# resolve_and_check + PinnedAsyncTransport — DNS-rebinding defense
#
# check_outbound_url only reports (ok, reason); a plain httpx client that then
# posts to the same URL re-resolves the host independently at connect time. A
# low-TTL DNS record can pass the check as a public IP and then flip to an
# internal address (169.254.169.254, 127.0.0.1, LAN) for the actual connect.
# resolve_and_check returns the exact IPs that were validated so the caller can
# pin the connect to them instead of letting the client re-resolve.
# ---------------------------------------------------------------------------

def test_resolve_and_check_returns_validated_ips_on_success():
    ok, reason, ips = resolve_and_check("https://example.com/v1", resolver=PUBLIC)
    assert ok is True, reason
    assert ips == [ipaddress.ip_address("93.184.216.34")]


def test_resolve_and_check_rejects_metadata_and_returns_no_ips():
    ok, reason, ips = resolve_and_check("http://evil.example/", resolver=METADATA)
    assert ok is False
    assert "link-local" in reason
    assert ips == []


def test_resolve_and_check_dedupes_repeated_addresses():
    dup_resolver = _resolver({"multi.example": [
        "93.184.216.34", "93.184.216.34", "93.184.216.34",
    ]})
    ok, reason, ips = resolve_and_check("https://multi.example/", resolver=dup_resolver)
    assert ok is True, reason
    assert ips == [ipaddress.ip_address("93.184.216.34")]


def _serve(handler):
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def test_pinned_transport_connects_to_pinned_ip_not_the_url_host():
    """A request whose URL host would never resolve is still delivered to the
    pinned loopback IP - proving the socket destination comes from the pin,
    not from the HTTP client re-resolving the URL host (the DNS-rebinding
    window this transport closes)."""
    import httpx

    hits = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv, port = _serve(_Handler)
    try:
        ip = ipaddress.ip_address("127.0.0.1")
        transport = PinnedAsyncTransport([ip])

        async def go():
            async with httpx.AsyncClient(transport=transport, timeout=5) as client:
                return await client.get(f"http://unresolvable.invalid:{port}/probe")

        resp = asyncio.run(go())
        assert resp.status_code == 204
        assert hits == ["/probe"]
    finally:
        srv.shutdown()


def test_strict_mode_blocks_cgnat_shared_space():
    # RFC 6598 shared/CGNAT space (100.64.0.0/10) is not globally routable.
    # A public redirect into it must be rejected under full SSRF lockdown,
    # even though ipaddress reports is_private=False for this range.
    CGNAT = _resolver({"svc.example": ["100.64.0.1"]})
    ok, reason = check_outbound_url("http://svc.example:8080", block_private=True, resolver=CGNAT)
    assert ok is False
    assert "blocked" in reason


def test_strict_mode_still_allows_public_ip():
    # The lockdown must not reject a legitimate globally-routable target.
    ok, reason = check_outbound_url("https://example.com/v1", block_private=True, resolver=PUBLIC)
    assert ok is True, reason


def test_resolver_values_must_include_a_parseable_ip():
    ok, reason = check_outbound_url(
        "https://example.test",
        resolver=lambda _host: [None, 123, "not-an-ip"],
    )

    assert ok is False
    assert "does not resolve to an IP" in reason


def test_resolver_skips_invalid_values_but_accepts_public_ip():
    ok, reason = check_outbound_url(
        "https://example.test",
        resolver=lambda _host: [None, "not-an-ip", "93.184.216.34"],
    )

    assert ok is True
    assert reason == "ok"
