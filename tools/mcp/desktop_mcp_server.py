"""MCP server for controlling desktop apps on the Windows machine, served over SSE.

Companion to resolve_mcp_server.py. Same reasoning for living on the Windows
box rather than beside Odysseus: launching and focusing windows, and sending
media keys, only mean anything in the interactive desktop session.

Three groups of tools, and one rule that shapes all of them.

The rule: nothing here execs an arbitrary command. `launch_app` takes a key
from a fixed table or the name of a Start menu entry (whose AppID comes from
Windows), never a path or command line, so a prompt-injected model
cannot turn "open my editor" into "run this binary". Giving an app special
handling (process checks, CLI access) means editing APPS below, deliberately.

- VS Code: driven through its own CLI, which is the supported way in and
  handles an already-running instance correctly (it reuses the window instead
  of starting a second copy).
- Minecraft: read-only. Real in-game control needs a mod or RCON, which is not
  installed; pretending otherwise would be worse than saying so. What is
  genuinely useful without one is answering "what mods am I running" and "why
  did it crash", so that is what is exposed.
- Media: Windows media keys, not a YouTube Music integration. There is no YTM
  desktop app on this machine, and the keys work on whatever currently holds
  media focus (YouTube Music in a browser, Spotify, a video), which covers
  more than an app-specific binding would and cannot break when a site's DOM
  changes.
"""

import glob
import json
import asyncio
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("desktop")

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", r"C:\Users\jaron\AppData\Local")
APPDATA = os.environ.get("APPDATA", r"C:\Users\jaron\AppData\Roaming")

VSCODE_DIR = os.path.join(LOCALAPPDATA, "Programs", "Microsoft VS Code Insiders")
VSCODE_EXE = os.path.join(VSCODE_DIR, "Code - Insiders.exe")
# The .cmd wrapper, not the .exe: the exe detaches immediately and returns
# nothing, so `--list-extensions` and friends only work through the wrapper.
VSCODE_CLI = os.path.join(VSCODE_DIR, "bin", "code-insiders.cmd")

MINECRAFT_DIR = os.path.join(APPDATA, ".minecraft")

# The launch allowlist. Keys are what a model may ask for; values are resolved
# here and never taken from the caller.
APPS: Dict[str, Dict[str, Any]] = {
    "vscode": {
        "path": VSCODE_EXE,
        "process": "Code - Insiders",
        "description": "Visual Studio Code Insiders",
    },
    "resolve": {
        "path": r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe",
        "process": "Resolve",
        "description": "DaVinci Resolve Studio",
    },
    "youtube_music": {
        # No desktop app is installed, so this opens the web player in the
        # default browser. Playback is then controlled with media_control.
        "url": "https://music.youtube.com",
        "process": None,
        "description": "YouTube Music (web player in the default browser)",
    },
    "bambu_studio": {
        "path": r"C:\Program Files\Bambu Studio\bambu-studio.exe",
        "process": "bambu-studio",
        "description": "Bambu Studio (slicer for the Bambu Lab printer)",
    },
    "orca_slicer": {
        "path": r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
        "process": "orca-slicer",
        "description": "OrcaSlicer",
    },
    "minecraft": {
        # Installed from the Store, so there is no .exe to run: it is launched
        # by app id through the shell. The path below is the game data folder,
        # which is what the read-only minecraft_* tools inspect.
        "path": os.path.join(APPDATA, ".minecraft"),
        "aumid": r"Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft",
        # Two processes, and the difference matters: "Minecraft" is the
        # launcher, "javaw" is the game, and the game only exists once
        # somebody clicks Play. Watching for javaw alone reports a working
        # launch as a failure.
        "launcher_process": "Minecraft",
        "process": "javaw",
        "description": "Minecraft Java Edition (opens the launcher; Play still needs a click)",
    },
}


