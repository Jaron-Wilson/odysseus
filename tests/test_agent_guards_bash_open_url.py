"""Guards on two things seen live on 2026-09-27 in one chat:

- Asked why a YouTube link failed, the local model opened the API's health
  URL in a browser on the laptop (open_url), which raised a screen-control
  request for a machine the user was not at. Reading a URL is web_fetch/curl.
- It then read the project one `ssh … cat <file>` at a time: 22 bash calls in
  a turn ("12 in a row").
"""
import asyncio
import json

import src.agent_loop as al


def _events(gen):
    async def run():
        return [c async for c in gen]
    out = []
    for c in asyncio.run(run()):
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _loop(monkeypatch, rounds, user_text, tools):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    ran = []

    async def fake_exec(block, *a, **k):
        ran.append(block.tool_type)
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", fake_exec, raising=False)
    seen_results = []
    n = {"i": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        seen_results.append(json.dumps(messages[-1])[-400:])
        i = n["i"]
        n["i"] += 1
        text = rounds(i)
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)
    ev = _events(al.stream_agent_loop("http://x/v1", "m", [{"role": "user", "content": user_text}],
                                      max_rounds=20, relevant_tools=tools))
    return ran, seen_results, ev


def test_bash_streak_is_nudged_then_stopped(monkeypatch):
    def rounds(i):
        if i < 15:
            return f"Reading file {i}.\n```bash\ncat /tmp/file{i}.txt\n```"
        return "Here is what I found."
    ran, seen, _ = _loop(monkeypatch, rounds, "why does the upload fail?", {"bash"})
    assert ran.count("bash") == al.BASH_STREAK_STOP                 # the 13th was refused
    assert any("that is 6 bash calls in a row" in s for s in seen)
    assert any("more than 12 bash calls in a row" in s for s in seen)


def test_open_url_only_when_the_user_asks_to_see(monkeypatch):
    # MCP tools arrive as native calls; stand one in for the parser.
    from src.agent_tools import ToolBlock
    real = al._resolve_tool_blocks

    def resolve(text, native, round_num, **kw):
        if "OPEN_URL" in text:
            return [ToolBlock("mcp__0ff4f957__open_url", '{"url": "https://api.example/health"}')], True
        return real(text, native, round_num, **kw)
    monkeypatch.setattr(al, "_resolve_tool_blocks", resolve)
    call = "Checking. OPEN_URL"

    def rounds(i):
        return call if i == 0 else "Done."
    ran, seen, _ = _loop(monkeypatch, rounds, "https://youtu.be/x i put that in but it didnt work",
                         {"mcp__0ff4f957__open_url"})
    assert "mcp__0ff4f957__open_url" not in ran
    assert any("did not ask to see a page" in s for s in seen)
    ran, _, _ = _loop(monkeypatch, rounds, "open the health page on my laptop",
                      {"mcp__0ff4f957__open_url"})
    assert ran == ["mcp__0ff4f957__open_url"]


def test_approval_notes_are_not_the_user_asking():
    msgs = [{"role": "user", "content": "why did it fail"},
            {"role": "user", "content": "[Screen control approved for linux-laptop]\n\nThe permission..."}]
    assert al._last_real_user_text(msgs) == "why did it fail"


def test_screen_control_prompt_names_the_other_machine():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(here, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert "Heads up: this is ${name || 'another machine'}, not" in js
    routes = open(os.path.join(here, "routes", "screen_control_routes.py"), encoding="utf-8").read()
    assert '"other_machine": bool(you and target' in routes
