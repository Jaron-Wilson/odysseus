"""What's new (src/whats_new.py, routes/whats_new_routes.py).

Asked for on 2026-10-01: "add a whats new page? on it? and then basically for
each pr i can see then ask questions too." What matters:

  * the list is the "Merge pull request #N" commits on the checkout's
    first-parent history, newest first, with each one's files and line counts
    from git alone;
  * GitHub adds the description, author and date, and a GitHub that cannot be
    reached (or a rate limit) leaves the cached and the git data in place;
  * a merge is "running" when it is an ancestor of the commit the server
    started with, and a checkout ahead of that asks for a restart;
  * "Ask about this" makes a new chat on the default model whose context (the
    PR's title, description, files and a capped diff) is added to every turn;
  * "new since you last looked" is kept per user;
  * the agent's whats_new tool lists, searches and gets PRs.
"""
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from src import build_info
from src import whats_new as wn


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _merge(repo, n, title, files):
    _git(repo, "checkout", "-q", "-b", f"f{n}")
    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", f"work for {n}")
    _git(repo, "checkout", "-q", "dev")
    _git(repo, "merge", "-q", "--no-ff", f"f{n}", "-m", f"Merge pull request #{n} from Jaron-Wilson/f{n}", "-m", title)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "dev")
    (r / "app.py").write_text("x = 1\n")
    _git(r, "add", "app.py")
    _git(r, "commit", "-q", "-m", "start")
    shas = {
        10: _merge(r, 10, "Voice call: talk to the agent", {"src/call.py": "a = 1\nb = 2\n", "tests/test_call.py": "ok\n"}),
        11: _merge(r, 11, "Theme colors", {"static/theme.css": "body {}\n"}),
    }
    # A merge that is not a PR, and a direct commit: neither is listed.
    _git(r, "checkout", "-q", "-b", "side")
    (r / "side.txt").write_text("s\n")
    _git(r, "add", "side.txt")
    _git(r, "commit", "-q", "-m", "side work")
    _git(r, "checkout", "-q", "dev")
    _git(r, "merge", "-q", "--no-ff", "side", "-m", "Merge branch 'side' into dev")
    _git(r, "commit", "-q", "--allow-empty", "-m", "Fix #99 later")
    shas[12] = _merge(r, 12, "Phone calls", {"src/phone.py": "p = 1\n"})
    monkeypatch.setattr(wn, "REPO_DIR", str(r))
    monkeypatch.setattr(wn, "CACHE_DIR", str(tmp_path / "data" / "whats_new"))
    wn._merges_cache.clear()
    wn._ancestors.clear()
    monkeypatch.setattr(wn, "_state", {"refreshed": 0.0, "github_error": "", "summarizing": False})
    monkeypatch.setattr(build_info, "INFO", {"pr": 12, "commit": shas[12][:8], "commit_full": shas[12],
                                             "branch": "dev", "started": 1})
    monkeypatch.setattr(wn, "github_repo", lambda: "Jaron-Wilson/odysseus")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return SimpleNamespace(path=r, shas=shas)


# ── git ─────────────────────────────────────────────────────────────────

def test_merges_come_from_git_newest_first_with_their_files(repo):
    ms = wn.merges()
    assert [m["pr"] for m in ms] == [12, 11, 10]
    first = ms[2]
    assert first["title"] == "Voice call: talk to the agent" and first["branch"] == "f10"
    assert first["sha"] == repo.shas[10] and first["parent"]
    assert {f["path"]: (f["additions"], f["deletions"]) for f in first["files"]} == {
        "src/call.py": (2, 0), "tests/test_call.py": (1, 0)}
    assert first["additions"] == 3
    # The side branch's file belongs to no PR.
    assert all("side.txt" not in [f["path"] for f in m["files"]] for m in ms)


def test_running_follows_the_commit_the_server_started_with(repo, monkeypatch):
    d = wn.entries("alice")
    assert all(e["running"] for e in d["entries"]) and not d["restart_needed"] and d["pending"] == []
    # Started before #12 was pulled: #12 is merged but not running.
    monkeypatch.setattr(build_info, "INFO", dict(build_info.INFO, commit_full=repo.shas[11], commit=repo.shas[11][:8]))
    d = wn.entries("alice")
    assert {e["pr"]: e["running"] for e in d["entries"]} == {12: False, 11: True, 10: True}
    assert d["restart_needed"] and d["pending"] == [12]
    assert d["running_commit"] == repo.shas[11][:8] and d["head_commit"] == repo.shas[12][:8]
    # Same answer as git merge-base --is-ancestor, from a short hash too.
    assert wn.is_running(repo.shas[10], repo.shas[11][:8]) and not wn.is_running(repo.shas[12], repo.shas[11])