def _run(argv: List[str], timeout: int = 30) -> Dict[str, Any]:
    """Run a command and return its result rather than raising on failure.

    A non-zero exit with its stderr is far more useful to a model than a
    stack trace, and keeps 'the tool broke' distinguishable from 'the command
    said no'.
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": p.returncode == 0,
            "exit_code": p.returncode,
            "stdout": (p.stdout or "").strip(),
            "stderr": (p.stderr or "").strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s: {argv[0]}"}
    except FileNotFoundError:
        return {"ok": False, "error": f"not installed or wrong path: {argv[0]}"}


def _processes() -> Dict[str, int]:
    """Running process names mapped to one pid each."""
    out: Dict[str, int] = {}
    r = _run(["tasklist", "/fo", "csv", "/nh"], timeout=20)
    for line in (r.get("stdout") or "").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2:
            name = parts[0].removesuffix(".exe")
            try:
                out.setdefault(name, int(parts[1]))
            except ValueError:
                pass
    return out



def _wait_for_process(name: Optional[str], timeout: float = 20.0) -> Optional[int]:
    """Poll until a process appears, returning its pid, or None on timeout.

    Launching is asynchronous on Windows: the call that starts an app returns
    long before the app exists. Without waiting, every launch looks
    successful, including the ones that silently did nothing.
    """
    if not name:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _processes().get(name)
        if pid:
            return pid
        time.sleep(1.0)
    return None


# --------------------------------------------------------------------------
# Everything else in the Start menu
# --------------------------------------------------------------------------
#
# The table above is six apps; this machine has ~300 in its Start menu. The
# rest are found, not configured: Get-StartApps lists every Start menu entry
# (classic programs, Store apps, Steam games) with an AppID that Explorer can
# launch through shell:AppsFolder. That keeps the rule: the caller names an
# app from that list, and the AppID comes from Windows, never from the caller,
# so no path or command line can be smuggled in.

START_APPS_TTL = 300.0
_start_cache: Dict[str, Any] = {"at": 0.0, "apps": []}

# Entries that launch but should never be opened by an assistant: removing
# software, repairing it, resetting it, or reinstalling it.
_SKIP_NAME = re.compile(
    r"\b(uninstall\w*|uninst|remove|repair|reset|recovery|setup|installer|"
    r"modify|change or remove)\b", re.I)
# Documents rather than programs: help files, readmes, web shortcuts.
_SKIP_SUFFIX = (".chm", ".txt", ".pdf", ".rtf", ".htm", ".html", ".url", ".ini", ".log")


def _parse_start_apps(raw: str) -> List[Dict[str, str]]:
    """`Get-StartApps | ConvertTo-Json` to a clean, sorted [{name, app_id}]."""
    raw = (raw or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):                  # one app comes back unwrapped
        data = [data]
    out, seen = [], set()
    for d in data if isinstance(data, list) else []:
        name = str(d.get("Name") or "").strip()
        app_id = str(d.get("AppID") or "").strip()
        if not name or not app_id:
            continue
        leaf = app_id.replace("/", "\\").rsplit("\\", 1)[-1]
        if _SKIP_NAME.search(name) or _SKIP_NAME.search(leaf):
            continue
        if leaf.lower().endswith(_SKIP_SUFFIX):
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "app_id": app_id})
    out.sort(key=lambda a: a["name"].lower())
    return out


def _start_apps(refresh: bool = False) -> List[Dict[str, str]]:
    if not refresh and _start_cache["apps"] and time.time() - _start_cache["at"] < START_APPS_TTL:
        return _start_cache["apps"]
    r = _run(["powershell", "-NoProfile", "-Command",
              "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"], timeout=45)
    if not r.get("ok"):
        return _start_cache["apps"]              # keep the last good list
    try:
        apps = _parse_start_apps(r.get("stdout", ""))
    except (ValueError, TypeError):
        return _start_cache["apps"]
    _start_cache.update(at=time.time(), apps=apps)
    return apps


def _find_start_app(apps: List[Dict[str, str]], want: str) -> Dict[str, Any]:
    """Pick the one app `want` means: exact name, then a unique prefix, then a
    unique substring. Returns {"app": ...} or {"candidates": [...]}."""
    w = (want or "").strip().lower()
    if not w:
        return {"candidates": []}
    exact = [a for a in apps if a["name"].lower() == w]
    if exact:
        return {"app": exact[0]}
    for test in (lambda n: n.startswith(w), lambda n: w in n):
        hits = [a for a in apps if test(a["name"].lower())]
        if len(hits) == 1:
            return {"app": hits[0]}
        if hits:
            return {"candidates": [a["name"] for a in hits[:12]]}
    return {"candidates": []}


# --------------------------------------------------------------------------
# Apps
# --------------------------------------------------------------------------

@mcp.tool()
def list_apps(match: str = "") -> Dict[str, Any]:
    """Apps on this PC. `curated` are the ones with special handling (VS Code,
    Resolve, Minecraft, ...) and a running state; `installed` is everything
    else in the Start menu, by name. Pass `match` to filter, e.g. "adobe".
    Launch any of them with launch_app(app=<key or name>)."""
    m = (match or "").strip().lower()
    installed = [a["name"] for a in _start_apps() if not m or m in a["name"].lower()]
    return {
        "curated": [a for a in _curated_apps()
                    if not m or m in a["app"] or m in a["description"].lower()],
        "installed": installed if m else installed[:150],
        "installed_total": len(installed),
        "note": ("" if m or len(installed) <= 150 else
                 f"Showing 150 of {len(installed)}; pass match= to narrow it."),
    }


def _curated_apps() -> List[Dict[str, Any]]:
    """The apps that can be launched, and whether each is running right now."""
    procs = _processes()
    out = []
    for key, spec in APPS.items():
        installed = bool(
            spec.get("url")
            or (spec.get("aumid") and os.path.exists(spec.get("path", "")))
            or os.path.exists(spec.get("path", ""))
        )
        pname = spec.get("process")
        lname = spec.get("launcher_process")
        running = bool(pname and pname in procs)
        entry = {
            "app": key,
            "description": spec["description"],
            "installed": installed,
            "running": running,
            "pid": procs.get(pname) if pname else None,
        }
        if lname:
            # Without this, a launcher sitting on the Play screen is
            # indistinguishable from nothing having happened at all.
            entry["launcher_running"] = lname in procs
            if entry["launcher_running"] and not running:
                entry["state"] = "launcher_open"
        out.append(entry)
    return out


@mcp.tool()
def launch_app(app: str) -> Dict[str, Any]:
    """Start an app from list_apps: a curated key ("vscode", "resolve") or the
    name of anything in the Start menu ("Adobe Photoshop 2025", "Audacity").
    Never a path or command line.
    """
    spec = APPS.get((app or "").strip().lower())
    if not spec:
        return _launch_start_app(app)

    if spec.get("url"):
        os.startfile(spec["url"])  # noqa: S606 - a constant from APPS, not caller input
        return {"ok": True, "opened": spec["url"], "app": app}

    if spec.get("aumid"):
        # Store apps have no runnable path; the shell resolves them by app id.
        _run(["explorer.exe", f"shell:AppsFolder\\{spec['aumid']}"], timeout=20)
        # explorer.exe's exit code says nothing about the app, so the only
        # honest answer comes from watching for the process. Returning ok
        # without this is how "I opened Minecraft" gets said about a launcher
        # that never appeared.
        appeared = _wait_for_process(spec.get("process"), timeout=25.0)
        if appeared:
            return {"ok": True, "app": app, "launched_via": "shell:AppsFolder",
                    "pid": appeared, "state": "running"}
        launcher = _wait_for_process(spec.get("launcher_process"), timeout=1.0)
        if launcher:
            # The launcher came up and is waiting for a human. Saying "ok"
            # here would invite the claim that the game is running; saying
            # "failed" would be wrong too.
            return {
                "ok": True,
                "app": app,
                "state": "launcher_open",
                "pid": launcher,
                "note": (
                    f"The {app} launcher is open but the game has not started — that "
                    f"needs Play to be clicked. Do not say {app} is running. Take a "
                    f"screenshot to see the launcher, or ask the user to press Play."
                ),
            }
        return {
            "ok": False,
            "app": app,
            "launched_via": "shell:AppsFolder",
            "state": "not_started",
            "error": (
                f"Asked Windows to start {app}, but neither the launcher nor the game "
                f"appeared within 25s. Do not report it as open — take a screenshot "
                f"and look."
            ),
        }

    target = spec.get("path")
    if not os.path.exists(target or ""):
        return {"ok": False, "app": app, "error": f"not installed at {target}"}

    subprocess.Popen([target], close_fds=True)
    appeared = _wait_for_process(spec.get("process"), timeout=20.0)
    if appeared:
        return {"ok": True, "app": app, "pid": appeared}
    # A slow-starting app is common, so this is "not yet" rather than
    # "failed" — but it is not success, and must not read as it.
    return {
        "ok": False,
        "app": app,
        "error": (
            f"Started {target} but no {spec.get('process')} process appeared within 20s. "
            f"It may still be loading. Take a screenshot and look before saying it is open."
        ),
    }


def _launch_start_app(app: str) -> Dict[str, Any]:
    """Launch a Start menu entry by name, through Explorer's app folder."""
    found = _find_start_app(_start_apps(), app)
    if "app" not in found:
        found = _find_start_app(_start_apps(refresh=True), app)   # just installed?
    if "app" not in found:
        cands = found.get("candidates") or []
        return {
            "ok": False,
            "app": app,
            "error": (f"{app!r} matches several apps: {', '.join(cands)}. Say which one."
                      if cands else
                      f"No app called {app!r} on this PC. Call list_apps(match=...) to look."),
        }
    entry = found["app"]
    # The AppID came from Get-StartApps, not from the caller.
    _run(["explorer.exe", f"shell:AppsFolder\\{entry['app_id']}"], timeout=20)
    # Explorer's exit code says nothing about the app, and a Start menu entry
    # does not say which process it becomes, so this cannot confirm the app
    # is up. Say so rather than claim it.
    return {
        "ok": True,
        "app": entry["name"],
        "launched_via": "shell:AppsFolder",
        "note": (f"Asked Windows to open {entry['name']}. It can take a few seconds; "
                 f"take a screenshot before saying it is open."),
    }


@mcp.tool()
def focus_app(app: str) -> Dict[str, Any]:
    """Bring a running app's window to the front."""
    spec = APPS.get((app or "").strip().lower())
    if not spec:
        raise RuntimeError(f"Unknown app {app!r}. Allowed: {', '.join(sorted(APPS))}")
    pname = spec.get("process")
    if not pname:
        return {"ok": False, "error": f"{app} has no window of its own to focus (it opens in a browser)."}
    # AppActivate takes the process id; going through PowerShell avoids needing
    # a win32 dependency on the Windows side.
    ps = (
        f"$p = Get-Process '{pname}' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1; "
        "if (-not $p) { Write-Output 'NOWINDOW'; exit }; "
        "(New-Object -ComObject WScript.Shell).AppActivate($p.Id) | Out-Null; "
        "Write-Output 'OK'"
    )
    r = _run(["powershell", "-NoProfile", "-Command", ps])
    if "NOWINDOW" in (r.get("stdout") or ""):
        return {"ok": False, "app": app, "error": f"{app} is not running, or has no visible window."}
    return {"ok": r.get("ok", False), "app": app}


# --------------------------------------------------------------------------
# VS Code
# --------------------------------------------------------------------------

@mcp.tool()
def vscode_open(path: str, line: Optional[int] = None) -> Dict[str, Any]:
    """Open a file or folder in VS Code, optionally at a line number.

    Reuses the running window rather than starting a second copy.
    """
    if not os.path.exists(VSCODE_CLI):
        return {"ok": False, "error": f"VS Code CLI not found at {VSCODE_CLI}"}
    path = (path or "").strip()
    if not path:
        raise RuntimeError("path is required")
    if not os.path.exists(path):
        return {"ok": False, "error": f"no such file or folder on the Windows machine: {path}"}
    argv = [VSCODE_CLI, "--reuse-window"]
    if line and os.path.isfile(path):
        argv += ["--goto", f"{path}:{int(line)}"]
    else:
        argv.append(path)
    r = _run(argv)
    r["opened"] = path
    return r


