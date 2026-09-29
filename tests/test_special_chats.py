"""Chats with a standing job: MCP Maker and Odysseus development.

Asked for on 2026-09-29: "what if I see an app does not have an MCP and then I
can say generate with AI and task it off to an OpenCode or a Claude Code
session? generates a chat called like MCP MAKER - DEVICE IT'S ON", and "maybe
a designated Odysseus chat that is set on just modifying the website".
"""
import asyncio
import os

import pytest
from fastapi import HTTPException

from core.database import init_db
from core.models import set_session_manager_instance, get_session_manager_instance
from core.session_manager import SessionManager
import routes.special_chat_routes as scr
from src import chat_memory

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {"content-type": "application/json"}

    async def json(self):
        return self._body


@pytest.fixture
def env(tmp_path, monkeypatch):
    init_db()
    sm = SessionManager()
    prev = get_session_manager_instance()
    set_session_manager_instance(sm)
    monkeypatch.setattr(chat_memory, "MEMORY_FILE", str(tmp_path / "chat_memory.json"), raising=False)
    for attr in ("DATA_DIR",):
        if hasattr(chat_memory, attr):
            monkeypatch.setattr(chat_memory, attr, str(tmp_path))
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    import core.middleware as mw
    monkeypatch.setattr(mw, "require_admin", lambda request: None)
    import src.auth_helpers as ah
    monkeypatch.setattr(ah, "effective_user", lambda request: "jaron")
    monkeypatch.setattr(scr, "_repo", lambda: "Jaron-Wilson/odysseus")
    monkeypatch.setattr(scr, "_workspaces", lambda: "/data/workspaces")
    turns = []
    import src.screen_control_resume as res
    monkeypatch.setattr(res, "start_turn", lambda sid, prompt, **kw: turns.append((sid, prompt, kw)) or True)
    from src import device_routing, machines
    monkeypatch.setattr(device_routing, "all_devices", lambda mgr: [
        {"server_id": "win", "name": "windows-desktop", "host": "100.102.86.125"}])
    monkeypatch.setattr(machines, "peers", lambda refresh=False: [{"host": "desktop-jaron", "os": "windows", "ips": ["100.102.86.125"]}])
    monkeypatch.setattr(machines, "find_peer", lambda ps, h: ps[0] if h == "100.102.86.125" else None)
    router = scr.setup_special_chat_routes(sm, get_mcp_manager=lambda: None)
    routes = {r.path: r.endpoint for r in router.routes}
    yield sm, routes, turns
    set_session_manager_instance(prev)


MODEL = {"endpoint_url": "http://192.168.100.101:8114/v1", "model": "qwen3.8-27b"}


def test_mcp_maker_opens_a_named_chat_and_starts_the_agent(env):
    sm, routes, turns = env
    r = asyncio.run(routes["/api/special-chats/mcp-maker"](_Req(dict(MODEL, server_id="win", app="OBS Studio"))))
    assert r["name"] == "MCP Maker · OBS Studio · windows-desktop" and r["started"]
    sid, prompt, kw = turns[-1]
    assert sid == r["id"] and kw["note_source"] == "mcp_maker"
    assert prompt.startswith("[MCP maker · OBS Studio on windows-desktop]")
    assert "tools/mcp/obs_studio_mcp_server.py" in prompt and "gh pr create -R Jaron-Wilson/odysseus -B dev" in prompt
    assert "desktop_mcp_server.py" in prompt                              # the Windows conventions
    notes = [i["text"] for i in chat_memory.get(r["id"])["items"]]
    assert len(notes) == 2 and "branch mcp-obs_studio" in notes[0] and "Never merge" in notes[1]
    with pytest.raises(HTTPException):
        asyncio.run(routes["/api/special-chats/mcp-maker"](_Req(dict(MODEL, server_id="nope", app="X"))))
    with pytest.raises(HTTPException):
        asyncio.run(routes["/api/special-chats/mcp-maker"](_Req({"server_id": "win", "app": "X"})))   # no model


def test_odysseus_dev_is_one_chat_with_its_rules_pinned(env):
    sm, routes, turns = env
    first = asyncio.run(routes["/api/special-chats/odysseus-dev"](_Req(MODEL)))
    again = asyncio.run(routes["/api/special-chats/odysseus-dev"](_Req(MODEL)))
    assert first["created"] and not again["created"] and first["id"] == again["id"]
    assert first["name"] == "Odysseus development" and turns == []        # waits for the user
    notes = [i["text"] for i in chat_memory.get(first["id"])["items"]]
    assert any("/data/workspaces/odysseus" in n and "Never edit the live install" in n for n in notes)
    assert any("gh pr create -R Jaron-Wilson/odysseus -B dev" in n and "Never merge" in n for n in notes)
    assert all(len(n) <= chat_memory.MAX_ITEM_CHARS for n in notes)
    assert sm.get_session(first["id"]).history[-1].metadata["source"] == "odysseus_dev_intro"


def test_the_page_wires_them_in():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    assert 'id="tool-odysseus-dev-btn"' in read("static", "index.html")
    assert "import './specialChats.js';" in read("static", "js", "chat.js")
    assert "data-pc-mcp=" in read("static", "js", "devicesSettings.js")
    assert "setup_special_chat_routes(session_manager)" in read("app.py")
