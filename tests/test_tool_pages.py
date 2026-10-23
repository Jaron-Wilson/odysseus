"""The agent's half of "take me to <any page>" (src/tool_pages.py, ui_control
open_panel, and the tool selection that has to offer ui_control).

Seen 2026-09-30: "take me to devices page please" (gpt-6-astra, agent mode)
got "I don't have an app-navigation tool available here." Two causes:
ui_control open_panel only knew nine panels, and the request matched the
devices/shell hints but nothing that offered ui_control, so the model was
never given it. What matters:

  * the agent's page list is the one static/js/toolPages.js holds (the same
    names and aliases the sidebar, the composer chip and Ctrl+K use);
  * open_panel opens any page, by key, label or alias, several words
    included, and the old panel names still work;
  * admin-only pages are refused for a non-admin owner;
  * a navigation request offers ui_control whether retrieval works or not,
    and promotes a plain chat to agent mode;
  * native function calling (ChatGPT plan models and other API models)
    carries the page name through.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from src import tool_pages
from src.ai_interaction import do_ui_control

_ROOT = Path(__file__).resolve().parent.parent
_JS = (_ROOT / "static" / "js" / "toolPages.js").read_text()


def _ui(cmd, owner=None):
    return asyncio.run(do_ui_control(cmd, owner=owner))


# ── The list ───────────────────────────────────────────────────────────

def test_the_agent_reads_the_same_list_as_the_page():
    block = re.search(r"/\*\s*tool-pages:begin\s*\*/(.*?)/\*\s*tool-pages:end\s*\*/", _JS, re.S).group(1)
    assert json.loads(block)["pages"] == tool_pages.PAGES
    assert [p["key"] for p in json.loads(block)["moved"]] == [p["key"] for p in tool_pages.MOVED]
    assert [g["id"] for g in tool_pages.GROUPS] == ["organize", "create", "build", "system"]
    assert {"devices", "terminal", "browser", "code", "calendar", "tasks", "devops", "background", "company", "claude-sessions", "adsb", "games", "print",
            "odysseus-dev", "research", "compare", "library", "notes", "plans", "brain", "gallery", "cookbook",
            "theme", "usage", "whats-new", "email", "chats", "skills", "settings"} == set(tool_pages.keys())


@pytest.mark.parametrize("name,key", [
    ("devices", "devices"), ("the devices page", "devices"), ("computers", "devices"), ("phones", "devices"),
    ("machines", "devices"), ("command line", "terminal"), ("shell", "terminal"), ("console", "terminal"),
    ("vs code", "code"), ("editor", "code"), ("IDE", "code"), ("Deep Research", "research"),
    ("odysseus dev", "odysseus-dev"), ("claude code sessions", "claude-sessions"), ("org chart", "company"), ("paperclip", "company"), ("memories", "brain"), ("documents", "library"), ("sessions", "chats"),
    ("inbox", "email"), ("preferences", "settings"), ("calendars", "calendar"),
])
def test_page_names_and_aliases(name, key):
    assert tool_pages.page(name)["key"] == key


def test_unknown_names_are_not_guessed():
    assert tool_pages.page("pod bay doors") is None
    assert tool_pages.page("") is None


def test_the_prompt_lists_every_page_in_its_group():
    names = tool_pages.names_for_prompt()
    assert names.startswith("Organize: calendar, tasks, notes, plans, brain;")
    # Terminal, Claude sessions and DevOps moved to mods: still pages the agent
    # can open, listed after the built-in ones.
    assert "Build: code, browser, background, company, odysseus-dev, terminal, claude-sessions, devops" in names
    assert "command line/shell/console->terminal" in tool_pages.aliases_for_prompt()


# ── ui_control open_panel ───────────────────────────────────────────────

@pytest.mark.parametrize("cmd,key", [
    ("open_panel devices", "devices"), ("open_panel terminal", "terminal"), ("open_panel command line", "terminal"),
    ("open_panel vs code", "code"), ("open_panel odysseus dev", "odysseus-dev"), ("open_panel the browser page", "browser"),
    ("open_panel calendar", "calendar"), ("open_panel background", "background"), ("open_panel devops", "devops"),
    ("open_panel deep research", "research"), ("open_panel compare", "compare"), ("open_panel theme", "theme"),
    # The old panel names.
    ("open_panel documents", "library"), ("open_panel memories", "brain"), ("open_panel sessions", "chats"),
    ("open_panel email", "email"), ("open_panel skills", "skills"), ("open_panel cookbook", "cookbook"),
])
def test_open_panel_opens_any_page(cmd, key):
    out = _ui(cmd)
    assert out["ui_event"] == "open_panel" and out["panel"] == key, out
    assert out["results"] == f"Opening {tool_pages.page(key)['label']}"


def test_a_page_that_moved_to_a_mod_still_opens():
    """The 3D print helper moved into the Print Hub mod. It is no longer a
    built-in page (a mod tab's `after` can't name it), but "take me to 3D
    print" still opens it: the browser routes the key through
    modLinks.openLegacy (the mod's tab, or the "now a mod" card)."""
    from src.mods import placement
    assert "print" not in [p["key"] for p in tool_pages.PAGES]
    assert "print" not in placement.page_keys("create")
    assert "print" in placement.LEGACY_PAGES
    for name in ("3d print", "3D printing", "print helper", "the 3d print page", "make it printable", "bambu"):
        assert tool_pages.page(name)["key"] == "print", name
    out = _ui("open_panel 3d print helper")
    assert out["ui_event"] == "open_panel" and out["panel"] == "print", out
    assert "Create & research: library, gallery, research, compare, print" in tool_pages.names_for_prompt()
    assert "if (p.moved)" in _JS and "openLegacy(p.key)" in _JS


def test_games_is_the_games_mod_and_nothing_of_it_is_left_in_the_core():
    """Games (the page, its Settings > Devices card, the five game tools, the
    nightly Steam sync, the /play redirect and the frame-src hook) moved into
    the Games mod (github.com/Jaron-Wilson/odysseus-games)."""
    import os
    from src import tool_index, tool_schemas, tool_security
    from src.agent_loop import TOOL_SECTIONS
    from src.mods import placement
    import src.agent_tools as at
    assert "games" not in [p["key"] for p in tool_pages.PAGES] and "games" in placement.LEGACY_PAGES
    assert "games" not in placement.page_keys("system")
    for name in ("games", "play games", "steam", "sunshine", "moonlight"):
        assert tool_pages.page(name)["key"] == "games", name
    assert _ui("open_panel games")["panel"] == "games"
    names = {t["function"]["name"] for t in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    game_tools = {"games_list", "game_status", "game_launch", "game_stop", "game_sync_steam"}
    assert not game_tools & names and not game_tools & at.TOOL_TAGS
    assert not game_tools & set(tool_index.BUILTIN_TOOL_DESCRIPTIONS) and not game_tools & set(TOOL_SECTIONS)
    assert not game_tools & tool_security.plan_mode_disabled_tools()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for gone in ("src/games.py", "routes/games_routes.py", "src/agent_tools/game_tools.py", "static/js/gamesPanel.js",
                 "static/js/gamesSettings.js", "static/css/gamesPanel.css", "docs/games.md"):
        assert not os.path.exists(os.path.join(root, gone)), gone
    app = open(os.path.join(root, "app.py"), encoding="utf-8").read()
    assert "setup_games_routes" not in app and "play_redirect" not in app and "_games.nightly_loop" not in app
    mw = open(os.path.join(root, "core", "middleware.py"), encoding="utf-8").read()
    assert "_games_frame_src" not in mw
    html = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    assert 'id="tool-games-btn"' not in html and 'id="games-settings-card"' not in html and "gamesPanel" not in html


def test_the_adsb_receiver_moved_to_a_mod_and_still_opens():
    """The ADS-B receiver page is now the ADS-B Receiver mod (odysseus-adsb):
    no route, panel or sidebar button in the core, and "take me to ADS-B"
    goes through modLinks.openLegacy like the print helper."""
    import os
    from src.mods import placement
    assert "adsb" not in [p["key"] for p in tool_pages.PAGES]
    assert "adsb" not in placement.page_keys("system")
    assert "adsb" in placement.LEGACY_PAGES
    for name in ("adsb", "ADS-B", "ads-b receiver", "tar1090", "aircraft", "the adsb page"):
        assert tool_pages.page(name)["key"] == "adsb", name
    out = _ui("open_panel ads-b receiver")
    assert out["ui_event"] == "open_panel" and out["panel"] == "adsb", out
    assert "System: devices, cookbook, theme, usage, whats-new, adsb" in tool_pages.names_for_prompt()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for gone in ("routes/adsb_routes.py", "src/adsb.py", "static/js/adsbPanel.js", "static/css/adsbPanel.css"):
        assert not os.path.exists(os.path.join(root, gone)), gone
    html = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    assert "tool-adsb-btn" not in html and "adsbPanel" not in html


def test_open_panel_settings_still_carries_the_setting():
    out = _ui("open_panel settings speech to text engine")
    assert out["panel"] == "settings" and out["settings_target"] == "speech to text engine"


def test_an_unknown_page_lists_the_real_ones():
    out = _ui("open_panel pod bay doors")
    assert "error" in out and "devices" in out["error"] and "terminal" in out["error"]


def test_admin_only_pages_are_refused_for_a_non_admin(monkeypatch):
    import src.tool_security as sec
    monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda owner: owner == "boss")
    assert "only for admins" in _ui("open_panel code", owner="guest")["error"]
    assert "only for admins" in _ui("open_panel devices", owner="guest")["error"]
    assert _ui("open_panel calendar", owner="guest")["panel"] == "calendar"
    assert _ui("open_panel code", owner="boss")["panel"] == "code"
    # A page that moved to a mod opens the mod's tab or the "now a mod" card;
    # the mod decides who may use it (the Programming mod: admins only).
    assert _ui("open_panel terminal", owner="guest")["panel"] == "terminal"


def test_a_native_tool_call_passes_the_page_through():
    import src.agent_tools  # noqa: F401  (tool_schemas imports through it)
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    block = function_call_to_tool_block("ui_control", json.dumps({"action": "open_panel", "name": "devices"}))
    assert block.content == "open_panel devices"
    assert _ui(block.content)["panel"] == "devices"
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "ui_control")["function"]
    assert "take me to devices" in schema["description"]
    assert "devices" in schema["parameters"]["properties"]["name"]["description"]