@mcp.tool()
def vscode_extensions() -> Dict[str, Any]:
    """List the extensions installed in VS Code."""
    if not os.path.exists(VSCODE_CLI):
        return {"ok": False, "error": f"VS Code CLI not found at {VSCODE_CLI}"}
    r = _run([VSCODE_CLI, "--list-extensions", "--show-versions"], timeout=60)
    if r.get("ok"):
        r["extensions"] = [ln for ln in (r.get("stdout") or "").splitlines() if ln.strip()]
        r["count"] = len(r["extensions"])
        r.pop("stdout", None)
    return r


@mcp.tool()
def vscode_status() -> Dict[str, Any]:
    """Whether VS Code is installed and running, and which build."""
    procs = _processes()
    spec = APPS["vscode"]
    out: Dict[str, Any] = {
        "installed": os.path.exists(VSCODE_EXE),
        "edition": "Insiders",
        "running": spec["process"] in procs,
        "pid": procs.get(spec["process"]),
        "cli": VSCODE_CLI if os.path.exists(VSCODE_CLI) else None,
    }
    if out["installed"]:
        v = _run([VSCODE_CLI, "--version"], timeout=30)
        if v.get("ok"):
            out["version"] = (v.get("stdout") or "").splitlines()[:1]
    return out


# --------------------------------------------------------------------------
# Minecraft (read-only)
# --------------------------------------------------------------------------

@mcp.tool()
def minecraft_status() -> Dict[str, Any]:
    """Whether Minecraft is running, and what the install looks like."""
    procs = _processes()
    if not os.path.isdir(MINECRAFT_DIR):
        return {"installed": False, "reason": f"no .minecraft at {MINECRAFT_DIR}"}
    mods = glob.glob(os.path.join(MINECRAFT_DIR, "mods", "*.jar"))
    return {
        "installed": True,
        "running": "javaw" in procs,
        "pid": procs.get("javaw"),
        "dir": MINECRAFT_DIR,
        "mod_count": len(mods),
        "world_count": len(next(os.walk(os.path.join(MINECRAFT_DIR, "saves")))[1])
        if os.path.isdir(os.path.join(MINECRAFT_DIR, "saves")) else 0,
        "note": "Read-only. Changing anything in-game would need a mod or RCON, neither of which "
                "is set up here.",
    }


@mcp.tool()
def minecraft_mods(limit: int = 200) -> Dict[str, Any]:
    """The mod jars installed, by filename."""
    d = os.path.join(MINECRAFT_DIR, "mods")
    if not os.path.isdir(d):
        return {"ok": False, "error": f"no mods folder at {d}"}
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*.jar")))
    return {"ok": True, "count": len(names), "mods": names[:max(1, limit)]}


@mcp.tool()
def minecraft_worlds() -> Dict[str, Any]:
    """Saved worlds, most recently played first."""
    d = os.path.join(MINECRAFT_DIR, "saves")
    if not os.path.isdir(d):
        return {"ok": False, "error": f"no saves folder at {d}"}
    worlds = []
    for name in next(os.walk(d))[1]:
        p = os.path.join(d, name)
        worlds.append({"name": name, "last_played": time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))})
    worlds.sort(key=lambda w: w["last_played"], reverse=True)
    return {"ok": True, "count": len(worlds), "worlds": worlds}


@mcp.tool()
def minecraft_last_crash(lines: int = 60) -> Dict[str, Any]:
    """The tail of the most recent crash report, for diagnosing a crash."""
    d = os.path.join(MINECRAFT_DIR, "crash-reports")
    files = sorted(glob.glob(os.path.join(d, "*.txt")), key=os.path.getmtime, reverse=True)
    if not files:
        return {"ok": True, "crash_reports": 0, "note": "No crash reports on disk."}
    newest = files[0]
    with open(newest, "r", encoding="utf-8", errors="replace") as fh:
        body = fh.read().splitlines()
    return {
        "ok": True,
        "file": os.path.basename(newest),
        "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(newest))),
        "crash_reports": len(files),
        "tail": "\n".join(body[-max(10, lines):]),
    }


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------

_last_media_source = ""

_MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
}


@mcp.tool()
def media_control(action: str, repeat: int = 1) -> Dict[str, Any]:
    """Send a media key: play_pause, next, previous, stop, volume_up, volume_down, mute.

    Acts on whatever currently has media focus, so it drives YouTube Music in
    the browser, Spotify, or a video, without needing an integration per app.

    For volume, prefer set_volume(percent): it is exact and takes one call.
    volume_up and volume_down move one notch each, so reaching a target with
    them means guessing repeatedly.
    """
    action = (action or "").strip().lower()
    vk = _MEDIA_KEYS.get(action)
    if vk is None:
        raise RuntimeError(f"action must be one of: {', '.join(sorted(_MEDIA_KEYS))}")
    repeat = max(1, min(int(repeat or 1), 10))

    import ctypes
    KEYEVENTF_KEYUP = 0x0002
    for _ in range(repeat):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
    return {
        "ok": True,
        "action": action,
        "repeat": repeat,
        "note": "Sent to whichever app currently holds media focus. If nothing was playing, "
                "nothing happens and this still reports ok.",
    }


@mcp.tool()
def now_playing() -> Dict[str, Any]:
    """What Windows reports as the current media session, if any."""
    # GlobalSystemMediaTransportControls is the only reliable source, and it is
    # async WinRT. PowerShell cannot await those directly: the returned object
    # is a COM IAsyncOperation with no GetAwaiter, so the usual .NET pattern
    # fails with "does not contain a method named 'GetAwaiter'". The supported
    # bridge is WindowsRuntimeSystemExtensions::AsTask, reflected out and made
    # generic per result type, which is what Await below does.
    ps = r"""
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, $type) {
      $netTask = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
      $netTask.Wait(-1) | Out-Null
      $netTask.Result
  }
  $mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media,ContentType=WindowsRuntime]
  $mgr = Await ($mgrType::RequestAsync()) ($mgrType)
  $s = $mgr.GetCurrentSession()
  if (-not $s) { Write-Output '{"playing":false,"reason":"no active media session"}'; exit }
  $propType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties,Windows.Media,ContentType=WindowsRuntime]
  $p = Await ($s.TryGetMediaPropertiesAsync()) ($propType)
  $info = $s.GetPlaybackInfo()
  @{ playing = ($info.PlaybackStatus -eq 'Playing')
     status  = "$($info.PlaybackStatus)"
     title   = $p.Title
     artist  = $p.Artist
     album   = $p.AlbumTitle
     source  = $s.SourceAppUserModelId } | ConvertTo-Json -Compress
} catch { @{ error = "$_" } | ConvertTo-Json -Compress }
"""
    r = _run(["powershell", "-NoProfile", "-Command", ps], timeout=45)
    raw = (r.get("stdout") or "").strip()
    try:
        out = json.loads(raw)
    except Exception:
        return {"ok": False, "error": "could not read the media session", "raw": raw[:500],
                "stderr": (r.get("stderr") or "")[:300]}
    global _last_media_source
    if isinstance(out, dict) and out.get("source"):
        # Remembered so get_app_volume() knows which app is playing without
        # a second (slow) PowerShell round trip.
        _last_media_source = str(out["source"])
    return out



# --------------------------------------------------------------------------
# Computer use: see the screen, act on it
#
# Off unless DESKTOP_MCP_ALLOW_INPUT is set. Everything above this point is
# bounded — a fixed table of apps, read-only queries, media keys. Arbitrary
# clicking and typing is not bounded, and the only thing guarding this port
# is the tailnet, so it gets a switch that does not require redeploying.
#
# All coordinates are physical screen pixels with the origin at the top-left
# of the primary monitor. screen_info reports the real size; do not assume
# one, and never guess coordinates from a resized screenshot without scaling
# them back.
# --------------------------------------------------------------------------

INPUT_ENABLED = os.environ.get("DESKTOP_MCP_ALLOW_INPUT", "").strip().lower() in (
    "1", "true", "yes", "on")


