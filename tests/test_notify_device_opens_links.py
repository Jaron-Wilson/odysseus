"""notify_device: open the link directly when a push cannot land, and say why.

Seen live on 2026-09-25: "open this page on my phone" went out as a push to
"pixel-8a", found no subscription under that name ("no matching
subscriptions"), and the model then invented a cause -- "the WebSocket drops
when the phone locks" -- while the phone's listener was up and able to open
the link directly with the token Odysseus already stores.
"""
import asyncio
import json

import pytest

from src import devices, webpush
from src.agent_tools.notify_tool import NotifyDeviceTool

URL = "https://gloo-hackathon2026.example.workers.dev"


@pytest.fixture
def phone(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "DEVICES_FILE", str(tmp_path / "devices.json"))
    monkeypatch.setattr(devices, "DATA_DIR", str(tmp_path))
    devices.register("pixel-8a", kind="phone", commands=["notify", "open_url"],
                     endpoint="http://pixel-8a.tail0.ts.net:8778")
    # A subscription exists, but under the browser's own name, not linked.
    monkeypatch.setattr(webpush, "load_subscriptions",
                        lambda: [{"device": "android-phone", "endpoint": "https://fcm/x"}])

    async def no_match(*a, **k):
        return {"sent": 0, "failed": 0, "detail": "no matching subscriptions"}

    monkeypatch.setattr(webpush, "send", no_match)
    return devices.get("pixel-8a")


def _run(args):
    return asyncio.run(NotifyDeviceTool().execute(json.dumps(args), {"owner": "jaron"}))


def test_link_opens_directly_when_the_push_has_nowhere_to_go(phone, monkeypatch):
    sent = []

    async def fake_command(device, command, params=None, timeout=10.0):
        sent.append((device["name"], command, params))
        return {"ok": True, "result": {"ok": True, "opened": params["url"]}}

    monkeypatch.setattr(devices, "send_command", fake_command)
    r = _run({"device": "pixel-8a", "message": "Your site is live", "click": URL})
    assert r["exit_code"] == 0 and r["opened"] == URL
    assert sent == [("pixel-8a", "open_url", {"url": URL})]


def test_without_a_link_the_error_is_the_real_reason(phone):
    r = _run({"device": "pixel-8a", "message": "Your site is live"})
    assert r["exit_code"] == 1
    assert "Settings → Devices" in r["error"]
    assert '"command":"open_url"' in r["error"]
    assert "do not guess another" in r["error"]


def test_no_fallback_for_a_device_that_cannot_open_links(phone, monkeypatch):
    devices.update("pixel-8a", commands=["notify"])
    called = []

    async def fake_command(*a, **k):
        called.append(a)
        return {"ok": True}

    monkeypatch.setattr(devices, "send_command", fake_command)
    r = _run({"device": "pixel-8a", "message": "hi", "click": URL})
    assert r["exit_code"] == 1 and called == []


def test_a_failed_direct_open_still_reports_the_push_reason(phone, monkeypatch):
    async def asleep(device, command, params=None, timeout=10.0):
        return {"ok": False, "error": "Could not reach pixel-8a"}

    monkeypatch.setattr(devices, "send_command", asleep)
    r = _run({"device": "pixel-8a", "message": "hi", "click": URL})
    assert r["exit_code"] == 1 and "Settings → Devices" in r["error"]
