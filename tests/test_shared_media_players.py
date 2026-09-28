"""Shared media always gets a player in the chat.

Seen live on 2026-09-28 ("for music when it pulls it up I don't see how to
play it"): the agent shared six tracks from the PC with share_media and then
listed them without links, first as a table, then as bare paths.
"""
import asyncio
import json
import os
import shutil
import subprocess

import pytest

import src.agent_loop as al

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_loop_links_what_share_media_returned(monkeypatch):
    from src.agent_tools import ToolBlock
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    link = "/api/device-media/Xokjm-PB/05_Along-The-Way_95bpm.mp3"

    async def fake_exec(block, *a, **k):
        return (block.tool_type, {"stdout": json.dumps({"ok": True, "name": "05_Along-The-Way_95bpm.mp3", "link": link}), "stderr": "", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", fake_exec, raising=False)
    real = al._resolve_tool_blocks

    def resolve(text, native, round_num, **kw):
        if "SHARE" in text:
            return [ToolBlock("mcp__19d772b0__share_media", '{"path": "C:/m/05.mp3"}')], True
        return real(text, native, round_num, **kw)
    monkeypatch.setattr(al, "_resolve_tool_blocks", resolve)
    n = {"i": 0}

    async def fake_stream(_c, messages, **kw):
        n["i"] += 1
        text = "Sharing it. SHARE" if n["i"] == 1 else "Here is my top pick: Along The Way."
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    async def run():
        return [c async for c in al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "find me music"}], max_rounds=3,
            relevant_tools={"mcp__19d772b0__share_media"})]
    deltas = "".join(json.loads(c[6:]).get("delta", "") for c in asyncio.run(run())
                     if c.startswith("data: {"))
    assert f"[05_Along-The-Way_95bpm.mp3]({link})" in deltas


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_bare_media_paths_are_found():
    src = open(os.path.join(HERE, "static", "js", "markdown.js"), encoding="utf-8").read()
    i = src.index("const _BARE_MEDIA_RE =")
    line = src[i:src.index("\n", i)]
    reply = ("**05 · Along The Way** (95 BPM)\n"
             "/api/device-media/Xokjm-PB-WPNt5Vxp2-KhwCB/05_Along-The-Way_AlexProductions_95bpm.mp3\n\n"
             "(see /api/chat-media/teaser.mp4) and /api/device-media/x/notes.txt and "
             "https://example.com/api/device-media/a/b.mp3")
    script = line + "\nconst t = " + json.dumps(reply) + ";\n" \
        "console.log(JSON.stringify([...t.matchAll(_BARE_MEDIA_RE)].map(m => m[2])));"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert json.loads(r.stdout) == [
        "/api/device-media/Xokjm-PB-WPNt5Vxp2-KhwCB/05_Along-The-Way_AlexProductions_95bpm.mp3",
        "/api/chat-media/teaser.mp4"], r.stderr


def test_one_player_per_file():
    js = open(os.path.join(HERE, "static", "js", "markdown.js"), encoding="utf-8").read()
    assert "if (played.has(href)) continue;" in js and "_linkBareMediaPaths(container);" in js