# ── GitHub ──────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}

    def json(self):
        return self._data


class _FakeClient:
    calls = []
    script = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        _FakeClient.calls.append({"url": url, "params": params, "headers": headers})
        r = _FakeClient.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _pr(n, title, body, login="Jaron-Wilson"):
    return {"number": n, "title": title, "body": body, "user": {"login": login}, "merged_at": "2026-09-30T12:00:00Z",
            "html_url": f"https://github.com/Jaron-Wilson/odysseus/pull/{n}", "merge_commit_sha": "x",
            "head": {"ref": f"f{n}"}}


@pytest.fixture
def fake_github(monkeypatch):
    import httpx
    _FakeClient.calls, _FakeClient.script = [], []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient


def test_github_adds_descriptions_and_an_unchanged_answer_is_not_refetched(repo, fake_github):
    fake_github.script = [_Resp(200, [_pr(12, "Phone calls: call a number", "## Why\n\nYou can **call** Odysseus now.\n\nMore."),
                                      _pr(11, "Theme colors", ""), {"number": 9, "merged_at": None}],
                                headers={"etag": 'W/"abc"'})]
    assert wn.refresh(summaries=False)["github_error"] == ""
    c = fake_github.calls[0]
    assert c["url"].endswith("/repos/Jaron-Wilson/odysseus/pulls")
    assert c["params"]["base"] == "dev" and c["params"]["state"] == "closed"
    assert "Authorization" not in c["headers"]               # no token configured, none sent
    e = {x["pr"]: x for x in wn.entries(None)["entries"]}
    assert e[12]["title"] == "Phone calls: call a number" and e[12]["author"] == "Jaron-Wilson"
    assert e[12]["summary"] == "You can call Odysseus now." and e[12]["summary_source"] == "description"
    assert e[12]["merged_at"] == "2026-09-30T12:00:00Z" and e[12]["from_github"]
    # #10 is older than page 1 reached: page 2 was asked for... and was short.
    assert len(fake_github.calls) == 1 or fake_github.calls[1]["params"]["page"] == 2
    # Again: the ETag goes along and a 304 keeps what was cached.
    fake_github.calls.clear()
    fake_github.script = [_Resp(304), _Resp(200, [])]
    wn.refresh(summaries=False)
    assert fake_github.calls[0]["headers"]["If-None-Match"] == 'W/"abc"'
    assert {x["pr"]: x for x in wn.entries(None)["entries"]}[12]["title"] == "Phone calls: call a number"


