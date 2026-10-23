"""Settings > Devices by machine: tailnet peers, what runs on each, Ping and
the background reconnect.

Before this, MCP servers were listed as if each were a computer
("davinci-resolve" and "windows-desktop" looked like two PCs, Cloudflare's
servers looked like computers too), and a laptop asleep when Odysseus started
stayed "can't reach it" until a restart, even after it woke.
"""
import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import machines, mcp_health

NOW = time.time()


def _iso(days_ago):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - days_ago * 86400, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _p(host, name, ips, os, online=True, age=0.0, is_self=False, shared=False, suffix="tail90b62a"):
    return {"host": host, "name": name, "dns": f"{host}.{suffix}.ts.net", "ips": ips, "os": os,
            "online": online, "last_seen": "" if is_self else _iso(age), "age_days": age,
            "is_self": is_self, "shared": shared}


# What the Tailscale add-on's `peers` answers (Jaron-Wilson/odysseus-tailscale
# parses `tailscale status --json` into these).
PEERS = [
    _p("jaron-dev-server", "jaron-dev-server", ["100.121.62.9"], "linux", is_self=True),
    _p("desktop-jaron", "DESKTOP-JARON", ["100.102.86.125"], "windows"),
    _p("jaron-laptop", "jaron-laptop", ["100.103.158.40"], "linux", online=False, age=0.1),
    _p("pixel-8a", "Pixel 8a", ["100.96.131.64"], "android"),
    _p("pikvm", "pikvm", ["100.69.4.11"], "linux", online=False, age=40),
    _p("ubuntu-club-3090", "ubuntu-club-3090", ["100.109.202.13"], "linux", shared=True, suffix="tail361357"),
]

SERVERS = [
    {"id": "res", "name": "davinci-resolve", "url": "http://100.102.86.125:8930/sse", "transport": "sse", "enabled": True},
    {"id": "win", "name": "windows-desktop", "url": "http://100.102.86.125:8931/sse", "transport": "sse", "enabled": True},
    {"id": "lap", "name": "linux-laptop", "url": "http://jaron-laptop.tail90b62a.ts.net:8932/sse", "transport": "sse", "enabled": True},
    {"id": "cf", "name": "cloudflare", "url": "https://mcp.cloudflare.com/mcp", "transport": "http", "enabled": True},
    {"id": "loc", "name": "local-tool", "url": "", "transport": "stdio", "enabled": True},
]
PHONE = {"name": "pixel-8a", "kind": "phone", "endpoint": "http://pixel-8a.tail90b62a.ts.net:8778",
         "commands": ["notify", "open_url"], "has_token": True, "token_hint": "abcd", "aliases": []}


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(machines, "MACHINES_FILE", str(tmp_path / "machines.json"))
    monkeypatch.setattr(machines, "DATA_DIR", str(tmp_path))


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_peers_come_from_the_network_add_on_and_are_cleaned(monkeypatch):
    from src.mods import providers
    calls = []

    def fake(cap, action, payload=None, **kw):
        calls.append((cap, action))
        return _Resp(200, {"peers": PEERS + [{"host": ""}, "junk",
                                             dict(PEERS[1], host="PC2", online="yes", ips="x")]})

    monkeypatch.setattr(providers, "call_sync", fake)
    machines._status_cache.update(at=0.0, data=None)
    got = {p["host"]: p for p in machines.peers(refresh=True)}
    assert calls == [("network.peers", "peers")]
    assert got["jaron-dev-server"]["is_self"] and got["ubuntu-club-3090"]["shared"]
    assert got["pc2"]["online"] is True and got["pc2"]["ips"] == []
    assert len(got) == len(PEERS) + 1
    machines.peers()                                          # cached
    assert len(calls) == 1


def test_no_network_add_on_means_no_peers(monkeypatch):
    from src.mods import providers

    def none(*a, **k):
        raise providers.ProviderUnavailable("No add-on provides network.peers.", 404)

    monkeypatch.setattr(providers, "call_sync", none)
    monkeypatch.setattr(providers, "describe",
                        lambda cap: {"capability": cap, "providers": [], "installed_off": False})
    machines._status_cache.update(at=0.0, data=None)
    assert machines.peers(refresh=True) == []
    ov = machines.overview(SERVERS, {}, {}, [], [], {})
    assert ov["machines"] == [] and not ov["tailscale"]
    assert ov["network"] == {"provider": None, "installed_off": False, "error": ""}
    # Every MCP server with a URL is a service then; nothing is lost.
    assert {s["name"] for s in ov["services"]} >= {"davinci-resolve", "cloudflare"}
    assert asyncio.run(machines.tailscale_ping(PEERS[1])) is False