# ── ui_control is offered for navigation ────────────────────────────────

_NAV = ["take me to devices page please", "Take me to the terminal", "go to the command line",
        "bring up my calendar", "open the terminal", "show me devices", "open vs code",
        "navigate to devops", "can you take me to the browser"]


def _index_without_embeddings():
    from src.tool_index import ToolIndex
    ti = ToolIndex.__new__(ToolIndex)        # skip __init__ (no ChromaDB/fastembed)
    ti.retrieve = lambda query, k=8: []
    return ti


@pytest.mark.parametrize("q", _NAV)
def test_a_navigation_request_selects_ui_control(q):
    assert "ui_control" in _index_without_embeddings().get_tools_for_query(q)


@pytest.mark.parametrize("q", _NAV)
def test_a_navigation_request_is_a_ui_request(q):
    from src.agent_loop import _classify_agent_request
    assert "ui" in _classify_agent_request([], q)["domains"]


def test_the_keyword_fallback_offers_ui_control_too():
    """With retrieval down, agent_loop matches the hint keywords as plain
    substrings; "take me to" must be among them."""
    from src.tool_index import ToolIndex
    ql = "take me to devices page please"
    hit = set()
    for keywords, tools in ToolIndex._KEYWORD_HINTS.items():
        if any(kw in ql for kw in keywords):
            hit |= tools
    assert "ui_control" in hit


def test_plain_questions_dont_drag_in_ui_control():
    ti = _index_without_embeddings()
    assert "ui_control" not in ti.get_tools_for_query("tell me a joke")
    assert "ui_control" not in ti.get_tools_for_query("what is a terminal velocity")


@pytest.mark.parametrize("q", ["take me to devices page please", "go to the terminal", "please take me to settings"])
def test_a_navigation_request_promotes_chat_to_agent_mode(q):
    from src.action_intents import classify_tool_intent
    intent = classify_tool_intent(q)
    assert intent.needs_tools and intent.category == "ui", intent


def test_showing_code_is_still_a_chat_message():
    from src.action_intents import classify_tool_intent
    assert not classify_tool_intent("show me the code for a binary search").needs_tools


def test_the_ui_rules_name_the_pages():
    from src.agent_loop import _DOMAIN_RULES, TOOL_SECTIONS
    for text in (_DOMAIN_RULES["ui"], TOOL_SECTIONS["ui_control"]):
        assert "{pages}" not in text and "devices" in text and "terminal" in text
    assert "never answer that there is no navigation tool" in _DOMAIN_RULES["ui"]