def _input_guard() -> Optional[Dict[str, Any]]:
    if INPUT_ENABLED:
        return None
    return {
        "ok": False,
        "error": "Screen control is switched off on this machine. Set "
                 "DESKTOP_MCP_ALLOW_INPUT=1 in the OdysseusDesktopMCP task and restart "
                 "it to enable clicking and typing. Reading the screen with screenshot() "
                 "is also covered by this switch.",
    }


def _pyautogui():
    """Import lazily and disable the fail-safe.

    pyautogui aborts if the pointer reaches a screen corner, which is a
    sensible default for a human at the keyboard and a source of random
    unexplained failures for a program driving the mouse.
    """
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05
    return pyautogui


@mcp.tool()
def screen_info() -> Dict[str, Any]:
    """Screen size and whether screen control is available.

    Worth calling before the first click: coordinates mean nothing without
    the real resolution, and this says plainly when control is switched off
    rather than letting every action fail the same way.
    """
    out: Dict[str, Any] = {"input_enabled": INPUT_ENABLED}
    try:
        import ctypes
        u = ctypes.windll.user32
        u.SetProcessDPIAware()
        out["width"] = u.GetSystemMetrics(0)
        out["height"] = u.GetSystemMetrics(1)
        out["monitors"] = u.GetSystemMetrics(80)
        out["virtual"] = {
            "width": u.GetSystemMetrics(78), "height": u.GetSystemMetrics(79),
            "left": u.GetSystemMetrics(76), "top": u.GetSystemMetrics(77),
        }
    except Exception as e:
        out["error"] = f"could not read screen metrics: {e}"
    if not INPUT_ENABLED:
        out["note"] = ("Screen control is off. Set DESKTOP_MCP_ALLOW_INPUT=1 on the "
                       "OdysseusDesktopMCP task to turn it on.")
    return out


@mcp.tool()
def screenshot(max_width: int = 1280, region: Optional[List[int]] = None):
    """Capture the screen for the model to look at.

    `region` is [left, top, width, height] in screen pixels; omit it for the
    whole screen. The image is scaled down to `max_width` because a raw 4K
    PNG is mostly cost, but the response states the scale factor: a click
    coordinate read off the image must be divided by it to land in the right
    place.
    """
    blocked = _input_guard()
    if blocked:
        raise RuntimeError(blocked["error"])
    from mcp.server.fastmcp import Image
    from PIL import ImageGrab
    import io

    box = None
    if region:
        if len(region) != 4:
            raise RuntimeError("region must be [left, top, width, height]")
        left, top, width, height = region
        box = (left, top, left + width, top + height)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    full_w = img.width
    max_width = max(320, min(int(max_width or 1280), 3840))
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    # The scale factor rides in the log line rather than the image, which can
    # only carry pixels; callers should re-read screen_info if unsure.
    print(f"screenshot {img.width}x{img.height} (scale {img.width / full_w:.3f})", flush=True)
    return Image(data=buf.getvalue(), format="png")


@mcp.tool()
def click(x: int, y: int, button: str = "left", clicks: int = 1,
          label: str = "", confidence: Optional[float] = None) -> Dict[str, Any]:
    """Click at a screen coordinate. Origin is the top-left of the primary monitor.

    Pass `label` (and optionally `confidence`, 0 to 1) to record what you
    believed you were clicking. It changes nothing about the click; it puts
    your claim in the transcript so a wrong one is visible afterwards.
    """
    blocked = _input_guard()
    if blocked:
        return blocked
    button = (button or "left").strip().lower()
    if button not in ("left", "right", "middle"):
        raise RuntimeError("button must be left, right or middle")
    clicks = max(1, min(int(clicks or 1), 3))
    pg = _pyautogui()
    w, h = pg.size()
    if not (0 <= x < w and 0 <= y < h):
        # Off-screen clicks land nowhere and look like the app ignoring us.
        return {"ok": False, "error": f"({x},{y}) is outside the {w}x{h} screen."}
    pg.click(x=x, y=y, button=button, clicks=clicks)
    out = {"ok": True, "clicked": [x, y], "button": button, "clicks": clicks}
    if label:
        # Echoing the claim back makes the transcript say what was aimed at,
        # not just where the pointer went.
        out["aimed_at"] = label
        if confidence is not None:
            try:
                out["confidence"] = round(float(confidence), 2)
            except (TypeError, ValueError):
                pass
        out["note"] = (f"Clicked at ({x},{y}), believed to be {label!r}. "
                       f"Take a screenshot to confirm it did what you expected "
                       f"before saying it worked.")
    return out


@mcp.tool()
def move_mouse(x: int, y: int) -> Dict[str, Any]:
    """Move the pointer without clicking, to reveal hover states."""
    blocked = _input_guard()
    if blocked:
        return blocked
    pg = _pyautogui()
    pg.moveTo(x, y)
    return {"ok": True, "at": [x, y]}


@mcp.tool()
def drag(from_x: int, from_y: int, to_x: int, to_y: int, duration: float = 0.4) -> Dict[str, Any]:
    """Press at one point, move, and release at another."""
    blocked = _input_guard()
    if blocked:
        return blocked
    pg = _pyautogui()
    pg.moveTo(from_x, from_y)
    # A drag with no duration is often dropped: many UIs need to see motion
    # between press and release to treat it as a drag rather than a click.
    pg.dragTo(to_x, to_y, duration=max(0.1, min(float(duration or 0.4), 3.0)),
              button="left")
    return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y]}


@mcp.tool()
def type_text(text: str, interval: float = 0.01) -> Dict[str, Any]:
    """Type text into whatever currently has focus.

    Focus is not checked, because nothing here can know what is focused.
    Take a screenshot first if it matters where the text lands.
    """
    blocked = _input_guard()
    if blocked:
        return blocked
    if not text:
        raise RuntimeError("text is required")
    if len(text) > 5000:
        return {"ok": False, "error": "text is longer than 5000 characters."}
    pg = _pyautogui()
    pg.write(text, interval=max(0.0, min(float(interval or 0.01), 0.5)))
    return {"ok": True, "typed_chars": len(text)}


@mcp.tool()
def press_keys(keys: str) -> Dict[str, Any]:
    """Press a key or a chord, e.g. "enter", "ctrl+s", "alt+tab", "win".

    Note that Ctrl+Alt+Del and the UAC prompt live on Windows' secure
    desktop, which synthetic input cannot reach by design. Those will appear
    to do nothing rather than fail.
    """
    blocked = _input_guard()
    if blocked:
        return blocked
    combo = [k.strip().lower() for k in (keys or "").split("+") if k.strip()]
    if not combo:
        raise RuntimeError('keys is required, e.g. "enter" or "ctrl+s"')
    pg = _pyautogui()
    valid = set(pg.KEYBOARD_KEYS)
    unknown = [k for k in combo if k not in valid]
    if unknown:
        return {"ok": False,
                "error": f"unknown key(s): {', '.join(unknown)}. "
                         f"Examples: enter, tab, esc, ctrl, alt, shift, win, f1-f12."}
    if len(combo) == 1:
        pg.press(combo[0])
    else:
        pg.hotkey(*combo)
    return {"ok": True, "pressed": "+".join(combo)}


@mcp.tool()
def scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
    """Scroll by `amount` clicks; positive is up, negative is down."""
    blocked = _input_guard()
    if blocked:
        return blocked
    pg = _pyautogui()
    if x is not None and y is not None:
        pg.moveTo(x, y)
    pg.scroll(int(amount))
    return {"ok": True, "scrolled": int(amount), "at": [x, y] if x is not None else "pointer"}


