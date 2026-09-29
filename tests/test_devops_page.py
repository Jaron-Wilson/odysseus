"""The DevOps page (src/devops_stats.py) and which models are busy (opencode_providers.in_use).

Asked for on 2026-09-29: "can we get a devops page, ie: show everything
happening, how many tokens/s or tokens/h etc average speed, most liked
coder, etc." and "when approving an opencode and selecting a model i would
like to see if a model is already being used".
"""
import json
import os
import time

import pytest

from src import claude_code_jobs as jobs
from src import devops_stats as dv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(jobs, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    jobs._JOBS.clear()
    dv._LOG_CACHE.clear()
    (tmp_path / "runs").mkdir()
    return tmp_path / "runs"


def _log(runs, rid, lines):
    d = runs / rid
    d.mkdir()
    (d / "out.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_average_speed_is_all_tokens_over_all_writing_time(monkeypatch):
    now = time.time()
    rows = [  # 1000 tokens at 50 tok/s (20 s) and 100 at 5 tok/s (20 s): 1100 / 40 s
        {"t": now - 60, "model": "a", "out": 1000, "in": 10, "tps": 50, "ttft": 1, "secs": 21, "session_id": "", "chat": ""},
        {"t": now - 7200, "model": "b", "out": 100, "in": 10, "tps": 5, "ttft": 3, "secs": 23, "session_id": "", "chat": ""},
    ]
    monkeypatch.setattr(dv, "_replies", lambda since, owner="": [r for r in rows if r["t"] >= since])
    c = dv.chat_stats(24, now=now)
    assert c["tokens_per_s"] == 27.5
    assert c["tokens_last_hour"] == 1000 and c["tokens_per_hour"] == round(1100 / 24)
    assert c["busiest_model"] == "a" and len(c["series"]) == 24
    assert sum(b["output_tokens"] for b in c["series"]) == 1100
    assert c["fastest_model"] is None                  # neither has 3 replies yet


def test_coder_runs_come_from_history_and_old_logs(runs):
    now = time.time()
    _log(runs, "claude1", [{"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
                           {"type": "result", "is_error": False, "duration_ms": 600000, "total_cost_usd": 1.5,
                            "usage": {"output_tokens": 30000}}])
    ts = int((now - 300) * 1000)
    _log(runs, "oc1", [{"type": "step_start", "timestamp": ts},
                       {"type": "step_finish", "timestamp": ts + 60000, "part": {"reason": "stop", "tokens": {"output": 900, "reasoning": 100}}}])
    _log(runs, "oc2", [{"type": "error", "timestamp": ts}])
    (runs / "never").mkdir()                            # an empty run dir: it never started
    j = jobs.register(chat_session_id="c1", owner="jaron", action="execute", cwd="/tmp",
                      model="vllm3090/qwen3.8-27b", engine="opencode", prompt="x")
    jobs.finish(j, "done")
    k = dv.coder_stats(24, now=now + 1)
    by = {(c["engine"], c["model"]): c for c in k["coders"]}
    assert by[("claude", "claude-opus-5-5")]["done"] == 1 and by[("claude", "claude-opus-5-5")]["cost_usd"] == 1.5
    old = by[("opencode", "")]
    assert (old["runs"], old["done"], old["failed"], old["output_tokens"]) == (2, 1, 1, 1000)
    assert by[("opencode", "vllm3090/qwen3.8-27b")]["done"] == 1     # from the history file
    assert k["runs"] == 4 and k["favorite"]["runs"] == 2
    rec = json.loads(open(jobs.HISTORY_FILE).read().splitlines()[-1])
    assert rec["engine"] == "opencode" and rec["status"] == "done"


def test_busy_models_are_named(runs, monkeypatch):
    from src import opencode_providers as op
    own = {"model": "vllm3090/qwen3.8-27b", "provider": {
        "vllm3090": {"options": {"baseURL": "http://192.168.100.101:8114/v1"}, "models": {"qwen3.8-27b": {}, "other": {}}},
        "ollama-desktop": {"options": {"baseURL": "http://100.102.86.125:11434/v1"}, "models": {"qwen3:8b": {}}}}}
    monkeypatch.setattr(op, "_own_config", lambda: own)
    monkeypatch.setattr(op, "_endpoints", lambda: [])
    monkeypatch.setattr(op, "_replying_chats", lambda: [
        {"id": "s1", "name": "CleverNode fixes", "base": op._base("http://192.168.100.101:8114/v1/chat/completions"),
         "model": "qwen3.8-27b"}])
    monkeypatch.setattr(jobs, "_chat_name", lambda sid: "Music bar")
    jobs.register(chat_session_id="c2", owner="", action="plan", cwd="/tmp", model="", engine="opencode", prompt="p")
    use = op.in_use(["vllm3090/qwen3.8-27b", "vllm3090/other", "ollama-desktop/qwen3:8b"])
    busy = use["vllm3090/qwen3.8-27b"]["busy"]
    assert any("CleverNode fixes" in b for b in busy)
    assert any("OpenCode plan in Music bar" in b for b in busy)       # no model: OpenCode's default
    assert use["vllm3090/other"]["busy"] == [] and len(use["vllm3090/other"]["server_busy"]) == 2
    assert "ollama-desktop/qwen3:8b" not in use                      # free


def test_page_and_email_fill_are_wired():
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert 'id="tool-devops-btn"' in html and "/static/js/devopsPage.js" in html
    app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "setup_devops_routes()" in app
    lib = open(os.path.join(ROOT, "static", "js", "emailLibrary.js"), encoding="utf-8").read()
    assert lib.count("addFillChatAreaButton(content") == 3            # inbox, email tab, email window
    inbound = open(os.path.join(ROOT, "static", "js", "inboundMail.js"), encoding="utf-8").read()
    assert "addFillChatAreaButton(" in inbound
    js = open(os.path.join(ROOT, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert "In use now:" in js and "server busy" in js
