"""Mod pages follow the user's Odysseus theme (static/js/modThemeCore.js,
static/js/modTheme.js, the odysseus_mod `themed()` page wrapper).

The pure rules run under node (skipped without it), like tests/test_mods_links.py."""
import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.mods_helpers import ROOT

CORE = os.path.join(ROOT, "static", "js", "modThemeCore.js")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def js(expr):
    src = f"import * as T from '{CORE}'; const r = (() => {{ {expr} }})(); console.log(JSON.stringify(r === undefined ? null : r));"
    p = subprocess.run(["node", "--input-type=module"], input=src, capture_output=True, text=True, cwd=ROOT, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip())


@needs_node
def test_a_dark_and_a_light_theme_are_told_apart():
    assert js("return [T.modeOf('#0f1115'), T.modeOf('#f7f7f8'), T.modeOf('#fff'), T.modeOf('')]") == \
        ["dark", "light", "dark", "dark"]


@needs_node
def test_the_message_carries_the_colors_the_accent_and_a_readable_ink():
    m = js("""const v = {'--bg': '#0f1115', '--fg': '#e6e8ec', '--panel': '#16191e', '--border': '#262a31', '--red': '#7c8cff'};
              return T.buildMessage(n => v[n], 'Inter, sans-serif');""")
    assert m["type"] == "odysseus:theme" and m["mode"] == "dark" and m["version"] == 1
    assert m["vars"] == {"--bg": "#0f1115", "--fg": "#e6e8ec", "--panel": "#16191e", "--border": "#262a31",
                         "--accent": "#7c8cff", "--accent-ink": "#ffffff", "--font": "Inter, sans-serif"}
    light = js("""const v = {'--bg': '#f7f7f8', '--fg': '#1d2024', '--red': '#ffd54a'};
                  return T.buildMessage(n => v[n], '');""")
    assert light["mode"] == "light" and light["vars"]["--accent-ink"] == "#111111" and "--font" not in light["vars"]


@needs_node
def test_only_plain_colors_and_a_font_stack_are_passed_on():
    m = js("""const v = {'--bg': '#0f1115', '--fg': 'red; background:url(//evil)', '--panel': 'url(x)', '--border': '#262a31', '--red': '#7c8cff'};
              return T.buildMessage(n => v[n], 'x; } body { display:none');""")
    assert "--fg" not in m["vars"] and "--panel" not in m["vars"] and "--font" not in m["vars"]
    assert m["vars"]["--bg"] == "#0f1115"


def test_the_core_gives_every_mod_frame_the_theme():
    panel = open(os.path.join(ROOT, "static", "js", "modPanel.js"), encoding="utf-8").read()
    assert "import './modTheme.js'" in panel
    theme = open(os.path.join(ROOT, "static", "js", "modTheme.js"), encoding="utf-8").read()
    # tabs, home cards and tutorials, on load and on every change, and on request
    for needle in ("iframe.mod-frame", ".ws-mod-card iframe", "iframe.mod-tut-frame", "'load'", "THEME_REQUEST",
                   "attributeFilter: ['style'"):
        assert needle in theme, needle


def test_the_template_sdk_wraps_a_page_in_the_theme():
    tpl = os.path.join(ROOT, "templates", "mod-template")
    sys.path.insert(0, tpl)
    try:
        import importlib
        mod = importlib.import_module("odysseus_mod")
        page = "<!doctype html><html><head><title>x</title></head><body>hi</body></html>"
        r = mod.themed(page)
        body = r.body.decode() if isinstance(r.body, bytes) else r.body
        assert body.index("odysseus-theme") < body.index("</head>") and "--accent" in body
        assert "odysseus:theme" in body and "odysseus:theme-request" in body and r.status == 200
        # No <head> at all: still themed, still before the content.
        bare = mod.themed("<p>hi</p>").body
        bare = bare.decode() if isinstance(bare, bytes) else bare
        assert bare.endswith("<p>hi</p>") and "odysseus-theme" in bare
    finally:
        sys.path.remove(tpl)
        sys.modules.pop("odysseus_mod", None)
