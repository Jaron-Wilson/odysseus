"""Messages guarded on `window.uiModule` can never appear.

Nothing anywhere assigns `window.uiModule`. Every call site guarded on
it was therefore dead: `if (window.uiModule) window.uiModule.showError(...)`
is always false, so the call never runs. Eleven of them had accumulated,
and each one is a toast or an error -- "Failed to create event",
"Failed to delete 3 chats", "Sorted 12 sessions". The user is told
nothing at precisely the moment something has gone wrong, which is worse
than having written no message at all: the code reads as though it
reports failures.

Each of those files already imports the module as `uiModule`, so the
working form is the import.
"""
import re
from pathlib import Path

_JS = Path(__file__).resolve().parent.parent / "static" / "js"


def _sources():
    return sorted(p for p in _JS.rglob("*.js") if p.is_file())


def test_nothing_assigns_window_ui_module():
    """The premise. If something starts assigning it, this test is wrong."""
    assignment = re.compile(r"window\.uiModule\s*=")
    setters = [p.name for p in _sources() if assignment.search(p.read_text())]
    assert not setters, (
        "window.uiModule is assigned in %s -- revisit this test" % setters
    )


def test_no_code_calls_through_window_ui_module():
    offenders = []
    for path in _sources():
        for num, line in enumerate(path.read_text().splitlines(), start=1):
            if "window.uiModule" not in line:
                continue
            # A comment naming the trap is documentation, not a call.
            if line.lstrip().startswith(("//", "*", "/*")):
                continue
            offenders.append("%s:%d %s" % (path.name, num, line.strip()[:90]))
    assert not offenders, (
        "these never run, because nothing assigns window.uiModule:\n"
        + "\n".join(offenders)
    )


def test_the_stop_toast_uses_the_imported_module():
    """The one added for Stop, pinned so it cannot regress to the dead form."""
    src = (_JS / "chat.js").read_text()
    assert "import uiModule from './ui.js';" in src
    i = src.index("screen_control_revoked")
    window = src[i:i + 700]
    assert "uiModule.showToast(" in window, "the Stop toast is not shown"
    assert "window.uiModule.showToast(" not in window, (
        "the Stop toast went back through the dead global"
    )
