"""Outbound URL safety checks (SSRF hardening).

Run before the server makes a request to a *user-supplied* URL — e.g. the custom
embedding endpoint set via ``POST /api/embeddings/endpoint``, which then triggers
an outbound ``httpx`` call.

Odysseus is local-first: pointing the embedding endpoint at a loopback or LAN
address (a local vLLM / llama.cpp / Ollama server) is a normal, intended setup.
So this guard does **not** blanket-block private addresses by default — that would
break the primary use case. What it *always* rejects:

  - a non-HTTP(S) scheme (``file://``, ``gopher://``, ``ftp://`` …), and
  - the link-local range (``169.254.0.0/16`` / ``fe80::/10``), i.e. the cloud
    instance-metadata SSRF credential-exfil vector — nobody serves embeddings
    there — plus multicast / reserved / unspecified addresses.

For exposed multi-tenant deployments, set ``EMBEDDING_BLOCK_PRIVATE_IPS=true`` to
additionally reject all private and loopback targets (full SSRF lockdown).
"""

import ipaddress
import socket
import time
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

import httpcore
import httpx

ALLOWED_SCHEMES = ("http", "https")


def _default_resolver(host: str) -> List[str]:
    """Resolve a hostname to the list of IP strings it maps to (A + AAAA)."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _classify(ip: ipaddress._BaseAddress, *, block_private: bool) -> Optional[str]:
    """Return a rejection reason for an IP, or None if it is allowed."""
    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) — judge the embedded v4.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local:
        return f"link-local address blocked (SSRF metadata risk): {ip}"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return f"disallowed address: {ip}"
    if block_private and (ip.is_private or ip.is_loopback):
        return f"private/loopback address blocked: {ip}"
    return None


def resolve_and_check(
    url: str,
    *,
    block_private: bool = False,
    resolver: Optional[Callable[[str], List[str]]] = None,
) -> Tuple[bool, str, List["ipaddress._BaseAddress"]]:
    """Validate a user-supplied outbound URL and return the IPs it resolved to.

    Returns ``(ok, reason, ips)``. ``ok`` is True only when the URL is safe to
    fetch, in which case ``ips`` is the de-duplicated, order-preserved list of
    every address the host resolved to (all of which passed the check) — the
    caller should pin its connection to one of these rather than letting the
    HTTP client re-resolve the host, which would reopen a DNS-rebinding TOCTOU
    between this check and the actual connect.
    """
    if not isinstance(url, str):
        return False, "URL must be a string", []
    if not url or not url.strip():
        return False, "URL is required", []
    try:
        parsed = urlparse(url.strip())
    except Exception as e:  # pragma: no cover - urlparse is very tolerant
        return False, f"unparseable URL: {e}", []

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False, f"scheme must be http or https, got '{parsed.scheme or '(none)'}'", []
    host = parsed.hostname
    if not host:
        return False, "URL has no host", []

    resolve = resolver or _default_resolver
    try:
        raw_ips = resolve(host)
    except Exception as e:
        return False, f"host does not resolve: {e}", []
    if not raw_ips:
        return False, "host does not resolve", []

    ips: List[ipaddress._BaseAddress] = []
    seen = set()
    for raw in raw_ips:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])  # strip IPv6 zone id
        except ValueError:
            continue
        reason = _classify(ip, block_private=block_private)
        if reason:
            return False, reason, []
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    if not ips:
        return False, "host did not resolve to a usable address", []
    return True, "ok", ips


def check_outbound_url(
    url: str,
    *,
    block_private: bool = False,
    resolver: Optional[Callable[[str], List[str]]] = None,
) -> Tuple[bool, str]:
    """Validate a user-supplied outbound URL.

    Returns ``(ok, reason)``. ``ok`` is True only when the URL is safe to fetch.
    ``resolver`` is injectable so callers/tests can avoid real DNS.

    Thin wrapper around ``resolve_and_check`` for callers that only need the
    accept/reject decision. Callers that then make an outbound request should
    use ``resolve_and_check`` (or ``PinnedAsyncTransport``) directly instead, so
    the IPs that were actually validated are the ones the connect uses — this
    function by itself does not defend against DNS rebinding between the check
    and a later, independent connect.
    """
    ok, reason, _ips = resolve_and_check(url, block_private=block_private, resolver=resolver)
    return ok, reason


# httpcore raises its own exception hierarchy; map the ones a simple request can
# surface back to their httpx equivalents so callers' `except httpx.*` blocks
# behave exactly as they did with the default transport.
_HTTPCORE_TO_HTTPX_EXC = {
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.WriteError: httpx.WriteError,
    httpcore.WriteTimeout: httpx.WriteTimeout,
}


class PinnedAsyncBackend(httpcore.AsyncNetworkBackend):
    """Network backend that connects only to the pre-validated IPs, in order.

    Every address here came out of a single SSRF resolution (see
    ``resolve_and_check``), so falling back to the next one after a connect
    failure is not re-resolution — it is ordinary multi-address fallback
    restricted to the set the guard already approved. httpcore takes TLS SNI
    and the ``Host`` header from the request URL rather than the connect host,
    so pinning the socket destination leaves certificate validation and vhost
    routing pointed at the original hostname.
    """

    def __init__(self, ips: List["ipaddress._BaseAddress"]):
        self._ips = [str(ip) for ip in ips]
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        # One shared connect budget: each attempt gets the time left until the
        # original deadline, so N dead addresses can't stretch the connect
        # phase to N * timeout.
        deadline = None if timeout is None else time.monotonic() + timeout
        last_exc: Optional[Exception] = None
        for ip in self._ips:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                return await self._real.connect_tcp(
                    ip, port, remaining, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
                if deadline is not None and time.monotonic() >= deadline:
                    break
        raise last_exc

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return await self._real.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        return await self._real.sleep(seconds)


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """httpx transport that pins the TCP connect to the pre-resolved IP(s).

    Shared by every outbound surface that validates a user-supplied URL with
    this module (reminder webhook/ntfy sends, integration api_call, webhook
    delivery) so each one only has to resolve+validate once and pin the actual
    connect to that result, closing the DNS-rebinding TOCTOU between the check
    and a plain client's independent re-resolution at connect time. The request
    URL passes through unchanged, so SNI and the ``Host`` header stay the
    original hostname; only the socket destination is pinned.
    """

    def __init__(self, ips: List["ipaddress._BaseAddress"]):
        self._pool = httpcore.AsyncConnectionPool(
            # Reuse the CA trust a default httpx client would build (certifi
            # plus SSL_CERT_FILE / SSL_CERT_DIR when trust_env is set) so
            # swapping in this transport doesn't quietly change which chains
            # verify.
            ssl_context=httpx.create_ssl_context(),
            http1=True,
            http2=False,
            network_backend=PinnedAsyncBackend(ips),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            core_resp = await self._pool.handle_async_request(core_req)
            content = b"".join([chunk async for chunk in core_resp.aiter_stream()])
            await core_resp.aclose()
        except Exception as exc:
            mapped = _HTTPCORE_TO_HTTPX_EXC.get(type(exc))
            if mapped is not None:
                raise mapped(str(exc)) from exc
            raise
        return httpx.Response(
            status_code=core_resp.status,
            headers=core_resp.headers,
            content=content,
            extensions=core_resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()
