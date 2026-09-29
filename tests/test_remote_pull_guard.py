"""The agent does not copy big files from the user's machines onto this server.

Seen 2026-09-29: with the PC's desktop MCP down, the agent set out to "fetch
it over the tailnet ... and then stream it from here" for a render the user
said is 63 GB, onto a server with under 7 GB free: "why is it pulling the
video instead of doing the hosting cause my server does not have much
storage".
"""
import asyncio
import os

import pytest

from src.agent_tools.remote_pull_guard import refusal
from src.agent_tools.subprocess_tools import BashTool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("cmd", [
    "scp -i ~/.ssh/odysseus_mcp jaron@100.102.86.125:'C:/Users/jaron/Videos/CostaRica/renders/final.mp4' /tmp/",
    "scp -r jaron@pc:C:/Users/jaron/Videos/CostaRica /tmp/x",
    "rsync -av jaron@laptop:~/Videos/ ./v",
    "ssh -i k jaron@100.102.86.125 'type C:\\Videos\\a.mp4' > a.mp4",
    "ssh jaron@pc 'tar c Videos' | tar x",
    "curl -o v.mp4 http://100.102.86.125:8931/media/abc",
    "cd /tmp && scp jaron@pc:D:/renders/story.mov .",
    "sftp jaron@pc",
])
def test_pulls_of_media_or_folders_are_refused(cmd):
    msg = refusal(cmd)
    assert msg and "share_media" in msg and "Settings > Devices" in msg


@pytest.mark.parametrize("cmd", [
    "scp jaron@100.102.86.125:C:/logs/out.log /tmp/",               # a log is fine
    "scp ./file.mp4 jaron@pc:C:/tmp/",                               # sending, not pulling
    "rsync jaron@laptop:~/notes.txt .",
    "ssh -i k jaron@100.102.86.125 'dir /s C:\\Users\\jaron\\Videos\\*.mp4 2^>nul'",   # a listing
    "ssh jaron@pc 'ls ~/Videos' > list.txt",
    "curl http://100.102.86.125:8931/sse",
    "ls -la ~/odysseus-data && df -h",
])
def test_everything_else_runs(cmd):
    assert refusal(cmd) is None


def test_the_bash_tool_does_not_run_a_refused_pull(tmp_path):
    marker = tmp_path / "ran"
    out = asyncio.run(BashTool().execute(
        f"touch {marker}; scp jaron@pc:C:/Videos/big.mp4 {tmp_path}/", {"workspace": str(tmp_path)}))
    assert out["exit_code"] == 1 and "share_media" in out["error"]
    assert not marker.exists()


def test_the_prompt_says_to_ask_not_copy():
    src = open(os.path.join(ROOT, "src", "agent_loop.py"), encoding="utf-8").read()
    assert "do NOT fetch the file over the tailnet (no scp, rsync, or ssh cat)" in src
    assert "Windows and Linux alike, has share_media" in src           # "the Windows PC can't stream": wrong
    enroll = open(os.path.join(ROOT, "routes", "enroll_routes.py"), encoding="utf-8").read()
    assert enroll.count('"Play its videos in the chat": "share_media"') == 2   # Devices offers the update
