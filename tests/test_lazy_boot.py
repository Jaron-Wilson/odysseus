"""The boot path must not statically pull in the on-demand panels."""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_LAZY = ("document", "slashCommands", "settings", "cookbook", "compare/index", "calendar",
         "tasks", "notes", "group", "research/panel", "search-chat", "gallery", "admin", "emailLibrary")


def _static_imports(path):
    src = path.read_text()
    return re.findall(r"""^import\s[^'"]*?from\s*['"]([^'"]+)['"]""", src, re.M)


def test_boot_files_do_not_statically_import_lazy_panels():
    for name in ("app.js", "js/chat.js", "js/emailInbox.js", "js/modelPicker.js", "js/chatRenderer.js"):
        for spec in _static_imports(_STATIC / name):
            base = re.sub(r"^(\./|\.\./)+(js/)?", "", spec).removesuffix(".js")
            assert base not in _LAZY, f"{name} statically imports {spec}"


def test_index_has_no_script_tags_for_lazy_panels():
    html = (_STATIC / "index.html").read_text()
    for n in _LAZY:
        assert f'src="/static/js/{n}.js' not in html, n


def test_every_lazy_panel_has_a_stand_in():
    lazy = (_STATIC / "js" / "lazyModules.js").read_text()
    for n in ("document", "compare/index", "group", "research/panel", "search-chat", "cookbook",
              "calendar", "tasks", "notes", "settings", "slashCommands"):
        assert f"import('./{n}.js')" in lazy, n