def test_a_configured_token_is_used(repo, fake_github, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    fake_github.script = [_Resp(200, [])]
    wn.refresh(summaries=False)
    assert fake_github.calls[0]["headers"]["Authorization"] == "Bearer ghp_secret"
    assert "ghp_secret" not in json.dumps(wn.entries(None))     # never shown


def test_offline_and_rate_limited_keep_git_and_the_cache(repo, fake_github):
    import httpx
    fake_github.script = [httpx.ConnectError("no network")]
    wn.refresh(summaries=False)
    d = wn.entries(None)
    assert "could not be reached" in d["github"]["error"]
    e = {x["pr"]: x for x in d["entries"]}
    assert e[10]["title"] == "Voice call: talk to the agent" and e[10]["file_count"] == 2   # git alone
    assert e[10]["url"] == "https://github.com/Jaron-Wilson/odysseus/pull/10"
    fake_github.script = [_Resp(200, [_pr(10, "Voice call (from GitHub)", "Talk to it.")])]
    wn.refresh(summaries=False)
    fake_github.script = [_Resp(403, {"message": "rate limit"}, headers={"x-ratelimit-reset": str(int(time.time()) + 600)})]
    wn.refresh(summaries=False)
    d = wn.entries(None)
    assert "rate limit" in d["github"]["error"] and d["github"]["fetched"]
    assert {x["pr"]: x for x in d["entries"]}[10]["title"] == "Voice call (from GitHub)"


# ── Summaries ───────────────────────────────────────────────────────────

def test_first_paragraph_skips_headings_comments_and_markup():
    body = "<!-- template -->\n## Summary\n\n- Calls now **ring** on [your phone](https://x).\n- And more\n\n## Tests\n\nran"
    assert wn.first_paragraph(body) == "Calls now ring on your phone. And more"
    assert wn.first_paragraph("") == ""
    assert wn.first_paragraph("word " * 200).endswith("...")


def test_summaries_are_written_once_by_the_utility_model(repo, fake_github, monkeypatch):
    fake_github.script = [_Resp(200, [_pr(12, "Phone calls", "Call it.")])]
    wn.refresh(summaries=False)
    import src.endpoint_resolver as er
    import src.llm_core as lc
    monkeypatch.setattr(er, "resolve_endpoint", lambda prefix, owner=None: ("http://u/v1/chat/completions", "util-model", {}))
    asked = []

    def fake_llm(url, model, messages, **kw):
        asked.append(messages[1]["content"])
        return f"<think>hm</think>You can phone Odysseus \u2014 and talk. ({len(asked)})"
    monkeypatch.setattr(lc, "llm_call", fake_llm)
    assert wn.summarize_missing(limit=5) == 3
    assert "Title: Phone calls" in asked[0] and "src/phone.py" in asked[0]
    e = {x["pr"]: x for x in wn.entries(None)["entries"]}
    assert e[12]["summary"] == "You can phone Odysseus, and talk. (1)" and e[12]["summary_source"] == "ai"
    assert wn.summarize_missing(limit=5) == 0                 # cached, not asked again
    # An edited description gets a new one; until then the description stands in.
    fake_github.script = [_Resp(200, [_pr(12, "Phone calls", "Call it, now with video.")])]
    wn.refresh(force=True, summaries=False)
    e = {x["pr"]: x for x in wn.entries(None)["entries"]}
    assert e[12]["summary_source"] == "description" and e[12]["summary"] == "Call it, now with video."


def test_no_utility_model_means_no_summaries(repo, monkeypatch):
    import src.endpoint_resolver as er
    monkeypatch.setattr(er, "resolve_endpoint", lambda prefix, owner=None: (None, None, None))
    assert wn.summarize_missing() == 0


# ── New since you last looked ───────────────────────────────────────────

def test_last_seen_is_per_user(repo):
    now = time.time()
    # Never looked: the last week's merges are new (all of them here).
    assert wn.unseen_count("alice") == 3 and wn.unseen_count("bob") == 3
    wn.mark_seen("alice", now + 5)
    assert wn.unseen_count("alice") == 0 and wn.unseen_count("bob") == 3
    assert not any(e["new"] for e in wn.entries("alice")["entries"])
    assert all(e["new"] for e in wn.entries("bob")["entries"])
    assert wn.last_seen("bob") == 0 and wn.last_seen("alice") == pytest.approx(now + 5)
    # Single-user mode (no login) has its own record.
    wn.mark_seen(None, now + 5)
    assert wn.unseen_count(None) == 0 and wn.unseen_count("bob") == 3


# ── Ask about this ──────────────────────────────────────────────────────

def test_ask_context_has_the_pr_and_a_capped_diff(repo, monkeypatch):
    r = repo.path
    big = "".join(f"line {i} " + "x" * 60 + "\n" for i in range(3000))       # ~200 KB
    files = {f"src/many/f{i}.py": f"v = {i}\n" for i in range(80)}
    files.update({"src/huge.py": big, "package-lock.json": '{"a": 1}\n'})
    sha = _merge(r, 13, "A very big one", files)
    monkeypatch.setattr(build_info, "INFO", dict(build_info.INFO, commit_full=repo.shas[12]))
    wn._merges_cache.clear()
    ctx = wn.build_ask_context([13])
    t = ctx["text"]
    assert ctx["prs"] == [13] and ctx["titles"] == ["A very big one"] and ctx["diff_cut"]
    assert "## PR #13: A very big one" in t and sha[:8] in t
    assert "Running on this server: no, waiting for a restart" in t
    assert "Files changed (82 files" in t and "...and 22 more files" in t      # 60 listed, the rest summed up
    assert "more lines of src/huge.py not shown" in t or "src/huge.py" in t.split("Diff not shown")[-1]
    assert "package-lock.json" in t.split("Diff not shown")[-1]               # noisy: named, not shown
    assert len(t) < wn.MAX_DIFF_CHARS + 12_000
    # Several share the budget.
    two = wn.build_ask_context([13, 10])
    assert two["prs"] == [13, 10] and "## PR #10: Voice call" in two["text"] and "+a = 1" in two["text"]
    assert len(two["text"]) < wn.MAX_DIFF_CHARS + 16_000
    assert wn.build_ask_context([999])["prs"] == []


def test_the_chat_keeps_its_context_on_every_turn(repo, tmp_path, monkeypatch):
    ctx = wn.build_ask_context([10])
    wn.save_ask("sid-1", "alice", ctx)
    assert wn.chat_context("sid-1") == ctx["text"]
    assert wn.chat_context("other") == "" and wn.chat_context("../../etc/passwd") == ""
    assert wn.ask_info("sid-1") == {"prs": [10], "titles": ["Voice call: talk to the agent"]}

    # build_chat_context adds it after the system preface, protected from trimming.
    import asyncio
    from routes import chat_helpers as ch

    async def fake_preprocess(chat_handler, message, att_ids, sess, **kw):
        return ch.PreprocessedMessage(enhanced_message=message, user_content=message, text_for_context=message,
                                      youtube_transcripts=[], attachment_meta=[])

    async def fake_compact(sess, url, model, messages, headers, owner=None):
        return messages, 8192, False
    monkeypatch.setattr(ch, "preprocess", fake_preprocess)
    monkeypatch.setattr(ch, "extract_preset", lambda h, p: ch.PresetInfo(temperature=0.7, max_tokens=512,
                                                                          system_prompt="sys", character_name=None))
    monkeypatch.setattr(ch, "add_user_message", lambda sess, h, pre, incognito=False: sess.msgs.append(
        {"role": "user", "content": pre.user_content}))
    monkeypatch.setattr(ch, "load_prefs_for_user", lambda u: {})
    monkeypatch.setattr(ch, "get_current_user", lambda r: "alice")
    monkeypatch.setattr(ch, "normalize_model_id", lambda *a, **k: None)
    monkeypatch.setattr(ch, "_normalize_model_id_from_cache", lambda s: None)
    monkeypatch.setattr(ch, "maybe_compact", fake_compact)
    monkeypatch.setattr(ch, "fire_message_event", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(ch, "trim_for_context", lambda m, n: seen.setdefault("m", m))
    sess = SimpleNamespace(endpoint_url="http://x/v1", model="m", headers={}, msgs=[{"role": "assistant", "content": "hi"}])
    sess.get_context_messages = lambda: list(sess.msgs)
    proc = SimpleNamespace(build_context_preface=lambda **k: ([{"role": "system", "content": "sys"}], [], []))
    for sid, expect in (("sid-1", True), ("plain-chat", False)):
        seen.clear()
        out = asyncio.run(ch.build_chat_context(sess, SimpleNamespace(), SimpleNamespace(), proc,
                                                "what does it change?", sid))
        msgs = out.messages
        has = [m for m in msgs if "PR #10: Voice call" in (m.get("content") or "")]
        assert bool(has) is expect
        if expect:
            assert msgs[0]["role"] == "system" and msgs[1] is has[0]
            assert has[0]["role"] == "user" and has[0]["_protected"] and "+a = 1" in has[0]["content"]


def test_trimming_keeps_the_pr_context():
    from src.context_compactor import trim_for_context
    pr = {"role": "user", "content": "PR context " * 400, "_protected": True}
    msgs = [{"role": "system", "content": "sys"}, pr] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "chat " * 300} for i in range(30)]
    out = trim_for_context(msgs, 4000)
    assert pr in out and len(out) < len(msgs)


