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
    "minecraft": {
        # Installed from the Store, so there is no .exe to run: it is launched
        # by app id through the shell. The path below is the game data folder,
        # which is what the read-only minecraft_* tools inspect.
        "path": os.path.join(APPDATA, ".minecraft"),
        "aumid": r"Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft",
        "process": "javaw",
        "description": "Minecraft Java Edition (opens the official launcher)",
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
        out.append({
            "app": key,
            "description": spec["description"],
            "installed": installed,
            "running": bool(pname and pname in procs),
            "pid": procs.get(pname) if pname else None,
        })
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
        r = _run(["explorer.exe", f"shell:AppsFolder\\{spec['aumid']}"], timeout=20)
        # explorer.exe returns a non-zero exit even on success, so its code
        # says nothing useful; report the launch as attempted instead of
        # inventing a result from it.
        return {"ok": True, "app": app, "launched_via": "shell:AppsFolder",
                "note": "The launcher was asked to start. Store apps report no exit status, "
                        "so call list_apps in a few seconds to confirm it came up."}

    target = spec.get("path")
    if not os.path.exists(target or ""):
        return {"ok": False, "app": app, "error": f"not installed at {target}"}

    subprocess.Popen([target], close_fds=True)
    time.sleep(1.5)
    procs = _processes()
    return {"ok": True, "app": app, "running": bool(spec.get("process") in procs)}


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
