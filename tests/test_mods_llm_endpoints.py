"""Models served by an add-on (src/mods/llm_endpoints.py): a mod://<id>/v1
endpoint is resolved to the mod only when a request is sent, signed for the
chat's user, and says "Install the add-on" when the mod is missing. The
mod keeps its endpoint rows through the mod API. Nothing leaves the machine."""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.mods import llm_endpoints as le, registry
from tests.mod_llm_helpers import (  # noqa: F401  (fixtures)
    MOD_BASE, MOD_ID, MOD_URL, MODEL, drop_endpoint, install_fake_mod, mod_endpoint, mod_llm, mods_dir,
)
from tests.helpers.sqlite_db import make_temp_sqlite

_DB = {}


@pytest.fixture(autouse=True)
def _own_db(monkeypatch):
    import core.database as cdb
    if _DB.get("base") is not cdb.Base:
        ts, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
        _DB.update(base=cdb.Base, ts=ts, engine=engine, tmp=tmp)
    monkeypatch.setattr(cdb, "SessionLocal", _DB["ts"])


def _drain(agen):
    async def go():
        return [c async for c in agen]
    return asyncio.run(go())


def test_mod_urls():
    assert le.is_mod_url(MOD_URL) and le.mod_id_of(MOD_URL) == MOD_ID
    assert not le.is_mod_url("https://api.openai.com/v1") and le.mod_id_of("http://x") is None
    assert le.mod_id_of("mod://Bad_Id/v1") is None
    from src.endpoint_resolver import build_chat_url, build_models_url, resolve_url, uses_request_scoped_bearer
    assert resolve_url(MOD_BASE) == MOD_BASE
    assert build_chat_url(MOD_BASE) == MOD_URL
    assert build_models_url(MOD_BASE) == MOD_BASE + "/models"
    assert uses_request_scoped_bearer(MOD_URL)


def test_provider_name_billing_and_cap_come_from_the_manifest(mod_llm):
    from src.llm_core import _detect_provider, _provider_label
    from src import pricing, tool_cap
    assert _detect_provider(MOD_URL) == "chatgpt-plan"
    assert _provider_label(MOD_URL) == "ChatGPT"
    assert pricing.classify(MOD_BASE) == "subscription"
    assert tool_cap.needs_tool_cap(MOD_URL)


def test_a_mod_cannot_borrow_a_protocol_name(mods_dir):
    install_fake_mod(meta={"usage_provider": "anthropic", "billing": "free-money"})
    m = le.meta(MOD_ID)
    assert m["usage_provider"] == "mod:chatgpt" and m["billing"] == "" and m["tool_cap"] is False
    from src.llm_core import _detect_provider
    assert _detect_provider(MOD_URL) == "mod:chatgpt"


def test_stream_goes_to_the_mod_signed_for_the_owner(mod_llm):
    from src.llm_core import stream_llm
    chunks = _drain(stream_llm(MOD_URL, MODEL, [{"role": "user", "content": "hi"}],
                               headers={"Authorization": "Bearer stale"}, owner="alice"))
    text = "".join(chunks)
    assert "Working on it." in text and "[DONE]" in text
    call = mod_llm.chat_calls()[-1]
    assert call["path"] == "/mod/cap/llm.provider/v1/chat/completions"
    assert call["user"] == "alice" and call["auth"] is None
    assert call["json"]["model"] == MODEL and call["json"]["stream"] is True


def test_blocking_calls_reach_the_mod(mod_llm):
    from src.llm_core import llm_call, llm_call_async
    assert llm_call(MOD_URL, MODEL, [{"role": "user", "content": "hi"}], owner="bob") == "Working on it."
    assert mod_llm.chat_calls()[-1]["user"] == "bob"
    mod_llm.reply = "async ok"
    out = asyncio.run(llm_call_async(MOD_URL, MODEL, [{"role": "user", "content": "yo"}], owner="carol"))
    assert out == "async ok" and mod_llm.chat_calls()[-1]["user"] == "carol"


def test_without_the_mod_the_answer_is_install_the_add_on(mods_dir):
    from fastapi import HTTPException
    from src.llm_core import llm_call, stream_llm
    text = "".join(_drain(stream_llm(MOD_URL, MODEL, [{"role": "user", "content": "hi"}], owner="alice")))
    assert "event: error" in text
    assert "not installed" in text and "Settings > Mods > Browse" in text
    with pytest.raises(HTTPException) as e:
        llm_call(MOD_URL, MODEL, [{"role": "user", "content": "no add-on here"}], owner="alice")
    assert e.value.status_code == 404


def test_a_turned_off_mod_says_so(mods_dir):
    install_fake_mod(enabled=False)
    with pytest.raises(le.ModEndpointUnavailable) as e:
        le.resolve(MOD_URL)
    assert "turned off" in e.value.message


