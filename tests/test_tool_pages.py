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
    assert [g["id"] for g in tool_pages.GROUPS] == ["organize", "create", "build", "system"]
    assert {"devices", "terminal", "browser", "code", "calendar", "tasks", "devops", "background",
            "odysseus-dev", "research", "compare", "library", "notes", "brain", "gallery", "cookbook",
            "theme", "whats-new", "email", "chats", "skills", "settings"} == set(tool_pages.keys())


@pytest.mark.parametrize("name,key", [
    ("devices", "devices"), ("the devices page", "devices"), ("computers", "devices"), ("phones", "devices"),
    ("machines", "devices"), ("command line", "terminal"), ("shell", "terminal"), ("console", "terminal"),
    ("vs code", "code"), ("editor", "code"), ("IDE", "code"), ("Deep Research", "research"),
    ("odysseus dev", "odysseus-dev"), ("memories", "brain"), ("documents", "library"), ("sessions", "chats"),
    ("inbox", "email"), ("preferences", "settings"), ("calendars", "calendar"),
])
def test_page_names_and_aliases(name, key):
    assert tool_pages.page(name)["key"] == key


def test_unknown_names_are_not_guessed():
    assert tool_pages.page("pod bay doors") is None
    assert tool_pages.page("") is None


def test_the_prompt_lists_every_page_in_its_group():
    names = tool_pages.names_for_prompt()
    assert names.startswith("Organize: calendar, tasks, notes, brain;")
    assert "Build: code, terminal, browser, background, devops, odysseus-dev" in names
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


def test_open_panel_settings_still_carries_the_setting():
    out = _ui("open_panel settings speech to text engine")
    assert out["panel"] == "settings" and out["settings_target"] == "speech to text engine"


def test_an_unknown_page_lists_the_real_ones():
    out = _ui("open_panel pod bay doors")
    assert "error" in out and "devices" in out["error"] and "terminal" in out["error"]


def test_admin_only_pages_are_refused_for_a_non_admin(monkeypatch):
    import src.tool_security as sec
    monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda owner: owner == "boss")
    assert "only for admins" in _ui("open_panel terminal", owner="guest")["error"]
    assert "only for admins" in _ui("open_panel devices", owner="guest")["error"]
    assert _ui("open_panel calendar", owner="guest")["panel"] == "calendar"
    assert _ui("open_panel terminal", owner="boss")["panel"] == "terminal"


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
