"""Every entity anchor must reach a function that exists.

`#document-<id>`, `#image-<id>` and friends are routed by a table in
chatRenderer.js that names a module and the export to call. Nothing checks
the name at build time and a miss is silent -- the dynamic import resolves,
no function matches, and the click does nothing at all. `#image-<id>`
pointed at `openGalleryImage`, which gallery.js never exported, and had
therefore never worked.

So the names are pinned here against the modules' real exports. A rename on
either side fails this test instead of quietly dropping a link.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_JS = _REPO / "static" / "js"
_RENDERER = _JS / "chatRenderer.js"


def _routes():
    """kind -> (module path, [candidate export names]) from ENTITY_ROUTES."""
    src = _RENDERER.read_text()
    m = re.search(r"const ENTITY_ROUTES = \{(.*?)\n\};", src, re.S)
    assert m, "ENTITY_ROUTES table not found in chatRenderer.js"
    body = m.group(1)
    out = {}
    for kind, path, names in re.findall(
        r"(\w+):\s*\[\s*'([^']+)'\s*,\s*\[([^\]]*)\]", body
    ):
        out[kind] = (path, re.findall(r"'([^']+)'", names))
    return out


def _exports(path: Path):
    """Names importable from a module: named exports plus default-object keys."""
    src = path.read_text()
    names = set(re.findall(r"^\s*export\s+(?:async\s+)?function\s+(\w+)", src, re.M))
    names |= set(re.findall(r"^\s*export\s+(?:const|let|class)\s+(\w+)", src, re.M))
    for block in re.findall(r"export\s*\{([^}]*)\}", src):
        for part in block.split(","):
            part = part.strip()
            if not part:
                continue
            names.add(part.split(" as ")[-1].strip())
    # A default-exported object is reachable as mod.default.<key>, which the
    # router tries too, so its keys count as available names.
    dm = re.search(r"export default (\w+);", src)
    if dm:
        om = re.search(
            r"const %s = \{(.*?)\n\};" % re.escape(dm.group(1)), src, re.S
        )
        if om:
            names |= set(re.findall(r"^\s*(\w+)\s*[,:]", om.group(1), re.M))
    return names


def test_every_route_target_exists():
    routes = _routes()
    assert routes, "no routes parsed"
    missing = []
    for kind, (mod_path, candidates) in routes.items():
        target = (_JS / mod_path.lstrip("./")).resolve()
        assert target.is_file(), f"{kind} -> missing module {mod_path}"
        available = _exports(target)
        if not (set(candidates) & available):
            missing.append(f"{kind}: {mod_path} exports none of {candidates}")
    assert not missing, (
        "entity anchors routed to nonexistent exports:\n" + "\n".join(missing)
    )


def test_covers_the_kinds_the_renderer_links():
    """The table must cover every kind the click handler matches.

    The handler's regex is what decides a link is ours and calls
    preventDefault; a kind it claims but the table omits is worse than an
    external link, because the default navigation is suppressed too.
    """
    src = _RENDERER.read_text()
    m = re.search(r"href\.match\(/\^#\(([^)]+)\)-", src)
    assert m, "click handler kind regex not found"
    claimed = set(m.group(1).split("|"))
    # Handled by their own branches rather than the table.
    claimed -= {"session", "claudecode", "screencontrol"}
    assert claimed <= set(_routes()), (
        f"claimed but unrouted: {sorted(claimed - set(_routes()))}"
    )


def test_page_load_uses_the_same_opener():
    """A hash that arrives with the page must open the same thing a click does.

    Bookmarks, pasted links and new tabs all land here, and startup used to
    read the hash purely as a session id -- matching nothing, so some other
    chat opened instead of the document the URL named.
    """
    sessions = (_JS / "sessions.js").read_text()
    assert "openEntityHash" in sessions, "startup does not call the opener"
    assert "hashId && !_entityHash && activeSessions.some" in sessions, (
        "an entity hash can still be mistaken for a session id"
    )


def test_click_handler_does_not_keep_a_second_table():
    """One table, or the copies drift -- which is how three kinds broke."""
    src = _RENDERER.read_text()
    after = src[src.index("document.addEventListener('click', function(e) {"):]
    for name in ("openGalleryImage", "openCalendarTo", "openEmailLibrary"):
        assert name not in after, (
            f"{name} is routed in the click handler as well as the table"
        )