def test_model_list_and_probe_ask_the_mod(mod_llm):
    from src.llm_core import list_model_ids
    from routes.model_routes import _probe_endpoint
    assert _probe_endpoint(MOD_BASE) == [MODEL]
    assert mod_llm.calls[-1]["path"] == "/mod/cap/llm.provider/v1/models"
    assert list_model_ids(MOD_URL, owner="alice") == [MODEL]


# ── The mod API keeps the rows ───────────────────────────────────────────

class _Auth:
    users = {"alice": {}, "bob": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"


@pytest.fixture
def api(mods_dir, monkeypatch):
    from src.mods import access, routes
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(access, "_auth_manager", _Auth())
    app = FastAPI()
    app.include_router(routes.setup_mods_routes(_Auth()))
    routes._calls.clear()
    return TestClient(app)


def _rows():
    from core.database import SessionLocal, ModelEndpoint
    db = SessionLocal()
    try:
        return [(e.base_url, e.owner, json.loads(e.cached_models or "[]"), e.supports_tools, e.name)
                for e in db.query(ModelEndpoint).filter(ModelEndpoint.base_url == MOD_BASE).all()]
    finally:
        db.close()


def test_endpoint_rows_through_the_mod_api(api):
    _, tok = install_fake_mod()
    h = {"Authorization": f"Bearer {tok}"}
    r = api.put("/api/mods/v1/llm/endpoint", json={"user": "bob", "models": [MODEL, "gpt-5.5"]}, headers=h)
    assert r.status_code == 200, r.text
    assert _rows() == [(MOD_BASE, "bob", [MODEL, "gpt-5.5"], True, "ChatGPT")]
    api.put("/api/mods/v1/llm/endpoint", json={"user": "bob", "models": [MODEL]}, headers=h)
    assert _rows()[0][2] == [MODEL]
    assert api.put("/api/mods/v1/llm/endpoint", json={"user": "mallory", "models": []}, headers=h).status_code == 400
    assert api.put("/api/mods/v1/llm/endpoint", json={"user": "bob", "models": "x"}, headers=h).status_code == 400
    assert api.delete("/api/mods/v1/llm/endpoint?user=bob", headers=h).json() == {"ok": True, "removed": 1}
    assert _rows() == []


def test_only_an_llm_provider_may_register(api):
    from tests.mods_helpers import add_mod
    _, tok = add_mod(mod_id="other")
    r = api.put("/api/mods/v1/llm/endpoint", json={"user": "bob", "models": [MODEL]},
                headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


# ── The old Sign in with ChatGPT rows move to the add-on ─────────────────

def test_old_chatgpt_plan_rows_and_chats_are_migrated(monkeypatch):
    import core.database as cdb
    from core.database import ModelEndpoint, Session as DBSession
    monkeypatch.setattr(cdb, "engine", _DB["engine"])
    db = _DB["ts"]()
    try:
        db.add(ModelEndpoint(id="cgp1", name="ChatGPT", base_url=cdb._OLD_CHATGPT_PLAN_BASE, owner="alice",
                             provider_auth_id="chatgpt-plan", cached_models=json.dumps([MODEL])))
        db.add(DBSession(id="s-cgp", name="x", endpoint_url=cdb._OLD_CHATGPT_PLAN_BASE + "/responses",
                         model=MODEL, owner="alice"))
        db.commit()
    finally:
        db.close()
    cdb._migrate_chatgpt_plan_to_mod()
    cdb._migrate_chatgpt_plan_to_mod()  # idempotent
    db = _DB["ts"]()
    try:
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == "cgp1").one()
        assert (ep.base_url, ep.provider_auth_id, ep.owner, ep.supports_tools) == (MOD_BASE, None, "alice", True)
        assert db.query(DBSession).filter(DBSession.id == "s-cgp").one().endpoint_url == MOD_URL
        db.query(ModelEndpoint).filter(ModelEndpoint.id == "cgp1").delete()
        db.query(DBSession).filter(DBSession.id == "s-cgp").delete()
        db.commit()
    finally:
        db.close()


def test_the_settings_card_points_to_the_add_on():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    html = (root / "static/index.html").read_text()
    js = (root / "static/js/chatgptAddon.js").read_text()
    assert 'id="cgp-addon-btn"' in html and "Install the ChatGPT add-on" in html
    assert "/api/mods/providers?cap=llm.provider" in js and "#mod/" in js
    assert not (root / "src/chatgpt_plan.py").exists()


def test_an_add_on_error_is_shown_as_written(mod_llm):
    from src.llm_core import stream_llm
    mod_llm.status = 401
    text = "".join(_drain(stream_llm(MOD_URL, MODEL, [{"role": "user", "content": "signed out?"}], owner="alice")))
    assert "Sign in with ChatGPT on the ChatGPT tab first." in text and "re-paste the key" not in text
