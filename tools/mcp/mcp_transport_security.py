"""Shared transport-security setup for the desktop MCP servers.

mcp 1.13 added DNS-rebinding protection to the SSE transport: it checks the
Host header against an allowlist that defaults to localhost only. Anything
bound to a tailnet address therefore answers every request with a bare
`421 Misdirected Request`, which names neither the header nor the setting and
looks for all the world like a broken server.

The protection is worth keeping — it is what stops a web page in a browser on
the same machine from driving these tools via a rebound DNS name. So rather
than disable it, or pin back to a version that never had it, each server
declares the hosts it is legitimately reached on.

Kept in one place because all three servers need the identical treatment, and
because a future `pip install --upgrade mcp` on the Windows boxes (still on
1.12.2, which predates the check) would otherwise break them exactly the same
way, with the same unhelpful 421.
"""

import os
import socket
from typing import List, Optional


def allowed_hosts(host: str, port: int, extra_env: str = "") -> List[str]:
    """Every host:port this server should accept a Host header for.

    Covers the bound address, loopback, the machine's own hostname, and its
    MagicDNS name, because which of those a client uses is the client's
    choice. Ports matter: the check compares the whole `host:port`.
    """
    names = {host, "localhost", "127.0.0.1"}

    hostname = socket.gethostname()
    names.add(hostname)
    # MagicDNS: `<host>.<tailnet>.ts.net`. Resolving our own name is the least
    # fragile way to learn it without shelling out to tailscale.
    try:
        fqdn = socket.getfqdn()
        if fqdn and fqdn != hostname:
            names.add(fqdn)
    except Exception:
        pass

    for extra in (os.environ.get(extra_env, "") if extra_env else "").split(","):
        extra = extra.strip()
        if extra:
            names.add(extra)

    out = []
    for n in sorted(n for n in names if n):
        out.append(f"{n}:{port}")
        out.append(n)          # some clients omit the port on default schemes
    return out


def security_settings(host: str, port: int, extra_env: str = ""):
    """TransportSecuritySettings for this bind, or None on older mcp.

    Returning None rather than raising keeps these servers working on mcp
    1.12.x, which has no such module and needs no such setting.
    """
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:
        return None
    hosts = allowed_hosts(host, port, extra_env)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        # Browsers send Origin; these servers are spoken to by Odysseus, not by
        # a page, so an empty Origin is normal and anything else is suspect.
        allowed_origins=[f"http://{h}" for h in hosts],
    )
