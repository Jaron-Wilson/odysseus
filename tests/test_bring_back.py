"""Bring a background run back into the chat.

Asked for on 2026-09-28: "after pushing a task to background let me pull it
back into the chat. like for example I had a few questions to get right and a
couple code changes, so I pushed the claude to background, now I want it back."
"""
import asyncio
import json
import os
import stat
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import claude_code_approvals as approvals
from src import claude_code_jobs as jobs
from src.agent_tools import claude_code_tool as cct

FAKE = r'''#!{py}
import json, sys, time
sys.stdin.read()
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Working on the changes."}}]}}}}), flush=True)
time.sleep({delay})
print(json.dumps({{"type": "result", "result": "Both code changes made.", "is_error": False}}), flush=True)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def make(delay):
        exe = bindir / "claude"
        exe.write_text(FAKE.format(py=sys.executable, delay=delay))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(approvals, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(approvals, "APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(jobs, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(cct, "DEFAULT_ENGINE", "claude")
    monkeypatch.setattr(cct, "PROGRESS_INTERVAL_S", 0.2)
    jobs._JOBS.clear()
    posted = []

    class _SM:                                   # the real posting code writes here
        def add_message(self, sid, msg):
            import re
            m = re.search(r"job `([0-9a-f]{8})`", msg.content)
            posted.append(m.group(1) if m else msg.content[:40])

        def save_sessions(self):
            pass
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: _SM())
    return make, tmp_path, posted


async def _start_and_background(tmp_path):
    task = asyncio.create_task(cct.ClaudeCodeTool().execute(
        json.dumps({"action": "ask", "prompt": "questions and a couple of code changes", "cwd": str(tmp_path)}),
        {"session_id": "chat-1"}))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if jobs.list_jobs():
            break
    job = jobs.list_jobs()[0]
    await asyncio.sleep(0.3)
    assert jobs.detach(job.id)
    first = await asyncio.wait_for(task, timeout=5)
    assert first["background"]
    return job


def test_brought_back_the_chat_follows_it_and_gets_the_result(env):
    make, tmp_path, posted = env
    make(delay=2)

    async def run():
        job = await _start_and_background(tmp_path)
        seen = []

        async def progress(p):
            seen.append(p)
        out = await cct.ClaudeCodeTool().execute(json.dumps({"action": "attach", "job_id": job.id}),
                                                 {"session_id": "chat-1", "progress_cb": progress})
        await asyncio.wait_for(job.task, timeout=10)
        return job, out, seen
    job, out, seen = asyncio.run(run())
    assert out["brought_back"] and out["output"] == "Both code changes made." and out["exit_code"] == 0
    assert any("Working on the changes." in p["tail"] for p in seen) and seen[0]["can_background"]
    assert posted == []                                # no second "background job finished" post


def test_sent_away_again_it_is_posted_as_usual(env):
    make, tmp_path, posted = env
    make(delay=2)

    async def run():
        job = await _start_and_background(tmp_path)
        attach = asyncio.create_task(cct.ClaudeCodeTool().execute(
            json.dumps({"action": "attach", "job_id": job.id}), {"session_id": "chat-1"}))
        await asyncio.sleep(0.5)
        assert job.attached and jobs.detach(job.id)       # Send to background, from the card
        out = await asyncio.wait_for(attach, timeout=5)
        await asyncio.wait_for(job.task, timeout=10)
        return job, out
    job, out = asyncio.run(run())
    assert out["background"] and "Sent back to the background" in out["output"]
    assert posted == [job.id]


def test_the_route_starts_or_queues_the_turn(env, monkeypatch):
    make, tmp_path, posted = env
    import routes.claude_code_routes as ccr
    import src.screen_control_resume as scr
    from src import chat_queue
    monkeypatch.setattr(ccr, "_auth_disabled", lambda: True)
    monkeypatch.setattr(ccr, "get_current_user", lambda r: "")
    started, queued = [], []
    busy = {"v": False}
    monkeypatch.setattr(scr, "start_turn", lambda sid, prompt, **kw: (started.append(prompt) or True) if not busy["v"] else False)
    monkeypatch.setattr(chat_queue, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(chat_queue, "QUEUE_FILE", str(tmp_path / "chat_queue.json"))
    j = jobs.register(chat_session_id="chat-1", owner="", action="execute", cwd="/w", model="m",
                      engine="opencode", prompt="p")
    j.detached = True
    app = FastAPI()
    app.include_router(ccr.setup_claude_code_routes())
    c = TestClient(app)
    r = c.post(f"/api/claude_code/jobs/{j.id}/foreground").json()
    assert r["resuming"] and r["chat_session_id"] == "chat-1"
    assert f'"job_id": "{j.id}"' in started[0] and "· OpenCode]" in started[0]
    busy["v"] = True
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "is_active", lambda sid: sid == "chat-1")
    r = c.post(f"/api/claude_code/jobs/{j.id}/foreground").json()
    assert r["queued"] and "already running in that chat" in r["reason"]
    # Clicked again (nothing seemed to happen): still one queued prompt.
    assert c.post(f"/api/claude_code/jobs/{j.id}/foreground").json()["queued"]
    items = chat_queue.get("chat-1")["items"]
    assert len(items) == 1 and items[0]["label"] == f"Bring back OpenCode job {j.id}"
    assert '"action": "attach"' in items[0]["text"]
    jobs.finish(j, "done")
    assert c.post(f"/api/claude_code/jobs/{j.id}/foreground").status_code == 409


def test_a_run_followed_in_its_chat_is_not_background(env):
    # Seen live: brought back, the chip still said "running · background"
    # and offered "Bring back to chat" again, so nothing seemed to happen.
    j = jobs.register(chat_session_id="chat-1", owner="", action="execute", cwd="/w", model="m",
                      engine="opencode", prompt="p")
    j.detached = True
    assert j.public()["background"] and not j.public()["attached"]
    j.attached = True
    assert not j.public()["background"] and j.public()["attached"]


def test_the_chip_lists_every_run_in_the_chat():
    # Seen live: "it had 2 background tasks per 1 chat and it only showed one".
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(here, "static", "js", "bgTasks.js"), encoding="utf-8").read()
    assert "${here.map((j) => _chipRow(j, true)).join('')}" in js
    assert "j.chat_session_id === sid && (j.background || j.attached)" in js
    assert "window.chatModule.watchQueue(chat)" in js


def test_queue_items_keep_a_label_and_are_not_added_twice(tmp_path, monkeypatch):
    from src import chat_queue
    monkeypatch.setattr(chat_queue, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(chat_queue, "QUEUE_FILE", str(tmp_path / "q.json"))
    chat_queue.add("c", "a long server-written prompt", label="Bring back job 1", key="k1")
    chat_queue.add("c", "a long server-written prompt", label="Bring back job 1", key="k1")
    chat_queue.add("c", "typed by the user")
    items = chat_queue.get("c")["items"]
    assert [i.get("label") for i in items] == ["Bring back job 1", None]
    assert chat_queue.claim("c")["text"] == "a long server-written prompt"


def _src(*p):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(here, *p), encoding="utf-8").read()


def test_bringing_back_shows_the_live_output_like_watch_here():
    # Asked for: "if I bring it back from background it should show how we do
    # the watch here section". Seen live: only "Working..." for the whole run,
    # and the model took over two minutes to get to the attach call.
    bg = _src("static", "js", "bgTasks.js")
    assert "watchInChat(b.dataset.bringBack, chat, { reloadWhenDone: false });" in bg
    assert "(into || box).appendChild(card);" in bg and "if (reloadWhenDone) setTimeout(" in bg
    chat = _src("static", "js", "chat.js")
    assert "json.type === 'tool_progress' && json.job_id && !_watchedJobs.has(json.job_id)" in chat
    assert "{ into: holder.querySelector('.body'), reloadWhenDone: false }" in chat


def test_button_prompts_are_a_short_note_and_recall_no_skills():
    import re
    import routes.claude_code_routes as ccr
    renderer = _src("static", "js", "chatRenderer.js")
    pattern = re.search(r"String\(textRaw \|\| ''\)\.match\(/(.+?)/\);", renderer).group(1)
    pattern = pattern.replace("\\u00b7", "·")
    for prompt in (ccr._BRING_BACK_PROMPT.format(job_id="878698a8", engine="OpenCode", args="{}"),
                   ccr._EXECUTE_PROMPT.format(run_id="beae9b52", engine="OpenCode", args="{}")):
        assert re.match(pattern, prompt), prompt[:60]
        assert prompt.startswith(("[Brought back from the background ·", "[Plan approved ·"))
    loop = _src("src", "agent_loop.py")
    assert "if _skill_max_injected > 0 and not _button_prompt else []" in loop
