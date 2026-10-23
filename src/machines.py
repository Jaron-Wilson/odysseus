"""The user's machines, as the tailnet sees them, and what runs on each.

Settings > Devices used to list MCP servers as if each were a machine, so
"davinci-resolve" and "windows-desktop" looked like two computers and the
Cloudflare servers looked like computers too. What the user has is machines
-- a PC, a laptop, a phone -- each running zero or more MCP servers, plus
some services that are not on any machine at all.

The tailnet already knows the machines: the Tailscale add-on (it provides
`network.peers`) gives every peer's name, OS, addresses and whether it is
online. An MCP server belongs
to the peer its URL points at (by Tailscale IP or MagicDNS name); anything
else with a URL is a service; a stdio server runs here, on this server.

Also here: per-machine preferences the user sets (preferred for heavy work,
has a GPU), and Ping -- reconnect a machine's servers, and if the machine is
up but its servers are not, start them over SSH.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

MACHINES_FILE = os.path.join(DATA_DIR, "machines.json")
RECENT_DAYS = 30            # peers unseen for longer go under "other devices"
_STATUS_TTL = 15.0
_status_cache: Dict = {"at": 0.0, "data": None}


# ── Peers, from the network add-on ─────────────────────────────────────────
#
# Which machines exist comes from an add-on that provides `network.peers`
# (src/mods/providers.py), the Tailscale add-on (Jaron-Wilson/odysseus-tailscale)
# today. It answers `peers` with records in the shape below, already parsed;
# the core only cleans them. Without one there are no peers: Settings >
# Devices lists MCP servers as services and says to install the add-on.

PEERS_CAP = "network.peers"
_last_error: Dict = {"error": ""}

_PEER_FIELDS = {"host": str, "name": str, "dns": str, "os": str, "last_seen": str}


def _clean_peer(raw) -> Optional[Dict]:
    if not isinstance(raw, dict):
        return None
    out = {k: str(raw.get(k) or "")[:200] for k in _PEER_FIELDS}
    out["host"] = out["host"].lower()
    out["os"] = out["os"].lower()
    if not out["host"]:
        return None
    ips = raw.get("ips") if isinstance(raw.get("ips"), list) else []
    out["ips"] = [i[:64] for i in ips if isinstance(i, str)][:8]
    age = raw.get("age_days")
    out["age_days"] = float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None
    for k in ("online", "is_self", "shared"):
        out[k] = bool(raw.get(k))
    return out


def _fetch_peers() -> Optional[List[Dict]]:
    """The provider's peers, or None when there is no provider (or it failed)."""
    from src.mods import providers
    try:
        r = providers.call_sync(PEERS_CAP, "peers", None, method="GET", timeout=12)
    except providers.ProviderUnavailable as e:
        _last_error["error"] = e.message
        return None
    if r.status_code != 200:
        _last_error["error"] = f"The network add-on answered {r.status_code}."
        return None
    try:
        raw = r.json().get("peers") or []
    except (ValueError, AttributeError):
        raw = []
    _last_error["error"] = ""
    return [p for p in (_clean_peer(x) for x in raw[:500]) if p]


def peers(refresh: bool = False) -> List[Dict]:
    if refresh or time.time() - _status_cache["at"] > _STATUS_TTL:
        _status_cache.update(at=time.time(), data=_fetch_peers())
    return list(_status_cache["data"] or [])


def network_status() -> Dict:
    """For the Devices page: is there a network add-on, and did it answer."""
    from src.mods import providers
    d = providers.describe(PEERS_CAP)
    return {"provider": (d["providers"] or [None])[0], "installed_off": d["installed_off"],
            "error": _last_error["error"] if d["providers"] else ""}


def find_peer(all_peers: List[Dict], host_or_ip: str) -> Optional[Dict]:
    h = (host_or_ip or "").strip().lower().rstrip(".")
    if not h:
        return None
    for p in all_peers:
        if h in [i.lower() for i in p["ips"]] or h == p["dns"].lower() or h == p["host"]:
            return p
    short = h.split(".")[0]
    for p in all_peers:
        if short == p["host"]:
            return p
    return None


def url_host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


# ── Preferences ────────────────────────────────────────────────────────────

