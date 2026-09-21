"""MCP server for controlling a Linux desktop, served over SSE.

The Linux counterpart to desktop_mcp_server.py, and it lives on the laptop for
the same reason that one lives on the Windows box: launching an app or pausing
music only means anything inside the graphical session that owns them.

That constraint is sharper here than on Windows. Everything below talks to the
session bus, and `DBUS_SESSION_BUS_ADDRESS` only exists inside a logged-in
graphical session. Started from a plain SSH shell this server runs but can see
nothing, so it is meant to be a `systemd --user` service, which inherits the
session environment. `desktop_status` exists to say so out loud rather than
letting every other tool fail in the same shapeless way.

No playerctl, wmctrl or gtk-launch dependency. Media goes over MPRIS on the
session bus through gdbus, which ships with GLib and is therefore already
present wherever a desktop is; apps come from parsing .desktop files, which is
where the desktop itself gets them. Depending on packages that may not be
installed would turn "not installed" into "the tool is broken".

Wayland is assumed possible throughout: it refuses window focus to outside
processes by design, so focus is reported as unsupported there rather than
silently doing nothing.
"""

import configparser
import glob
import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from mcp_transport_security import security_settings

# Read here, not in __main__: mcp validates the Host header against an
# allowlist built from the bind address, so the server object cannot be
# constructed until the bind is known.
HOST = os.environ.get("LINUX_DESKTOP_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("LINUX_DESKTOP_MCP_PORT", "8932"))

_sec = security_settings(HOST, PORT, "LINUX_DESKTOP_MCP_ALLOWED_HOSTS")
mcp = FastMCP("linux-desktop", **({"transport_security": _sec} if _sec else {}))

APP_DIRS = [
    os.path.expanduser("~/.local/share/applications"),
    "/usr/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
    "/var/lib/snapd/desktop/applications",
]

MPRIS_PREFIX = "org.mpris.MediaPlayer2."


def _run(argv: List[str], timeout: int = 20) -> Dict[str, Any]:
    """Run a command, returning its result rather than raising."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "exit_code": p.returncode,
                "stdout": (p.stdout or "").strip(), "stderr": (p.stderr or "").strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s: {argv[0]}"}
    except FileNotFoundError:
        return {"ok": False, "error": f"not installed: {argv[0]}"}


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _session_bus() -> Optional[str]:
    return os.environ.get("DBUS_SESSION_BUS_ADDRESS")


def _require_session() -> Optional[Dict[str, Any]]:
    """The one precondition every desktop tool shares."""
    if _session_bus():
        return None
    return {
        "ok": False,
        "error": "No session bus (DBUS_SESSION_BUS_ADDRESS is unset), so there is no desktop "
                 "to act on. This happens when the server was started from an SSH shell "
                 "instead of inside the graphical session. Run it as a systemd --user "
                 "service, and make sure the laptop is logged in.",
    }


# --------------------------------------------------------------------------
# Apps
# --------------------------------------------------------------------------

def _desktop_entries() -> Dict[str, Dict[str, str]]:
    """Every launchable .desktop entry, keyed by its id (filename sans suffix)."""
    out: Dict[str, Dict[str, str]] = {}
    for d in APP_DIRS:
        for path in glob.glob(os.path.join(d, "*.desktop")):
            app_id = os.path.basename(path)[: -len(".desktop")]
            if app_id in out:
                continue  # earlier directories win, matching XDG precedence
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(path, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("NoDisplay", "false").lower() == "true":
                continue
            if e.get("Type", "Application") != "Application":
                continue
            out[app_id] = {
                "id": app_id,
                "name": e.get("Name", app_id),
                "exec": e.get("Exec", ""),
                "path": path,
                "terminal": e.get("Terminal", "false"),
            }
    return out


@mcp.tool()
def list_apps(match: str = "") -> Dict[str, Any]:
    """Installed desktop applications, optionally filtered by a substring."""
    entries = _desktop_entries()
    want = (match or "").strip().lower()
    apps = [
        {"id": v["id"], "name": v["name"]}
        for v in entries.values()
        if not want or want in v["name"].lower() or want in v["id"].lower()
    ]
    apps.sort(key=lambda a: a["name"].lower())
    return {"ok": True, "count": len(apps), "apps": apps,
            "note": "Pass an id from here to launch_app." if apps else
                    f"Nothing matched {match!r}."}


@mcp.tool()
def launch_app(app: str) -> Dict[str, Any]:
    """Launch an installed app by its id or name, as listed by list_apps."""
    blocked = _require_session()
    if blocked:
        return blocked
    want = (app or "").strip()
    if not want:
        raise RuntimeError("app is required — an id or name from list_apps")
    entries = _desktop_entries()

    entry = entries.get(want)
    if entry is None:
        matches = [v for v in entries.values()
                   if want.lower() == v["name"].lower() or want.lower() in v["id"].lower()
                   or want.lower() in v["name"].lower()]
        if not matches:
            return {"ok": False, "error": f"No app matching {want!r}. Use list_apps to see ids."}
        if len(matches) > 1:
            exact = [m for m in matches if m["name"].lower() == want.lower()]
            if len(exact) != 1:
                # Ambiguity resolved by naming the options, not by picking one:
                # launching the wrong app is worse than asking again.
                return {"ok": False,
                        "error": f"{want!r} matches several apps; say which: "
                                 + ", ".join(sorted(m["id"] for m in matches[:8]))}
            matches = exact
        entry = matches[0]

    # `gio launch` honours the desktop entry properly (Terminal=, field codes,
    # startup notification, systemd app scope). Spawning Exec by hand gets
    # those subtly wrong.
    #
    # It also *waits for the launched app to exit*, which is the trap here:
    # waiting on it with a timeout and reading the timeout as failure starts
    # the app, falls through to the Exec branch, and starts it a second time.
    # So only an immediate non-zero exit counts as failure; a launcher still
    # alive after a moment means the app is up and holding it open.
    err = "gio is not installed"
    if _have("gio"):
        try:
            proc = subprocess.Popen(
                ["gio", "launch", entry["path"]], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            err = f"could not run gio: {e}"
        else:
            try:
                rc = proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                # Still running: gio is waiting on the app, so the app started.
                return {"ok": True, "launched": entry["id"], "name": entry["name"]}
            if rc == 0:
                return {"ok": True, "launched": entry["id"], "name": entry["name"]}
            stderr = ""
            try:
                stderr = (proc.stderr.read() or "").strip() if proc.stderr else ""
            except Exception:
                pass
            err = f"gio launch exited {rc}" + (f": {stderr[:200]}" if stderr else "")

    # Fall back to the Exec line with field codes stripped; %U/%f and friends
    # are placeholders for files and must not reach the shell as literals.
    cmd = re.sub(r"%[fFuUdDnNickvm]", "", entry.get("exec", "")).strip()
    if not cmd:
        return {"ok": False, "error": f"{err}, and {entry['id']} has no usable Exec line."}
    try:
        subprocess.Popen(cmd, shell=True, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return {"ok": False, "error": f"{err}; falling back to Exec also failed: {e}"}
    # Say which route was taken and why, rather than asserting a cause. The
    # previous wording blamed a missing gio unconditionally, which hid a real
    # double launch for as long as nobody counted the processes.
    return {"ok": True, "launched": entry["id"], "name": entry["name"],
            "note": f"Launched via its Exec line ({err})."}


@mcp.tool()
def open_url(url: str) -> Dict[str, Any]:
    """Open a URL in the default browser."""
    blocked = _require_session()
    if blocked:
        return blocked
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise RuntimeError("url must start with http:// or https://")
    r = _run(["xdg-open", url], timeout=15)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("stderr") or r.get("error") or "xdg-open failed"}
    return {"ok": True, "opened": url}


# --------------------------------------------------------------------------
# Media, over MPRIS
# --------------------------------------------------------------------------

def _gdbus(args: List[str], timeout: int = 15) -> Dict[str, Any]:
    return _run(["gdbus"] + args, timeout=timeout)


def _mpris_players() -> List[str]:
    r = _gdbus(["call", "--session", "-d", "org.freedesktop.DBus",
                "-o", "/org/freedesktop/DBus",
                "-m", "org.freedesktop.DBus.ListNames"])
    if not r.get("ok"):
        return []
    return sorted(set(re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", r.get("stdout", ""))))


def _unwrap(raw: str) -> str:
    """gdbus prints results as a GVariant tuple; pull out the useful middle."""
    s = (raw or "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    if s.endswith(","):
        s = s[:-1]
    return s.strip()


@mcp.tool()
def now_playing() -> Dict[str, Any]:
    """What is playing, via MPRIS. Covers any compliant player."""
    blocked = _require_session()
    if blocked:
        return blocked
    players = _mpris_players()
    if not players:
        return {"playing": False, "reason": "No MPRIS player is running. Spotify, a browser "
                                            "playing media, VLC and most others register one "
                                            "while they have something loaded."}
    out = []
    for p in players:
        status = _gdbus(["call", "--session", "-d", p, "-o", "/org/mpris/MediaPlayer2",
                         "-m", "org.freedesktop.DBus.Properties.Get",
                         "org.mpris.MediaPlayer2.Player", "PlaybackStatus"])
        meta = _gdbus(["call", "--session", "-d", p, "-o", "/org/mpris/MediaPlayer2",
                       "-m", "org.freedesktop.DBus.Properties.Get",
                       "org.mpris.MediaPlayer2.Player", "Metadata"])
        raw = meta.get("stdout", "")
        title = re.search(r"'xesam:title':\s*<'([^']*)'>", raw)
        artist = re.search(r"'xesam:artist':\s*<\['([^']*)'", raw)
        out.append({
            "player": p[len(MPRIS_PREFIX):],
            "status": _unwrap(status.get("stdout", "")).strip("<>'"),
            "title": title.group(1) if title else None,
            "artist": artist.group(1) if artist else None,
        })
    return {"playing": any(p["status"] == "Playing" for p in out), "players": out}


@mcp.tool()
def media_control(action: str, player: str = "") -> Dict[str, Any]:
    """Control playback: play_pause, play, pause, stop, next, previous.

    Without `player`, the first MPRIS player found is used; pass one from
    now_playing when several are running.
    """
    blocked = _require_session()
    if blocked:
        return blocked
    methods = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause",
               "stop": "Stop", "next": "Next", "previous": "Previous"}
    method = methods.get((action or "").strip().lower())
    if not method:
        raise RuntimeError(f"action must be one of: {', '.join(sorted(methods))}")

    players = _mpris_players()
    if not players:
        return {"ok": False, "error": "No MPRIS player is running, so there is nothing to "
                                      "control. Start the player first."}
    target = None
    if player:
        want = player.strip().lower()
        target = next((p for p in players
                       if want == p[len(MPRIS_PREFIX):].lower()
                       or want in p.lower()), None)
        if not target:
            return {"ok": False, "error": f"No player matching {player!r}. Running: "
                                          + ", ".join(p[len(MPRIS_PREFIX):] for p in players)}
    else:
        target = players[0]

    r = _gdbus(["call", "--session", "-d", target, "-o", "/org/mpris/MediaPlayer2",
                "-m", f"org.mpris.MediaPlayer2.Player.{method}"])
    if not r.get("ok"):
        return {"ok": False, "error": r.get("stderr") or r.get("error") or "call failed"}
    return {"ok": True, "action": action, "player": target[len(MPRIS_PREFIX):]}


# --------------------------------------------------------------------------
# Windows and status
# --------------------------------------------------------------------------

@mcp.tool()
def focus_app(name: str) -> Dict[str, Any]:
    """Raise a window whose title or class matches. X11 only."""
    blocked = _require_session()
    if blocked:
        return blocked
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return {"ok": False, "error":
                "This session is Wayland, which does not let one process raise another's "
                "window. Nothing can do this here, so the app was left alone rather than "
                "reporting a success that did not happen."}
    if not _have("wmctrl"):
        return {"ok": False, "error": "wmctrl is not installed (apt install wmctrl)."}
    r = _run(["wmctrl", "-a", name])
    if not r.get("ok"):
        return {"ok": False, "error": f"No window matching {name!r}."}
    return {"ok": True, "focused": name}


@mcp.tool()
def desktop_status() -> Dict[str, Any]:
    """What this machine can and cannot be asked to do, and why.

    Worth calling first: every other tool here depends on a graphical session,
    and this reports its absence once instead of once per failed call.
    """
    session = _session_bus()
    stype = os.environ.get("XDG_SESSION_TYPE", "unknown")
    out: Dict[str, Any] = {
        "host": os.uname().nodename,
        "session_bus": bool(session),
        "session_type": stype,
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
        "app_count": len(_desktop_entries()),
        "tools": {
            "gio": _have("gio"),
            "gdbus": _have("gdbus"),
            "xdg-open": _have("xdg-open"),
            "wmctrl": _have("wmctrl"),
        },
    }
    if not session:
        out["usable"] = False
        out["reason"] = ("No session bus, so launching, media and focus will all refuse. The "
                         "server is running outside the graphical session, or the machine is "
                         "not logged in. Run it as a systemd --user service.")
        return out
    out["usable"] = True
    out["players"] = [p[len(MPRIS_PREFIX):] for p in _mpris_players()]
    if stype.lower() == "wayland":
        out["focus_supported"] = False
        out["focus_note"] = "Wayland refuses cross-process window raising; focus_app will say so."
    else:
        out["focus_supported"] = _have("wmctrl")
    return out


if __name__ == "__main__":
    import uvicorn  # noqa: F401
    # HOST/PORT come from the module scope above, where the Host allowlist was
    # built from them. Binds the tailnet address by default, never all
    # interfaces: these tools launch applications, and the laptop joins
    # networks you do not control.
    mcp.settings.host = HOST
    mcp.settings.port = PORT
    print(f"linux-desktop-mcp listening on {HOST}:{PORT} (sse)", flush=True)
    mcp.run(transport="sse")
