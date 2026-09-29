"""Chats a restart cut off mid-reply carry on by themselves (src/restart_resume.py).

Asked for on 2026-09-29: "after deploying are we able to auto continue when a
chat gets forced stopped?"
"""
import asyncio
import json
import os
import time

import pytest

from src import agent_runs, restart_resume

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def data(tmp_path, monkeypatch):
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    return tmp_path


def test_remembered_chats_are_taken_once_and_only_recent(data):
    restart_resume.remember(["chat-a", "chat-b", ""])
    with open(data / "resume_after_restart.json") as f:
        stored = json.load(f)["chats"]
    stored["chat-old"] = time.time() - restart_resume.RESUME_WITHIN_S - 60
    with open(data / "resume_after_restart.json", "w") as f:
        json.dump({"chats": stored}, f)
    assert sorted(restart_resume.take()) == ["chat-a", "chat-b"]      # the old one is not replayed
    assert not (data / "resume_after_restart.json").exists()
    assert restart_resume.take() == []                                 # and never twice


def test_each_is_started_again_with_the_reason(data, monkeypatch):
    calls = []
    import src.screen_control_resume as scr
    monkeypatch.setattr(scr, "start_turn", lambda sid, prompt, **kw: calls.append((sid, prompt, kw)) or sid != "gone")
    restart_resume.remember(["chat-a", "gone"])
    assert restart_resume.resume_pending() == ["chat-a"]
    sid, prompt, kw = next(c for c in calls if c[0] == "chat-a")
    assert prompt.startswith("[Continued after a restart]") and "do not repeat" in prompt
    assert kw["note_source"] == "resumed_after_restart"


def test_a_running_reply_is_noted_and_stopped_before_a_restart(data):
    saved = []

    async def reply():
        try:
            yield "data: {}\n\n"
            await asyncio.sleep(30)
            yield "data: {}\n\n"
        except asyncio.CancelledError:
            saved.append("partial saved")
            raise

    async def go():
        agent_runs.start("chat-live", reply())
        await asyncio.sleep(0.05)
        assert "chat-live" in agent_runs.active_sessions()
        restart_resume.remember_active()
        stopped = await agent_runs.stop_all(timeout=2)
        return stopped

    assert asyncio.run(go()) == 1
    assert saved == ["partial saved"]
    assert "chat-live" in restart_resume.take()
    agent_runs._RUNS.pop("chat-live", None)


def test_deploy_shutdown_and_startup_are_wired():
    deploy = open(os.path.join(ROOT, "routes", "deploy_routes.py"), encoding="utf-8").read()
    assert deploy.index("restart_resume.remember_active()") < deploy.index("agent_runs.stop_all(timeout=10)")
    app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    shut = app[app.index("async def _shutdown_event():"):]
    assert shut.index("restart_resume.remember_active()") < shut.index("agent_runs.stop_all(timeout=8)")
    assert "restart_resume.resume_later()" in app
    js = open(os.path.join(ROOT, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert "Continued after a restart" in js
