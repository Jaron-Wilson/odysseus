"""Keep machine MCP servers connected while their machine is on.

MCP servers were connected once, at startup. A laptop asleep at that moment
stayed "can't reach it" in Settings > Devices -- and unusable to the agent --
until Odysseus restarted, even after it woke up. And a PC whose servers had
stopped stayed down until someone started them by hand.

Every minute this looks at the configured servers that are not connected.
If the machine a server runs on is online on the tailnet, it reconnects; if
that fails, it starts the machine's Odysseus services over SSH (at most once
per machine per START_BACKOFF) and tries again. Machines that are off are
left alone: there is nothing to do until they come back.
"""

import asyncio
import getpass
import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)

INTERVAL_S = 60
FIRST_DELAY_S = 90          # let startup's own connect attempt finish first
START_BACKOFF_S = 600

_last_start: Dict[str, float] = {}


async def check_once(mgr=None, servers=None, all_peers=None, prefs=None,
                     start_services=None, now=None) -> Dict[str, str]:
    """One pass. Returns {server name: what happened} for logging and tests."""
    from src import machines
    if mgr is None:
        from src.tool_utils import get_mcp_manager
        mgr = get_mcp_manager()
    if mgr is None:
        return {}
    if servers is None:
        from routes.device_routes import _configured_servers
        servers = _configured_servers()
    all_peers = machines.peers(refresh=True) if all_peers is None else all_peers
    prefs = machines.load_prefs() if prefs is None else prefs
    start_services = start_services or machines.start_services
    now = now or time.time()

    done: Dict[str, str] = {}
    by_host: Dict[str, list] = {}
    for s in servers:
        if not s.get("enabled", True) or not s.get("url"):
            continue
        st = mgr.get_server_status(s["id"]).get("status")
        if st in ("connected", "connecting", "needs_auth"):
            continue
        peer = machines.find_peer(all_peers, machines.url_host(s["url"]))
        if peer is None:
            continue                         # a service, not one of the machines
        if not peer["online"]:
            done[s["name"]] = "machine offline"
            continue
        by_host.setdefault(peer["host"], [peer, []])[1].append(s)

    for host, (peer, down) in by_host.items():
        still = []
        for s in down:
            ok = await mgr._reconnect_configured(s["id"])
            done[s["name"]] = "reconnected" if ok else "reconnect failed"
            if not ok:
                still.append(s)
        if not still or peer.get("is_self"):
            continue
        if now - _last_start.get(host, 0) < START_BACKOFF_S:
            continue
        _last_start[host] = now
        user = prefs.get(host, {}).get("ssh_user") or getpass.getuser()
        r = await start_services(peer, user)
        if not r.get("ok"):
            for s in still:
                done[s["name"]] = f"start failed: {r.get('error', '')[:120]}"
            continue
        await asyncio.sleep(8)
        for s in still:
            ok = await mgr._reconnect_configured(s["id"])
            done[s["name"]] = "started and reconnected" if ok else "started, still down"
    return done


async def run_forever():
    await asyncio.sleep(FIRST_DELAY_S)
    while True:
        try:
            result = await check_once()
            changed = {k: v for k, v in result.items() if v != "machine offline"}
            if changed:
                logger.info("[mcp-health] %s", changed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[mcp-health] check failed: %s", e)
        await asyncio.sleep(INTERVAL_S)
