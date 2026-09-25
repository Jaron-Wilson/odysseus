"""Registry of the user's own devices, so "my phone" resolves to one of them.

A device is a name, a delivery topic, and a list of commands its automation
actually honours. That last part is the point: the assistant should be able to
tell the user "this phone can launch apps but cannot install them" from the
registry rather than guessing, and should refuse a command the device never
claimed to support instead of pushing it into the void.

Stored as plain JSON next to the other per-install state, alongside the shared
secret each device's listener requires. That token is the only thing standing
between the tailnet and "launch an arbitrary app on my phone", so it is
generated here rather than chosen, and never logged or returned in full.
"""

import ipaddress
import json
import logging
import os
import re
import secrets
import tempfile
import time
from typing import Dict, List, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")

# What a phone-side automation can realistically be asked to do. Anything
# needing install rights or credential entry is deliberately absent: Android
# will not honour it from a push, and advertising it would only let the model
# promise something that silently fails.
KNOWN_COMMANDS = {
    "open_app": "Launch an installed app by package name",
    "open_url": "Open a URL in the browser",
    "set_timer": "Start a timer for N seconds",
    "set_alarm": "Set an alarm at a time",
    "speak": "Read the message aloud",
    "notify": "Show a plain notification (always supported)",
    "list_apps": "List the apps installed on the device",
    "install_app": "Open the Play Store page for a package so it can be installed",
}

# Commands that need a reachable listener on the device rather than just a
# notification. Kept explicit so `manage_devices` can say which of a device's
# claimed commands will actually work with no endpoint set, instead of the
# model discovering it one failure at a time.
ENDPOINT_COMMANDS = {"open_app", "open_url", "set_timer", "set_alarm",
                     "speak", "list_apps", "install_app"}

_NAME_RE = re.compile(r"^[a-zA-Z0-9 _.-]{1,48}$")


def _load() -> List[Dict]:
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return []


