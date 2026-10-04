"""The read-only transcript viewer (PR 1).

src/transcript_summaries.py parses a coder run's single ``out.jsonl`` into
thinking blocks (capped), tool calls with their results, and the parsed
message/tool/result/turn counts; it persists an on-disk summary so the first
listing after a restart is a file read, not a re-parse.
routes/transcript_routes.py serves the list and the detail behind the existing
auth/ownership gate, read-only: nothing it does resumes, edits or deletes a
session, and no ``subagents/*.jsonl`` file is ever read.
"""
import json
import os

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("starlette.testclient").TestClient

from fastapi import FastAPI

from src import claude_code_jobs as jobs
from src import transcript_summaries as summaries
import routes.transcript_routes as routes


CLAUDE = [
    {"type": "system", "subtype": "init", "session_id": "abc12345", "model": "claude-sonnet-5-5"},
    {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "thinking", "thinking": "a " * 5000},
        {"type": "text", "text": "I will run it."}]}},
    {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls -la"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "file1 file2"}]}]}},
    {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Done."}]}},
    {"type": "result", "result": "All done.", "is_error": False, "num_turns": 2,
     "duration_ms": 15000, "total_cost_usd": 0.12, "usage": {"output_tokens": 5432}},
]

OPENCODE = [
    {"type": "step_start", "timestamp": 1000, "sessionID": "ses_zzz"},
    {"type": "text", "part": {"text": "Working."}},
    {"type": "tool", "part": {"tool": "bash", "state": {"input": {"command": "pwd"}, "output": "/home"}}},
    {"type": "step_finish", "timestamp": 160000, "part": {"reason": "stop", "cost": 0.03,
     "tokens": {"output": 900, "reasoning": 250}}},
]

ANTIGRAVITY = [
    {"event": "init", "conversation_id": "conv-1"},
    {"event": "step_update", "step_update": {"step_type": "agent_response", "step_index": 0,
     "state": "DONE", "text_delta": "Here is the answer.", "usage": {"thinking_tokens": 30}}},
    {"event": "step_update", "step_update": {"step_type": "tool", "step_index": 1, "state": "DONE",
     "tool_name": "view_file", "tool_info": {"parameters": {"AbsolutePath": "/a/b.py"}}}},
    {"event": "result", "result": {"status": "SUCCESS", "duration_seconds": 6.5,
     "usage": {"output_tokens": 589}}},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(jobs, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(summaries, "SUMMARY_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    jobs._JOBS.clear()
    yield tmp_path
    for j in list(jobs._JOBS.values()):
        if j.status == "running":
            jobs.kill_run(j)
    jobs._JOBS.clear()


def _write(run_dir, lines):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(l) for l in lines) + "\n")


def _job_with(run_dir, owner="jaron", engine="claude"):
    job = jobs.register(chat_session_id="c1", owner=owner, action="execute", cwd="/tmp",
                        model="sonnet", engine=engine, prompt="x")
    _write(run_dir, CLAUDE if engine == "claude" else [])
    job.id_link = run_dir  # not a real attr; keep the mapping for the test
    return job


# ── Parser: Claude ───────────────────────────────────────────────────────

def test_claude_counts_are_parsed_not_approximated(env):
    rd = str(env / "runs" / "cl1")
    _write(rd, CLAUDE)
    s = summaries.parse_run(rd)
    c = s["counts"]
    assert c["messages"] == 2            # two assistant text blocks
    assert c["tool_calls"] == 1
    assert c["results"] == 1             # the tool_result that answered t1
    assert c["thinking_blocks"] == 1
    assert c["turns"] == 2               # from the result line's num_turns
    assert s["engine"] == "claude" and s["model"] == "claude-sonnet-5-5"
    assert s["status"] == "done" and s["is_error"] is False
    assert s["cost_usd"] == 0.12 and s["output_tokens"] == 5432
    assert s["resume_command"] == "claude --resume abc12345"


def test_thinking_is_capped_but_still_shown(env):
    rd = str(env / "runs" / "cl2")
    _write(rd, CLAUDE)
    s = summaries.parse_run(rd)
    thought = [b for b in s["blocks"] if b["kind"] == "thinking"][0]
    # The real blob is ~10k chars; the cap is 4000, and the note records the
    # full length so the viewer can say "… N chars total".
    assert thought["clipped"] is True
    assert len(thought["text"]) <= summaries.MAX_THINKING_CHARS
    assert thought["full_len"] > summaries.MAX_THINKING_CHARS


def test_tool_result_is_bound_to_its_call_by_id(env):
    rd = str(env / "runs" / "cl3")
    _write(rd, CLAUDE)
    s = summaries.parse_run(rd)
    call = [b for b in s["blocks"] if b["kind"] == "tool_call"][0]
    assert call["name"] == "bash" and "ls -la" in call["hint"]
    assert call["output"].startswith("file1") and call["error"] is False


# ── Parser: OpenCode and Antigravity ─────────────────────────────────────

def test_opencode_counts_and_reasoning(env):
    rd = str(env / "runs" / "oc1")
    _write(rd, OPENCODE)
    s = summaries.parse_run(rd)
    c = s["counts"]
    assert c["tool_calls"] == 1 and c["results"] == 1
    assert c["thinking_blocks"] == 1            # the 250 reasoning tokens
    assert c["turns"] == 1                       # the one step_start
    assert s["engine"] == "opencode" and s["status"] == "done"
    assert s["resume_command"] == "opencode --session ses_zzz"
    assert s["reasoning_tokens"] == 250


def test_antigravity_counts(env):
    rd = str(env / "runs" / "ag1")
    _write(rd, ANTIGRAVITY)
    s = summaries.parse_run(rd)
    c = s["counts"]
    assert c["tool_calls"] == 1 and c["thinking_blocks"] == 1 and c["turns"] >= 1
    assert s["engine"] == "antigravity" and s["status"] == "done"
    assert s["resume_command"] == "agy --conversation conv-1"


# ── Exclusions ───────────────────────────────────────────────────────────

def test_subagents_jsonl_is_never_read(env):
    rd = str(env / "runs" / "clx")
    _write(rd, CLAUDE)
    base = summaries.parse_run(rd)
    sub = os.path.join(rd, "subagents")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "sub.jsonl"), "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "SECRET SUBAGENT LEAK"}]}}) + "\n")
    again = summaries.parse_run(rd)
    assert again["counts"] == base["counts"]
    assert "SECRET SUBAGENT LEAK" not in json.dumps(again)


def test_no_mutating_endpoint_exists(env):
    """PR 1 is read-only: the only routes are GETs, and the resume command
    shows up as a plain string, never as a request the app can trigger."""
    methods = set()
    for r in routes.setup_transcript_routes().routes:
        if getattr(r, "methods", None):
            methods |= set(r.methods)
    assert methods <= {"GET"}, methods


def test_resume_command_is_display_only(env):
    assert summaries.resume_command("claude", "ses_1") == "claude --resume ses_1"
    assert summaries.resume_command("opencode", "ses_2") == "opencode --session ses_2"
    assert summaries.resume_command("antigravity", "conv_3") == "agy --conversation conv_3"
    assert summaries.resume_command("claude", "") == ""


# ── Persistent cache: first listing after restart ────────────────────────

def test_first_listing_after_restart_serves_from_disk(env, monkeypatch):
    rd = str(env / "runs" / "cache1")
    _write(rd, CLAUDE)
    first = summaries.get_summary(rd)                 # cold: parses + writes to disk
    assert first is not None and first["counts"]["turns"] == 2

    # Simulate a restart: count parser calls, and the next get_summary must be
    # a hit on the file the first call wrote, so the parser must not run again.
    calls = {"n": 0}
    real = summaries.parse_run

    def counting(run_dir):
        calls["n"] += 1
        return real(run_dir)

    monkeypatch.setattr(summaries, "parse_run", counting)
    second = summaries.get_summary(rd)
    assert second["counts"] == first["counts"]
    assert calls["n"] == 0, "the summary was re-parsed instead of served from the on-disk cache"


def test_stale_cache_is_recomputed_when_the_file_changes(env, monkeypatch):
    rd = str(env / "runs" / "cache2")
    _write(rd, CLAUDE)
    first = summaries.get_summary(rd)
    assert first is not None

    # A change to out.jsonl changes its size/mtime, so the (size,mtime) cache
    # key no longer matches and the summary must be recomputed, not served stale.
    _write(rd, CLAUDE + [{"type": "assistant", "message": {"id": "m3", "content": [
        {"type": "text", "text": "One more line."}]}}])
    # Bump the mtime so the (size, mtime) key definitely changes.
    os.utime(os.path.join(rd, "out.jsonl"))
    again = summaries.get_summary(rd)
    assert again is not None
    assert again["counts"]["messages"] > first["counts"]["messages"]


# ── Routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(env, monkeypatch):
    def auth_on():
        monkeypatch.setattr(routes, "get_current_user", lambda r: "jaron")
        monkeypatch.setattr(routes, "_auth_disabled", lambda: False)

    auth_on()
    job = jobs.register(chat_session_id="c1", owner="jaron", action="execute", cwd="/tmp",
                        model="sonnet", engine="claude", prompt="x")
    rd = os.path.join(jobs.RUNS_DIR, job.id)
    _write(rd, CLAUDE)
    app = FastAPI()
    app.include_router(routes.setup_transcript_routes())
    return TestClient(app, raise_server_exceptions=False), job


def test_detail_is_read_only_and_parses(client):
    c, job = client
    r = c.get(f"/api/transcript/runs/{job.id}")
    assert r.status_code == 200
    d = r.json()
    assert d["counts"]["tool_calls"] == 1 and d["counts"]["turns"] == 2
    assert d["resume_command"] == "claude --resume abc12345"
    assert d["final_text"].startswith("All done")
    assert any(b["kind"] == "thinking" and b["text"] for b in d["blocks"])


def test_list_carries_parsed_counts_from_cache(client):
    c, job = client
    r = c.get("/api/transcript/runs?limit=50")
    assert r.status_code == 200
    row = next(x for x in r.json()["runs"] if x["id"] == job.id)
    assert row["has_detail"] is True
    assert row["counts"]["tool_calls"] == 1
    assert row["resume_command"] == "claude --resume abc12345"


def test_other_owners_run_is_not_visible(client, monkeypatch):
    c, job = client
    monkeypatch.setattr(routes, "get_current_user", lambda r: "someone-else")
    assert c.get(f"/api/transcript/runs/{job.id}").status_code == 404
    rows = c.get("/api/transcript/runs").json()["runs"]
    assert all(x["id"] != job.id for x in rows)


def test_unknown_job_id_is_400(client):
    c, _ = client
    assert c.get("/api/transcript/runs/notanid").status_code == 400


def test_app_registers_the_routes_and_the_ui_is_present():
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "setup_transcript_routes()" in app
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert "/static/js/devopsPage.js" in html          # the viewer mounts in the DevOps page
    js = open(os.path.join(ROOT, "static", "js", "devopsPage.js"), encoding="utf-8").read()
    assert "/api/transcript/runs/" in js and "dv-transcript" in js and "data-resume" in js
