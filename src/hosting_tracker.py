"""Background hosting-service tracker for Odysseus.

Tracks and manages long-lived background services (web apps, dev servers,
APIs, local model endpoints) launched by Odysseus without claiming to track
arbitrary externally started system services.

Key requirements and safeguards:
  * Process identity verification: records process start time at launch
    (from /proc/{pid}/stat or ps) to guarantee PID reuse never targets an
    unrelated system process upon Stop or status check.
  * Port & readiness discovery: discovers listening ports from process socket
    inspection (lsof/proc) and log patterns; probes local readiness conservatively.
  * Localhost labeling: distinguishes localhost-only binds (127.0.0.1 / ::1)
    from all-interface binds (0.0.0.0 / *). Never alters firewall or bind addresses.
  * Persisted service records: survives server restarts and reconciles running
    state upon boot and on demand.
  * Secret redaction: scrubs tokens, API keys, bearer tokens, passwords, and URL
    credentials from commands, status, and logs.
  * Access control: non-admins only see and stop services they own; admins may
    inspect and stop any service.
  * Safe confirmation-backed stopping: stopping terminates the process tree
    safely and strictly verifies process identity first.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.atomic_io import atomic_write_json
from core.platform_compat import (
    detached_popen_kwargs,
    find_bash,
    git_bash_path,
    kill_process_tree,
    pid_alive,
)
from src.constants import DATA_DIR
from src.tool_security import owner_is_admin_or_single_user

logger = logging.getLogger(__name__)

SERVICES_DIR = Path(DATA_DIR) / "hosting_services"
SERVICES_FILE = Path(DATA_DIR) / "hosting_services.json"

# Common service secret patterns to redact
_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}", re.IGNORECASE), "sk-***REDACTED***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}", re.IGNORECASE), "ghp_***REDACTED***"),
    (re.compile(r"AIza[0-9A-Za-z-_]{35}"), "AIza***REDACTED***"),
    (re.compile(r"(https?://)([^:\s]+):([^@\s]+)@", re.IGNORECASE), r"\1\2:***@"),
    (re.compile(r"(Authorization:\s*Bearer\s+)[^\s'\"]+", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"((?:--?|export\s+|)(?:api[_-]?key|token|secret|password|passwd|auth[_-]?token|bearer)[=:\s]+)([\"']?)([^\s\"'&|]+)\2", re.IGNORECASE), r"\1\2***REDACTED***\2"),
]


def redact_secrets(text: str) -> str:
    """Mask credentials, API keys, passwords, and tokens in command text or logs."""
    if not text:
        return ""
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def get_process_start_time(pid: Optional[int]) -> Optional[str]:
    """Return the process creation timestamp / clock tick token for PID identity.

    On Linux, reads field 22 (starttime) from /proc/{pid}/stat.
    Fallback queries ps -o lstart= -p {pid}.
    """
    if not pid:
        return None
    stat_path = f"/proc/{pid}/stat"
    if os.path.exists(stat_path):
        try:
            with open(stat_path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
            # Everything after the last ')' contains space-separated fields starting at field 3
            after_paren = data[data.rfind(")") + 2:].split()
            # Field 22 is starttime (index 19 in after_paren: 22 - 3 = 19)
            if len(after_paren) > 19:
                return after_paren[19]
        except Exception:
            pass
    # Cross-platform / fallback:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return None


def is_process_genuine(pid: Optional[int], expected_start_time: Optional[str]) -> bool:
    """Verify that PID is currently alive AND is genuinely the original process,

    preventing PID reuse attacks or mistakes on unrelated processes.
    """
    if not pid or not pid_alive(pid):
        return False
    if not expected_start_time:
        # If no start time was recorded, assume alive (e.g. tests or synthetic records)
        return True
    current = get_process_start_time(pid)
    if not current:
        return False
    return current == str(expected_start_time)


def get_process_descendants(pid: int) -> List[int]:
    """Find all descendant PIDs of pid on Linux using /proc/*/stat."""
    descendants = [pid]
    if not os.path.exists("/proc"):
        return descendants
    try:
        # Build ppid map
        parent_map: Dict[int, List[int]] = {}
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    cpid = int(entry.name)
                    with open(f"/proc/{cpid}/stat", "r", encoding="utf-8", errors="replace") as f:
                        data = f.read()
                    after_paren = data[data.rfind(")") + 2:].split()
                    ppid = int(after_paren[1]) # field 4 is ppid
                    parent_map.setdefault(ppid, []).append(cpid)
                except Exception:
                    pass
        # Traverse descendants
        queue = [pid]
        while queue:
            curr = queue.pop(0)
            children = parent_map.get(curr, [])
            for ch in children:
                if ch not in descendants:
                    descendants.append(ch)
                    queue.append(ch)
    except Exception:
        pass
    return descendants


def discover_ports(pid: Optional[int], log_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Discover listening TCP ports bound by the process or its children.

    Returns a list of dicts:
      {"port": int, "host": str, "localhost_only": bool}
    """
    results: List[Dict[str, Any]] = []
    seen_ports: Set[int] = set()

    # 1. Socket inspection via lsof for the process and its descendants
    if pid and pid_alive(pid):
        pids = get_process_descendants(pid)
        try:
            cmd = ["lsof", "-Pan", "-iTCP", "-sTCP:LISTEN"]
            for p in pids:
                cmd.extend(["-p", str(p)])
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
            for line in out.strip().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 9:
                    addr = parts[8]  # e.g. 127.0.0.1:8000 or *:3000 or [::1]:5173
                    if ":" in addr:
                        host_part, port_str = addr.rsplit(":", 1)
                        if port_str.isdigit():
                            port = int(port_str)
                            if port not in seen_ports:
                                seen_ports.add(port)
                                is_lh = host_part in ("127.0.0.1", "::1", "localhost")
                                results.append({
                                    "port": port,
                                    "host": host_part or "*",
                                    "localhost_only": is_lh,
                                })
        except Exception:
            pass

    # 2. Log parsing fallback if socket discovery found nothing or as supplement
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-100:]  # Last 100 lines
            log_text = "".join(lines)

            # Match patterns like:
            # http://localhost:3000, http://127.0.0.1:8080, port 5000, listening on :8000
            url_matches = re.findall(r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})", log_text, re.IGNORECASE)
            for host_str, port_str in url_matches:
                port = int(port_str)
                if port not in seen_ports:
                    seen_ports.add(port)
                    is_lh = host_str.lower() in ("localhost", "127.0.0.1")
                    results.append({
                        "port": port,
                        "host": host_str,
                        "localhost_only": is_lh,
                    })

            port_matches = re.findall(r"(?:listening|running|started).*?(?:port|on)\s*(?::\s*)?(\d{2,5})", log_text, re.IGNORECASE)
            for port_str in port_matches:
                port = int(port_str)
                if port not in seen_ports:
                    seen_ports.add(port)
                    results.append({
                        "port": port,
                        "host": "localhost",
                        "localhost_only": True,
                    })
        except Exception:
            pass

    return results


def probe_readiness(port: int, host: str = "127.0.0.1", timeout_s: float = 0.8) -> Tuple[bool, str]:
    """Conservative probe to check whether port is responding to TCP/HTTP."""
    if host in ("*", "0.0.0.0", "::"):
        host = "127.0.0.1"

    # Quick TCP connect
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        sock.connect((host, port))
        sock.close()
    except Exception as e:
        return False, f"Connection failed: {e}"

    # Try quick HTTP probe
    try:
        url = f"http://{host}:{port}/"
        req = urllib.request.Request(url, headers={"User-Agent": "Odysseus-Service-Probe/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as he:
        # HTTP 404, 401, 403, 500 still means the server is UP and responding
        return True, f"HTTP {he.code}"
    except Exception:
        # TCP worked even if HTTP didn't respond to root URL
        return True, "TCP connected"


def _load_store() -> Dict[str, Dict[str, Any]]:
    try:
        if SERVICES_FILE.exists():
            data = json.loads(SERVICES_FILE.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        logger.warning("Failed to load hosting services store: %s", e)
    return {}


def _save_store(services: Dict[str, Dict[str, Any]]) -> None:
    try:
        SERVICES_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(SERVICES_FILE), services, indent=2)
    except Exception as e:
        logger.error("Failed to save hosting services store: %s", e)


def launch_service(
    command: str,
    *,
    cwd: Optional[str] = None,
    owner: str = "",
    name: str = "",
    session_id: str = "",
    env_vars: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Launch a long-lived service detached, recording immutable process identity."""
    SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    service_id = f"srv_{uuid.uuid4().hex[:8]}"
    log_path = SERVICES_DIR / f"{service_id}.log"
    exit_path = SERVICES_DIR / f"{service_id}.exit"

    clean_cmd = (command or "").strip()
    if not clean_cmd:
        raise ValueError("Service command cannot be empty")

    svc_name = (name or "").strip()
    if not svc_name:
        first_line = clean_cmd.splitlines()[0][:40]
        svc_name = first_line

    bash = find_bash()
    if bash:
        cmd_path = SERVICES_DIR / f"{service_id}.cmd.sh"
        cmd_path.write_text(clean_cmd + "\n", encoding="utf-8")
        lp, xp, cp = (shlex.quote(git_bash_path(p)) for p in (log_path, exit_path, cmd_path))
        script_path = SERVICES_DIR / f"{service_id}.sh"
        script_path.write_text(
            f"bash {cp} > {lp} 2>&1\n"
            f"echo $? > {xp}\n",
            encoding="utf-8",
        )
        argv = [bash, str(script_path)]
    else:
        child_path = SERVICES_DIR / f"{service_id}.child.cmd"
        child_path.write_text("@echo off\r\n" + clean_cmd + "\r\n", encoding="utf-8")
        script_path = SERVICES_DIR / f"{service_id}.cmd"
        script_path.write_text(
            "@echo off\r\n"
            f'call "{child_path}" > "{log_path}" 2>&1\r\n'
            f'echo %ERRORLEVEL%> "{exit_path}"\r\n',
            encoding="utf-8",
        )
        argv = [os.environ.get("ComSpec", "cmd.exe"), "/c", str(script_path)]

    sub_env = dict(os.environ)
    if env_vars:
        sub_env.update(env_vars)

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        cwd=cwd or None,
        env=sub_env,
        **detached_popen_kwargs(),
    )

    # Immediately capture immutable process start time token
    time.sleep(0.05)
    start_token = get_process_start_time(proc.pid)

    rec = {
        "id": service_id,
        "name": svc_name,
        "command": clean_cmd,
        "cwd": cwd or "",
        "owner": owner or "",
        "session_id": session_id or "",
        "pid": proc.pid,
        "process_start_time": start_token,
        "status": "starting",         # starting | ready | stopped | failed | dead
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
        "ports": [],
        "port": None,
        "bind_address": None,
        "is_localhost_only": True,
        "readiness": "starting",
        "readiness_detail": "Starting process",
        "url": None,
        "log_path": str(log_path),
        "exit_path": str(exit_path),
    }

    services = _load_store()
    services[service_id] = rec
    _save_store(services)
    logger.info("Launched hosting service %s (PID %s) owner=%s", service_id, proc.pid, owner)

    # Trigger quick initial port check
    reconcile_service(rec)
    _save_store(services)

    return public_view(rec)


def register_service(
    pid: int,
    *,
    name: str,
    command: str,
    cwd: Optional[str] = None,
    owner: str = "",
    session_id: str = "",
    port: Optional[int] = None,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Register an existing Odysseus-managed background process as a hosting service."""
    if not pid_alive(pid):
        raise ValueError(f"Process PID {pid} is not currently running")

    start_token = get_process_start_time(pid)
    service_id = f"srv_{uuid.uuid4().hex[:8]}"

    rec = {
        "id": service_id,
        "name": name or f"Service {service_id}",
        "command": command or "",
        "cwd": cwd or "",
        "owner": owner or "",
        "session_id": session_id or "",
        "pid": pid,
        "process_start_time": start_token,
        "status": "starting",
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
        "ports": [port] if port else [],
        "port": port,
        "bind_address": "127.0.0.1",
        "is_localhost_only": True,
        "readiness": "checking",
        "readiness_detail": "Registered existing process",
        "url": f"http://localhost:{port}" if port else None,
        "log_path": log_path or "",
        "exit_path": "",
    }

    services = _load_store()
    services[service_id] = rec
    _save_store(services)
    logger.info("Registered hosting service %s (PID %s) owner=%s", service_id, pid, owner)

    reconcile_service(rec)
    _save_store(services)
    return public_view(rec)


def reconcile_service(rec: Dict[str, Any]) -> bool:
    """Reconcile a single service against disk, sockets, and OS process table.

    Returns True if record was modified.
    """
    changed = False
    curr_status = rec.get("status")
    pid = rec.get("pid")
    start_time_token = rec.get("process_start_time")
    exit_path = Path(rec.get("exit_path", "")) if rec.get("exit_path") else None
    log_path = rec.get("log_path")

    # If already stopped/failed/dead, nothing to reconcile unless exit code arrived
    if curr_status in ("stopped", "failed", "dead"):
        if rec.get("exit_code") is None and exit_path and exit_path.exists():
            try:
                rec["exit_code"] = int(exit_path.read_text(encoding="utf-8").strip() or "1")
                changed = True
            except Exception:
                pass
        return changed

    now = time.time()

    # 1. Check if exit file was written
    if exit_path and exit_path.exists():
        try:
            code = int(exit_path.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            code = 1
        rec["exit_code"] = code
        rec["status"] = "failed" if code != 0 else "stopped"
        rec["readiness"] = "stopped"
        rec["readiness_detail"] = f"Exited with code {code}"
        rec["ended_at"] = rec.get("ended_at") or now
        return True

    # 2. Check process alive and identity verification
    if not pid_alive(pid):
        rec["status"] = "dead"
        rec["readiness"] = "failed"
        rec["readiness_detail"] = "Process died unexpectedly"
        rec["ended_at"] = now
        return True

    if not is_process_genuine(pid, start_time_token):
        logger.warning(
            "PID reuse detected for service %s: PID %s start time mismatch",
            rec.get("id"), pid
        )
        rec["status"] = "dead"
        rec["readiness"] = "failed"
        rec["readiness_detail"] = "Process terminated (PID reused by another process)"
        rec["ended_at"] = now
        rec["pid"] = None
        return True

    # 3. Process is genuine and running — discover ports & readiness
    discovered = discover_ports(pid, log_path)
    if discovered:
        ports_list = [d["port"] for d in discovered]
        if rec.get("ports") != ports_list:
            rec["ports"] = ports_list
            changed = True

        primary = discovered[0]
        port = primary["port"]
        host = primary["host"]
        is_lh = primary["localhost_only"]

        if rec.get("port") != port:
            rec["port"] = port
            changed = True
        if rec.get("bind_address") != host:
            rec["bind_address"] = host
            changed = True
        if rec.get("is_localhost_only") != is_lh:
            rec["is_localhost_only"] = is_lh
            changed = True

        # Construct safe reachable URL
        url = f"http://localhost:{port}"
        if rec.get("url") != url:
            rec["url"] = url
            changed = True

        # Probe readiness
        is_ready, detail = probe_readiness(port, host=host)
        if is_ready:
            if rec.get("status") != "ready":
                rec["status"] = "ready"
                changed = True
            rec["readiness"] = "ready"
            rec["readiness_detail"] = detail
        else:
            if rec.get("status") != "starting":
                rec["status"] = "starting"
                changed = True
            rec["readiness"] = "starting"
            rec["readiness_detail"] = detail
    else:
        # Still running but no port discovered yet
        rec["status"] = "starting"
        rec["readiness"] = "starting"
        rec["readiness_detail"] = "Process running, waiting for port to open"

    return changed


def reconcile_services() -> Dict[str, Dict[str, Any]]:
    """Reconcile all recorded services against disk, OS, and network state."""
    services = _load_store()
    changed = False
    for rec in services.values():
        if reconcile_service(rec):
            changed = True
    if changed:
        _save_store(services)
    return services


def visible_to(rec: Dict[str, Any], user: str) -> bool:
    """Ownership check: visible to creator or admin."""
    if not user:
        return True
    if owner_is_admin_or_single_user(user):
        return True
    owner = rec.get("owner", "")
    return not owner or owner == user


def list_services(user: str = "") -> List[Dict[str, Any]]:
    """List services visible to user, with reconciled state and redacted secrets."""
    services = reconcile_services()
    out = [public_view(rec) for rec in services.values() if visible_to(rec, user)]
    out.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
    return out


def get_service(service_id: str, user: str = "") -> Optional[Dict[str, Any]]:
    """Fetch a service record by ID if visible to user."""
    services = reconcile_services()
    rec = services.get(service_id)
    if not rec or not visible_to(rec, user):
        return None
    return public_view(rec)


def stop_service(service_id: str, *, user: str, confirm: bool = False) -> Dict[str, Any]:
    """Safely terminate a service process tree with strict process identity checks."""
    if not confirm:
        raise ValueError("Stopping a service requires explicit confirmation (confirm=True)")

    services = _load_store()
    rec = services.get(service_id)
    if not rec or not visible_to(rec, user):
        raise KeyError(f"No such service: {service_id}")

    pid = rec.get("pid")
    start_time_token = rec.get("process_start_time")

    # PID-reuse safeguard: NEVER signal an unrelated process
    if pid and pid_alive(pid) and not is_process_genuine(pid, start_time_token):
        logger.error(
            "REFUSING to kill PID %s for service %s: PID was reused by an unrelated process!",
            pid, service_id
        )
        rec["status"] = "dead"
        rec["readiness"] = "failed"
        rec["readiness_detail"] = "Process terminated (PID reused by another process)"
        rec["ended_at"] = time.time()
        rec["pid"] = None
        _save_store(services)
        raise RuntimeError("Process ID has been reused by an unrelated system process; termination aborted.")

    # Reconcile first
    reconcile_service(rec)

    if rec.get("status") in ("stopped", "failed", "dead"):
        return public_view(rec)

    if not pid or not pid_alive(pid):
        rec["status"] = "stopped"
        rec["ended_at"] = time.time()
        _save_store(services)
        return public_view(rec)

    # PID-reuse safeguard: NEVER signal an unrelated process
    if not is_process_genuine(pid, start_time_token):
        logger.error(
            "REFUSING to kill PID %s for service %s: PID was reused by an unrelated process!",
            pid, service_id
        )
        rec["status"] = "dead"
        rec["readiness"] = "failed"
        rec["readiness_detail"] = "Process terminated (PID reused by another process)"
        rec["ended_at"] = time.time()
        rec["pid"] = None
        _save_store(services)
        raise RuntimeError("Process ID has been reused by an unrelated system process; termination aborted.")

    logger.info("Stopping hosting service %s (PID %s) requested by user=%s", service_id, pid, user)
    kill_process_tree(pid)

    rec["status"] = "stopped"
    rec["readiness"] = "stopped"
    rec["readiness_detail"] = "Stopped by user"
    rec["ended_at"] = time.time()
    _save_store(services)

    return public_view(rec)


def get_logs(service_id: str, *, user: str = "", max_lines: int = 500) -> str:
    """Read service logs with secret redaction applied."""
    services = _load_store()
    rec = services.get(service_id)
    if not rec or not visible_to(rec, user):
        raise KeyError(f"No such service: {service_id}")

    log_path = rec.get("log_path")
    if not log_path or not os.path.exists(log_path):
        return "(no log output recorded)"

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-max_lines:] if len(lines) > max_lines else lines
        raw = "".join(tail)
        return redact_secrets(raw)
    except Exception as e:
        return f"Error reading log file: {e}"


def public_view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare a safe, secret-redacted public dictionary for the UI and APIs."""
    started = rec.get("started_at") or time.time()
    ended = rec.get("ended_at")
    elapsed = (ended or time.time()) - started

    is_lh = rec.get("is_localhost_only", True)
    port = rec.get("port")
    url = rec.get("url") or (f"http://localhost:{port}" if port else None)

    return {
        "id": rec.get("id"),
        "name": rec.get("name"),
        "command": redact_secrets(rec.get("command", "")),
        "cwd": rec.get("cwd", ""),
        "owner": rec.get("owner", ""),
        "session_id": rec.get("session_id", ""),
        "pid": rec.get("pid"),
        "status": rec.get("status", "unknown"),
        "started_at": started,
        "ended_at": ended,
        "elapsed_s": round(elapsed, 1),
        "exit_code": rec.get("exit_code"),
        "port": port,
        "ports": rec.get("ports", []),
        "bind_address": rec.get("bind_address"),
        "is_localhost_only": is_lh,
        "bind_label": "Localhost only" if is_lh else "Local / All interfaces",
        "url": url,
        "readiness": rec.get("readiness", "unknown"),
        "readiness_detail": rec.get("readiness_detail", ""),
    }
