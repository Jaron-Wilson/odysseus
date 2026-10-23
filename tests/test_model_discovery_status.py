"""Model discovery scans this server and its online tailnet peers. The peers
come from the network add-on (src/machines.py, `network.peers`); without one
there are none to scan."""
from src import machines, model_discovery


def _peer(host, ips, online=True, os="linux", is_self=False, shared=False, name=None):
    return {"host": host, "name": name or host, "dns": f"{host}.tail.ts.net", "ips": ips, "os": os,
            "online": online, "is_self": is_self, "shared": shared, "last_seen": "", "age_days": 0.0}


def _fresh(monkeypatch, peers):
    monkeypatch.setattr(machines, "peers", lambda refresh=False: peers)
    model_discovery._hosts_cache = []
    model_discovery._hosts_cache_time = 0


def test_discovery_scans_self_and_online_peers(monkeypatch):
    _fresh(monkeypatch, [
        _peer("box", ["fd7a::2", "100.1.1.2"]),
        _peer("me", ["100.1.1.1"], is_self=True),
        _peer("asleep", ["100.1.1.4"], online=False),
        _peer("phone", ["100.1.1.5"], os="android"),
        _peer("friend", ["100.1.1.6"], shared=True),
        _peer("ingress", ["100.1.1.7"], name="funnel-ingress-node"),
    ])
    assert model_discovery.discover_tailscale_hosts() == ["100.1.1.1", "100.1.1.2"]


def test_discovery_ignores_peers_without_an_ipv4(monkeypatch):
    _fresh(monkeypatch, [_peer("v6", ["fd7a::3"]), _peer("ok", ["100.1.1.3"])])
    assert model_discovery.discover_tailscale_hosts() == ["100.1.1.3"]


def test_no_network_add_on_means_no_peers_to_scan(monkeypatch):
    _fresh(monkeypatch, [])
    assert model_discovery.discover_tailscale_hosts() == []