def _save(devices: List[Dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".devices_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(devices, f, indent=2)
        os.replace(tmp, DEVICES_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_devices() -> List[Dict]:
    return _load()


_ENDPOINT_RE = re.compile(r"^https?://[A-Za-z0-9._:\[\]-]+(?::\d+)?/?$")
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _check_endpoint(endpoint: str) -> str:
    """Validate a device endpoint, and refuse one that is not private.

    The token is the real protection, but pointing a device record at a public
    host would quietly send a launch command, and the token with it, off the
    tailnet. Refusing here means a typo cannot exfiltrate the secret.
    """
    endpoint = (endpoint or "").strip().rstrip("/")
    if not endpoint:
        return ""
    if not _ENDPOINT_RE.match(endpoint + "/"):
        raise ValueError(f"endpoint must look like http://host:port — got {endpoint!r}")
    host = endpoint.split("//", 1)[1].split("/")[0].rsplit(":", 1)[0].strip("[]")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A hostname rather than a literal. Tailscale's MagicDNS names are the
        # expected case; anything else is almost certainly a mistake.
        if not (host.endswith(".ts.net") or host in ("localhost",)):
            raise ValueError(
                f"endpoint host {host!r} is not a private address or a *.ts.net name. "
                "A device listener must be reachable only over the tailnet.")
        return endpoint
    # Tailscale hands out 100.64.0.0/10, which Python does NOT count as
    # private: it is RFC 6598 carrier-grade NAT space, not RFC 1918. Left to
    # `is_private` alone every tailnet address would be rejected, which is the
    # one case this whole feature is built around.
    if not (ip.is_private or ip.is_loopback or ip in _TAILSCALE_CGNAT):
        raise ValueError(
            f"endpoint {host} is a public address. A device listener must be on the "
            "tailnet or LAN — sending it the device token otherwise would leak it.")
    return endpoint


def register(name: str, *, topic: str = "", kind: str = "phone",
             commands: Optional[List[str]] = None,
             endpoint: Optional[str] = None) -> Dict:
    """Add or update a device. Returns the stored record.

    A token is minted on first registration and kept across updates, so
    re-registering to add a command does not silently invalidate the secret
    already configured on the phone.
    """
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("device name must be 1-48 chars of letters, digits, space, _ . or -")
    # The topic doubles as a URL path segment, so keep it conservative.
    topic = (topic or re.sub(r"[^A-Za-z0-9_-]", "-", name)).strip("-") or "Reminders"
    cmds = [c for c in (commands or ["notify"]) if c in KNOWN_COMMANDS] or ["notify"]
    endpoint = _check_endpoint(endpoint or "")

    devices = _load()
    for d in devices:
        if d.get("name", "").lower() == name.lower():
            d.update({"topic": topic, "kind": kind, "commands": cmds,
                      "updated": time.time()})
            if endpoint:
                d["endpoint"] = endpoint
            if not d.get("token"):
                d["token"] = secrets.token_urlsafe(32)
            _save(devices)
            return d
    rec = {"name": name, "topic": topic, "kind": kind, "commands": cmds,
           "endpoint": endpoint, "token": secrets.token_urlsafe(32),
           "created": time.time(), "updated": time.time()}
    devices.append(rec)
    _save(devices)
    return rec


def remove(name: str) -> bool:
    devices = _load()
    keep = [d for d in devices if d.get("name", "").lower() != (name or "").strip().lower()]
    if len(keep) == len(devices):
        return False
    _save(keep)
    return True


def resolve(name: str) -> Optional[Dict]:
    """Find a device by name, case-insensitively, then by prefix.

    Prefix matching exists because the model will say "phone" when the device
    is registered as "pixel-8a", and failing on that would be pedantic.
    """
    want = (name or "").strip().lower()
    if not want:
        return None
    devices = _load()
    for d in devices:
        if d.get("name", "").lower() == want:
            return d
    matches = [d for d in devices
               if want in d.get("name", "").lower() or want in d.get("kind", "").lower()]
    return matches[0] if len(matches) == 1 else None


async def send_command(device: Dict, command: str, params: Optional[Dict] = None,
                       timeout: float = 10.0) -> Dict:
    """Send a command to a device's listener and return what it said.

    Every refusal is spelled out rather than returned as a bare failure,
    because the interesting cases here all look identical from the outside: a
    phone that is asleep, one that is off the tailnet, and one whose listener
    was never started all just fail to connect.
    """
    import httpx

    name = device.get("name") or "?"
    if not supports(device, command):
        return {"ok": False, "error":
                f"{name} is not registered as handling {command!r}. It handles: "
                f"{', '.join(device.get('commands') or ['notify'])}."}
    endpoint = (device.get("endpoint") or "").strip().rstrip("/")
    if not endpoint:
        return {"ok": False, "error":
                f"{name} has no endpoint, so there is nothing to send {command!r} to. "
                f"Register it again with an endpoint like http://<phone>.ts.net:8778 "
                f"once the Modes app's remote-control listener is running."}
    token = device.get("token") or ""
    if not token:
        return {"ok": False, "error": f"{name} has no token; re-register it to mint one."}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{endpoint}/command",
                json={"command": command, "params": params or {}},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as e:
        return {"ok": False, "error":
                f"Could not reach {name} at {endpoint} ({type(e).__name__}). The phone may be "
                f"asleep, off the tailnet, or the Modes listener may not be running."}

    if resp.status_code == 401:
        return {"ok": False, "error":
                f"{name} rejected the token. The token in Odysseus and the one in the Modes "
                f"app no longer match — copy it across again."}
    if resp.status_code >= 400:
        return {"ok": False, "error":
                f"{name} returned HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:400]}
    return {"ok": True, "device": name, "command": command, "result": body}


def supports(device: Dict, command: str) -> bool:
    return command in (device.get("commands") or [])


# ── Settings > Devices ─────────────────────────────────────────────────────
#
# A phone reaches Odysseus two ways: its listener (the Modes app, addressed by
# `endpoint` + token) and a browser push subscription, which the browser names
# itself when it subscribes ("android-phone"). Those names never matched the
# registry ("pixel-8a"), so notify_device to a registered phone found nothing.
# `aliases` records which subscription names belong to which device.

def public(device: Dict) -> Dict:
    """A device as the UI may see it: everything but the token."""
    out = {k: v for k, v in device.items() if k != "token"}
    token = device.get("token") or ""
    out["has_token"] = bool(token)
    out["token_hint"] = token[-4:] if token else ""
    out["aliases"] = list(device.get("aliases") or [])
    out["listener_commands"] = [c for c in (device.get("commands") or [])
                                if c in ENDPOINT_COMMANDS]
    return out


def _find(devices: List[Dict], name: str) -> Optional[Dict]:
    want = (name or "").strip().lower()
    return next((d for d in devices if d.get("name", "").lower() == want), None)


def get(name: str) -> Optional[Dict]:
    """Exact (case-insensitive) lookup, for the settings routes."""
    return _find(_load(), name)


def update(name: str, *, new_name: Optional[str] = None, kind: Optional[str] = None,
           endpoint: Optional[str] = None, commands: Optional[List[str]] = None) -> Dict:
    """Edit a device in place. The token is kept, so a rename does not break
    the listener already configured with it."""
    devices = _load()
    d = _find(devices, name)
    if d is None:
        raise KeyError(f"no device named {name!r}")
    if new_name is not None and new_name.strip() and new_name.strip() != d["name"]:
        new_name = new_name.strip()
        if not _NAME_RE.match(new_name):
            raise ValueError("device name must be 1-48 chars of letters, digits, space, _ . or -")
        if _find(devices, new_name) is not None:
            raise ValueError(f"a device named {new_name!r} already exists")
        d["name"] = new_name
    if kind is not None and kind.strip():
        d["kind"] = kind.strip()
    if endpoint is not None:
        d["endpoint"] = _check_endpoint(endpoint)
    if commands is not None:
        d["commands"] = [c for c in commands if c in KNOWN_COMMANDS] or ["notify"]
    d["updated"] = time.time()
    _save(devices)
    return d


def add_alias(name: str, alias: str) -> Dict:
    """Say that the push subscription called `alias` is this device. An alias
    belongs to one device only, so linking it here unlinks it elsewhere."""
    alias = (alias or "").strip()
    if not alias:
        raise ValueError("alias is required")
    devices = _load()
    d = _find(devices, name)
    if d is None:
        raise KeyError(f"no device named {name!r}")
    for other in devices:
        other["aliases"] = [a for a in (other.get("aliases") or [])
                            if a.lower() != alias.lower()]
    d["aliases"] = (d.get("aliases") or []) + [alias]
    d["updated"] = time.time()
    _save(devices)
    return d


def remove_alias(name: str, alias: str) -> Dict:
    devices = _load()
    d = _find(devices, name)
    if d is None:
        raise KeyError(f"no device named {name!r}")
    d["aliases"] = [a for a in (d.get("aliases") or [])
                    if a.lower() != (alias or "").strip().lower()]
    d["updated"] = time.time()
    _save(devices)
    return d


def push_names(device: Dict) -> set:
    """Every push-subscription name that reaches this device, lowercased."""
    names = [device.get("name") or ""] + list(device.get("aliases") or [])
    return {n.lower() for n in names if n}


def owner_of_alias(alias: str) -> Optional[str]:
    """The device a push subscription name is linked to, if any."""
    want = (alias or "").strip().lower()
    if not want:
        return None
    for d in _load():
        if want in push_names(d):
            return d.get("name")
    return None
