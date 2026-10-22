"""My Mod under the stand-in core (odysseus_mod.devcore), as Odysseus would
run it: through the proxy with a signed identity, the tool, the events.
Run from the mod folder: python -m pytest -q"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odysseus_mod.devcore import DevCore  # noqa: E402


def get(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    with open(os.path.join(ROOT, "odysseus-mod.json")) as f:
        mod_id = json.load(f)["id"]
    c = DevCore(ROOT, port=0, user="alice", state_dir=str(tmp_path_factory.mktemp("devcore")))
    c.serve()
    c.start_mod()
    c.mod_id_for_tests = mod_id
    yield c
    c.close()


def test_health(core):
    assert get(f"http://127.0.0.1:{core.mod_port}/health")[0] == 200


def test_direct_unsigned_requests_are_refused(core):
    assert get(f"http://127.0.0.1:{core.mod_port}/")[0] == 401


def test_page_through_the_proxy_sees_the_user(core):
    code, body = get(f"{core.url}/mods/{core.mod_id}/")
    assert code == 200 and b"alice" in body


def test_card_and_settings(core):
    assert get(f"{core.url}/mods/{core.mod_id}/cards/summary")[0] == 200
    assert get(f"{core.url}/mods/{core.mod_id}/settings")[0] == 200


def test_tools(core):
    tools = core.list_tools()
    assert len(tools) == 1 and tools[0]["function"]["name"].endswith("_count")
    out = core.call_tool(tools[0]["function"]["name"], {})
    assert "clicked" in out["output"]


def test_click_notifies_and_emits(core):
    code, body = get(f"{core.url}/mods/{core.mod_id}/api/click", "POST", {})
    assert code == 200 and json.loads(body)["clicks"] >= 1
    assert core.notifications and core.notifications[-1]["title"] == "Clicked"
    assert core.events and core.events[-1]["type"].endswith(".clicked")


def test_manifest_has_a_tutorial_written_for_this_mod(core):
    """Every mod ships a tutorial (shown the first time someone opens it). Its
    steps describe this mod's real features, and its actions point at pages
    the mod has."""
    from odysseus_mod.devcore import tutorial_problems
    assert tutorial_problems(core.manifest, ROOT) == []
    tut = core.manifest["tutorial"]
    assert 1 <= len(tut["steps"]) <= 8 and isinstance(tut.get("version", 1), int)
    paths = {t["path"] for t in core.manifest["ui"]["tabs"]}
    for s in tut["steps"]:
        assert s["title"].strip() and s["body"].strip()
        a = s.get("action") or {}
        if a.get("settings"):
            assert core.manifest["ui"].get("settings_path")
        if a.get("path"):
            assert get(f"{core.url}/mods/{core.mod_id}{a['path']}")[0] == 200 or a["path"] in paths


def test_tool_list_refresh(core):
    code, body = get(f"{core.url}/api/mods/v1/tools/refresh", "POST", {"wait": True},
                     {"Authorization": f"Bearer {core.token}"})
    assert code == 200 and json.loads(body)["tools"] and core.tool_refreshes >= 1


def test_notification_links_into_the_tab(core):
    get(f"{core.url}/mods/{core.mod_id}/api/click", "POST", {})
    assert core.notifications[-1]["url"].startswith(f"/#mod/{core.mod_id}/")


def test_subscribed_events_are_delivered(core):
    ev = {"id": "x", "source": "print-hub", "type": "print.finished", "data": {}, "ts": time.time()}
    core._deliver(ev)
    code, body = get(f"{core.url}/mods/{core.mod_id}/cards/summary")
    assert b"1 events" in body or b"events" in body


def test_capabilities_under_devcore(core):
    """devcore brokers capability calls like the core: only declared ones,
    and messaging.sms only to the user's own number."""
    auth = {"Authorization": f"Bearer {core.token}"}
    url = f"{core.url}/api/mods/v1/cap/messaging.sms/send"
    assert get(url, "POST", {"text": "hi"}, auth)[0] == 403          # not in requires/optional
    core.manifest["optional"] = ["messaging.sms"]
    try:
        code, body = get(url, "POST", {"text": "hi"}, auth)
        assert code == 200 and json.loads(body)["to"] == core.SMS_NUMBER and core.texts[-1]["text"] == "hi"
        assert get(url, "POST", {"text": "hi", "to": "+15550109999"}, auth)[0] == 403
        caps = json.loads(get(f"{core.url}/api/mods/v1/cap", headers=auth)[1])
        assert caps["optional"][0]["available"] and caps["missing"] == []
    finally:
        core.manifest.pop("optional", None)
