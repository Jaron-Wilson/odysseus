"""MCP server for controlling desktop apps on the Windows machine, served over SSE.

Companion to resolve_mcp_server.py. Same reasoning for living on the Windows
box rather than beside Odysseus: launching and focusing windows, and sending
media keys, only mean anything in the interactive desktop session.

Three groups of tools, and one rule that shapes all of them.

The rule: nothing here execs an arbitrary command. `launch_app` takes a key
from a fixed table, never a path or command line, so a prompt-injected model
cannot turn "open my editor" into "run this binary". Adding an app means
editing APPS below, deliberately.

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
import os
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
# Apps
# --------------------------------------------------------------------------

@mcp.tool()
def list_apps() -> List[Dict[str, Any]]:
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
    """Start one of the apps from list_apps. Only those keys are accepted.

    `app` is a key such as "vscode", never a path or command line.
    """
    spec = APPS.get((app or "").strip().lower())
    if not spec:
        raise RuntimeError(
            f"Unknown app {app!r}. Allowed: {', '.join(sorted(APPS))}. "
            "This tool only launches apps from that fixed list."
        )

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
        return json.loads(raw)
    except Exception:
        return {"ok": False, "error": "could not read the media session", "raw": raw[:500],
                "stderr": (r.get("stderr") or "")[:300]}



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
