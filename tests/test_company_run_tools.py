"""Company runs get a focused tool set, and the tools sent to OpenAI-style
backends are capped.

A Company Engineer on a ChatGPT plan model was sent 252 tools (every
connected desktop MCP tool, none of its Company tools as native schemas)
and replied that it had no Company tools or claude_code. The loop now sends
only the run's selected tools, Company tools first, and never more than
tool_cap.SAFE_TOOL_CAP to OpenAI or the ChatGPT plan.

Everything is mocked: no request leaves the machine.
"""
import json

import pytest

from src import chatgpt_plan as cgp
from src import company_executor as cx
from src import company_tools as ct
from src import tool_cap
from tests.test_chatgpt_plan_request_auth import (  # noqa: F401  (signed_in is a fixture)
    ACCESS, MODEL, PLAN_URL, _agent_common, _collect, _drop_endpoint, _plan_endpoint, signed_in,
)


from tests.helpers.sqlite_db import make_temp_sqlite  # noqa: E402

_DB = {}


@pytest.fixture(autouse=True)
def _own_db(monkeypatch):
    """Endpoint rows in a DB of this module's own: earlier test modules can
    leave core.database bound to one without the tables. Built from the
    core.database in use at test time; importing or re-importing it at
    collection time would split the suite between two module copies."""
    import core.database as cdb
    if _DB.get("base") is not cdb.Base:
        ts, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
        _DB.update(base=cdb.Base, ts=ts, engine=engine, tmp=tmp)
    monkeypatch.setattr(cdb, "SessionLocal", _DB["ts"])


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


class _FakeMCP:
    """A connected MCP server with many desktop tools, like the live one."""

    def __init__(self, n=240):
        self.schemas = [_schema(f"mcp__19d772b0__tool_{i}") for i in range(n)]

    def get_all_openai_schemas(self, disabled_map):
        return list(self.schemas)

    def get_tool_descriptions_for_prompt(self, disabled_map):
        return ""

    def plan_mode_blocked_mcp(self):
        return {}, set()


# ── The focused set ──────────────────────────────────────────────────────

def test_run_tools_are_focused():
    tools = cx.run_tools("claude", None, {"ask_user"})
    assert set(ct.TOOL_NAMES) <= tools
    assert "claude_code" in tools
    assert {"bash", "read_file", "edit_file", "grep", "glob", "web_search"} <= tools
    assert not any(t.startswith("mcp__") for t in tools)
    assert "ask_user" not in tools


def test_run_tools_without_coding_agent_and_with_extras():
    tools = cx.run_tools(None, ["mcp__19d772b0__screenshot", "manage_notes"], {"bash"})
    assert "claude_code" not in tools and "bash" not in tools
    assert {"mcp__19d772b0__screenshot", "manage_notes"} <= tools


def test_core_tools_exist_in_the_registry():
    import src.agent_loop  # noqa: F401  (registers the schemas)
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert set(cx.CORE_RUN_TOOLS) <= names
    assert set(ct.TOOL_NAMES) <= names and "claude_code" in names


# ── The cap ──────────────────────────────────────────────────────────────

def test_cap_keeps_priority_then_relevant_then_builtin(caplog):
    schemas = ([_schema(f"mcp__x__t{i}") for i in range(150)] + [_schema("bash"), _schema("manage_notes")]
               + [_schema(n) for n in sorted(ct.TOOL_NAMES)] + [_schema("claude_code")])
    with caplog.at_level("WARNING"):
        kept = tool_cap.cap_tools(schemas, 120, priority=sorted(ct.TOOL_NAMES) + ["claude_code"],
                                  relevant={"bash"})
    names = [tool_cap.tool_name(s) for s in kept]
    assert len(names) == 120
    # Company tools and claude_code lead, then the relevant and built-in tools.
    assert names[:len(ct.TOOL_NAMES) + 1] == sorted(ct.TOOL_NAMES) + ["claude_code"]
    assert "bash" in names and "manage_notes" in names
    assert "exceed the cap" in caplog.text


def test_cap_leaves_short_lists_alone():
    schemas = [_schema("a"), _schema("b")]
    assert tool_cap.cap_tools(schemas, 120) == schemas


def test_needs_tool_cap():
    assert tool_cap.needs_tool_cap(PLAN_URL)
    assert tool_cap.needs_tool_cap("https://api.openai.com/v1/chat/completions")
    assert tool_cap.needs_tool_cap("https://chatgpt.com/backend-api/codex/responses")
    assert not tool_cap.needs_tool_cap("http://127.0.0.1:11434/v1")
    assert not tool_cap.needs_tool_cap("https://openrouter.ai/api/v1")


def test_chatgpt_plan_payload_never_exceeds_the_cap():
    tools = [_schema(f"mcp__x__t{i}") for i in range(200)] + [_schema(n) for n in sorted(ct.TOOL_NAMES)]
    payload = cgp.build_payload(MODEL, [{"role": "user", "content": "hi"}], tools=tools)
    names = [t["name"] for t in payload["tools"]]
    assert len(names) <= tool_cap.SAFE_TOOL_CAP < tool_cap.OPENAI_MAX_TOOLS
    assert set(ct.TOOL_NAMES) <= set(names)


