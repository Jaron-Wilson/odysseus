"""One-time codes for adding a device, pairing a phone, or checking one.

Adding a machine used to mean copying server files over, writing a launcher
with its Tailscale address in it, registering a scheduled task or a systemd
unit, adding an SSH key, and typing the MCP server into Agent Tools. Now
Settings > Devices hands out a code, and one command (or one QR scan) does
the rest.

The code is the credential for the few unauthenticated /enroll/<code>/...
paths, so it is random, short-lived and single-purpose:

- "computer": the install script and its files can be fetched while the code
  is valid; registering the machine uses it up.
- "phone":    the pairing page on the phone; subscribing it for
  notifications under the device's own name uses it up.
- "check":    a page that tests an existing device; it expires rather than
  being used up, so it can be reloaded.

Nothing here is reachable off the tailnet: Odysseus binds loopback behind
Tailscale Serve.
"""

import json
import logging
import os
import secrets
import tempfile
import time
from typing import Dict, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

CODES_FILE = os.path.join(DATA_DIR, "enroll_codes.json")
TTL_S = {"computer": 20 * 60, "phone": 20 * 60, "check": 10 * 60}
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"          # no 0/o, 1/l/i
CODE_LEN = 16


def _load() -> Dict[str, Dict]:
    try:
        with open(CODES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _save(data: Dict[str, Dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".enroll_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CODES_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune(data: Dict[str, Dict], now: float) -> Dict[str, Dict]:
    # Keep used/expired ones for an hour so a late "is it done?" still answers.
    return {c: r for c, r in data.items() if r.get("expires", 0) + 3600 > now}


def create(kind: str, *, device: str = "", owner: str = "", now: Optional[float] = None) -> Dict:
    if kind not in TTL_S:
        raise ValueError(f"unknown code kind {kind!r}")
    now = now or time.time()
    code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN))
    rec = {"code": code, "kind": kind, "device": (device or "").strip()[:48],
           "owner": owner or "", "created": now, "expires": now + TTL_S[kind],
           "used": False, "result": None}
    data = _prune(_load(), now)
    data[code] = rec
    _save(data)
    return rec


def valid(code: str, kind: Optional[str] = None, now: Optional[float] = None) -> Optional[Dict]:
    """The code's record if it is live (and of `kind`), else None."""
    if not code or len(code) != CODE_LEN or any(c not in _ALPHABET for c in code):
        return None
    rec = _load().get(code)
    now = now or time.time()
    if not rec or rec.get("used") or rec.get("expires", 0) < now:
        return None
    if kind and rec.get("kind") != kind:
        return None
    return rec


def status(code: str) -> Optional[Dict]:
    """The record whether or not it is still live, for the UI to poll."""
    return _load().get(code)


def use(code: str, result: Dict, now: Optional[float] = None) -> bool:
    """Mark a live code used, recording what it produced. Single use."""
    now = now or time.time()
    data = _prune(_load(), now)
    rec = data.get(code)
    if not rec or rec.get("used") or rec.get("expires", 0) < now:
        return False
    rec["used"] = True
    rec["used_at"] = now
    rec["result"] = result
    data[code] = rec
    _save(data)
    return True
