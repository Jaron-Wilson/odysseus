"""MCP server exposing DaVinci Resolve Studio, served over SSE.

Runs on the machine with Resolve installed and is reached over the tailnet,
because Resolve's scripting API is an in-process COM-style bridge: it only
works on the same box as the running application.

Studio is required — the free edition does not expose external scripting at
all — and Resolve must actually be open. The API returns None rather than
raising when it cannot attach, which reads as a mysterious null far from the
cause, so connect() says plainly which of those two it is.

Read and write are kept apart deliberately. Anything that changes a project is
prefixed and described as such, so an agent cannot wander into mutating a
timeline while answering a question about it.
"""

import os
import sys
from typing import Any, Dict, List, Optional

# The API ships with Resolve rather than on PyPI, so its location has to be put
# on the path before the import can work.
_PD = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
_BASE = os.path.join(_PD, "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting")
os.environ.setdefault("RESOLVE_SCRIPT_API", _BASE)
os.environ.setdefault(
    "RESOLVE_SCRIPT_LIB",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll")
sys.path.append(os.path.join(_BASE, "Modules"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("resolve")

_resolve = None


def connect():
    """Attach to a running Resolve, or explain why we cannot."""
    global _resolve
    if _resolve is not None:
        return _resolve
    try:
        import DaVinciResolveScript as dvr
    except ImportError as e:
        raise RuntimeError(
            f"Resolve's scripting module is not importable ({e}). Expected it under "
            f"{_BASE}\\Modules — check that Resolve Studio is installed."
        )
    r = dvr.scriptapp("Resolve")
    if r is None:
        raise RuntimeError(
            "Could not attach to Resolve. It returns nothing rather than an error when "
            "the application is not running, so: open DaVinci Resolve and try again. "
            "External scripting also requires the Studio edition."
        )
    _resolve = r
    return r


def _project():
    pm = connect().GetProjectManager()
    p = pm.GetCurrentProject()
    if p is None:
        raise RuntimeError("No project is open in Resolve.")
    return p


@mcp.tool()
def resolve_status() -> Dict[str, Any]:
    """Whether Resolve is reachable, and what is currently open."""
    try:
        r = connect()
    except RuntimeError as e:
        return {"connected": False, "reason": str(e)}
    pm = r.GetProjectManager()
    proj = pm.GetCurrentProject()
    out: Dict[str, Any] = {
        "connected": True,
        "product": r.GetProductName(),
        "version": r.GetVersionString(),
        "page": r.GetCurrentPage(),
        "project": proj.GetName() if proj else None,
    }
    if proj:
        tl = proj.GetCurrentTimeline()
        out["timeline"] = tl.GetName() if tl else None
        out["timeline_count"] = proj.GetTimelineCount()
    if out["page"] is None:
        # A null page alongside a named project is confusing on its face, so
        # say what it means rather than leaving it to be guessed at.
        out["note"] = ("No page is open — Resolve is showing the Project Manager. "
                       "Project queries still work; set_page will not until a "
                       "project is opened in the window.")
    return out


@mcp.tool()
def list_projects() -> List[str]:
    """Names of the projects in the current database folder."""
    return connect().GetProjectManager().GetProjectListInCurrentFolder() or []


@mcp.tool()
def list_timelines() -> List[str]:
    """Timelines in the open project."""
    p = _project()
    return [p.GetTimelineByIndex(i).GetName()
            for i in range(1, (p.GetTimelineCount() or 0) + 1)]


@mcp.tool()
def timeline_info(name: Optional[str] = None) -> Dict[str, Any]:
    """Details of a timeline — the current one unless a name is given."""
    p = _project()
    tl = None
    if name:
        for i in range(1, (p.GetTimelineCount() or 0) + 1):
            cand = p.GetTimelineByIndex(i)
            if cand.GetName() == name:
                tl = cand
                break
        if tl is None:
            raise RuntimeError(f"No timeline named {name!r}")
    else:
        tl = p.GetCurrentTimeline()
        if tl is None:
            raise RuntimeError("No timeline is open.")
    return {
        "name": tl.GetName(),
        "start_frame": tl.GetStartFrame(),
        "end_frame": tl.GetEndFrame(),
        "video_tracks": tl.GetTrackCount("video"),
        "audio_tracks": tl.GetTrackCount("audio"),
        "fps": p.GetSetting("timelineFrameRate"),
    }


@mcp.tool()
def list_media_pool(folder: str = "") -> List[Dict[str, Any]]:
    """Clips in the media pool — the root bin unless a folder name is given."""
    mp = _project().GetMediaPool()
    target = mp.GetRootFolder()
    if folder:
        match = [f for f in (target.GetSubFolderList() or [])
                 if f.GetName() == folder]
        if not match:
            raise RuntimeError(f"No media pool folder named {folder!r}")
        target = match[0]
    out = []
    for clip in (target.GetClipList() or []):
        out.append({
            "name": clip.GetName(),
            "duration": clip.GetClipProperty("Duration"),
            "fps": clip.GetClipProperty("FPS"),
            "resolution": clip.GetClipProperty("Resolution"),
        })
    return out


@mcp.tool()
def set_page(page: str) -> Dict[str, Any]:
    """Switch Resolve's page: media, cut, edit, fusion, color, fairlight, deliver.

    Changes what is on screen but touches no media, which is why this is the
    one non-read tool here that needs no further guarding.
    """
    valid = {"media", "cut", "edit", "fusion", "color", "fairlight", "deliver"}
    page = (page or "").strip().lower()
    if page not in valid:
        raise RuntimeError(f"page must be one of: {', '.join(sorted(valid))}")
    r = connect()
    ok = r.OpenPage(page)
    now = r.GetCurrentPage()
    out: Dict[str, Any] = {"ok": bool(ok), "page": now}
    if not ok:
        # OpenPage just returns False with no explanation. The usual cause by
        # far is that Resolve is showing the Project Manager rather than a
        # loaded project, in which case there is no page to switch to — and
        # GetCurrentPage() is None too, which is how we tell them apart.
        out["reason"] = (
            "Resolve refused the page switch. GetCurrentPage() is also None, which means "
            "Resolve is on the Project Manager screen rather than in a project — open a "
            "project in the Resolve window first."
            if now is None else
            f"Resolve refused the page switch and is still on {now!r}."
        )
    return out


@mcp.tool()
def project_settings(keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """Read project settings. Without `keys`, returns the common ones."""
    p = _project()
    wanted = keys or ["timelineFrameRate", "timelineResolutionWidth",
                      "timelineResolutionHeight", "colorScienceMode",
                      "timelineOutputResolutionWidth"]
    return {k: p.GetSetting(k) for k in wanted}


if __name__ == "__main__":
    # SSE rather than stdio: Odysseus runs on another machine and reaches this
    # over the tailnet, which stdio cannot cross.
    import uvicorn
    # Default to the tailnet address, never all interfaces.
    host = os.environ.get("RESOLVE_MCP_HOST", "100.102.86.125")
    port = int(os.environ.get("RESOLVE_MCP_PORT", "8930"))
    mcp.settings.host = host
    mcp.settings.port = port
    print(f"resolve-mcp listening on {host}:{port} (sse)", flush=True)
    mcp.run(transport="sse")
