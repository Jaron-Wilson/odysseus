"""Settings > Devices: the registry editable by hand, and push aliases.

The live failure this covers: the Pixel was registered as "pixel-8a", but
its browser push subscription named itself "android-phone", so
notify_device("pixel-8a") found "no matching subscriptions" and the agent
told the user the phone was unreachable. Linking the subscription name to
the device as an alias makes the push land.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import device_routes
from src import devices, webpush


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "DEVICES_FILE", str(tmp_path / "devices.json"))
    monkeypatch.setattr(devices, "DATA_DIR", str(tmp_path))
    subs = [
        {"device": "android-phone", "owner": "jaron", "endpoint": "https://fcm.example/phone"},
        {"device": "windows-desktop", "owner": "jaron", "endpoint": "https://fcm.example/pc"},
    ]
    monkeypatch.setattr(webpush, "load_subscriptions", lambda: list(subs))
    return subs


def test_rename_keeps_the_token(registry):
    rec = devices.register("pixel-8a", kind="phone", commands=["notify", "open_url"])
    token = rec["token"]
    devices.update("pixel-8a", new_name="pixel", endpoint="http://pixel-8a.tail0.ts.net:8778")
    d = devices.get("pixel")
    assert d["token"] == token
    assert d["endpoint"] == "http://pixel-8a.tail0.ts.net:8778"
    assert devices.get("pixel-8a") is None


def test_rename_refuses_a_taken_name(registry):
    devices.register("pixel-8a")
    devices.register("desktop", kind="desktop")
    with pytest.raises(ValueError):
        devices.update("pixel-8a", new_name="desktop")


def test_public_view_hides_the_token(registry):
    rec = devices.register("pixel-8a")
    view = devices.public(rec)
    assert "token" not in view
    assert view["has_token"] and view["token_hint"] == rec["token"][-4:]


def test_an_alias_belongs_to_one_device(registry):
    devices.register("pixel-8a")
    devices.register("old-phone")
    devices.add_alias("old-phone", "android-phone")
    devices.add_alias("pixel-8a", "android-phone")
    assert devices.owner_of_alias("android-phone") == "pixel-8a"
    assert "android-phone" not in (devices.get("old-phone").get("aliases") or [])


def _posted_to(monkeypatch, device):
    """Run the real webpush.send() and return the endpoints it posted to.
    Only encryption and the HTTP client are stubbed; matching is real."""
    import httpx

    posted = []

    class _Resp:
        status_code = 201
        text = ""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            posted.append(url)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(webpush, "_encrypt", lambda payload, p256dh, auth: b"x")
    monkeypatch.setattr(webpush, "_vapid_header", lambda endpoint, subject: {})
    result = asyncio.run(webpush.send("Odysseus", "hi", device=device))
    return posted, result


def test_push_to_a_registered_phone_reaches_its_linked_subscription(registry, monkeypatch):
    for s in registry:                      # send() reads the key fields
        s.update({"p256dh": "k", "auth": "a"})
    devices.register("pixel-8a")

    # Before linking: the live bug.
    posted, result = _posted_to(monkeypatch, "pixel-8a")
    assert posted == [] and result["detail"] == "no matching subscriptions"

    devices.add_alias("pixel-8a", "android-phone")
    posted, result = _posted_to(monkeypatch, "pixel-8a")
    assert posted == ["https://fcm.example/phone"]
    assert result["sent"] == 1


def _client(monkeypatch):
    app = FastAPI()
    app.include_router(device_routes.setup_device_routes())
    monkeypatch.setattr(device_routes, "require_admin", lambda request: None)
    return TestClient(app)


def test_routes_add_list_link_remove(registry, monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/devices", json={"name": "pixel-8a", "kind": "phone",
                                     "endpoint": "http://pixel-8a.tail0.ts.net:8778",
                                     "commands": ["notify", "open_url"]})
    assert r.status_code == 200, r.text
    assert "token" not in r.json()["device"]

    listing = c.get("/api/devices").json()
    assert [d["name"] for d in listing["devices"]] == ["pixel-8a"]
    sub = next(s for s in listing["subscriptions"] if s["device"] == "android-phone")
    assert sub["linked_to"] is None

    assert c.post("/api/devices/pixel-8a/aliases", json={"alias": "android-phone"}).status_code == 200
    sub = next(s for s in c.get("/api/devices").json()["subscriptions"] if s["device"] == "android-phone")
    assert sub["linked_to"] == "pixel-8a"

    tok = c.post("/api/devices/pixel-8a/token").json()["token"]
    assert tok == devices.get("pixel-8a")["token"]

    assert c.delete("/api/devices/pixel-8a").status_code == 200
    assert c.get("/api/devices").json()["devices"] == []


def test_routes_refuse_bad_input(registry, monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/devices", json={"name": "bad/name"}).status_code == 400
    assert c.post("/api/devices", json={"name": "ok", "endpoint": "http://8.8.8.8:8778"}).status_code == 400
    c.post("/api/devices", json={"name": "ok"})
    assert c.post("/api/devices", json={"name": "ok"}).status_code == 409
    assert c.delete("/api/devices/nope").status_code == 404


def test_routes_are_admin_only(registry):
    app = FastAPI()
    app.include_router(device_routes.setup_device_routes())
    # No monkeypatch: the real require_admin runs, with no auth manager set up.
    c = TestClient(app)
    import os
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        pytest.skip("auth disabled in this environment")
    assert c.get("/api/devices").status_code in (401, 403)