class _Sess:
    def __init__(self, sid, name, owner):
        self.id, self.name, self.owner, self.history = sid, name, owner, []

    def add_message(self, m):
        self.history.append(m)


@pytest.fixture
def client(repo, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.whats_new_routes as wr
    from routes.whats_new_routes import setup_whats_new_routes
    monkeypatch.setenv("AUTH_ENABLED", "false")
    made = {}
    monkeypatch.setattr(wr, "default_chat", lambda user, admin: {"model": "gpt-x", "endpoint_id": "ep1",
                                                                          "endpoint_url": "http://x"})

    def fake_create(sm, owner, model, endpoint_id, name):
        s = _Sess("new-sid", name, owner)
        made.update(model=model, endpoint_id=endpoint_id, sess=s)
        return s.id, s
    monkeypatch.setattr(wr, "new_chat", fake_create)
    sm = SimpleNamespace(get_session=lambda sid: made["sess"] if sid == "new-sid" else (_ for _ in ()).throw(KeyError(sid)))
    monkeypatch.setattr(wn, "refresh", lambda *a, **k: {"ok": True, "count": 0, "github_error": ""})
    app = FastAPI()
    app.include_router(setup_whats_new_routes(sm))
    return TestClient(app), made


def test_routes_list_mark_seen_and_ask(client):
    c, made = client
    d = c.get("/api/whats-new").json()
    assert [e["pr"] for e in d["entries"]] == [12, 11, 10] and d["new_count"] == 3
    assert c.get("/api/whats-new/unseen").json()["count"] == 3
    c.post("/api/whats-new/seen")
    assert c.get("/api/whats-new/unseen").json()["count"] == 0
    r = c.post("/api/whats-new/ask", json={"prs": [10]}).json()
    assert r["id"] == "new-sid" and r["name"] == "About #10 Voice call: talk to the agent"
    assert r["placeholder"] == "Ask about #10 Voice call: talk to the agent..."
    assert made["model"] == "gpt-x" and made["endpoint_id"] == "ep1"
    # Nothing was sent to the model: one assistant note, no user turn.
    hist = made["sess"].history
    assert len(hist) == 1 and hist[0].role == "assistant" and "#10" in hist[0].content
    assert "PR #10" in wn.chat_context("new-sid")
    assert c.get("/api/whats-new/ask/new-sid").json()["placeholder"].startswith("Ask about #10")
    assert c.get("/api/whats-new/ask/nope").json() == {}
    many = c.post("/api/whats-new/ask", json={"prs": [12, 11, 10]}).json()
    assert many["name"] == "About #12, #11, #10" and many["placeholder"] == "Ask about #12, #11, #10..."
    assert c.post("/api/whats-new/ask", json={"prs": []}).status_code == 400
    assert c.post("/api/whats-new/ask", json={"prs": [555]}).status_code == 404


# ── The agent's tool ────────────────────────────────────────────────────

def test_the_agent_tool_lists_searches_and_gets(repo):
    out = wn.run_tool('{"action": "list"}')["output"]
    assert out.index("#12 Phone calls") < out.index("#10 Voice call")
    found = wn.run_tool('{"action": "list", "query": "what changed with the voice call?"}')["output"]
    assert found.splitlines()[1].startswith("#10 Voice call")
    one = wn.run_tool('{"action": "get", "pr": 10}')["output"]
    assert "PR #10: Voice call" in one and "src/call.py (+2 -0)" in one and "Running on this server: yes" in one
    assert wn.run_tool('{"pr": "#11"}')["output"].startswith("PR #11")
    assert wn.run_tool('{"action": "get", "pr": 404}')["exit_code"] == 1


def test_the_tool_runs_through_the_executor(repo):
    import asyncio
    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block
    desc, result = asyncio.run(execute_tool_block(ToolBlock("whats_new", '{"action": "get", "pr": 12}'),
                                                  session_id="s", owner="alice"))
    assert desc.startswith("whats_new") and "PR #12: Phone calls" in result["output"]


def test_the_tool_is_registered_everywhere():
    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS, ToolIndex
    from src.agent_loop import TOOL_SECTIONS
    assert "whats_new" in TOOL_TAGS and "whats_new" in BUILTIN_TOOL_DESCRIPTIONS and "whats_new" in TOOL_SECTIONS
    assert any(s["function"]["name"] == "whats_new" for s in FUNCTION_TOOL_SCHEMAS)
    blk = function_call_to_tool_block("whats_new", '{"action": "get", "pr": 127}')
    assert blk.tool_type == "whats_new" and json.loads(blk.content) == {"action": "get", "pr": 127}
    hints = [tools for kws, tools in ToolIndex._KEYWORD_HINTS.items() if "what changed" in kws]
    assert hints and "whats_new" in hints[0]


def test_the_page_is_in_the_taxonomy():
    from src import tool_pages
    p = tool_pages.page("what's new")
    assert p["key"] == "whats-new" and p["group"] == "system" and not p.get("adminOnly")
    for name in ("changelog", "release notes", "the whats new page", "updates", "what changed"):
        assert tool_pages.page(name)["key"] == "whats-new", name


@pytest.mark.parametrize("text", ["what changed with the voice call?", "what's new", "what did PR #127 do",
                                  "is that fix deployed yet", "show me the changelog"])
def test_questions_about_changes_offer_the_tool(text):
    # Without this, "what changed with the voice call?" read as small talk and
    # the turn went out with only the always-available tools.
    from src.agent_loop import _classify_agent_request
    intent = _classify_agent_request([{"role": "user", "content": text}], text)
    assert "changes" in intent["domains"] and not intent["low_signal"]


def test_small_talk_does_not():
    from src.agent_loop import _classify_agent_request
    assert "changes" not in _classify_agent_request([], "tell me a joke")["domains"]