def test_overview_groups_servers_and_phones_under_machines():
    peers = list(PEERS)
    statuses = {"res": {"status": "connected", "tool_count": 7}, "win": {"status": "connected"},
                "lap": {"status": "error", "error": "x"}, "cf": {"status": "connected"}}
    tools = {"win": {"list_apps", "launch_app", "screenshot"}}
    ov = machines.overview(SERVERS, statuses, tools, [PHONE], peers,
                           {"desktop-jaron": {"preferred": True, "gpu": True}})
    by = {m["host"]: m for m in ov["machines"]}
    assert [s["name"] for s in by["desktop-jaron"]["servers"]] == ["davinci-resolve", "windows-desktop"]
    assert by["desktop-jaron"]["apps"] and by["desktop-jaron"]["preferred"] and by["desktop-jaron"]["gpu"]
    assert [s["name"] for s in by["jaron-laptop"]["servers"]] == ["linux-laptop"]
    assert [s["name"] for s in by["jaron-dev-server"]["servers"]] == ["local-tool"]   # stdio runs here
    assert [p["name"] for p in by["pixel-8a"]["phones"]] == ["pixel-8a"]
    assert [s["name"] for s in ov["services"]] == ["cloudflare"]
    assert ov["machines"][0]["host"] == "desktop-jaron"                 # preferred first
    others = {p["host"] for p in ov["others"]}
    assert others == {"pikvm", "ubuntu-club-3090"}                    # stale, shared


def test_a_computer_registered_for_notifications_sits_under_its_machine():
    """No listener address, so it is matched by name, not dropped into
    Services (linking the PC's browser to the PC registers it this way)."""
    peers = list(PEERS)
    pc = {"name": "desktop-jaron", "kind": "desktop", "endpoint": "", "commands": ["notify"],
          "has_token": True, "token_hint": "x", "aliases": ["windows-desktop"]}
    ov = machines.overview([], {}, {}, [pc], peers, {})
    by = {m["host"]: m for m in ov["machines"]}
    assert [p["name"] for p in by["desktop-jaron"]["phones"]] == ["desktop-jaron"]
    assert ov["services"] == []


def test_only_one_machine_is_preferred(prefs_file):
    machines.set_prefs("desktop-jaron", preferred=True, gpu=True)
    machines.set_prefs("jaron-laptop", preferred=True)
    p = machines.load_prefs()
    assert p["jaron-laptop"]["preferred"] and "preferred" not in p["desktop-jaron"]
    assert p["desktop-jaron"]["gpu"]
    with pytest.raises(ValueError):
        machines.set_prefs("../etc", preferred=True)


def test_start_commands_are_fixed_per_os():
    win = machines.start_command("windows")
    assert win[0] == "powershell" and "Get-ScheduledTask -TaskName 'Odysseus*'" in win[-1]
    assert "systemctl --user start 'odysseus-*.service'" in machines.start_command("linux")[-1]
    assert machines.start_command("android") is None
    argv = machines._ssh_argv("jaron", "desktop-jaron.tail90b62a.ts.net", "/k", win, "windows")
    assert argv[-1].startswith("powershell -NoProfile -EncodedCommand ")   # cmd.exe-safe
    assert "BatchMode=yes" in argv


def test_agent_summary_names_the_preferred_machine():
    peers = list(PEERS)
    ov = machines.overview(SERVERS, {"res": {"status": "connected"}}, {}, [PHONE], peers,
                           {"desktop-jaron": {"preferred": True, "gpu": True}})
    text = machines.summary_for_agent(ov)
    assert "DESKTOP-JARON (desktop-jaron): windows; online; preferred for heavy work; has a GPU; MCP: davinci-resolve" in text
    assert "jaron-dev-server" not in text                              # this server itself


class _Mgr:
    def __init__(self, statuses, reconnect_ok):
        self.statuses, self.reconnect_ok, self.reconnects = statuses, reconnect_ok, []

    def get_server_status(self, sid):
        return self.statuses.get(sid, {"status": "disconnected"})

    async def _reconnect_configured(self, sid):
        self.reconnects.append(sid)
        ok = self.reconnect_ok(sid, len(self.reconnects))
        if ok:
            self.statuses[sid] = {"status": "connected"}
        return ok


