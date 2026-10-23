"""The core's own way to text you (routes/sms_routes.py), the built-in
`messaging.sms` provider while no Phone & Messages mod is installed.

Texting the agent (the inbound webhook, commands, conversations) is the
Phone & Messages mod now, with its own tests; here: the settings, a text
going out through the phone's send endpoint or a web push, and that the
inbound path is gone from the core.
"""
import os
import re

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routes import prefs_routes, sms_routes

ALICE_NUM = "+15550102000"
BOB_NUM = "+15550103000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    posted, pushed = [], []

    async def fake_post(url, payload):
        posted.append((url, payload))
        return 200

    monkeypatch.setattr(sms_routes, "_post_reply", fake_post)
    from src import webpush
    monkeypatch.setattr(webpush, "load_subscriptions", lambda: [
        {"endpoint": "https://push/alice", "owner": "alice"},
        {"endpoint": "https://push/bob", "owner": "bob"},
    ])

    async def fake_send(title, body, **kw):
        pushed.append({"body": body, **kw})
        return {"sent": 1, "failed": 0}

    monkeypatch.setattr(webpush, "send", fake_send)
    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(sms_routes.setup_sms_routes())
    with TestClient(app) as client:
        def setup(user, number, reply_url=""):
            numbers = number if isinstance(number, list) else [number]
            r = client.put("/api/sms/config", headers={"x-test-user": user},
                           json={"numbers": numbers, "reply_url": reply_url})
            assert r.status_code == 200
            return r.json()
        yield {"client": client, "setup": setup, "posted": posted, "pushed": pushed}


def test_settings_are_per_user_and_validated(env):
    c = env["client"]
    got = env["setup"]("alice", ["555.010.2000"], "http://sms:pw@100.84.92.36:8080/message")
    assert got["numbers"] == [ALICE_NUM] and got["reply_url"].endswith(":8080/message")
    assert "Phone & Messages" in got["mod_hint"]
    assert c.get("/api/sms/config", headers={"x-test-user": "bob"}).json()["numbers"] == []
    assert c.put("/api/sms/config", headers={"x-test-user": "alice"},
                 json={"numbers": ["12"]}).status_code == 400
    assert c.put("/api/sms/config", headers={"x-test-user": "alice"},
                 json={"numbers": [ALICE_NUM], "reply_url": "ftp://x"}).status_code == 400


def test_the_old_gateways_secret_is_reported_not_used(env, tmp_path):
    prefs_routes._save_for_user("alice", {"sms_gateway": {"numbers": [ALICE_NUM], "secret_hash": "ab" * 32}})
    got = env["client"].get("/api/sms/config", headers={"x-test-user": "alice"}).json()
    assert got["had_gateway"] is True and "secret_hash" not in got


async def test_deliver_goes_to_the_reply_url(env):
    cfg = env["setup"]("alice", [ALICE_NUM], "http://sms:pw@100.84.92.36:8080/message")
    assert await sms_routes.deliver("alice", cfg, ALICE_NUM, "Print finished") == "reply_url"
    url, payload = env["posted"][0]
    assert payload == {"textMessage": {"text": "Print finished"}, "phoneNumbers": [ALICE_NUM]}


async def test_without_a_reply_url_it_is_a_push_to_the_owners_browsers(env):
    cfg = env["setup"]("alice", [ALICE_NUM])
    assert await sms_routes.deliver("alice", cfg, ALICE_NUM, "hello") == "push"
    assert env["posted"] == [] and [p["endpoint"] for p in env["pushed"]] == ["https://push/alice"]


def test_long_texts_are_split_into_numbered_parts():
    assert sms_routes.split_reply("short") == ["short"]
    long = " ".join(f"Sentence number {i} is here." for i in range(200))
    parts = sms_routes.split_reply(long)
    assert len(parts) == sms_routes.MAX_PARTS
    assert all(len(p) <= sms_routes.REPLY_CHARS for p in parts)
    assert parts[0].startswith("(1/6) ") and parts[-1].endswith(sms_routes.CONTINUED)


def test_sending_for_mods_is_the_capability_not_this_module():
    # The broker (src/mods/capabilities.py) applies the number rules and
    # sends with deliver(); this module has no send API of its own.
    assert not hasattr(sms_routes, "send_text")


def test_test_text_reaches_every_number_with_a_reply_url(env):
    env["setup"]("alice", [ALICE_NUM, BOB_NUM], reply_url="http://sms:pw@100.64.0.9:8080/message")
    body = env["client"].post("/api/sms/test", headers={"x-test-user": "alice"}).json()
    assert body["ok"] is True and [x["to"] for x in body["results"]] == [ALICE_NUM, BOB_NUM]


def test_test_text_pushes_once_without_a_reply_url(env):
    env["setup"]("alice", [ALICE_NUM, BOB_NUM])
    body = env["client"].post("/api/sms/test", headers={"x-test-user": "alice"}).json()
    assert body["ok"] is True and [x["to"] for x in body["results"]] == [ALICE_NUM]
    assert len(env["pushed"]) == 1


def test_the_inbound_gateway_is_gone_from_the_core(env):
    c = env["client"]
    assert c.post("/api/sms/inbound/" + "a" * 43, json={"from": ALICE_NUM, "text": "help"}).status_code in (404, 405)
    assert c.post("/api/sms/secret", headers={"x-test-user": "alice"}).status_code in (404, 405)
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "/api/sms/inbound" not in src
    # Mods' public paths are the only webhook pattern of that kind now.
    pat = re.compile(re.search(r'_re\.compile\(r"(\^/mods/\[a-z\][^"]+/public/[^"]*)"\)', src).group(1))
    assert pat.match("/mods/phone/public/sms/inbound/" + "a" * 43)
    assert not pat.match("/mods/phone/settings") and not pat.match("/mods/phone/~tok.sig/public/x")


@pytest.mark.parametrize("raw,want", [
    ("+1 (555) 010-2000", ALICE_NUM), ("555.010.2000", ALICE_NUM), ("15550102000", ALICE_NUM),
    ("+44 7700 900123", "+447700900123"), ("0044 7700 900123", "+447700900123"),
    ("22395", "22395"), ("", ""), ("abc", ""),
])
def test_number_normalization(raw, want):
    assert sms_routes.normalize_number(raw) == want
