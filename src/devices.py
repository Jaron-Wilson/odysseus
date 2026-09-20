"""Registry of the user's own devices, so "my phone" resolves to one of them.

A device is a name, a delivery topic, and a list of commands its automation
actually honours. That last part is the point: the assistant should be able to
tell the user "this phone can launch apps but cannot install them" from the
registry rather than guessing, and should refuse a command the device never
claimed to support instead of pushing it into the void.

Stored as plain JSON next to the other per-install state. No secrets live here
— delivery happens over ntfy, which holds its own credentials in the
integration record.
"""

import json
import logging
import os
import re
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
}

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


def register(name: str, *, topic: str = "", kind: str = "phone",
             commands: Optional[List[str]] = None) -> Dict:
    """Add or update a device. Returns the stored record."""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("device name must be 1-48 chars of letters, digits, space, _ . or -")
    # The topic doubles as a URL path segment, so keep it conservative.
    topic = (topic or re.sub(r"[^A-Za-z0-9_-]", "-", name)).strip("-") or "Reminders"
    cmds = [c for c in (commands or ["notify"]) if c in KNOWN_COMMANDS] or ["notify"]

    devices = _load()
    for d in devices:
        if d.get("name", "").lower() == name.lower():
            d.update({"topic": topic, "kind": kind, "commands": cmds,
                      "updated": time.time()})
            _save(devices)
            return d
    rec = {"name": name, "topic": topic, "kind": kind, "commands": cmds,
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


def supports(device: Dict, command: str) -> bool:
    return command in (device.get("commands") or [])