# ── What the agent loop sends in a Company run ───────────────────────────

def _plan_run(signed_in, monkeypatch, *, supports_tools, message, relevant, mcp=None):
    al = _agent_common(monkeypatch)
    fake = mcp or _FakeMCP()
    monkeypatch.setattr(al, "get_mcp_manager", lambda: fake, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())  # an admin
    ep_id = _plan_endpoint(supports_tools=supports_tools)
    signed_in.calls.clear()
    signed_in.lines = [
        'data: {"type":"response.output_text.delta","delta":"Working on it."}',
        'data: {"type":"response.completed","response":{}}',
    ]
    try:
        with ct.agent_context(owner="alice", company_id="c1", agent_id="a1", run_id="r1"):
            _collect(al.stream_agent_loop(
                PLAN_URL, MODEL, [{"role": "system", "content": "You are Engineer."},
                                  {"role": "user", "content": message}],
                headers={}, owner="alice", max_rounds=1, relevant_tools=relevant,
                disabled_tools={"ask_user"},
            ))
    finally:
        _drop_endpoint(ep_id)
    calls = [c for c in signed_in.calls if c["url"] == cgp.RESPONSES_URL]
    assert calls
    return [t.get("name") for t in (calls[0]["json"].get("tools") or [])]


WAKE = ("Why you are awake:\n- You were assigned JAR-3 \"Review the browser screenshot flow\"\n"
        "- You were assigned JAR-5 \"Fix the calendar event click\"")


def test_company_run_sends_its_focused_tools_first(signed_in, monkeypatch):
    names = _plan_run(signed_in, monkeypatch, supports_tools=True, message=WAKE,
                      relevant=cx.run_tools("claude", None, {"ask_user"}))
    assert names, "native tools expected"
    assert set(ct.TOOL_NAMES) <= set(names) and "claude_code" in names
    assert not any(n.startswith("mcp__") for n in names)
    # Company tools and the coding agent lead the list.
    assert set(names[:len(ct.TOOL_NAMES) + 1]) == set(ct.TOOL_NAMES) | {"claude_code"}
    # Topic words in the wake message add nothing to a Company run's set.
    assert "google_meet" not in names and "ui_control" not in names and "manage_calendar" not in names


def test_fenced_mode_does_not_send_every_mcp_tool(signed_in, monkeypatch):
    """The live bug: native tools off for the endpoint, so tools went as
    fenced blocks, but a keyword ("screenshot", "click") made the loop send
    every MCP tool as a native schema; the model took that list as its
    whole tool set."""
    names = _plan_run(signed_in, monkeypatch, supports_tools=False, message=WAKE,
                      relevant=cx.run_tools("claude", None, {"ask_user"}))
    assert not any(n.startswith("mcp__") for n in names)


def test_selected_mcp_tools_still_go_in_fenced_mode(signed_in, monkeypatch):
    names = _plan_run(signed_in, monkeypatch, supports_tools=False, message=WAKE,
                      relevant=cx.run_tools(None, ["mcp__19d772b0__tool_3"], {"ask_user"}))
    assert names == ["mcp__19d772b0__tool_3"]


def test_chat_with_many_tools_is_capped_for_chatgpt_plan(signed_in, monkeypatch, caplog):
    many = set(ct.TOOL_NAMES) | {"claude_code", "bash"} | {f"mcp__19d772b0__tool_{i}" for i in range(200)}
    with caplog.at_level("WARNING"):
        names = _plan_run(signed_in, monkeypatch, supports_tools=True, message=WAKE, relevant=many)
    assert len(names) <= tool_cap.SAFE_TOOL_CAP
    assert set(ct.TOOL_NAMES) <= set(names) and "claude_code" in names and "bash" in names
    assert "[tool-cap]" in caplog.text


def test_low_signal_chat_mcp_schemas_are_kept_in_fenced_mode(signed_in, monkeypatch):
    """Outside Company runs nothing changes when retrieval was skipped: the
    prompt lists every MCP tool, so their schemas still go out (capped)."""
    al = _agent_common(monkeypatch)
    fake = _FakeMCP(10)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: fake, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())  # an admin
    ep_id = _plan_endpoint(supports_tools=False)
    signed_in.calls.clear()
    signed_in.lines = ['data: {"type":"response.completed","response":{}}']
    try:
        _collect(al.stream_agent_loop(
            PLAN_URL, MODEL, [{"role": "user", "content": "ok take a screenshot"}],
            headers={}, owner="alice", max_rounds=1,
        ))
    finally:
        _drop_endpoint(ep_id)
    calls = [c for c in signed_in.calls if c["url"] == cgp.RESPONSES_URL]
    assert calls
    names = [t.get("name") for t in (calls[0]["json"].get("tools") or [])]
    assert len(names) <= tool_cap.SAFE_TOOL_CAP


# ── The UI shows the new outcome ─────────────────────────────────────────