def load_prefs() -> Dict[str, Dict]:
    try:
        with open(MACHINES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def save_prefs(prefs: Dict[str, Dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".machines_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
        os.replace(tmp, MACHINES_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def set_prefs(host: str, **fields) -> Dict:
    """Set preferred / gpu / label / ssh_user for a machine. Only one machine
    is preferred at a time, since "prefer this one" means over the others."""
    host = (host or "").strip().lower()
    if not _HOST_RE.match(host):
        raise ValueError(f"not a machine name: {host!r}")
    prefs = load_prefs()
    cur = prefs.get(host, {})
    for k in ("preferred", "gpu"):
        if k in fields and fields[k] is not None:
            cur[k] = bool(fields[k])
    for k in ("label", "ssh_user"):
        if k in fields and fields[k] is not None:
            v = str(fields[k]).strip()[:48]
            if v:
                cur[k] = v
            else:
                cur.pop(k, None)
    if cur.get("preferred"):
        for other, p in prefs.items():
            if other != host:
                p.pop("preferred", None)
    prefs[host] = cur
    save_prefs(prefs)
    return cur


# ── The overview Settings > Devices shows ──────────────────────────────────

def overview(servers: List[Dict], statuses: Dict[str, Dict], tools: Dict[str, set],
             phones: List[Dict], all_peers: List[Dict], prefs: Dict[str, Dict]) -> Dict:
    """Machines with their MCP servers and phones, services, and the rest.

    `servers`  configured MCP rows: {id, name, url, transport, enabled}
    `statuses` mcp_manager status per server id
    `tools`    tool names per server id
    `phones`   device-registry records (already public, no token)
    """
    machines: Dict[str, Dict] = {}
    services: List[Dict] = []

    def machine_for(peer: Dict) -> Dict:
        m = machines.get(peer["host"])
        if m is None:
            p = prefs.get(peer["host"], {})
            m = dict(peer, servers=[], phones=[], label=p.get("label") or peer["name"],
                     preferred=bool(p.get("preferred")), gpu=bool(p.get("gpu")),
                     apps=False)
            machines[peer["host"]] = m
        return m

    for s in servers:
        st = statuses.get(s["id"], {})
        t = tools.get(s["id"], set())
        row = {
            "id": s["id"], "name": s["name"], "enabled": s.get("enabled", True),
            "status": st.get("status", "disconnected"), "error": st.get("error"),
            "tool_count": st.get("tool_count", len(t)),
            "apps": "list_apps" in t, "screen": "screenshot" in t,
        }
        host = url_host(s.get("url", ""))
        peer = find_peer(all_peers, host) if host else None
        if peer is None and not host:
            peer = next((p for p in all_peers if p["is_self"]), None)   # stdio: runs here
        if peer is not None:
            m = machine_for(peer)
            m["servers"].append(row)
            m["apps"] = m["apps"] or (row["apps"] and row["status"] == "connected")
        else:
            services.append(row)

    for ph in phones:
        # By its listener's address, or -- for a computer registered only for
        # notifications, which has no listener -- by its name.
        peer = find_peer(all_peers, url_host(ph.get("endpoint", ""))) or find_peer(all_peers, ph["name"])
        if peer is not None:
            machine_for(peer)["phones"].append(ph)
        else:
            services.append({"id": "", "name": ph["name"], "phone": ph, "status": "registered"})

    for p in all_peers:
        if p["shared"] or p["host"] in machines:
            continue
        if p["is_self"] or (p["age_days"] is not None and p["age_days"] <= RECENT_DAYS):
            machine_for(p)

    order = sorted(machines.values(), key=lambda m: (
        not m["preferred"], not m["online"], m["is_self"], m["label"].lower()))
    others = [p for p in all_peers if p["host"] not in machines]
    others.sort(key=lambda p: (p["shared"], not p["online"], p["name"].lower()))
    return {"machines": order, "services": services, "others": others,
            "tailscale": bool(all_peers), "network": network_status()}


def summary_for_agent(ov: Dict) -> str:
    """One short block the agent can read to pick a machine."""
    lines = []
    for m in ov.get("machines", []):
        if m["is_self"]:
            continue
        bits = [m["os"] or "?", "online" if m["online"] else "offline"]
        if m["preferred"]:
            bits.append("preferred for heavy work")
        if m["gpu"]:
            bits.append("has a GPU")
        up = [s["name"] for s in m["servers"] if s["status"] == "connected"]
        if up:
            bits.append("MCP: " + ", ".join(up))
        if m["phones"]:
            bits.append("device: " + ", ".join(p["name"] for p in m["phones"]))
        lines.append(f"- {m['label']} ({m['host']}): " + "; ".join(bits))
    return "\n".join(lines)


# ── Ping ───────────────────────────────────────────────────────────────────

def _ssh_keys() -> List[str]:
    home = os.path.expanduser("~/.ssh")
    return [k for k in (os.path.join(home, "odysseus_mcp"), os.path.join(home, "id_ed25519"),
                        os.path.join(home, "id_rsa")) if os.path.exists(k)]


def start_command(os_name: str) -> Optional[List[str]]:
    """The fixed, remote-side command that starts Odysseus's MCP services.

    Windows: the Odysseus* scheduled tasks. They are interactive-session
    tasks, so the servers land on the logged-in desktop (the only place app
    and Resolve control work); starting the servers directly over SSH would
    put them in the SSH session, and they would die when it closed.
    Linux: the odysseus-* systemd --user units.
    """
    if os_name == "windows":
        return ["powershell", "-NoProfile", "-Command",
                "Get-ScheduledTask -TaskName 'Odysseus*' | Start-ScheduledTask; "
                "Get-ScheduledTask -TaskName 'Odysseus*' | Select-Object -ExpandProperty TaskName"]
    if os_name in ("linux",):
        return ["sh", "-c", "systemctl --user start 'odysseus-*.service' && "
                            "systemctl --user list-units 'odysseus-*' --no-legend --plain"]
    if os_name in ("macos", "darwin"):
        return ["sh", "-c", "launchctl list | grep -i odysseus | awk '{print $3}' | "
                            "xargs -n1 -I{} launchctl kickstart gui/$(id -u)/{}"]
    return None


def _ssh_argv(user: str, host: str, key: str, remote: List[str], os_name: str) -> List[str]:
    # Windows' OpenSSH lands in cmd.exe, which mangles quoted arguments; send
    # PowerShell as an encoded command there instead.
    if os_name == "windows" and remote[:1] == ["powershell"]:
        import base64
        enc = base64.b64encode(remote[-1].encode("utf-16-le")).decode()
        cmd = f"powershell -NoProfile -EncodedCommand {enc}"
    else:
        import shlex
        cmd = " ".join(shlex.quote(x) for x in remote)
    return ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new", f"{user}@{host}", cmd]


async def _run(argv: List[str], timeout: float) -> Dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"rc": proc.returncode, "out": out.decode(errors="replace").strip(),
                "err": err.decode(errors="replace").strip()}
    except asyncio.TimeoutError:
        return {"rc": -1, "out": "", "err": f"timed out after {timeout:.0f}s"}
    except FileNotFoundError as e:
        return {"rc": -1, "out": "", "err": str(e)}


async def start_services(peer: Dict, user: str) -> Dict:
    """SSH to the machine and start its Odysseus MCP services."""
    remote = start_command(peer["os"])
    if remote is None:
        return {"ok": False, "error": f"don't know how to start services on {peer['os'] or 'this OS'}"}
    host = peer["dns"] or (peer["ips"][0] if peer["ips"] else peer["host"])
    last = ""
    for key in _ssh_keys():
        r = await _run(_ssh_argv(user, host, key, remote, peer["os"]), timeout=40)
        if r["rc"] == 0:
            return {"ok": True, "started": [x for x in r["out"].splitlines() if x.strip()][:10]}
        last = r["err"] or r["out"]
        if "Permission denied" not in last:
            break               # a real failure, not the wrong key
    return {"ok": False, "error": ("SSH was refused; add this server's key to the machine "
                                   "(the install script in Settings > Devices does it)")
            if "Permission denied" in last else (last[:300] or "SSH failed")}


# Tasks on a machine the web app may start on request, by exact name. Kept
# out of the Odysseus* services pattern above so the health loop never
# starts them on its own.
USER_TASKS = {"MusicOverlay"}
# The same tasks on Linux: systemd --user units, started on the logged-in
# desktop's session (install-linux-desktop.sh installs them).
LINUX_USER_UNITS = {"MusicOverlay": "odysseus-music-overlay.service"}


async def run_user_task(peer: Dict, user: str, task: str) -> Dict:
    """Start one allowlisted scheduled task on a Windows machine over SSH.
    The task runs on the logged-in desktop (see start_command)."""
    if task not in USER_TASKS:
        return {"ok": False, "error": f"{task!r} is not a task Odysseus may start"}
    os_name = peer.get("os")
    if os_name == "linux" and task in LINUX_USER_UNITS:
        # Asked for: "on my linux device opened the website tried popup but it
        # didn't work".
        remote = ["systemctl", "--user", "start", LINUX_USER_UNITS[task]]
    elif os_name == "windows":
        remote = ["powershell", "-NoProfile", "-Command", f"Start-ScheduledTask -TaskName '{task}'"]
    else:
        return {"ok": False, "error": "only Windows and Linux machines have this task"}
    host = peer.get("dns") or (peer["ips"][0] if peer.get("ips") else peer["host"])
    last = ""
    for key in _ssh_keys():
        r = await _run(_ssh_argv(user, host, key, remote, os_name), timeout=30)
        if r["rc"] == 0:
            return {"ok": True}
        last = r["err"] or r["out"]
        if "Permission denied" not in last:
            break
    return {"ok": False, "error": last[:300] or "SSH failed"}


async def tailscale_ping(peer: Dict) -> bool:
    """Whether the machine answers over the private network (the add-on's
    `ping`). False without the add-on."""
    from src.mods import providers
    target = peer["ips"][0] if peer.get("ips") else peer.get("host", "")
    try:
        r = await providers.call(PEERS_CAP, "ping", {"target": target}, timeout=15)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except (providers.ProviderUnavailable, ValueError, AttributeError):
        return False


# ── Which device the user is chatting from ─────────────────────────────────
#
# Odysseus sits behind Tailscale Serve, which passes the client's tailnet
# address through, so a request says which machine it came from. Asked to
# "use my phone" from the phone, the agent had no idea the phone was the
# thing in the user's hand; told "this phone", it could not know which.

def client_device(ip: str, all_peers: Optional[List[Dict]] = None,
                  registry: Optional[List[Dict]] = None) -> Optional[Dict]:
    """The machine at `ip`, with any registered device on it, or None."""
    ip = (ip or "").strip()
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return None
    all_peers = peers() if all_peers is None else all_peers
    peer = find_peer(all_peers, ip)
    if peer is None or peer.get("is_self"):
        return None
    if registry is None:
        from src import devices as _devices
        registry = _devices.list_devices()
    reg = None
    for d in registry:
        host = url_host(d.get("endpoint", ""))
        if (host and find_peer([peer], host)) or find_peer([peer], d.get("name", "")):
            reg = d
            break
    return {"peer": peer, "device": reg}


def client_device_note(info: Optional[Dict]) -> str:
    """A short system-prompt note: where this message came from, and how to act there."""
    if not info:
        return ""
    p, d = info["peer"], info.get("device")
    os_name = p.get("os") or "unknown OS"
    phone = os_name in ("android", "ios", "ipados")
    kind = "phone" if phone else "computer"
    lines = [f"The user is sending this message from their {kind} **{p['name']}** "
             f"({os_name}, tailnet {', '.join(p['ips'][:1])}). When they say \"this {kind}\", "
             f"\"my {kind}\", \"here\" or \"on this device\", they mean it."]
    if d:
        cmds = [c for c in (d.get("commands") or []) if c != "notify"]
        if d.get("endpoint") and cmds:
            lines.append(
                f"It is registered as device `{d['name']}` with a listener that handles: "
                f"{', '.join(cmds)}. When they want to see something there, open it with manage_devices "
                f"{{\"action\":\"control\",\"name\":\"{d['name']}\",\"command\":\"open_url\","
                f"\"params\":{{\"url\":\"...\"}}}} (or open_app with a package). It happens "
                f"at once, with no tap. notify_device only shows a notification. Anything they "
                f"only want looked up, do in the background and tell them.")
        else:
            lines.append(f"It is registered as device `{d['name']}` (notifications only).")
    elif not phone:
        lines.append("If a Linux or Mac computer, reach it with bash `ssh "
                     f"{p['dns'] or p['host']}`; a Windows one through its MCP tools.")
    return " ".join(lines)
