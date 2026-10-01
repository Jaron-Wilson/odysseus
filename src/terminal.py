"""Live terminal sessions: a real shell on a PTY, relayed over a WebSocket.

Asked for 2026-09-30: "can i also get a command line in mine? ... a live
command line so that i can ssh and do stuff myself please."

Each session is the server user's login shell on its own pseudo-terminal,
started in a new session (setsid) with the PTY as its controlling terminal,
so job control, Ctrl+C, full-screen programs and ssh all behave the way they
do in a normal terminal. The browser talks to it through
routes/terminal_routes.py.

A session outlives the WebSocket: when the page reloads or the phone drops
off the network it is only detached. Its recent output is kept (the
scrollback, replayed on reattach) and it is killed after a grace period with
no one attached. Closing it on purpose kills it at once.

Nothing typed or printed is ever logged: output only goes to the scrollback
in memory and to the attached socket.

Everything here runs on the event loop thread (the PTY is read with
loop.add_reader), so there is no locking.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import pwd
import secrets
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Tunables. Module attributes rather than constants so tests (and anyone
# reading settings) see one source; read at use time, not import time.
GRACE_S = _env_int("ODYSSEUS_TERMINAL_GRACE_S", 600)          # detached for this long: killed
MAX_SESSIONS = _env_int("ODYSSEUS_TERMINAL_MAX_SESSIONS", 8)  # per user
SCROLLBACK_BYTES = _env_int("ODYSSEUS_TERMINAL_SCROLLBACK", 200_000)
KILL_TIMEOUT_S = 3.0          # SIGHUP, then SIGKILL after this
# Output waiting to go out to a slow socket; past this the PTY is not read
# until it drains, so a runaway `cat` can't grow memory without bound.
_SEND_HIGH_WATER = 1_000_000

_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def login_shell() -> str:
    """The shell to run: $SHELL, else the user's entry in passwd, else bash."""
    for cand in (os.environ.get("SHELL"), _passwd_shell(), "/bin/bash", "/bin/sh"):
        if cand and os.path.isabs(cand) and os.access(cand, os.X_OK):
            return cand
    return "/bin/sh"


def _passwd_shell() -> Optional[str]:
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or None
    except Exception:
        return None


def home_dir() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_dir or os.path.expanduser("~")
    except Exception:
        return os.path.expanduser("~")


def resolve_cwd(requested: Optional[str]) -> str:
    """The start folder: home, or `requested` if it is a folder inside home.
    Raises ValueError for anything else."""
    home = os.path.realpath(home_dir())
    if not requested:
        return home
    path = os.path.realpath(os.path.expanduser(requested))
    if path != home and not path.startswith(home.rstrip("/") + "/"):
        raise ValueError("The start folder has to be inside your home folder.")
    if not os.path.isdir(path):
        raise ValueError("That start folder does not exist.")
    return path


def _shell_env(shell: str) -> Dict[str, str]:
    """A clean login environment. The server's own environment (API keys from
    .env, the internal tool token, the venv on PATH) is not passed on; the
    login shell's profile builds the rest, like an ssh login would."""
    try:
        pw = pwd.getpwuid(os.getuid())
        user, home = pw.pw_name, pw.pw_dir
    except Exception:
        user, home = os.environ.get("USER", ""), home_dir()
    env = {
        "HOME": home, "USER": user, "LOGNAME": user, "SHELL": shell,
        "PATH": _DEFAULT_PATH,
        "TERM": "xterm-256color", "COLORTERM": "truecolor",
        "ODYSSEUS_TERMINAL": "1",
    }
    for k in ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    env.setdefault("LANG", "C.UTF-8")
    return env


def _set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


class Attachment:
    """One WebSocket watching a session. Output is queued here and sent by
    its own task, so a slow socket never blocks reading the PTY."""

    def __init__(self, ws, backlog: bytes):
        self.ws = ws
        self.pending = bytearray(backlog)
        self.wake = asyncio.Event()
        self.closed = False
        if backlog:
            self.wake.set()

    def push(self, data: bytes) -> None:
        self.pending += data
        self.wake.set()