def test_company_ui_labels_no_progress_runs_as_a_warning():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    js = (root / "static/js/company.js").read_text()
    css = (root / "static/css/company.css").read_text()
    assert "no_progress: 'No progress'" in js and "'run.no_progress'" in js
    assert "run-pill-${esc(r.status)}" in js and "co-warn" in js
    assert ".run-no_progress" in css and ".co-pill.run-pill-no_progress" in css and ".co-note.co-warn" in css


# ── Prompts that fit a small local window ────────────────────────────────

def test_tool_schemas_are_trimmed_to_fit_the_window_not_the_messages(caplog):
    import src.agent_loop as al
    schemas = [_schema(n) for n in sorted(ct.TOOL_NAMES)] + [
        {"type": "function", "function": {"name": f"mcp__x__t{i}", "description": "x" * 400,
                                          "parameters": {"type": "object", "properties": {}}}}
        for i in range(250)]
    messages = [{"role": "system", "content": "s" * 4000}, {"role": "user", "content": "Do JAR-3"}]
    with caplog.at_level("WARNING"):
        kept = al._fit_tools_to_window(schemas, messages, 8192, 1024,
                                       priority=sorted(ct.TOOL_NAMES) + ["claude_code"])
    names = [tool_cap.tool_name(s) for s in kept]
    assert set(ct.TOOL_NAMES) <= set(names)
    assert len(json.dumps(kept)) // 4 + al.estimate_tokens(messages) + 1024 <= 8192
    assert "did not fit" in caplog.text
    # Plenty of room: untouched.
    assert al._fit_tools_to_window(schemas[:3], messages, 128000, 1024) == schemas[:3]


def test_company_run_prompt_has_no_skills_catalog(monkeypatch):
    import src.agent_loop as al
    captured = {}
    real = al._build_system_prompt

    def spy(*a, **kw):
        captured["include_skills"] = kw.get("include_skills", True)
        return real(*a, **kw)
    monkeypatch.setattr(al, "_build_system_prompt", spy)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)

    async def fake_stream(*a, **kw):
        yield 'data: {"delta": "ok"}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)
    monkeypatch.setattr(al, "stream_llm", fake_stream, raising=False)
    with ct.agent_context(owner="", company_id="c1", agent_id="a1", run_id="r1"):
        _collect(al.stream_agent_loop("http://127.0.0.1:9/v1", "m", [{"role": "user", "content": "Do JAR-3"}],
                                      max_rounds=1, relevant_tools=cx.run_tools(None, None, set())))
    assert captured["include_skills"] is False
    captured.clear()
    _collect(al.stream_agent_loop("http://127.0.0.1:9/v1", "m", [{"role": "user", "content": "hello there"}],
                                  max_rounds=1, relevant_tools={"bash"}))
    assert captured["include_skills"] is True


def test_build_system_prompt_skips_skills_when_asked(monkeypatch):
    import src.agent_loop as al
    calls = []

    class SM:
        def load(self, owner=None):
            calls.append("load")
            return []

        def get_relevant_skills(self, *a, **k):
            calls.append("match")
            return []
    import services.memory.skills as skills_mod
    monkeypatch.setattr(skills_mod, "SkillsManager", lambda *a, **k: SM(), raising=False)
    al._build_system_prompt([{"role": "user", "content": "deploy the thing"}], "m", None, None, set(),
                            relevant_tools={"bash"}, include_skills=False)
    assert "match" not in calls
    al._build_system_prompt([{"role": "user", "content": "deploy the other thing"}], "m", None, None, set(),
                            relevant_tools={"bash"})
    assert "match" in calls


def test_truncation_check_knows_the_ollama_cap(monkeypatch):
    from src import model_context
    monkeypatch.setattr(model_context, "ollama_max_ctx", lambda: 24576)
    caps = cx._suspect_caps()
    assert {2048, 4096, 8192, 32768, 24576} <= caps


# ── Ollama on a port other than 11434 ────────────────────────────────────

class _Resp:
    def __init__(self, ok, body):
        self.is_success = ok
        self._body = body

    def json(self):
        return self._body


def test_second_ollama_instance_is_detected_by_probe(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_OLLAMA_PROBE", "1")
    from src import model_context as mc
    from src import llm_core
    mc._ollama_probe_cache.clear()
    seen = []

    def fake_get(url, timeout=None):
        seen.append(url)
        if "11435" in url:
            return _Resp(True, {"version": "0.12.0"})
        return _Resp(False, {})
    monkeypatch.setattr(mc.httpx, "get", fake_get)
    assert mc.is_local_ollama_url("http://100.102.86.125:11435/v1")
    assert llm_core._ollama_compat_as_native("http://100.102.86.125:11435/v1/chat/completions") == \
        "http://100.102.86.125:11435/api/chat"
    # A vLLM server on another port is left alone, and asked only once.
    assert not mc.is_local_ollama_url("http://100.102.86.125:8000/v1")
    assert llm_core._ollama_compat_as_native("http://100.102.86.125:8000/v1/chat/completions") is None
    assert sum("8000" in u for u in seen) == 1
    # Hosted APIs are never probed.
    assert not mc.probe_is_ollama("https://api.openai.com/v1")
    assert not any("openai" in u for u in seen)
    mc._ollama_probe_cache.clear()
