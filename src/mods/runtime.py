"""Running mods: start, stop, health checks, restarts with backoff, and logs.

A process mod runs its manifest `command` in its src folder, with its own
virtualenv's python when it has one. A docker mod runs `docker compose up -d`
on its compose file (project odysseus-mod-<id>). Either way the core gives it
a free port on 127.0.0.1 and these variables:

    ODYSSEUS_URL         the core, for the mod API (/api/mods/v1)
    ODYSSEUS_MOD_TOKEN   its bearer token for that API, and its key for
                         checking the signed identity headers
    ODYSSEUS_MOD_ID      its id
    ODYSSEUS_MOD_BASE    the path it is served under, /mods/<id>
    MOD_PORT             the port to listen on (127.0.0.1 only)
    MOD_DATA_DIR         a folder of its own for files

Nothing else from the core's environment is passed on (the core's .env holds
API keys), apart from PATH, HOME, LANG, TZ and TMPDIR.

"lazy" mods start on first use (a page, a card, a tool call, an event) and,
with `idle_stop_minutes`, stop again when unused. "boot" mods start with the
server. A mod that exits or fails its health check three times in a row is
restarted after 2, 4, 8 ... seconds, at most 5 minutes apart; ten healthy
minutes reset the count.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
import socket
import sys
import time
from typing import Any, Deque, Dict, List, Optional

import httpx

from src.mods import paths
from src.mods import registry

logger = logging.getLogger(__name__)

LOG_MAX_BYTES = 1024 * 1024
START_TIMEOUT_S = 45
HEALTH_INTERVAL_S = 10
HEALTH_FAILS_TO_RESTART = 3
BACKOFF_MAX_S = 300
STABLE_RESET_S = 600
STOP_GRACE_S = 5


def core_url() -> str:
    from src.constants import internal_api_base
    return internal_api_base()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def base_env(mod_id: str, port: int) -> Dict[str, str]:
    entry = registry.get(mod_id) or {}
    man = entry.get("manifest") or {}
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update((man.get("runtime") or {}).get("env") or {})
    env.update({
        "ODYSSEUS_URL": core_url(),
        "ODYSSEUS_MOD_TOKEN": registry.token_of(mod_id),
        "ODYSSEUS_MOD_ID": mod_id,
        "ODYSSEUS_MOD_BASE": f"/mods/{mod_id}",
        "MOD_PORT": str(port),
        "MOD_HOST": "127.0.0.1",
        "MOD_DATA_DIR": paths.data_dir(mod_id),
        "PYTHONUNBUFFERED": "1",
    })
    return env


def resolve_command(mod_id: str, argv: List[str], setup: bool = False) -> List[str]:
    """`python`/`python3` becomes the mod's venv python (the core's during
    setup, before a venv exists); a ./relative program is taken from the mod
    folder."""
    if not argv:
        return argv
    head = argv[0]
    if head in ("python", "python3"):
        head = sys.executable if setup else registry.python_for(mod_id)
    elif head.startswith("./") or (("/" in head) and not os.path.isabs(head)):
        head = os.path.join(paths.src_dir(mod_id), head)
    return [head, *argv[1:]]


class ModState:
    def __init__(self, mod_id: str):
        self.mod_id = mod_id
        self.status = "stopped"          # stopped|starting|running|unhealthy|backoff|failed|stopping
        self.port: Optional[int] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.started_at: Optional[float] = None
        self.healthy_since: Optional[float] = None
        self.last_used = 0.0
        self.restarts = 0
        self.fails = 0
        self.next_restart_at: Optional[float] = None
        self.last_error = ""
        self.lock = asyncio.Lock()
        self.reader: Optional[asyncio.Task] = None
        self.tail: Deque[str] = collections.deque(maxlen=400)
        self.want_running = False

    def view(self) -> Dict[str, Any]:
        return {"status": self.status, "port": self.port, "pid": self.proc.pid if self.proc else None,
                "started_at": self.started_at, "restarts": self.restarts, "last_error": self.last_error,
                "next_restart_at": self.next_restart_at, "last_used": self.last_used or None}


_states: Dict[str, ModState] = {}
_monitor_task: Optional[asyncio.Task] = None
_http: Optional[httpx.AsyncClient] = None


def state(mod_id: str) -> ModState:
    st = _states.get(mod_id)
    if st is None:
        st = _states[mod_id] = ModState(mod_id)
    return st


_http_loop = None


def _client() -> httpx.AsyncClient:
    """One client per event loop (the app has one; tests make several)."""
    global _http, _http_loop
    loop = asyncio.get_running_loop()
    if _http is None or _http.is_closed or _http_loop is not loop:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(5.0), trust_env=False)
        _http_loop = loop
    return _http


def _manifest(mod_id: str) -> Dict[str, Any]:
    e = registry.get(mod_id)
    if not e:
        raise registry.RegistryError(f"no mod {mod_id!r}", 404)
    return e


# ── Logs ─────────────────────────────────────────────────────────────────

def _rotate(path: str) -> None:
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except FileNotFoundError:
        pass


def log_line(mod_id: str, text: str) -> None:
    p = paths.log_file(mod_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _rotate(p)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text.rstrip()}\n"
    with open(p, "a", encoding="utf-8", errors="replace") as f:
        f.write(line)
    state(mod_id).tail.append(line)


def read_log(mod_id: str, lines: int = 300) -> str:
    out: List[str] = []
    p = paths.log_file(mod_id)
    for f in (p + ".1", p):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                out.extend(fh.readlines())
        except FileNotFoundError:
            continue
    return "".join(out[-max(1, min(lines, 5000)):])


async def _pump(mod_id: str, stream: asyncio.StreamReader) -> None:
    while True:
        line = await stream.readline()
        if not line:
            return
        try:
            log_line(mod_id, line.decode("utf-8", "replace"))
        except Exception:
            pass


# ── Health ───────────────────────────────────────────────────────────────

async def check_health(mod_id: str) -> bool:
    st = state(mod_id)
    if not st.port:
        return False
    if st.proc is not None and st.proc.returncode is not None:
        return False
    health = ((_manifest(mod_id).get("manifest") or {}).get("runtime") or {}).get("health") or "/health"
    try:
        r = await _client().get(f"http://127.0.0.1:{st.port}{health}")
        return r.status_code < 500
    except httpx.HTTPError:
        return False


async def _wait_healthy(mod_id: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    st = state(mod_id)
    while time.time() < deadline:
        if st.proc is not None and st.proc.returncode is not None:
            return False
        if await check_health(mod_id):
            return True
        await asyncio.sleep(0.3)
    return False


# ── Start / stop ─────────────────────────────────────────────────────────

async def _compose(mod_id: str, args: List[str], env: Dict[str, str]) -> int:
    man = _manifest(mod_id)["manifest"]
    compose = os.path.join(paths.src_dir(mod_id), man["runtime"]["compose"])
    argv = ["docker", "compose", "-p", f"odysseus-mod-{mod_id}", "-f", compose, *args]
    proc = await asyncio.create_subprocess_exec(*argv, cwd=paths.src_dir(mod_id), env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    for line in (out or b"").decode("utf-8", "replace").splitlines()[-40:]:
        log_line(mod_id, "[docker] " + line)
    return proc.returncode or 0


async def _spawn(mod_id: str) -> None:
    entry = _manifest(mod_id)
    man = entry["manifest"]
    st = state(mod_id)
    st.port = free_port()
    env = base_env(mod_id, st.port)
    os.makedirs(paths.data_dir(mod_id), exist_ok=True)
    rt = man["runtime"]
    log_line(mod_id, f"[odysseus] starting {mod_id} {entry.get('version')} on port {st.port}")
    if rt["type"] == "docker":
        env_full = dict(env)
        env_full.update({k: v for k, v in os.environ.items() if k.startswith("DOCKER_")})
        code = await _compose(mod_id, ["up", "-d", "--remove-orphans"], env_full)
        if code != 0:
            raise RuntimeError(f"docker compose up failed (exit {code})")
        st.proc = None
    else:
        argv = resolve_command(mod_id, list(rt["command"]))
        st.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=paths.src_dir(mod_id), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        st.reader = asyncio.create_task(_pump(mod_id, st.proc.stdout))
    st.started_at = time.time()


async def _kill(mod_id: str) -> None:
    st = state(mod_id)
    man = (registry.get(mod_id) or {}).get("manifest") or {}
    if (man.get("runtime") or {}).get("type") == "docker":
        try:
            await _compose(mod_id, ["down"], base_env(mod_id, st.port or 0))
        except Exception as e:
            log_line(mod_id, f"[odysseus] docker compose down failed: {e}")
    proc = st.proc
    if proc is not None and proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), STOP_GRACE_S)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
    if st.reader:
        st.reader.cancel()
        st.reader = None
    st.proc = None


async def start(mod_id: str) -> Dict[str, Any]:
    """Start (or confirm running) an enabled mod and wait for its health check."""
    entry = _manifest(mod_id)
    if not entry.get("enabled"):
        raise registry.RegistryError(f"{mod_id} is disabled", 409)
    st = state(mod_id)
    async with st.lock:
        st.want_running = True
        st.last_used = time.time()
        if st.status == "running" and await check_health(mod_id):
            return st.view()
        if st.proc is not None or st.status in ("running", "unhealthy"):
            await _kill(mod_id)
        st.status = "starting"
        st.last_error = ""
        try:
            await _spawn(mod_id)
        except Exception as e:
            st.status = "failed"
            st.last_error = str(e)[:500]
            log_line(mod_id, f"[odysseus] could not start: {e}")
            _schedule_restart(st)
            return st.view()
        if await _wait_healthy(mod_id, START_TIMEOUT_S):
            st.status = "running"
            st.healthy_since = time.time()
            st.fails = 0
            st.next_restart_at = None
            log_line(mod_id, "[odysseus] healthy")
        else:
            code = st.proc.returncode if st.proc else None
            st.last_error = (f"exited with code {code}" if code is not None
                             else f"no answer from its health check within {START_TIMEOUT_S}s")
            log_line(mod_id, f"[odysseus] start failed: {st.last_error}")
            await _kill(mod_id)
            st.status = "failed"
            _schedule_restart(st)
        return st.view()


def _schedule_restart(st: ModState) -> None:
    if not st.want_running:
        return
    delay = min(BACKOFF_MAX_S, 2 ** min(st.restarts + 1, 9))
    st.restarts += 1
    st.next_restart_at = time.time() + delay
    st.status = "backoff"


async def stop(mod_id: str, reason: str = "stopped") -> Dict[str, Any]:
    st = state(mod_id)
    async with st.lock:
        st.want_running = False
        st.next_restart_at = None
        if st.proc is not None or st.status not in ("stopped",):
            st.status = "stopping"
            await _kill(mod_id)
            log_line(mod_id, f"[odysseus] {reason}")
        st.status = "stopped"
        st.port = None
        st.healthy_since = None
        return st.view()


async def restart(mod_id: str) -> Dict[str, Any]:
    await stop(mod_id, "restarting")
    state(mod_id).restarts = 0
    return await start(mod_id)


async def ensure_running(mod_id: str) -> Optional[int]:
    """The port of a running mod, starting a lazy one if needed. None if it
    is disabled or will not come up (its state says why)."""
    entry = registry.get(mod_id)
    if not entry or not entry.get("enabled"):
        return None
    st = state(mod_id)
    st.last_used = time.time()
    if st.status == "running" and st.port:
        return st.port
    if st.status == "backoff" and st.next_restart_at and st.next_restart_at > time.time():
        return None
    view = await start(mod_id)
    return view["port"] if view["status"] == "running" else None


def touch(mod_id: str) -> None:
    state(mod_id).last_used = time.time()


# ── The monitor ──────────────────────────────────────────────────────────

async def _monitor_once() -> None:
    now = time.time()
    for entry in registry.list_mods():
        mod_id = entry["id"]
        st = state(mod_id)
        rt = (entry.get("manifest") or {}).get("runtime") or {}
        if not entry.get("enabled"):
            if st.status != "stopped":
                await stop(mod_id, "disabled")
            continue
        if st.status == "backoff" and st.next_restart_at and now >= st.next_restart_at and st.want_running:
            log_line(mod_id, f"[odysseus] restart attempt {st.restarts}")
            await start(mod_id)
            continue
        if st.status in ("running", "unhealthy"):
            idle = int(rt.get("idle_stop_minutes") or 0)
            if idle and rt.get("start") == "lazy" and now - (st.last_used or now) > idle * 60:
                await stop(mod_id, f"stopped after {idle} idle minutes")
                continue
            ok = await check_health(mod_id)
            if ok:
                if st.status == "unhealthy":
                    log_line(mod_id, "[odysseus] healthy again")
                st.status = "running"
                st.fails = 0
                if st.healthy_since and now - st.healthy_since > STABLE_RESET_S:
                    st.restarts = 0
                continue
            st.fails += 1
            exited = st.proc is not None and st.proc.returncode is not None
            st.status = "unhealthy"
            if exited or st.fails >= HEALTH_FAILS_TO_RESTART:
                st.last_error = (f"exited with code {st.proc.returncode}" if exited
                                 else f"failed {st.fails} health checks in a row")
                log_line(mod_id, f"[odysseus] {st.last_error}")
                async with st.lock:
                    await _kill(mod_id)
                st.healthy_since = None
                _schedule_restart(st)


async def _monitor_loop() -> None:
    while True:
        try:
            await _monitor_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[mods] monitor pass failed")
        await asyncio.sleep(HEALTH_INTERVAL_S)


async def startup() -> None:
    """Called once at server start: start "boot" mods and the monitor."""
    global _monitor_task
    for entry in registry.list_mods():
        rt = (entry.get("manifest") or {}).get("runtime") or {}
        if entry.get("enabled") and rt.get("start") == "boot":
            try:
                await start(entry["id"])
            except Exception as e:
                logger.warning("[mods] %s did not start at boot: %s", entry["id"], e)
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())


async def shutdown() -> None:
    global _monitor_task
    if _monitor_task:
        _monitor_task.cancel()
        _monitor_task = None
    for mod_id in list(_states):
        try:
            await stop(mod_id, "server shutting down")
        except Exception:
            pass
    if _http is not None:
        try:
            await _http.aclose()
        except Exception:
            pass


def status_all() -> Dict[str, Dict[str, Any]]:
    return {mid: st.view() for mid, st in _states.items()}