class Session:
    def __init__(self, owner: str, title: str, cwd: str, rows: int, cols: int):
        self.id = secrets.token_urlsafe(9)
        self.owner = owner
        self.title = title
        self.start_cwd = cwd
        self.created = time.time()
        self.detached_at: Optional[float] = time.time()
        self.scrollback = bytearray()
        self.att: Optional[Attachment] = None
        self.exited = False
        self.exit_code: Optional[int] = None
        self._loop = asyncio.get_running_loop()
        self._reap_timer: Optional[asyncio.TimerHandle] = None
        self._reading = False
        self._eof = False
        self._outbuf = bytearray()      # input the PTY hasn't taken yet
        self._writing = False

        self.shell = login_shell()
        master, slave = os.openpty()
        try:
            _set_size(slave, rows, cols)
            self.proc = subprocess.Popen(
                [self.shell, "-l"],
                stdin=slave, stdout=slave, stderr=slave,
                cwd=cwd, env=_shell_env(self.shell),
                start_new_session=True,        # its own session and process group
                preexec_fn=_take_ctty,         # ... with the PTY as its terminal
                close_fds=True,
            )
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd = master
        os.set_blocking(master, False)
        self.pid = self.proc.pid
        self._start_reading()
        # The shell can exit while something it started still holds the PTY
        # open (so no EIO arrives); watch the process itself too.
        threading.Thread(target=self._wait_child, name=f"terminal-{self.id}", daemon=True).start()

    def _wait_child(self):
        try:
            self.proc.wait()
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._child_exited)
        except RuntimeError:      # the loop is gone (shutdown)
            pass

    def _child_exited(self):
        if self.exited:
            return
        # Take what the shell wrote last before closing the PTY.
        for _ in range(64):
            if self.exited:
                return
            try:
                data = os.read(self.fd, 65536)
            except (BlockingIOError, OSError):
                break
            if not data:
                break
            self._take(data)
        self._on_exit()

    # ── Output ──────────────────────────────────────────────────────────
    def _start_reading(self):
        if not self._reading and not self.exited and not self._eof:
            self._loop.add_reader(self.fd, self._on_readable)
            self._reading = True

    def _stop_reading(self):
        if self._reading:
            try:
                self._loop.remove_reader(self.fd)
            except Exception:
                pass
            self._reading = False

    def _on_readable(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:          # EIO: every process holding the PTY is gone
            data = b""
        if not data:
            # The shell is exiting; the process watcher (_child_exited)
            # finishes up once it has its exit code.
            self._eof = True
            self._stop_reading()
            if self.proc.poll() is not None:
                self._on_exit()
            return
        self._take(data)

    def _take(self, data: bytes):
        self.scrollback += data
        over = len(self.scrollback) - SCROLLBACK_BYTES
        if over > 0:
            # Cut at a line break where one is near, so the replay doesn't
            # start in the middle of an escape sequence or a character.
            nl = self.scrollback.find(b"\n", over, over + 4096)
            del self.scrollback[:(nl + 1) if nl >= 0 else over]
        if self.att and not self.att.closed:
            self.att.push(data)
            if len(self.att.pending) > _SEND_HIGH_WATER:
                self._stop_reading()      # resumed once the socket catches up

    def drained(self):
        """The attached socket sent what was queued: read the PTY again."""
        if not self.exited:
            self._start_reading()

    def _on_exit(self):
        if self.exited:
            return
        self.exited = True
        self._stop_reading()
        self._stop_writing()
        self.exit_code = self.proc.returncode
        manager.forget(self)
        if self.att and not self.att.closed:
            self.att.push(b"")           # wake the sender, which reports the exit
        self._close_fd()

    def _close_fd(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

    # ── Input ───────────────────────────────────────────────────────────
    def write(self, data: bytes) -> None:
        if self.exited or not data:
            return
        self._outbuf += data
        self._flush()

    def _flush(self):
        while self._outbuf and not self.exited:
            try:
                n = os.write(self.fd, self._outbuf[:65536])
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                self._outbuf.clear()
                break
            del self._outbuf[:n]
        if self._outbuf and not self.exited:
            if not self._writing:
                self._loop.add_writer(self.fd, self._flush)
                self._writing = True
        else:
            self._stop_writing()

    def _stop_writing(self):
        if self._writing:
            try:
                self._loop.remove_writer(self.fd)
            except Exception:
                pass
            self._writing = False

    def resize(self, rows, cols) -> None:
        if self.exited:
            return
        rows, cols = _clamp(rows, 1, 1000, 24), _clamp(cols, 1, 1000, 80)
        try:
            _set_size(self.fd, rows, cols)       # the kernel sends SIGWINCH
        except OSError:
            pass

    # ── Attach / detach ─────────────────────────────────────────────────
    def attach(self, ws) -> Attachment:
        """Make `ws` the one socket for this session (a second tab or a
        reconnect takes over from the first) and queue the scrollback."""
        old = self.att
        if old and not old.closed:
            old.closed = True
            old.wake.set()
        self._cancel_reap()
        self.detached_at = None
        self.att = Attachment(ws, bytes(self.scrollback))
        return self.att

    def detach(self, att: Attachment) -> None:
        att.closed = True
        att.wake.set()
        if self.att is not att:
            return
        self.att = None
        self._start_reading()
        if self.exited:
            return
        self.detached_at = time.time()
        self._cancel_reap()
        self._reap_timer = self._loop.call_later(GRACE_S, self._reap_idle)

    def _cancel_reap(self):
        if self._reap_timer:
            self._reap_timer.cancel()
            self._reap_timer = None

    def _reap_idle(self):
        self._reap_timer = None
        if self.att is None and not self.exited:
            logger.info("Terminal session %s idle for %ss, closing it", self.id, GRACE_S)
            self.kill()

    # ── Ending ──────────────────────────────────────────────────────────
    def _groups(self) -> List[int]:
        """Process groups to signal: the shell's own and whatever job is in
        the foreground. (An interactive shell passes SIGHUP on to its
        background jobs itself, as it does when an ssh connection drops.)"""
        groups = [self.pid]
        if self.fd >= 0:
            try:
                fg = os.tcgetpgrp(self.fd)
                if fg > 0 and fg not in groups:
                    groups.append(fg)
            except OSError:
                pass
        return groups

    def kill(self) -> None:
        """Hang up the session, then SIGKILL whatever ignores that."""
        self._cancel_reap()
        groups = self._groups()
        for g in groups:
            try:
                os.killpg(g, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
        manager.forget(self)
        if self.exited:
            return

        def _force():
            for g in groups:
                try:
                    os.killpg(g, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if not self.exited:
                # Closing the PTY ends any reader still holding it.
                self._on_exit()

        if self._loop.is_closed():
            # Shutting down (no loop to wait on): give it a moment, then force.
            end = time.time() + 0.5
            while time.time() < end and self.proc.poll() is None:
                time.sleep(0.02)
            for g in groups:
                try:
                    os.killpg(g, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            self.exited = True
            self._reading = self._writing = False
            self._close_fd()
            return
        self._loop.call_later(KILL_TIMEOUT_S, _force)

    def cwd(self) -> str:
        try:
            return os.readlink(f"/proc/{self.pid}/cwd")
        except OSError:
            return self.start_cwd

    def info(self) -> dict:
        return {
            "id": self.id, "title": self.title, "cwd": self.cwd(),
            "created": self.created, "attached": self.att is not None and not self.att.closed,
            "detached_at": self.detached_at, "shell": self.shell,
        }


def _take_ctty():
    # Runs in the child after setsid(): make the PTY (fd 0) its controlling
    # terminal, so Ctrl+C, job control and /dev/tty (ssh prompts) work.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class TooManySessions(Exception):
    pass


class TerminalManager:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}

    def create(self, owner: str, *, cwd: Optional[str] = None, rows=24, cols=80,
               title: Optional[str] = None) -> Session:
        mine = self.list(owner)
        if len(mine) >= MAX_SESSIONS:
            raise TooManySessions(
                f"You already have {len(mine)} terminals open, the most allowed. Close one first.")
        start = resolve_cwd(cwd)
        if not title:
            used = {s.title for s in mine}
            n = 1
            while f"Shell {n}" in used:
                n += 1
            title = f"Shell {n}"
        s = Session(owner, title[:60], start, _clamp(rows, 1, 1000, 24), _clamp(cols, 1, 1000, 80))
        self._sessions[s.id] = s
        logger.info("Terminal session %s started for %s (%s)", s.id, owner or "(no auth)", s.shell)
        return s

    def get(self, owner: str, sid: str) -> Optional[Session]:
        s = self._sessions.get(sid)
        return s if s and s.owner == owner and not s.exited else None

    def list(self, owner: str) -> List[Session]:
        return sorted((s for s in self._sessions.values() if s.owner == owner and not s.exited),
                      key=lambda s: s.created)

    def forget(self, s: Session) -> None:
        if self._sessions.get(s.id) is s:
            del self._sessions[s.id]

    def close(self, owner: str, sid: str) -> bool:
        s = self.get(owner, sid)
        if not s:
            return False
        logger.info("Terminal session %s closed", s.id)
        s.kill()
        return True

    def close_all(self) -> None:
        for s in list(self._sessions.values()):
            s.kill()


manager = TerminalManager()


async def pump(session: Session, att: Attachment, hello: dict) -> None:
    """Send `hello`, then the session's output, to the attached socket until
    it is detached or the shell exits."""
    ws = att.ws
    await ws.send_text(json.dumps(hello))
    while True:
        await att.wake.wait()
        att.wake.clear()
        if att.closed:
            return
        if att.pending:
            chunk = bytes(att.pending)
            att.pending.clear()
            await ws.send_bytes(chunk)
            session.drained()
        if session.exited and not att.pending:
            await ws.send_text(json.dumps({"type": "exit", "code": session.exit_code}))
            return
