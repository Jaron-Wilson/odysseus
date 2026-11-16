"""Spoken call turns answer sooner (src/call_turns.py, src/agent_loop.py, src/llm_core.py): no reasoning pass,
one tool set and system prompt for the whole call (so a local server's prefix cache holds), and no tool
re-indexing inside a call turn.

Everything is mocked: no request leaves the machine."""
import asyncio
import threading
from pathlib import Path

import pytest

from src import call_turns
from src.llm_core import no_thinking_payload
from tests.mods_helpers import add_mod, mods_dir, write_tools_cache  # noqa: F401

_ROOT = Path(__file__).resolve().parent.parent

PRINT_TOOLS = [
    {"name": "print_status", "description": "Progress of the current print", "parameters": {"type": "object"},
     "keywords": [], "controls_devices": False, "read_only": True},
]


@pytest.fixture(autouse=True)
def _fresh():
    call_turns._calls.clear()
    yield
    call_turns._calls.clear()


# ── Reasoning off ───────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["http://192.168.100.101:8114/v1", "http://127.0.0.1:7000/v1"])
def test_a_self_hosted_server_is_asked_for_no_reasoning(url):
    p = {"model": "qwen3.8-27b", "reasoning_effort": "low", "chat_template_kwargs": {"x": 1}}
    no_thinking_payload(p, url)
    assert p["reasoning_effort"] == "none"
    assert p["chat_template_kwargs"] == {"x": 1, "enable_thinking": False}


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://openrouter.ai/api/v1",
                                 "https://api.groq.com/openai/v1", "http://localhost:11434/v1"])
def test_hosted_apis_and_ollama_are_left_alone(url):
    p = {"model": "m", "reasoning_effort": "low"}
    no_thinking_payload(p, url)
    assert p == {"model": "m", "reasoning_effort": "low"}


def test_the_setting_is_off_by_default_and_in_the_voice_call_card():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["call_thinking"] is False
    assert call_turns.think_on_calls() is False
    html = (_ROOT / "static" / "index.html").read_text()
    card = html[html.index('id="voice-call-settings"'):]
    card = card[:card.index('id="set-vcMsg"')]
    assert 'for="set-vcThink">Think before answering<' in card and 'id="set-vcThink"' in card
    js = (_ROOT / "static" / "js" / "voiceCall.js").read_text()
    assert "save({ call_thinking: think.checked })" in js


def test_the_in_app_call_in_chat_mode_asks_for_no_reasoning_too():
    src = (_ROOT / "routes" / "chat_routes.py").read_text()
    assert '_call_think_kw = {"thinking": False}' in src and "**_call_think_kw," in src


# ── The agent loop ──────────────────────────────────────────────────────

class _Index:
    """Retrieval that picks different tools for every message, like the real one."""
    healthy = True

    def __init__(self):
        self.queries, self.reindexed, self.reindex_threads = [], 0, set()

    def index_mcp_tools(self, *a, **k):
        self.reindexed += 1
        self.reindex_threads.add(threading.current_thread().name)

    def index_extra_tools(self, *a, **k):
        self.reindexed += 1
        self.reindex_threads.add(threading.current_thread().name)

    def get_tools_for_query(self, q, k=8):
        from src.tool_index import ALWAYS_AVAILABLE
        self.queries.append(q)
        if "music" in q:
            return set(ALWAYS_AVAILABLE) | {"web_search", "mcp__5a1e__pause_music"}
        extra = [{"web_search", "web_fetch"}, {"read_file", "grep"}, {"manage_notes", "manage_tasks"}]
        return set(ALWAYS_AVAILABLE) | extra[(len(self.queries) - 1) % 3]


def _call_system():
    from routes.chat_routes import VOICE_CALL_NOTE
    return {"role": "system", "content": VOICE_CALL_NOTE + "\n\nOn a phone call"}


def _mcp():
    from src.mcp_manager import McpManager
    m = McpManager()
    m._connections["5a1e"] = {"name": "media"}
    m._tools["5a1e"] = [{"name": n, "description": "Media player: " + n.replace("_", " "),
                         "input_schema": {"type": "object", "properties": {}}}
                        for n in ("pause_music", "play_music", "set_volume")]
    m._generation = 1
    return m


def _turn(monkeypatch, idx, message, *, history=(), call=True, sid="call-1", thinking=False):
    import src.agent_loop as al
    import src.tool_index as ti
    mcp = _mcp()
    monkeypatch.setattr(al, "get_mcp_manager", lambda: mcp, raising=False)
    monkeypatch.setattr(ti, "get_tool_index", lambda: idx)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(call_turns, "think_on_calls", lambda: thinking)
    # The local time changes every minute: it goes at the end, so it never moves the cached prefix.
    import src.user_time as ut
    monkeypatch.setattr(ut, "current_datetime_context_message",
                        lambda: {"role": "user", "content": f"[time {len(idx.queries)}]"})
    seen = {}

    async def fake_stream(candidates, messages, **kw):
        if "tools" not in seen:
            seen["tools"] = [t["function"]["name"] for t in (kw.get("tools") or [])]
            seen["system"] = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
            seen["messages"] = messages
            seen["kw"] = kw
        yield 'data: {"delta": "Sure."}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream)

    msgs = ([_call_system()] if call else []) + list(history) + [{"role": "user", "content": message}]

    async def go():
        async for _ in al.stream_agent_loop("http://192.168.100.101:8114/v1", "qwen3.8-27b", msgs,
                                            max_rounds=1, owner=None, session_id=sid):
            pass
    asyncio.run(go())
    return seen


