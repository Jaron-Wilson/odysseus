"""The user's machines, as the tailnet sees them, and what runs on each.

Settings > Devices used to list MCP servers as if each were a machine, so
"davinci-resolve" and "windows-desktop" looked like two computers and the
Cloudflare servers looked like computers too. What the user has is machines
-- a PC, a laptop, a phone -- each running zero or more MCP servers, plus
some services that are not on any machine at all.

Tailscale already knows the machines: `tailscale status --json` gives every
peer's name, OS, addresses and whether it is online. An MCP server belongs
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
import subprocess
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


# ── Tailnet ────────────────────────────────────────────────────────────────

def _tailscale_json() -> Optional[dict]:
    try:
        p = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                           text=True, timeout=10)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None


def _short(dns: str) -> str:
    return (dns or "").rstrip(".").split(".")[0].lower()


def _suffix(dns: str) -> str:
    return ".".join((dns or "").rstrip(".").split(".")[1:]).lower()


def parse_peers(data: Optional[dict], now: Optional[float] = None) -> List[Dict]:
    """Tailnet peers (and this server) from `tailscale status --json`.

    Peers shared in from another tailnet are marked `shared`: they are
    someone else's machines, not the user's.
    """
    if not data:
        return []
    now = now or time.time()
    me = data.get("Self") or {}
    my_suffix = _suffix(me.get("DNSName", ""))
    out = []

    def rec(p: dict, is_self: bool) -> Dict:
        dns = (p.get("DNSName") or "").rstrip(".")
        last = p.get("LastSeen") or ""
        age_days = None
        if last and not last.startswith("0001"):
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
                age_days = max(0.0, (now - ts) / 86400)
            except ValueError:
                age_days = None
        # Tailscale's Online flag comes from its control server and can blink
        # off for a machine that is plainly reachable (seen: the laptop
        # "offline" while an SSH session to it was open). A direct handshake
        # in the last few minutes counts as online too.
        online = bool(p.get("Online"))
        hs = p.get("LastHandshake") or ""
        if not online and hs and not hs.startswith("0001"):
            try:
                from datetime import datetime
                online = now - datetime.fromisoformat(
                    re.sub(r"(\.\d{6})\d+", r"\1", hs).replace("Z", "+00:00")).timestamp() < 180
            except ValueError:
                pass
        return {
            "host": _short(dns) or (p.get("HostName") or "").lower(),
            "name": p.get("HostName") or _short(dns),
            "dns": dns,
            "ips": list(p.get("TailscaleIPs") or []),
            "os": (p.get("OS") or "").lower(),
            "online": True if is_self else online,
            "last_seen": "" if is_self else last,
            "age_days": 0.0 if is_self else age_days,
            "is_self": is_self,
            "shared": bool(my_suffix) and _suffix(dns) != my_suffix,
        }

    if me:
        out.append(rec(me, True))
    for p in (data.get("Peer") or {}).values():
        out.append(rec(p, False))
    return out


def peers(refresh: bool = False) -> List[Dict]:
    if refresh or time.time() - _status_cache["at"] > _STATUS_TTL:
        _status_cache.update(at=time.time(), data=_tailscale_json())
    return parse_peers(_status_cache["data"])


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
            "tailscale": bool(all_peers)}


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


async def tailscale_ping(peer: Dict) -> bool:
    target = peer["ips"][0] if peer["ips"] else peer["host"]
    r = await _run(["tailscale", "ping", "-c", "1", "--timeout", "5s", target], timeout=12)
    return r["rc"] == 0 and "pong" in r["out"].lower()
