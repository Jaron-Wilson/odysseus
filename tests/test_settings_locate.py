"""The server side of "take me to ..." in Settings.

Asked for 2026-09-30: "i want to be able to say bring me to the ai voice
settings ... i want to be able to ask a small helper model to get it to take
me to that page please." The page (static/js/settingsNav.js) matches the
request itself first; when it isn't sure, POST /api/settings/locate has the
Utility model pick one of the places the page listed. What matters:

  * the model's answer is a number from the list, checked, so a made-up
    place never comes back;
  * with no Utility (or Default Chat) model set up, the page is told so and
    lists the matches instead;
  * the chat agent can do it too: `ui_control open_panel settings <what>`
    hands the page what to open.

The model is mocked; nothing here talks to a real endpoint.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings_locate
import routes.settings_locate_routes as r

CANDS = [
    {"id": "voice-call-settings", "path": "AI Defaults > Voice call"},
    {"id": "set-vcStt", "path": "AI Defaults > Voice call > Hears with"},
    {"id": "set-vcTts", "path": "AI Defaults > Voice call > Speaks with"},
    {"id": "sms-card", "path": "Devices > Phone SMS"},
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(r, "require_authenticated_request", lambda req: None)
    monkeypatch.setattr(r, "effective_user", lambda req: "jaron")
    app = FastAPI()
    app.include_router(r.setup_settings_locate_routes())
    return TestClient(app)


@pytest.fixture
def model(monkeypatch):
    """A stand-in Utility model: records what it was sent, answers `reply`."""
    seen = {"calls": [], "reply": "2", "owner": None}

    def endpoint(owner):
        seen["owner"] = owner
        return "http://llm.test/v1/chat/completions", "tiny-model", {}

    async def call(**kw):
        seen["calls"].append(kw)
        return seen["reply"]

    monkeypatch.setattr(settings_locate, "_endpoint", endpoint)
    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", call)
    return seen


def test_the_model_picks_one_of_the_listed_places(client, model):
    res = client.post("/api/settings/locate", json={"query": "where do I change the speech to text engine", "candidates": CANDS})
    assert res.status_code == 200
    assert res.json() == {"id": "set-vcStt", "model": True}
    sent = model["calls"][0]
    assert sent["model"] == "tiny-model" and sent["temperature"] == 0
    user = sent["messages"][-1]["content"]
    assert "speech to text engine" in user
    assert "2. AI Defaults > Voice call > Hears with" in user
    assert model["owner"] == "jaron"


@pytest.mark.parametrize("reply,expected", [
    ("4", "sms-card"),
    ("<think>the user wants texts</think>\n4", "sms-card"),
    ("Number: 1", "voice-call-settings"),
    ("0", None),            # none fits
    ("9", None),            # not on the list
    ("set-vcTts", None),    # an id, not a number: not trusted
    ("", None),
])
def test_the_answer_is_checked_against_the_list(client, model, reply, expected):
    model["reply"] = reply
    res = client.post("/api/settings/locate", json={"query": "sms", "candidates": CANDS})
    assert res.json()["id"] == expected


def test_no_model_set_up_says_so(client, monkeypatch):
    monkeypatch.setattr(settings_locate, "_endpoint", lambda owner: (None, None, None))
    res = client.post("/api/settings/locate", json={"query": "voice", "candidates": CANDS})
    assert res.json() == {"id": None, "model": False}


def test_a_model_that_fails_is_not_an_error_page(client, model, monkeypatch):
    async def boom(**kw):
        raise RuntimeError("connection refused")
    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", boom)
    res = client.post("/api/settings/locate", json={"query": "voice", "candidates": CANDS})
    assert res.status_code == 200
    assert res.json()["id"] is None and res.json()["model"] is True


def test_bad_requests_are_refused(client, model):
    assert client.post("/api/settings/locate", json={"query": "voice"}).status_code == 400
    assert client.post("/api/settings/locate", json={"query": "", "candidates": CANDS}).status_code == 400
    assert client.post("/api/settings/locate", content="not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert model["calls"] == []


def test_candidates_are_cleaned_and_capped():
    raw = [{"id": "a", "path": "A"}, {"id": "a", "path": "again"}, {"id": "", "path": "x"}, "junk",
           {"id": "b", "path": "  B \n > c  "}] + [{"id": f"n{i}", "path": "P"} for i in range(500)]
    out = settings_locate.clean_candidates(raw)
    assert out[:2] == [{"id": "a", "path": "A"}, {"id": "b", "path": "B > c"}]
    assert len(out) == settings_locate.MAX_CANDIDATES
    assert settings_locate.clean_candidates("nope") == []


def test_the_route_needs_a_signed_in_user(monkeypatch, model):
    from fastapi import HTTPException

    def refuse(req):
        raise HTTPException(401, "Not signed in")
    monkeypatch.setattr(r, "require_authenticated_request", refuse)
    app = FastAPI()
    app.include_router(r.setup_settings_locate_routes())
    res = TestClient(app).post("/api/settings/locate", json={"query": "voice", "candidates": CANDS})
    assert res.status_code == 401
    assert model["calls"] == []


# ── The chat agent's way in: ui_control open_panel settings <what> ──────

def test_ui_control_open_panel_settings_carries_the_target():
    from src.ai_interaction import do_ui_control
    out = asyncio.run(do_ui_control("open_panel settings speech to text engine"))
    assert out["ui_event"] == "open_panel" and out["panel"] == "settings"
    assert out["settings_target"] == "speech to text engine"
    plain = asyncio.run(do_ui_control("open_panel settings"))
    assert plain["panel"] == "settings" and "settings_target" not in plain
    other = asyncio.run(do_ui_control("open_panel gallery cats"))
    assert other["panel"] == "gallery" and "settings_target" not in other


def test_a_native_tool_call_passes_the_setting_through():
    import src.agent_tools  # noqa: F401  (tool_schemas imports through it)
    from src.tool_schemas import function_call_to_tool_block
    block = function_call_to_tool_block("ui_control", json.dumps(
        {"action": "open_panel", "name": "settings", "value": "voice call settings"}))
    assert block.content == "open_panel settings voice call settings"
    block = function_call_to_tool_block("ui_control", json.dumps({"action": "open_panel", "name": "gallery"}))
    assert block.content == "open_panel gallery"
