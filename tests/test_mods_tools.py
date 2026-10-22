"""Mod tools in the agent loop (src/mods/tools.py, src/agent_loop.py):
only relevant mods' tools are offered, device control asks first, and a
low-signal message goes through retrieval instead of getting every MCP tool.

Everything is mocked: no request leaves the machine."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.mods import tools
from tests.mods_helpers import add_mod, mark_running, mods_dir, write_tools_cache  # noqa: F401

PRINT_TOOLS = [
    {"name": "print_status", "description": "Progress of the current print", "parameters": {"type": "object"},
     "keywords": [], "controls_devices": False, "read_only": True},
    {"name": "pause_print", "description": "Pause the print", "parameters": {"type": "object"},
     "keywords": [], "controls_devices": True, "read_only": False},
]
ROOK_TOOLS = [{"name": "rook_say", "description": "Say something in Minecraft", "parameters": {"type": "object"},
               "keywords": ["minecraft"], "controls_devices": False, "read_only": True}]


class _Admins:
    def is_admin(self, u):
        return u in ("alice", None, "")


@pytest.fixture
def two_mods(mods_dir, monkeypatch):
    from src.mods import access
    monkeypatch.setattr(access, "_auth_manager", _Admins())
    add_mod({"tools": {"keywords": ["print", "printer", "3d", "bambu"]}}, mod_id="print-hub",
            permissions=["devices.control"])
    write_tools_cache("print-hub", PRINT_TOOLS)
    add_mod({"tools": {"keywords": ["rook", "factorio"]}}, mod_id="rook")
    write_tools_cache("rook", ROOK_TOOLS)
    add_mod({"tools": {"keywords": ["print"]}}, mod_id="off-mod", enabled=False)
    write_tools_cache("off-mod", PRINT_TOOLS)
    return mods_dir


def test_parse_tool_list_cleans_schemas():
    parsed = tools.parse_tool_list({"tools": [
        {"type": "function", "function": {"name": "ok_tool", "description": "d", "parameters": {"type": "object"}},
         "x-odysseus": {"keywords": ["Filament"], "controls_devices": True}},
        {"type": "function", "function": {"name": "bad name!", "parameters": {}}},
        "junk",
        {"name": "bare_tool", "description": "no wrapper"},
    ]})
    assert [t["name"] for t in parsed] == ["ok_tool", "bare_tool"]
    assert parsed[0]["keywords"] == ["filament"] and parsed[0]["controls_devices"] is True


def test_selection_by_keywords(two_mods):
    assert set(tools.select("hows my 3d print doing?")) == {"mod__print-hub__print_status", "mod__print-hub__pause_print"}
    assert tools.select("tell rook to say hi in minecraft") == ["mod__rook__rook_say"]
    assert tools.select("what's the weather tomorrow?") == []
    # Word boundaries: "sprint" is not "print".
    assert tools.select("plan my sprint") == []
    # Retrieval can pick a single tool without keywords.
    assert tools.select("pause it", retrieved={"mod__print-hub__pause_print"}) == ["mod__print-hub__pause_print"]


def test_schemas_are_namespaced_and_labeled(two_mods):
    s = tools.schemas(["mod__print-hub__print_status"])
    assert s[0]["function"]["name"] == "mod__print-hub__print_status"
    assert s[0]["function"]["description"].startswith("[Mod: Test Mod]")
    assert "x-odysseus" not in json.dumps(s)
    assert "mod__print-hub__print_status" in tools.prompt_lines(["mod__print-hub__print_status"])


def test_admin_only_mod_tools_are_hidden_from_other_users(two_mods, monkeypatch):
    from src.mods import access
    monkeypatch.setenv("AUTH_ENABLED", "true")

    class A:
        def is_admin(self, u):
            return u == "alice"
    monkeypatch.setattr(access, "_auth_manager", A())
    assert tools.select("printer status", owner="alice")
    assert tools.select("printer status", owner="bob") == []


def test_device_control_tool_waits_for_approval(two_mods):
    from src import action_approvals as AA
    desc, result = asyncio.run(tools.execute("mod__print-hub__pause_print", "{}", "alice", "chat-1"))
    assert "waiting for approval" in desc and result["action_approval"]["status"] == "pending"
    e = AA.get(result["action_approval"]["id"])
    assert e["kind"] == "mod_action" and e["tool"] == "mod__print-hub__pause_print"
    assert "Your turn ends here" in result["output"]


class _ModServer:
    def __init__(self):
        outer = self
        self.calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.calls.append((self.path, json.loads(self.rfile.read(n) or b"{}"),
                                    self.headers.get("X-Odysseus-User")))
                b = json.dumps({"output": "Paused at layer 40", "data": {"layer": 40}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


def test_tool_call_reaches_the_mod_and_approved_calls_run_once(two_mods):
    from src import action_approvals as AA
    srv = _ModServer()
    mark_running("print-hub", srv.port)
    try:
        desc, result = asyncio.run(tools.execute("mod__print-hub__print_status", '{"printer": "p1s", "_owner": "x"}',
                                                 "alice", None))
        assert result == {"output": "Paused at layer 40", "exit_code": 0, "data": {"layer": 40}}
        assert srv.calls[-1] == ("/mod/tools/print_status", {"printer": "p1s"}, "alice")
        # The held device call, once approved, runs through the same path.
        _, held = asyncio.run(tools.execute("mod__print-hub__pause_print", "{}", "alice", "chat-2"))
        out = asyncio.run(AA.approve_and_run(held["action_approval"]["id"], "alice"))
        assert out["status"] == "approved" and srv.calls[-1][0] == "/mod/tools/pause_print"
    finally:
        srv.srv.shutdown()


def test_device_tool_of_a_mod_without_devices_control_never_runs(two_mods):
    add_mod({"tools": {"keywords": ["x"]}}, mod_id="sneaky")
    write_tools_cache("sneaky", PRINT_TOOLS)
    desc, r = asyncio.run(tools.execute("mod__sneaky__pause_print", "{}", "alice", "c"))
    assert "BLOCKED" in desc and "devices.control" in r["error"]


def test_unknown_and_disabled(two_mods):
    _, r = asyncio.run(tools.execute("mod__off-mod__print_status", "{}", None, None))
    assert r["exit_code"] == 1 and "turned off" in r["error"]
    _, r = asyncio.run(tools.execute("mod__print-hub__nope", "{}", None, None))
    assert r["exit_code"] == 1
    _, r = asyncio.run(tools.execute("mod__print-hub__print_status", "[1]", None, None))
    assert "JSON object" in r["error"]


def test_native_call_names_convert_to_tool_blocks():
    import src.agent_tools  # noqa: F401  (import order: src.tool_schemas needs it first)
    from src.tool_schemas import function_call_to_tool_block
    b = function_call_to_tool_block("mod__print-hub__print_status", '{"printer": "a"}')
    assert b.tool_type == "mod__print-hub__print_status" and json.loads(b.content) == {"printer": "a"}


# ── The agent loop ───────────────────────────────────────────────────────

def _mcp(n=200):
    from src.mcp_manager import McpManager
    m = McpManager()
    m._connections["0e578b9c"] = {"name": "davinci-resolve"}
    m._tools["0e578b9c"] = [{"name": f"timeline_tool_{i}", "description": "Resolve timeline tool " * 20,
                             "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}
                            for i in range(n)]
    m._generation = 1
    return m


class _Index:
    healthy = True

    def index_mcp_tools(self, *a, **k):
        pass

    def index_extra_tools(self, *a, **k):
        pass

    def get_tools_for_query(self, q, k=8):
        from src.tool_index import ALWAYS_AVAILABLE
        return set(ALWAYS_AVAILABLE) | {"web_search", "mcp__0e578b9c__timeline_tool_3"}


def _run_turn(monkeypatch, message, history=None):
    import src.agent_loop as al
    import src.tool_index as ti
    mcp = _mcp()
    monkeypatch.setattr(al, "get_mcp_manager", lambda: mcp, raising=False)
    monkeypatch.setattr(ti, "get_tool_index", lambda: _Index())
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    seen = {}

    async def fake_stream(candidates, messages, **kw):
        if "tools" not in seen:
            seen["tools"] = [t["function"]["name"] for t in (kw.get("tools") or [])]
            seen["system"] = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        yield 'data: {"delta": "ok"}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream)

    async def go():
        async for _ in al.stream_agent_loop("http://127.0.0.1:9/v1", "qwen3.8:27b",
                                            (history or []) + [{"role": "user", "content": message}],
                                            max_rounds=1, owner=None):
            pass
    asyncio.run(go())
    return seen


def test_low_signal_message_uses_retrieval_not_every_mcp_tool(two_mods, monkeypatch):
    seen = _run_turn(monkeypatch, "hows my 3d print doing?")
    mcp_sent = [n for n in seen["tools"] if n.startswith("mcp__")]
    assert mcp_sent == ["mcp__0e578b9c__timeline_tool_3"]        # only what retrieval picked
    assert "mod__print-hub__print_status" in seen["tools"]        # the mod the message is about
    assert not any(n.startswith("mod__rook") for n in seen["tools"])
    assert "trigger_research" not in seen["tools"]                # a status question
    assert len(seen["tools"]) < 20
    # The prompt describes only the offered MCP tool, and names the server.
    assert "timeline_tool_3" in seen["system"] and "timeline_tool_4:" not in seen["system"]


def test_trivial_message_gets_no_mcp_tools(two_mods, monkeypatch):
    seen = _run_turn(monkeypatch, "thanks!")
    assert not any(n.startswith(("mcp__", "mod__")) for n in seen["tools"])


def test_research_request_still_offers_trigger_research(two_mods, monkeypatch):
    seen = _run_turn(monkeypatch, "research the history of fused filament 3d printing")
    assert "trigger_research" in seen["tools"]


@pytest.mark.parametrize("owner,auth_enabled,want", [
    ("alice", "true", "alice"),            # the user the agent works for
    (None, "true", "odysseus-core"),       # no user: the core on its own
    (None, "false", "local"),              # sign-in turned off
])
def test_tool_calls_are_signed_as_the_user(two_mods, monkeypatch, owner, auth_enabled, want):
    monkeypatch.setenv("AUTH_ENABLED", auth_enabled)
    srv = _ModServer()
    mark_running("print-hub", srv.port)
    try:
        asyncio.run(tools.execute("mod__print-hub__print_status", "{}", owner, None))
        assert srv.calls[-1][2] == want
    finally:
        srv.srv.shutdown()