def test_health_check_reconnects_online_machines_and_skips_offline(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    mcp_health._last_start.clear()
    peers = list(PEERS)
    mgr = _Mgr({"res": {"status": "error"}, "win": {"status": "connected"},
                "lap": {"status": "error"}, "cf": {"status": "error"}}, lambda sid, n: True)
    done = asyncio.run(mcp_health.check_once(mgr, SERVERS, peers, {}, _never_start))
    assert done == {"davinci-resolve": "reconnected", "linux-laptop": "machine offline"}
    assert mgr.reconnects == ["res"]                        # not the offline laptop, not the service


def test_health_check_starts_services_once_then_backs_off(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    mcp_health._last_start.clear()
    peers = list(PEERS)
    starts = []

    async def start(peer, user):
        starts.append((peer["host"], user))
        return {"ok": True, "started": ["OdysseusDesktopMCP"]}

    # First reconnect fails; after the start, the next one succeeds.
    mgr = _Mgr({"res": {"status": "error"}}, lambda sid, n: n >= 2)
    done = asyncio.run(mcp_health.check_once(mgr, SERVERS[:1], peers, {}, start, now=1000.0))
    assert done == {"davinci-resolve": "started and reconnected"} and len(starts) == 1

    mgr2 = _Mgr({"res": {"status": "error"}}, lambda sid, n: False)
    asyncio.run(mcp_health.check_once(mgr2, SERVERS[:1], peers, {}, start, now=1100.0))
    assert len(starts) == 1                                  # within the backoff: no second SSH


async def _never_start(peer, user):
    raise AssertionError("should not SSH when the reconnect worked")


async def _nosleep(*_a, **_k):
    return None


def test_routes_overview_prefs_and_ping(prefs_file, monkeypatch):
    from routes import device_routes
    peers = list(PEERS)
    monkeypatch.setattr(device_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(device_routes, "_configured_servers", lambda: SERVERS)
    monkeypatch.setattr(machines, "peers", lambda refresh=False: peers)
    mgr = _Mgr({"res": {"status": "error"}, "win": {"status": "error"}}, lambda sid, n: False)
    mgr.get_all_tools = lambda: []
    mgr.get_all_statuses = lambda: dict(mgr.statuses)
    monkeypatch.setattr(device_routes, "_mcp", lambda: mgr)
    monkeypatch.setattr(device_routes.devices, "list_devices", lambda: [])

    async def pong(peer):
        return True

    started = []

    async def start(peer, user):
        started.append(peer["host"])
        return {"ok": False, "error": "SSH was refused"}

    monkeypatch.setattr(machines, "tailscale_ping", pong)
    monkeypatch.setattr(machines, "start_services", start)
    app = FastAPI()
    app.include_router(device_routes.setup_device_routes())
    c = TestClient(app)

    ov = c.get("/api/devices/overview").json()
    assert {m["host"] for m in ov["machines"]} >= {"desktop-jaron", "jaron-laptop"}

    assert c.post("/api/devices/machines/desktop-jaron/prefs", json={"preferred": True}).json()["prefs"]["preferred"]
    r = c.post("/api/devices/machines/desktop-jaron/ping").json()
    assert r["reachable"] and not r["ok"] and started == ["desktop-jaron"]
    assert r["started"]["error"] == "SSH was refused"
    assert any("DaVinci Resolve itself" in n for n in r["notes"])
    assert c.post("/api/devices/machines/nope/ping").json()["ok"] is False


class _ProbingMgr(_Mgr):
    def __init__(self, statuses, probes):
        super().__init__(statuses, lambda sid, n: True)
        self.probes = probes

    async def probe(self, sid, timeout=6.0):
        return self.probes[sid]


def test_a_connected_server_that_restarted_is_reconnected():
    # Seen live: the PC's desktop MCP was restarted with two new tools, and
    # Odysseus stayed "connected" to the old process, with the old tool list,
    # until Reconnect was pressed by hand.
    mcp_health._last_start.clear()
    peers = list(PEERS)
    servers = SERVERS[:2]                                    # both on the PC, which is online
    mgr = _ProbingMgr({"res": {"status": "connected"}, "win": {"status": "connected"}},
                      {"res": {"alive": True, "changed": False}, "win": {"alive": True, "changed": True}})
    done = asyncio.run(mcp_health.check_once(mgr, servers, peers, {}, _never_start))
    assert done == {"windows-desktop": "tools changed, reconnected"} and mgr.reconnects == ["win"]
    mgr = _ProbingMgr({"res": {"status": "connected"}, "win": {"status": "connected"}},
                      {"res": {"alive": False, "changed": False}, "win": {"alive": True, "changed": False}})
    done = asyncio.run(mcp_health.check_once(mgr, servers, peers, {}, _never_start))
    assert done == {"davinci-resolve": "stale, reconnected"} and mgr.reconnects == ["res"]


def test_probe_compares_tools_and_notices_a_dead_session():
    from types import SimpleNamespace
    from src.mcp_manager import McpManager as MCPManager

    class _Session:
        def __init__(self, names=None, dead=False):
            self.names, self.dead = names or [], dead

        async def list_tools(self):
            if self.dead:
                raise ConnectionError("closed")
            return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in self.names])
    m = MCPManager.__new__(MCPManager)
    m._sessions, m._tools = {}, {}
    m._sessions["win"] = _Session(["now_playing", "bluetooth_pair"])
    m._tools["win"] = [{"name": "now_playing"}]
    assert asyncio.run(m.probe("win")) == {"alive": True, "changed": True, "tool_count": 2}
    m._tools["win"] = [{"name": "bluetooth_pair"}, {"name": "now_playing"}]
    assert asyncio.run(m.probe("win"))["changed"] is False
    m._sessions["win"] = _Session(dead=True)
    assert asyncio.run(m.probe("win"))["alive"] is False
    assert asyncio.run(m.probe("nope")) == {"alive": False, "changed": False}


def test_devices_page_points_to_the_add_on_without_one():
    import os
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "static", "js", "devicesSettings.js")).read()
    assert "Install the Tailscale add-on" in js and "modLinks.browse('tailscale')" in js
    assert "Tailscale is not running on this server" not in js