def _settle():
    """Let the background reindex thread finish."""
    for _ in range(100):
        if not call_turns._index_busy.is_set():
            return
        threading.Event().wait(0.01)


def test_call_turns_keep_one_tool_set_and_system_prompt(monkeypatch):
    idx = _Index()
    t1 = _turn(monkeypatch, idx, "hey, can you hear me okay?")
    hist = [{"role": "user", "content": "hey, can you hear me okay?"}, {"role": "assistant", "content": "Sure."}]
    t2 = _turn(monkeypatch, idx, "what is a good quick dinner I can make tonight?", history=hist)
    hist += [{"role": "user", "content": "what is a good quick dinner I can make tonight?"},
             {"role": "assistant", "content": "Sure."}]
    t3 = _turn(monkeypatch, idx, "give me one quick tip for sleeping better", history=hist)
    assert len(idx.queries) == 1                        # retrieval on the call's first turn only
    assert t1["tools"] == t2["tools"] == t3["tools"]    # same tools, same order
    assert "opencode_chat" in t1["tools"]
    assert t1["system"] == t2["system"] == t3["system"]
    # What changes per turn (the time) is after the history, so the earlier turns are a shared prefix.
    assert t3["messages"][:len(t2["messages"]) - 2] == t2["messages"][:-2]


def test_a_later_call_turn_adds_what_its_message_needs_after_the_kept_tools(monkeypatch):
    idx = _Index()
    t1 = _turn(monkeypatch, idx, "hey, can you hear me okay?")
    assert "web_search" in t1["tools"]                  # what calls often need next is there from the start
    t2 = _turn(monkeypatch, idx, "any new emails in my inbox?")
    assert set(t1["tools"]) < set(t2["tools"])
    assert "list_emails" in t2["tools"] and "list_emails" not in t1["tools"]
    t3 = _turn(monkeypatch, idx, "thanks, and how are you?")
    assert t3["tools"] == t2["tools"]                   # it stays for the rest of the call


def test_a_later_call_turn_that_names_an_mcp_tool_gets_it(monkeypatch):
    idx = _Index()
    t1 = _turn(monkeypatch, idx, "hey, can you hear me okay?")
    t2 = _turn(monkeypatch, idx, "can you pause my music?")
    assert "mcp__5a1e__pause_music" not in t1["tools"] and "mcp__5a1e__pause_music" in t2["tools"]
    assert set(t2["tools"]) - set(t1["tools"]) == {"mcp__5a1e__pause_music"}   # only what retrieval found in MCP
    assert [n for n in t2["tools"] if n in t1["tools"]] == t1["tools"]


def test_outside_a_call_retrieval_runs_every_turn_and_the_call_is_forgotten(monkeypatch):
    idx = _Index()
    _turn(monkeypatch, idx, "hey, can you hear me okay?")
    assert call_turns.frozen("call-1")
    a = _turn(monkeypatch, idx, "what is a good quick dinner?", call=False)
    b = _turn(monkeypatch, idx, "give me one quick tip for sleeping better", call=False)
    assert len(idx.queries) == 3 and a["tools"] != b["tools"]
    assert call_turns.frozen("call-1") is None
    assert "thinking" not in a["kw"]


def test_call_turns_ask_for_no_reasoning_unless_the_setting_is_on(monkeypatch):
    idx = _Index()
    assert _turn(monkeypatch, idx, "hello there, how is it going?")["kw"].get("thinking") is False
    assert "thinking" not in _turn(monkeypatch, idx, "hello there, how is it going?", sid="c2", thinking=True)["kw"]


def test_a_call_turn_never_waits_for_the_tool_index(monkeypatch):
    idx = _Index()
    _turn(monkeypatch, idx, "hey, can you hear me okay?")
    _settle()
    assert idx.reindex_threads == {"call-tool-reindex"}
    idx2 = _Index()
    _turn(monkeypatch, idx2, "what is a good quick dinner?", call=False)
    assert "call-tool-reindex" not in idx2.reindex_threads and idx2.reindexed


def test_the_research_gate_does_not_move_a_calls_tool_list(monkeypatch):
    idx = _Index()
    t1 = _turn(monkeypatch, idx, "research the history of fused filament 3d printing for me")
    t2 = _turn(monkeypatch, idx, "hows my print doing?")
    assert "trigger_research" in t1["tools"] and t1["tools"] == t2["tools"]


def test_a_calls_mod_tools_stay_on_offer(mods_dir, monkeypatch):  # noqa: F811
    from src.mods import access
    monkeypatch.setattr(access, "_auth_manager", type("A", (), {"is_admin": lambda self, u: True})())
    add_mod({"tools": {"keywords": ["print", "printer"]}}, mod_id="print-hub")
    write_tools_cache("print-hub", PRINT_TOOLS)
    idx = _Index()
    t1 = _turn(monkeypatch, idx, "how is my printer doing?")
    t2 = _turn(monkeypatch, idx, "okay, and what time is it?")
    assert "mod__print-hub__print_status" in t1["tools"]
    assert t1["tools"] == t2["tools"] and t1["system"] == t2["system"]


def test_the_kept_tools_expire():
    call_turns.keep("s", tools={"a"}, mod_tools=[], needs_admin=False)
    assert call_turns.frozen("s")["tools"] == {"a"}
    call_turns._calls["s"]["at"] -= call_turns.IDLE_S + 1
    assert call_turns.frozen("s") is None
