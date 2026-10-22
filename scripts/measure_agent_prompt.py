"""How big is the agent's first request for one message?

    python scripts/measure_agent_prompt.py "hows my 3d print doing?"

Runs src/agent_loop.stream_agent_loop with no model behind it: the request it
would send is captured and measured, nothing leaves the machine. MCP servers
are stand-ins shaped like the ones connected on 2026-10-21 (three desktop
servers, 271 tools, about 1.9 KB of schema each, which is what the log of
that day reports: 280 tools, ~133,876 tokens of schemas). Tool retrieval is a
word-overlap stand-in for the embedding index, so the run needs no model
download. Token counts use the loop's own estimates (estimate_tokens for the
messages, JSON length / 4 for the schemas, as _fit_tools_to_window does).

Uses a temporary data dir; never the live one.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="odysseus-measure-")
os.environ["ODYSSEUS_DATA_DIR"] = _tmp
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/app.db"
os.environ.setdefault("ODYSSEUS_OLLAMA_PROBE", "0")
os.environ.setdefault("ODYSSEUS_CLAUDE_AGENTS", "0")
sys.path.insert(0, os.getcwd() if os.path.isdir(os.path.join(os.getcwd(), "src")) else ROOT)

import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING)

SERVERS = [("0e578b9c", "davinci-resolve", 180), ("19d772b0", "windows-desktop", 70), ("0ff4f957", "linux-laptop", 21)]
WORDS = ("timeline clip render project media pool page color node marker track audio export import window app "
         "volume screen click type key focus launch list status info set get open close move resize").split()


def _tool(server: str, i: int):
    a, b = WORDS[i % len(WORDS)], WORDS[(i * 7 + 3) % len(WORDS)]
    name = f"{a}_{b}_{i}"
    desc = (f"{server}: {a} {b}. " + " ".join(WORDS[(i + k) % len(WORDS)] for k in range(100)))[:790]
    props = {f"{WORDS[(i + k * 5) % len(WORDS)]}_{k}": {
        "type": "string", "description": " ".join(WORDS[(i + k + j) % len(WORDS)] for j in range(22))}
        for k in range(6)}
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": list(props)[:2]}}


def fake_mcp():
    from src.mcp_manager import McpManager
    m = McpManager()
    for sid, name, n in SERVERS:
        m._connections[sid] = {"name": name, "status": "connected"}
        m._tools[sid] = [_tool(name, i) for i in range(n)]
    m._generation = 1
    return m


class FakeIndex:
    """Word overlap over the same texts the embedding index holds; top k."""
    healthy = True

    def __init__(self, mcp):
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        self.docs = dict(BUILTIN_TOOL_DESCRIPTIONS)
        for t in mcp.get_all_tools():
            self.docs[t["qualified_name"]] = f"{t['name']} {t['server_name']} {t['description']}"

    def index_mcp_tools(self, *a, **k):
        pass

    def index_extra_tools(self, kind, docs, generation):
        self.docs.update(docs)

    def get_tools_for_query(self, query, k=8):
        from src.tool_index import ALWAYS_AVAILABLE, ToolIndex
        q = set(re.findall(r"[a-z0-9]+", query.lower())) - {"the", "my", "a", "is", "how", "doing", "hows"}
        scored = sorted(((len(q & set(re.findall(r"[a-z0-9]+", d.lower()))), n) for n, d in self.docs.items()),
                        reverse=True)
        # Like a vector search, it always returns k tools, related or not.
        out = set(ALWAYS_AVAILABLE) | {n for _s, n in scored[:k]}
        for keywords, tools in ToolIndex._KEYWORD_HINTS.items():
            if any(re.search(rf"\b{re.escape(kw)}\b", query.lower()) for kw in keywords):
                out.update(tools)
        return out


async def measure(message: str, context_length: int):
    import src.agent_loop as al
    import src.tool_index as ti
    from src.model_context import estimate_tokens
    mcp = fake_mcp()
    idx = FakeIndex(mcp)
    al.get_mcp_manager = lambda: mcp
    ti.get_tool_index = lambda: idx
    al.blocked_tools_for_owner = lambda owner: set()
    captured = {}

    async def fake_stream(candidates, messages, **kw):
        if "messages" not in captured:
            captured["messages"] = messages
            captured["tools"] = kw.get("tools") or []
        yield 'data: {"delta": "ok"}\n\n'
        yield "data: [DONE]\n\n"

    al.stream_llm_with_fallback = fake_stream
    async for _ in al.stream_agent_loop("http://127.0.0.1:9/v1", "qwen3.8:27b",
                                        [{"role": "user", "content": message}], max_rounds=1,
                                        context_length=context_length, owner=None):
        pass
    msgs, tools = captured["messages"], captured["tools"]
    names = [t["function"]["name"] for t in tools]
    return {
        "context_length": context_length or "none (untrimmed)",
        "tools_sent": len(tools),
        "mcp_tools_sent": sum(n.startswith("mcp__") for n in names),
        "mod_tools_sent": sum(n.startswith("mod__") for n in names),
        "trigger_research_offered": "trigger_research" in names,
        "message_tokens": estimate_tokens(msgs),
        "schema_tokens": len(json.dumps(tools)) // 4,
        "total_tokens": estimate_tokens(msgs) + len(json.dumps(tools)) // 4,
        "tool_names": names,
    }


def install_print_hub_standin():
    """A print-hub stand-in, as the mod system would hold it once installed:
    a registry entry and its cached tool list (src/mods). No-op on code
    without mods."""
    try:
        from src.mods import paths, registry
    except ImportError:
        return False
    man = {"id": "print-hub", "name": "Print Hub", "version": "0.1.0", "permissions": ["notify"],
           "runtime": {"type": "process", "command": ["true"], "start": "lazy"},
           "ui": {"tabs": [], "home_cards": []}, "events": {"emits": [], "subscribes": []},
           "tools": {"endpoint": "/mod/tools", "keywords": ["print", "printer", "3d", "bambu", "filament"]}}
    data = {"mods": {"print-hub": {"id": "print-hub", "name": "Print Hub", "version": "0.1.0", "enabled": True,
                                   "access": "admins", "manifest": man, "granted_permissions": ["notify"],
                                   "token": "odm_x"}}, "settings": {}}
    os.makedirs(paths.mod_dir("print-hub"), exist_ok=True)
    with open(paths.registry_file(), "w") as f:
        json.dump(data, f)
    tools = [{"name": n, "description": d, "parameters": {"type": "object", "properties": {
        "printer": {"type": "string", "description": "Which printer; blank for the only one"}}},
        "keywords": [], "controls_devices": False, "read_only": True} for n, d in (
        ("print_status", "The current print: progress, time left, layer, temperatures, and any detector alerts."),
        ("camera_snapshot", "A still from the printer camera, as an image URL."),
        ("list_jobs", "Recent print jobs with their outcome."),
        ("pause_print", "Pause the running print."))]
    tools[-1]["controls_devices"], tools[-1]["read_only"] = True, False
    with open(os.path.join(paths.mod_dir("print-hub"), "tools.json"), "w") as f:
        json.dump({"tools": tools}, f)
    return True


def main():
    message = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "hows my 3d print doing?"
    out = {"message": message, "code": os.getcwd()}
    if "--with-print-hub" in sys.argv:
        out["print_hub_standin"] = install_print_hub_standin()
    out["untrimmed"] = asyncio.run(measure(message, 0))
    out["window_32k"] = asyncio.run(measure(message, 32768))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil
        shutil.rmtree(_tmp, ignore_errors=True)
