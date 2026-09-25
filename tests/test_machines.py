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


TS = {
    "Self": {"HostName": "jaron-dev-server", "DNSName": "jaron-dev-server.tail90b62a.ts.net.",
             "TailscaleIPs": ["100.121.62.9"], "OS": "linux"},
    "Peer": {
        "a": {"HostName": "DESKTOP-JARON", "DNSName": "desktop-jaron.tail90b62a.ts.net.",
              "TailscaleIPs": ["100.102.86.125"], "OS": "windows", "Online": True, "LastSeen": _iso(0)},
        "b": {"HostName": "jaron-laptop", "DNSName": "jaron-laptop.tail90b62a.ts.net.",
              "TailscaleIPs": ["100.103.158.40"], "OS": "linux", "Online": False, "LastSeen": _iso(0.1)},
        "c": {"HostName": "Pixel 8a", "DNSName": "pixel-8a.tail90b62a.ts.net.",
              "TailscaleIPs": ["100.96.131.64"], "OS": "android", "Online": True, "LastSeen": _iso(0)},
        "d": {"HostName": "pikvm", "DNSName": "pikvm.tail90b62a.ts.net.",
              "TailscaleIPs": ["100.69.4.11"], "OS": "linux", "Online": False, "LastSeen": _iso(40)},
        "e": {"HostName": "ubuntu-club-3090", "DNSName": "ubuntu-club-3090.tail361357.ts.net.",
              "TailscaleIPs": ["100.109.202.13"], "OS": "linux", "Online": True, "LastSeen": _iso(0)},
    },
}

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


def test_parse_peers_marks_self_shared_and_age():
    ps = {p["host"]: p for p in machines.parse_peers(TS, now=NOW)}
    assert ps["jaron-dev-server"]["is_self"] and ps["jaron-dev-server"]["online"]
    assert ps["desktop-jaron"]["os"] == "windows" and not ps["desktop-jaron"]["shared"]
    assert ps["ubuntu-club-3090"]["shared"]                 # another tailnet's machine
    assert ps["pikvm"]["age_days"] > 30


def test_recent_handshake_counts_as_online_when_the_flag_blinks():
    from datetime import datetime, timezone
    hs = datetime.fromtimestamp(NOW - 30, tz=timezone.utc).astimezone().isoformat(timespec="microseconds")
    hs = hs.replace(".", ".", 1)[:-6] + "123" + hs[-6:]      # nanosecond digits, as tailscale prints
    data = {"Self": TS["Self"], "Peer": {"b": dict(TS["Peer"]["b"], Online=False, LastHandshake=hs)}}
    lap = [p for p in machines.parse_peers(data, now=NOW) if p["host"] == "jaron-laptop"][0]
    assert lap["online"]
    old = {"Self": TS["Self"], "Peer": {"b": dict(TS["Peer"]["b"], Online=False,
                                                    LastHandshake="0001-01-01T00:00:00Z")}}
    assert not [p for p in machines.parse_peers(old, now=NOW) if p["host"] == "jaron-laptop"][0]["online"]


def test_overview_groups_servers_and_phones_under_machines():
    peers = machines.parse_peers(TS, now=NOW)
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
    peers = machines.parse_peers(TS, now=NOW)
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
    peers = machines.parse_peers(TS, now=NOW)
    mgr = _Mgr({"res": {"status": "error"}, "win": {"status": "connected"},
                "lap": {"status": "error"}, "cf": {"status": "error"}}, lambda sid, n: True)
    done = asyncio.run(mcp_health.check_once(mgr, SERVERS, peers, {}, _never_start))
    assert done == {"davinci-resolve": "reconnected", "linux-laptop": "machine offline"}
    assert mgr.reconnects == ["res"]                        # not the offline laptop, not the service


def test_health_check_starts_services_once_then_backs_off(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    mcp_health._last_start.clear()
    peers = machines.parse_peers(TS, now=NOW)
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
    peers = machines.parse_peers(TS, now=NOW)
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