def _draw_regions(img, regions: List[Dict[str, Any]], scale: float = 1.0):
    """Draw labelled boxes on a screenshot. Coordinates are screen pixels."""
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", max(12, int(img.width / 70)))
    except Exception:
        font = ImageFont.load_default()

    # Terracotta, to match the rest of the interface, and distinct from most
    # application chrome.
    outline = (217, 122, 74, 255)
    for r in regions:
        try:
            x = int(r.get("x", 0) * scale)
            y = int(r.get("y", 0) * scale)
            w = int(r.get("width", 0) * scale)
            h = int(r.get("height", 0) * scale)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        width = max(2, int(img.width / 400))
        draw.rectangle([x, y, x + w, y + h], outline=outline, width=width)

        label = str(r.get("label") or "").strip()
        conf = r.get("confidence")
        if conf is not None:
            try:
                label = f"{label} {float(conf) * 100:.0f}%".strip()
            except (TypeError, ValueError):
                pass
        if not label:
            continue
        box = draw.textbbox((0, 0), label, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        # Above the region, unless that would fall off the top edge.
        ty = y - th - 6 if y - th - 6 > 0 else y + h + 4
        draw.rectangle([x, ty - 2, x + tw + 8, ty + th + 4], fill=(217, 122, 74, 220))
        draw.text((x + 4, ty), label, fill=(255, 255, 255, 255), font=font)
    return img


@mcp.tool()
def annotate_screen(regions: List[Dict[str, Any]], max_width: int = 1280):
    """Take a screenshot with boxes drawn where you believe things are.

    Pass one entry per thing you have located:
    `{"x": 100, "y": 200, "width": 300, "height": 80, "label": "Play button",
      "confidence": 0.8}` — coordinates in real screen pixels, confidence
    between 0 and 1.

    Use this before clicking something you are not sure about. The boxes are
    your claim about where things are, not a detection the machine made, so
    drawing one is how the user can catch a confident mistake before it
    turns into a click in the wrong place.
    """
    blocked = _input_guard()
    if blocked:
        raise RuntimeError(blocked["error"])
    if not isinstance(regions, list) or not regions:
        raise RuntimeError('regions is required, e.g. '
                           '[{"x":100,"y":200,"width":300,"height":80,'
                           '"label":"Play","confidence":0.8}]')
    from mcp.server.fastmcp import Image
    from PIL import ImageGrab
    import io

    img = ImageGrab.grab(all_screens=True)
    full_w = img.width
    max_width = max(320, min(int(max_width or 1280), 3840))
    scale = 1.0
    if img.width > max_width:
        scale = max_width / img.width
        img = img.resize((max_width, int(img.height * scale)))
    # Regions arrive in screen pixels, so they scale with the image.
    img = _draw_regions(img.convert("RGB"), regions, scale=scale)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    print(f"annotate_screen {len(regions)} region(s), scale {scale:.3f} "
          f"(screen {full_w}px)", flush=True)
    return Image(data=buf.getvalue(), format="png")


def _volume_endpoint():
    """The system volume control, via Core Audio.

    Raises with something actionable rather than a COM traceback, since the
    usual cause is simply that pycaw is not installed.
    """
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError as e:
        raise RuntimeError(
            f"Volume control needs pycaw ({e}). Install it on this machine with "
            f"`python -m pip install pycaw comtypes`."
        )
    speakers = AudioUtilities.GetSpeakers()

    # pycaw 2025 returns an AudioDevice wrapper rather than the raw IMMDevice
    # the widely-copied snippet assumes, so Activate() is simply absent and
    # every call fails with an attribute error. The wrapper exposes what we
    # want directly; the old path stays as a fallback because this file runs
    # against whatever version a given machine has.
    endpoint = getattr(speakers, "EndpointVolume", None)
    if endpoint is not None:
        return endpoint
    raw = getattr(speakers, "_dev", speakers)
    interface = raw.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


@mcp.tool()
def get_volume() -> Dict[str, Any]:
    """The system volume, 0-100, and whether it is muted.

    Read this before changing it if the user asked for a relative change
    ("turn it down a bit"); guessing from nothing is what turns one action
    into a dozen.
    """
    blocked = _input_guard()
    if blocked:
        return blocked
    try:
        vol = _volume_endpoint()
        return {
            "ok": True,
            "volume": round(vol.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(vol.GetMute()),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def set_volume(percent: int) -> Dict[str, Any]:
    """Set the system volume to an exact percentage, 0-100.

    One call, and it lands on the number asked for. Prefer this over
    repeating volume_up/volume_down, and never open the Sound settings
    panel to do it.
    """
    blocked = _input_guard()
    if blocked:
        return blocked
    try:
        pct = int(percent)
    except (TypeError, ValueError):
        raise RuntimeError("percent must be a whole number from 0 to 100")
    if not 0 <= pct <= 100:
        return {"ok": False, "error": f"percent must be 0-100, got {pct}"}
    try:
        vol = _volume_endpoint()
        before = round(vol.GetMasterVolumeLevelScalar() * 100)
        vol.SetMasterVolumeLevelScalar(pct / 100.0, None)
        # Setting a level on a muted device changes nothing audible, which
        # reads as the call having failed.
        unmuted = False
        if pct > 0 and vol.GetMute():
            vol.SetMute(0, None)
            unmuted = True
        out = {"ok": True, "volume": round(vol.GetMasterVolumeLevelScalar() * 100),
               "was": before}
        if unmuted:
            out["note"] = "Also unmuted, since setting a level on a muted device is silent."
        return out
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def set_mute(muted: bool = True) -> Dict[str, Any]:
    """Mute or unmute the system volume."""
    blocked = _input_guard()
    if blocked:
        return blocked
    try:
        vol = _volume_endpoint()
        vol.SetMute(1 if muted else 0, None)
        return {"ok": True, "muted": bool(vol.GetMute()),
                "volume": round(vol.GetMasterVolumeLevelScalar() * 100)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Per-app volume: the level in the Windows volume mixer, not the app's own
# slider. For YouTube Music in Chrome that is Chrome's mixer entry.
_FIREFOX_AUMID = "308046b0af4a39cb"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _app_sessions(app: str = ""):
    """(label, [ISimpleAudioVolume]) for the app playing, or for `app`.

    `app` may be a process name ("chrome.exe") or a media session id
    ("Chrome", "com.github.th-ch.youtube-music"). Empty means whatever
    now_playing last reported, then the one app that is making sound.
    """
    try:
        from pycaw.pycaw import AudioUtilities
    except ImportError as e:
        raise RuntimeError(
            f"App volume needs pycaw ({e}). Install it with `python -m pip install pycaw comtypes`.")
    sessions = []
    for s in AudioUtilities.GetAllSessions():
        proc = getattr(s, "Process", None)
        vol = getattr(s, "SimpleAudioVolume", None)
        if proc is None or vol is None:
            continue                               # "System Sounds"
        try:
            name = proc.name()
        except Exception:
            continue
        sessions.append((name, vol, getattr(s, "State", 0)))

    want = _norm(app or _last_media_source)
    if want:
        if _FIREFOX_AUMID in want:
            want = "firefox"
        hits = [x for x in sessions
                if (stem := _norm(x[0].rsplit(".", 1)[0])) and (stem in want or want in stem)]
        if hits:
            return hits[0][0], [v for _, v, _ in hits]
        if app:
            raise RuntimeError(f"no app named {app!r} is playing audio")
    active = {}
    for name, vol, state in sessions:
        if state == 1:                             # AudioSessionStateActive
            active.setdefault(name, []).append(vol)
    if len(active) == 1:
        name, vols = next(iter(active.items()))
        return name, vols
    raise RuntimeError("could not tell which app is playing; pass app, e.g. chrome.exe")


def _app_label(proc_name: str) -> str:
    stem = proc_name.rsplit(".", 1)[0]
    return {"chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
            "spotify": "Spotify"}.get(stem.lower(), stem)


@mcp.tool()
def get_app_volume(app: str = "") -> Dict[str, Any]:
    """The volume of one app in the Windows mixer, 0-100 (separate from the
    system volume). Defaults to the app that is playing media."""
    blocked = _input_guard()
    if blocked:
        return blocked
    try:
        name, vols = _app_sessions(app)
        v = vols[0]
        return {"ok": True, "app": name, "label": _app_label(name),
                "volume": round(v.GetMasterVolume() * 100), "muted": bool(v.GetMute())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def set_app_volume(percent: int, app: str = "") -> Dict[str, Any]:
    """Set one app's volume in the Windows mixer, 0-100, leaving the system
    volume alone. Defaults to the app that is playing media (YouTube Music in
    Chrome is Chrome's entry). Use set_volume for the whole computer."""
    blocked = _input_guard()
    if blocked:
        return blocked
    try:
        pct = int(percent)
    except (TypeError, ValueError):
        raise RuntimeError("percent must be a whole number from 0 to 100")
    if not 0 <= pct <= 100:
        return {"ok": False, "error": f"percent must be 0-100, got {pct}"}
    try:
        name, vols = _app_sessions(app)
        before = round(vols[0].GetMasterVolume() * 100)
        for v in vols:                             # an app can own several sessions
            v.SetMasterVolume(pct / 100.0, None)
            if pct > 0 and v.GetMute():
                v.SetMute(0, None)
        return {"ok": True, "app": name, "label": _app_label(name),
                "volume": round(vols[0].GetMasterVolume() * 100), "was": before}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def list_audio_devices() -> Dict[str, Any]:
    """Audio output devices, and which one is currently in use.

    Bluetooth headphones appear here alongside speakers, which is how to
    answer "am I on my AirPods".
    """
    blocked = _input_guard()
    if blocked:
        return blocked
    try:
        from pycaw.pycaw import AudioUtilities
    except ImportError as e:
        return {"ok": False,
                "error": f"Needs pycaw ({e}). Install with `python -m pip install pycaw comtypes`."}
    try:
        active_name = ""
        try:
            speakers = AudioUtilities.GetSpeakers()
            active_name = getattr(speakers, "FriendlyName", "") or ""
        except Exception:
            pass

        devices = []
        for d in AudioUtilities.GetAllDevices():
            name = getattr(d, "FriendlyName", "") or str(d)
            state = str(getattr(d, "state", "") or "")
            # GetAllDevices includes unplugged and disabled endpoints, which
            # would otherwise read as "connected headphones" when they are
            # anything but.
            if "Active" not in state:
                continue
            low = name.lower()
            devices.append({
                "name": name,
                "active": bool(active_name) and name == active_name,
                "bluetooth": any(w in low for w in
                                 ("airpod", "bluetooth", "headset", "buds", "beats")),
            })
        return {"ok": True, "count": len(devices), "default": active_name,
                "devices": devices}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# --------------------------------------------------------------------------
# Media, played in the Odysseus chat straight from this machine
#
# Asked for: "I don't want to copy anything, I want to host a video from any
# device: if it's on my device host it on mine, if not then host it from where
# it's from". share_media() hands out an unguessable link for ONE file; the
# bytes are streamed from here on request (with Range, so the player can
# seek) through Odysseus, which is behind the user's login. Nothing is copied.
# --------------------------------------------------------------------------
import secrets as _secrets
from urllib.parse import quote as _quote

_MEDIA_EXT = {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".mp3", ".m4a", ".aac", ".wav",
              ".flac", ".ogg", ".oga", ".opus"}
_MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                ".webm": "video/webm", ".ogv": "video/ogg", ".mp3": "audio/mpeg",
                ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav",
                ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
                ".opus": "audio/ogg"}
_SHARED: Dict[str, Dict[str, Any]] = {}      # token -> {"path", "ts"}
_SHARE_TTL_S = 7 * 24 * 3600


def _media_roots() -> List[str]:
    home = os.path.expanduser("~")
    return [os.path.join(home, d) for d in ("Videos", "Music", "Desktop", "Downloads", "Movies")]


@mcp.tool()
def find_media(match: str = "", folder: str = "", limit: int = 25) -> Dict[str, Any]:
    """Video and audio files on this machine, newest first. `match` filters by
    name (case-insensitive words); `folder` searches one folder (default: the
    user's Videos, Music, Desktop and Downloads)."""
    words = [w for w in (match or "").lower().split() if w]
    roots = [os.path.expanduser(folder)] if folder else _media_roots()
    found = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() not in _MEDIA_EXT:
                    continue
                if words and not all(w in f.lower() for w in words):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                found.append((st.st_mtime, p, st.st_size))
    found.sort(reverse=True)
    return {"ok": True, "count": len(found), "files": [
        {"path": p, "mb": round(size / 1048576, 1), "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(m))}
        for m, p, size in found[:max(1, min(int(limit or 25), 200))]]}


@mcp.tool()
def share_media(path: str) -> Dict[str, Any]:
    """Share ONE video or audio file on this machine so it plays in the chat,
    streamed from here (never copied). Put the returned `link` in the reply on
    its own line and the chat shows a player for it."""
    p = os.path.abspath(os.path.expanduser((path or "").strip().strip('"')))
    if not os.path.isfile(p):
        return {"ok": False, "error": f"no such file: {p}"}
    ext = os.path.splitext(p)[1].lower()
    if ext not in _MEDIA_EXT:
        return {"ok": False, "error": f"not a video or audio file the browser can play ({ext or 'no extension'})"}
    now = time.time()
    for t in [t for t, r in _SHARED.items() if now - r["ts"] > _SHARE_TTL_S]:
        _SHARED.pop(t, None)
    token = next((t for t, r in _SHARED.items() if r["path"] == p), None) or _secrets.token_urlsafe(18)
    _SHARED[token] = {"path": p, "ts": now}
    name = os.path.basename(p)
    return {"ok": True, "name": name, "mb": round(os.path.getsize(p) / 1048576, 1),
            "link": f"/api/device-media/{token}/{_quote(name)}",
            "note": "Streamed from this machine through Odysseus; the file stays where it is."}


def _media_response(request):
    """The shared file, honouring Range (206) so the player can seek."""
    from starlette.responses import Response, StreamingResponse
    rec = _SHARED.get(request.path_params.get("token", ""))
    if not rec or not os.path.isfile(rec["path"]):
        return Response("not shared", status_code=404)
    path = rec["path"]
    size = os.path.getsize(path)
    ctype = _MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
    start, end, status = 0, size - 1, 200
    rng = request.headers.get("range", "")
    m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:                                     # a suffix: the last N bytes
            start = max(0, size - int(m.group(2)))
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
               "Content-Type": ctype, "Cache-Control": "private, max-age=3600"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers)

    def chunks():
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = f.read(min(1 << 20, left))
                if not data:
                    break
                left -= len(data)
                yield data
    return StreamingResponse(chunks(), status_code=status, headers=headers)


@mcp.custom_route("/media/{token}", methods=["GET", "HEAD"])
async def _media_route(request):
    return _media_response(request)




_MEDIA_URL_RE = re.compile(r"^https://(?:www\.|m\.)?(?:music\.youtube\.com|youtube\.com|youtu\.be)/\S+$")


@mcp.tool()
def open_media_url(url: str) -> Dict[str, Any]:
    """Open a YouTube / YouTube Music link on this machine so it plays here
    (music handed over from another device). Only those links: this is not a
    general "open any URL" tool."""
    url = (url or "").strip()
    if not _MEDIA_URL_RE.match(url):
        return {"ok": False, "error": "only https YouTube or YouTube Music links"}
    # The YouTube Music app, when installed (Chrome installs it as an app with
    # a Start menu shortcut): the song opens there rather than in a browser
    # tab. Asked for: "it could open the app if I have it".
    app = _youtube_music_app() if "music.youtube.com" in url else None
    if app:
        try:
            import subprocess
            subprocess.Popen([app[0], *app[1], f"--app-launch-url-for-shortcuts-menu-item={url}"],
                             close_fds=True)
            return {"ok": True, "opened": url, "in": "YouTube Music app"}
        except Exception:
            pass
    os.startfile(url)  # noqa: S606 - validated above
    return {"ok": True, "opened": url, "in": "browser"}


def _youtube_music_app():
    """(program, [arguments]) of the installed YouTube Music app's shortcut,
    or None. Read from the shortcut so the Chrome profile and app id are the
    ones this machine actually has."""
    import glob
    import shlex
    root = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
    links = glob.glob(os.path.join(root, "**", "YouTube Music*.lnk"), recursive=True)
    if not links:
        return None
    try:
        import win32com.client  # pywin32
        sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(links[0])
        target, args = sc.TargetPath, sc.Arguments
    except Exception:
        try:
            import subprocess
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + links[0].replace("'", "''")
                  + "'); $s.TargetPath; $s.Arguments")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                                 text=True, timeout=10).stdout.splitlines()
            target, args = (out + ["", ""])[0].strip(), (out + ["", ""])[1].strip()
        except Exception:
            return None
    if not target or not os.path.exists(target):
        return None
    return target, shlex.split(args, posix=False)


# ── This PC as a Bluetooth speaker for a phone ───────────────────────────
# Asked for: "if I'm routing from phone it should be able to make the
# computer be a listener: if I have earbuds connected to the PC it should play
# through there, but the music engine is my phone". A phone will not let
# another app capture a music app's sound, so it is not streamed over the
# network: Windows receives it over Bluetooth instead (the same interface as
# the "Bluetooth Audio Receiver" app), and plays it on this PC's output. The
# phone must be paired with this PC once.
_RECEIVE: Dict[str, Any] = {"conn": None, "name": "", "id": ""}


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _name_matches(want: str, name: str) -> bool:
    """The phone's own name, not just a word in it: "pixel-8a" is "Pixel 8a"
    or "Jaron's Pixel 8a", never "Jaron's Pixel Buds Pro". Seen live: a loose
    match on "pixel" hit the Pixel Buds and removed their pairing."""
    got = _norm_name(name)
    return bool(want) and len(want) >= 4 and (got == want or got.endswith(want))


_AEP = ["System.Devices.Aep.DeviceAddress", "System.Devices.Aep.IsPaired"]


async def _is_phone(info) -> bool:
    """Whether a Bluetooth device's class says phone (not headphones or a
    speaker), from its address. Unknown counts as not a phone."""
    try:
        from winsdk.windows.devices.bluetooth import BluetoothDevice, BluetoothMajorClass
        addr = (info.properties.lookup("System.Devices.Aep.DeviceAddress") or "").replace(":", "")
        if not addr:
            return False
        dev = await BluetoothDevice.from_bluetooth_address_async(int(addr, 16))
        return dev is not None and dev.class_of_device.major_class == BluetoothMajorClass.PHONE
    except Exception:
        return False


async def _paired_named(want: str):
    """A paired Bluetooth device that is this phone (strict name match)."""
    from winsdk.windows.devices.bluetooth import BluetoothDevice
    from winsdk.windows.devices.enumeration import DeviceInformation, DeviceInformationKind
    for d in await DeviceInformation.find_all_async(BluetoothDevice.get_device_selector_from_pairing_state(True),
                                                    _AEP, DeviceInformationKind.ASSOCIATION_ENDPOINT):
        if _name_matches(want, d.name):
            return d
    return None


async def _audio_sources():
    from winsdk.windows.media.audio import AudioPlaybackConnection
    from winsdk.windows.devices.enumeration import DeviceInformation
    return list(await DeviceInformation.find_all_async(AudioPlaybackConnection.get_device_selector(), []))


@mcp.tool()
async def bluetooth_audio_sources() -> Dict[str, Any]:
    """Paired phones (or other devices) that can play their sound through
    this PC's speakers or headphones over Bluetooth, and which one is doing
    so now."""
    try:
        devs = await _audio_sources()
    except Exception as e:
        return {"ok": False, "error": f"Bluetooth audio is not available here: {e}"}
    return {"ok": True, "sources": [{"name": d.name, "id": d.id} for d in devs],
            "receiving": _RECEIVE["name"] or None}


@mcp.tool()
async def bluetooth_audio_receive(device: str = "", on: bool = True) -> Dict[str, Any]:
    """Play a paired phone's sound through this PC: the phone keeps playing
    its own music app and the sound comes out here (the headphones on this
    PC, say). `device` is part of its Bluetooth name ("Pixel"); on=false
    stops, and the phone goes back to its own speaker."""
    old = _RECEIVE.get("conn")
    if not on:
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        name = _RECEIVE["name"]
        _RECEIVE.update(conn=None, name="", id="")
        return {"ok": True, "receiving": None, "stopped": name or None}
    try:
        devs = await _audio_sources()
    except Exception as e:
        return {"ok": False, "error": f"Bluetooth audio is not available here: {e}"}
    want = _norm_name(device)
    match = [d for d in devs if _name_matches(want, d.name)] or (devs if not want and len(devs) == 1 else [])
    if not match:
        names = ", ".join(d.name for d in devs) or "none"
        stale = await _paired_named(want)
        if stale:
            # Seen live: the Pixel was paired, but only its plain Bluetooth
            # record existed (no audio side), so it could never stream here.
            return {"ok": False, "stale_pairing": stale.name, "sources": [d.name for d in devs],
                    "error": (f"{stale.name} is paired with this PC but not as an audio source (an old or "
                              "incomplete pairing). Re-pair it: Pair removes the old pairing and pairs again.")}
        return {"ok": False, "error": (f"No paired device matching {device!r} can play through this PC "
                                       f"(ones that can: {names}). Pair the phone with this PC first."),
                "sources": [d.name for d in devs]}
    d = match[0]
    if old is not None and _RECEIVE["id"] == d.id:
        return {"ok": True, "receiving": d.name, "already": True}
    from winsdk.windows.media.audio import AudioPlaybackConnection
    conn = AudioPlaybackConnection.try_create_from_id(d.id)
    if conn is None:
        return {"ok": False, "error": f"Windows would not open an audio connection to {d.name}"}
    await conn.start_async()
    res = await conn.open_async()
    if int(res.status) != 0:                   # 0 = Success; 1 timed out; 2 denied; 3 unknown
        try:
            conn.close()
        except Exception:
            pass
        why = {1: "the phone did not answer (is Bluetooth on and the phone nearby?)",
               2: "Windows refused it", 3: "an unknown Bluetooth failure"}.get(int(res.status), str(res.status))
        return {"ok": False, "error": f"Could not play {d.name} through this PC: {why}"}
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    _RECEIVE.update(conn=conn, name=d.name, id=d.id)
    return {"ok": True, "receiving": d.name}

@mcp.tool()
async def bluetooth_pair(device: str, seconds: int = 20) -> Dict[str, Any]:
    """Pair a phone with this PC over Bluetooth, from Odysseus, so it can
    play through this PC (bluetooth_audio_receive). The phone must be
    discoverable (on Android: Settings > Connected devices > Pair new
    device) and the user taps Pair when it asks; this PC accepts on its side.
    `device` is part of the phone's Bluetooth name ("Pixel")."""
    want = _norm_name(device)
    if not want:
        return {"ok": False, "error": "name the phone (part of its Bluetooth name)"}
    from winsdk.windows.devices.bluetooth import BluetoothDevice
    from winsdk.windows.devices.enumeration import DeviceInformation, DevicePairingKinds
    removed = ""
    if not any(_name_matches(want, d.name) for d in await _audio_sources()):
        stale = await _paired_named(want)
        if stale is not None and await _is_phone(stale):
            # Paired, but not as an audio source: start over. Only ever a phone.
            try:
                await stale.pairing.unpair_async()
                removed = stale.name
            except Exception as e:
                return {"ok": False, "error": f"Could not remove the old pairing of {stale.name}: {e}"}
    found: Dict[str, Any] = {}
    seen: List[str] = []

    def added(_w, info):
        seen.append(info.name)
        if info.name and _name_matches(want, info.name) and not found:
            found["info"] = info
    # Association endpoints: where nearby, not yet paired devices appear. The
    # default kind (device interfaces) never lists them, so the scan saw nothing.
    from winsdk.windows.devices.enumeration import DeviceInformationKind
    watcher = DeviceInformation.create_watcher(
        BluetoothDevice.get_device_selector_from_pairing_state(False), _AEP,
        DeviceInformationKind.ASSOCIATION_ENDPOINT)
    watcher.add_added(added)
    watcher.start()
    deadline = time.time() + max(5, min(60, int(seconds or 20)))
    while time.time() < deadline and not found:
        await asyncio.sleep(0.5)
    try:
        watcher.stop()
    except Exception:
        pass
    if not found:
        return {"ok": False, "error": ((f"Removed the old pairing of {removed}. " if removed else "")
                                       + f"No phone matching {device!r} is discoverable near this PC. On the phone "
                                       "open Settings > Connected devices > Pair new device (forget "
                                       "DESKTOP-JARON there first if it is listed), keep that screen open, and "
                                       "press Pair again."), "seen": sorted(set(n for n in seen if n))[:12]}
    info = found["info"]
    custom = info.pairing.custom

    def requested(_c, args):
        args.accept()          # this PC's side; the phone asks the user to confirm the code
    token = custom.add_pairing_requested(requested)
    try:
        res = await custom.pair_async(DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.CONFIRM_PIN_MATCH
                                      | DevicePairingKinds.DISPLAY_PIN)
    finally:
        try:
            custom.remove_pairing_requested(token)
        except Exception:
            pass
    status = int(res.status)
    names = {0: "paired", 1: "not ready to pair", 2: "not paired (not supported)", 3: "already paired",
             4: "rejected by the phone", 5: "too many connections", 6: "hardware failure",
             7: "authentication timed out (Pair was not tapped on the phone)", 8: "authentication not allowed",
             9: "authentication failed", 10: "no supported pairing", 11: "protection level not met",
             12: "access denied", 13: "invalid ceremony data", 14: "pairing canceled", 15: "operation already in progress",
             16: "required handler not registered", 17: "rejected by handler", 18: "remote device has an association",
             19: "failed"}
    ok = status in (0, 3)
    return {"ok": ok, "device": info.name, "status": names.get(status, str(status)),
            **({"removed_old_pairing": removed} if removed else {}),
            **({} if ok else {"error": f"Could not pair {info.name}: {names.get(status, status)}"})}


@mcp.tool()
def open_bluetooth_settings() -> Dict[str, Any]:
    """Open Windows' Bluetooth settings on this PC (to pair a phone by hand)."""
    os.startfile("ms-settings:bluetooth")  # noqa: S606 - fixed settings URI
    return {"ok": True}


# ── This PC's sound, streamed live to another machine on the tailnet ──────
# Asked for: "what if I'm wanting to stream from my PC over to my laptop but
# my laptop's not nearby? but it's on the tailnet". What the PC plays is
# captured from its default output (WASAPI loopback, needs pyaudiowpatch) and
# served here as a live WAV at /live/<key>.wav, on the tailnet address only,
# to whoever holds the one-time key. The laptop plays it (play_stream there).
import queue as _queue
import struct as _struct
import threading as _threading

_LIVE: Dict[str, Any] = {"key": "", "stop": None, "thread": None, "fmt": None,
                         "clients": set(), "error": ""}
LIVE_CHUNK = 1024                    # frames per read (about 21 ms at 48 kHz)


def _wav_header(rate: int, channels: int) -> bytes:
    """A WAV header for a stream of unknown length (sizes left at maximum)."""
    block = channels * 2
    return (b"RIFF" + _struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt "
            + _struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, 16)
            + b"data" + _struct.pack("<I", 0xFFFFFFFF))


def _capture(stop):
    try:
        import pyaudiowpatch as pa
    except ImportError:
        _LIVE["error"] = "pyaudiowpatch is not installed here: python -m pip install pyaudiowpatch"
        return
    p = pa.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        dev = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not dev.get("isLoopbackDevice"):
            for lb in p.get_loopback_device_info_generator():
                if dev["name"] in lb["name"]:
                    dev = lb
                    break
        rate, ch = int(dev["defaultSampleRate"]), int(dev["maxInputChannels"] or 2)
        stream = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True,
                        input_device_index=dev["index"], frames_per_buffer=LIVE_CHUNK)
        _LIVE["fmt"] = (rate, ch)
        while not stop.is_set():
            data = stream.read(LIVE_CHUNK, exception_on_overflow=False)
            for q in list(_LIVE["clients"]):
                try:
                    q.put_nowait(data)
                except _queue.Full:
                    pass                     # a slow listener drops, never stalls the rest
        stream.stop_stream()
        stream.close()
    except Exception as e:
        _LIVE["error"] = f"could not capture this PC's sound: {e}"
    finally:
        p.terminate()


