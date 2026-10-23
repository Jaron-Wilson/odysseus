"""In a mod's agent run (src/mods/runs.py) the agent loop sends that mod's
tools whatever the message says, first in the list, and no other mod's.
Everything is mocked: no request leaves the machine."""
import asyncio
import json
import os

import pytest

from src.mods import paths, runs
from tests.mods_helpers import add_mod, mods_dir  # noqa: F401
from tests.helpers.sqlite_db import make_temp_sqlite

# Imported now, before the fixture swaps core.database.SessionLocal: these
# bind it at import, and binding the swapped one would leak this module's
# database into later test modules.
import importlib  # noqa: E402

for _m in ("src.endpoint_resolver", "src.session_search", "routes.chat_helpers", "routes.chat_routes",
           "routes.model_routes", "routes.session_routes", "src.agent_loop", "src.ai_interaction"):
    importlib.import_module(_m)

_DB = {}


@pytest.fixture(autouse=True)
def _own_db(monkeypatch):
    import core.database as cdb
    if _DB.get("base") is not cdb.Base:
        ts, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
        _DB.update(base=cdb.Base, ts=ts, engine=engine, tmp=tmp)
    monkeypatch.setattr(cdb, "SessionLocal", _DB["ts"])


def _tools(mod_id, names, keywords=()):
    os.makedirs(paths.mod_dir(mod_id), exist_ok=True)
    with open(os.path.join(paths.mod_dir(mod_id), "tools.json"), "w") as f:
        json.dump({"tools": [{"name": n, "description": n, "keywords": list(keywords),
                              "parameters": {"type": "object", "properties": {}}} for n in names]}, f)


MODEL = "test-model"
URL = "http://model.test/v1"


def _collect(agen):
    async def go():
        return [c async for c in agen]
    return asyncio.run(go())


def _send(monkeypatch, message, relevant, ctx):
    import src.agent_loop as al
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())
    sent = []
    from core.database import SessionLocal, ModelEndpoint
    ep_id = "rt" + os.urandom(3).hex()
    db = SessionLocal()
    try:
        db.add(ModelEndpoint(id=ep_id, name="Test", base_url=URL, owner="alice", is_enabled=True,
                             api_key=None, cached_models=json.dumps([MODEL]), supports_tools=True))
        db.commit()
    finally:
        db.close()

    async def fake_stream(_candidates, messages, **kwargs):
        sent.append(kwargs.get("tools") or [])
        yield f'data: {json.dumps({"delta": "ok"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)
    try:
        with runs.run_context(**ctx) if ctx else _Null():
            _collect(al.stream_agent_loop(URL, MODEL, [{"role": "system", "content": "You are Ada."},
                                                        {"role": "user", "content": message}],
                                          headers={}, owner="alice", max_rounds=1, relevant_tools=relevant,
                                          disabled_tools={"ask_user"}))
    finally:
        db = SessionLocal()
        try:
            db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).delete()
            db.commit()
        finally:
            db.close()
    assert sent
    return [(t.get("function") or t).get("name") for t in sent[0]]


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def test_mod_run_forces_its_tools_first(monkeypatch, mods_dir):
    add_mod({"tools": {"endpoint": "/mod/tools", "keywords": ["company"]}}, mod_id="company", access="users")
    add_mod({"tools": {"endpoint": "/mod/tools", "keywords": ["printer"]}}, mod_id="print-hub", access="users")
    _tools("company", ["list_issues", "comment"])
    _tools("print-hub", ["printer_status"])
    names = _send(monkeypatch, "Check the printer and work your queue", {"bash", "read_file"},
                  {"mod_id": "company", "run_id": "r1", "tools": True, "other_mods": False})
    own = {"mod__company__list_issues", "mod__company__comment"}
    assert own <= set(names) and set(names[:2]) == own
    assert "mod__print-hub__printer_status" not in names       # other mods only when asked
    assert "bash" in names


def test_a_list_picks_some_of_the_mods_tools(monkeypatch, mods_dir):
    add_mod({"tools": {"endpoint": "/mod/tools"}}, mod_id="company", access="users")
    _tools("company", ["list_issues", "comment"])
    names = _send(monkeypatch, "hello", {"bash"},
                  {"mod_id": "company", "run_id": "r1", "tools": ["comment"], "other_mods": False})
    assert "mod__company__comment" in names and "mod__company__list_issues" not in names


def test_outside_a_run_the_mod_tools_need_their_keywords(monkeypatch, mods_dir):
    add_mod({"tools": {"endpoint": "/mod/tools", "keywords": ["company"]}}, mod_id="company", access="users")
    _tools("company", ["list_issues"])
    names = _send(monkeypatch, "what is the weather", {"bash"}, None)
    assert "mod__company__list_issues" not in names
