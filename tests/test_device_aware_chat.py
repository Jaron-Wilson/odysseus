"""The agent knows which device a message came from, and acts on it properly.

Seen live on 2026-09-26: from the phone, "using my phone, go to Amazon..." --
the agent first tried its own browser (broken: it wanted Google Chrome), then
sent notify_device with command=open_url. The push was delivered, but a
browser notification cannot open anything, so nothing happened. "I didn't see
anything happen" then went out low-signal, without the device tools, and the
agent told the user it had never had a way to reach their phone.
"""
import asyncio
import json
import time

import pytest

from src import devices, machines
from src.agent_loop import _is_explicit_continuation

NOW = time.time()
PEERS = [
    {"host": "jaron-dev-server", "name": "jaron-dev-server", "dns": "jaron-dev-server.tail0.ts.net",
     "ips": ["100.121.62.9"], "os": "linux", "online": True, "is_self": True, "shared": False,
     "age_days": 0, "last_seen": ""},
    {"host": "pixel-8a", "name": "Pixel 8a", "dns": "pixel-8a.tail0.ts.net", "ips": ["100.96.131.64"],
     "os": "android", "online": True, "is_self": False, "shared": False, "age_days": 0, "last_seen": ""},
    {"host": "desktop-jaron", "name": "DESKTOP-JARON", "dns": "desktop-jaron.tail0.ts.net",
     "ips": ["100.102.86.125"], "os": "windows", "online": True, "is_self": False, "shared": False,
     "age_days": 0, "last_seen": ""},
]
PIXEL = {"name": "pixel-8a", "kind": "phone", "endpoint": "http://pixel-8a.tail0.ts.net:8778",
         "commands": ["notify", "open_url", "open_app", "list_apps"], "token": "t"}


def test_message_from_the_phone_names_it_and_the_listener_route():
    info = machines.client_device("100.96.131.64", PEERS, [PIXEL])
    assert info["peer"]["name"] == "Pixel 8a" and info["device"]["name"] == "pixel-8a"
    note = machines.client_device_note(info)
    assert "from their phone **Pixel 8a** (android" in note
    assert '"this phone"' in note and '"here"' in note
    assert '"name":"pixel-8a","command":"open_url"' in note
    assert "notify_device only shows a notification" in note


def test_message_from_a_computer_says_so():
    note = machines.client_device_note(machines.client_device("100.102.86.125", PEERS, [PIXEL]))
    assert "from their computer **DESKTOP-JARON** (windows" in note and '"this computer"' in note


def test_no_note_for_loopback_this_server_or_strangers():
    for ip in ("127.0.0.1", "", "100.121.62.9", "100.99.99.99"):
        assert machines.client_device(ip, PEERS, [PIXEL]) is None, ip
    assert machines.client_device_note(None) == ""


def test_short_it_did_not_work_replies_continue_the_task():
    for t in ("I didn't se eanything happen", "didn't work", "nothing happened", "still nothing",
              "it didn't open", "doesn't work", "no luck"):
        assert _is_explicit_continuation(t), t
    assert not _is_explicit_continuation(
        "I didn't know you could do that, can you tell me how the calendar feature works in general")
    assert not _is_explicit_continuation("hello there")


@pytest.fixture
def phone(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "DEVICES_FILE", str(tmp_path / "devices.json"))
    monkeypatch.setattr(devices, "DATA_DIR", str(tmp_path))
    devices.register("pixel-8a", kind="phone", commands=PIXEL["commands"], endpoint=PIXEL["endpoint"])


def test_notify_with_open_url_goes_to_the_listener_not_a_push(phone, monkeypatch):
    from src import webpush
    from src.agent_tools.notify_tool import NotifyDeviceTool
    sent, pushed = [], []

    async def listener(device, command, params=None, timeout=10.0):
        sent.append((command, params))
        return {"ok": True, "result": {"opened": params.get("url")}}

    async def push(*a, **k):
        pushed.append(a)
        return {"sent": 1}

    monkeypatch.setattr(devices, "send_command", listener)
    monkeypatch.setattr(webpush, "send", push)
    monkeypatch.setattr(webpush, "load_subscriptions", lambda: [{"device": "pixel-8a"}])
    url = "https://www.amazon.com/s?k=galaxy+s26+ultra+case"
    r = asyncio.run(NotifyDeviceTool().execute(json.dumps(
        {"device": "pixel-8a", "message": "Cases", "command": "open_url", "command_arg": url}), {}))
    assert r["exit_code"] == 0 and "through its listener" in r["output"]
    assert sent == [("open_url", {"url": url})] and pushed == []


def test_listener_failure_falls_back_to_a_push(phone, monkeypatch):
    from src import webpush
    from src.agent_tools.notify_tool import NotifyDeviceTool
    pushed = []

    async def asleep(device, command, params=None, timeout=10.0):
        return {"ok": False, "error": "Could not reach pixel-8a"}

    async def push(*a, **k):
        pushed.append(k.get("command"))
        return {"sent": 1}

    monkeypatch.setattr(devices, "send_command", asleep)
    monkeypatch.setattr(webpush, "send", push)
    monkeypatch.setattr(webpush, "load_subscriptions", lambda: [{"device": "pixel-8a"}])
    r = asyncio.run(NotifyDeviceTool().execute(json.dumps(
        {"device": "pixel-8a", "message": "x", "command": "open_url", "command_arg": "https://a.b"}), {}))
    assert r["exit_code"] == 0 and pushed == ["open_url"]


def test_builtin_browser_uses_playwrights_chromium():
    from src.builtin_mcp import _BUILTIN_NPX_SERVERS
    args = _BUILTIN_NPX_SERVERS["builtin_browser"]["args"]
    assert args[args.index("--browser") + 1] == "chromium"


def test_agent_loop_accepts_the_client_device():
    import inspect
    from src.agent_loop import stream_agent_loop
    assert "client_device" in inspect.signature(stream_agent_loop).parameters