@mcp.tool()
def audio_stream_start() -> Dict[str, Any]:
    """Start streaming what this PC plays, live, for another machine on the
    tailnet to play (the laptop's play_stream). Returns the stream's URL."""
    if _LIVE["thread"] is None or not _LIVE["thread"].is_alive():
        _LIVE.update(key=_secrets.token_urlsafe(18), fmt=None, error="")
        _LIVE["stop"] = _threading.Event()
        _LIVE["thread"] = _threading.Thread(target=_capture, args=(_LIVE["stop"],), daemon=True)
        _LIVE["thread"].start()
        for _ in range(40):                  # the device opens in well under 2 s
            if _LIVE["fmt"] or _LIVE["error"]:
                break
            time.sleep(0.05)
    if _LIVE["error"]:
        return {"ok": False, "error": _LIVE["error"]}
    host = os.environ.get("DESKTOP_MCP_HOST", "100.102.86.125")
    port = int(os.environ.get("DESKTOP_MCP_PORT", "8931"))
    rate, ch = _LIVE["fmt"] or (48000, 2)
    return {"ok": True, "url": f"http://{host}:{port}/live/{_LIVE['key']}.wav",
            "rate": rate, "channels": ch}


@mcp.tool()
def audio_stream_stop() -> Dict[str, Any]:
    """Stop streaming this PC's sound."""
    if _LIVE["stop"] is not None:
        _LIVE["stop"].set()
    _LIVE.update(key="", thread=None, fmt=None)
    return {"ok": True}


@mcp.custom_route("/live/{name}", methods=["GET"])
async def _live_route(request):
    from starlette.responses import PlainTextResponse, StreamingResponse
    key = request.path_params["name"].removesuffix(".wav")
    if not _LIVE["key"] or not _secrets.compare_digest(key, _LIVE["key"]):
        return PlainTextResponse("not found", status_code=404)
    q: "_queue.Queue[bytes]" = _queue.Queue(maxsize=240)
    _LIVE["clients"].add(q)

    async def body():
        try:
            for _ in range(60):
                if _LIVE["fmt"]:
                    break
                await asyncio.sleep(0.05)
            rate, ch = _LIVE["fmt"] or (48000, 2)
            yield _wav_header(rate, ch)
            # Windows sends nothing while nothing plays: fill with silence so
            # the listener's player keeps the stream open and in time.
            silence = b"\0" * (LIVE_CHUNK * ch * 2)
            while _LIVE["key"] == key:
                try:
                    yield await asyncio.to_thread(q.get, True, LIVE_CHUNK / rate)
                except _queue.Empty:
                    yield silence
        finally:
            _LIVE["clients"].discard(q)
    return StreamingResponse(body(), media_type="audio/wav", headers={"Cache-Control": "no-store"})


@mcp.tool()
def tools_version() -> Dict[str, Any]:
    """Fingerprints (sha256) of this machine's Odysseus files, so Odysseus
    can tell when they are out of date and offer an update."""
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    out = {}
    for name in (os.path.basename(__file__), "music_overlay.py", "mcp_transport_security.py"):
        path = os.path.join(here, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                out[name] = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
    return {"ok": True, "files": out}


@mcp.tool()
def start_music_overlay() -> Dict[str, Any]:
    """Open the frameless music overlay on this desktop (Pop out in the
    Odysseus music bar), through its MusicOverlay task. Started from here, in
    the logged-in session, so Odysseus needs no SSH access for it."""
    import subprocess
    r = subprocess.run(["schtasks", "/run", "/tn", "MusicOverlay"], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()
                or "the MusicOverlay task is not set up here (update this computer from Settings > Devices)"}
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn  # noqa: F401  (imported for parity with the Resolve server)
    # Default to the tailnet address, never all interfaces. These tools launch
    # and focus applications, so the listener must not be reachable from the
    # LAN or anywhere else the machine happens to be attached to.
    host = os.environ.get("DESKTOP_MCP_HOST", "100.102.86.125")
    port = int(os.environ.get("DESKTOP_MCP_PORT", "8931"))
    mcp.settings.host = host
    mcp.settings.port = port
    print(f"desktop-mcp listening on {host}:{port} (sse)", flush=True)
    mcp.run(transport="sse")
