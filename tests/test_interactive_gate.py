"""Unit tests for the foreground-activity gate (src/interactive_gate.py).

Scheduled/background tasks should pause while the user is actively using
Odysseus (an in-flight request, a request that just finished, or a live chat
stream) and be free to run again once things go quiet. Purely passive polling
endpoints must never count as "the user is here", otherwise a background
task could never find a quiet window while any tab is merely open.
"""
import asyncio

import pytest

from src import interactive_gate as gate


@pytest.fixture(autouse=True)
def _reset_gate_state(monkeypatch):
    # Each test gets a clean slate regardless of execution order.
    monkeypatch.setattr(gate, "_ACTIVE_REQUESTS", 0)
    monkeypatch.setattr(gate, "_LAST_ACTIVITY", 0.0)
    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "true")
    monkeypatch.setenv("BACKGROUND_TASK_QUIET_MS", "20")
    monkeypatch.setenv("BACKGROUND_TASK_MAX_WAIT_SECONDS", "0")


# -- should_track_interactive_request -----------------------------------------

def test_passive_exact_paths_are_not_tracked():
    assert gate.should_track_interactive_request("/api/tasks/notifications") is False
    assert gate.should_track_interactive_request("/api/email/urgency-state") is False
    assert gate.should_track_interactive_request("/api/email/unread-state") is False


def test_passive_prefixes_are_not_tracked():
    assert gate.should_track_interactive_request("/api/chat/stream_status/abc") is False
    assert gate.should_track_interactive_request("/api/health") is False


def test_real_interactive_paths_are_tracked():
    assert gate.should_track_interactive_request("/api/chat_stream") is True
    assert gate.should_track_interactive_request("/api/tasks", method="POST") is True


def test_options_requests_are_never_tracked():
    assert gate.should_track_interactive_request("/api/chat_stream", method="OPTIONS") is False


def test_disabled_gate_never_tracks(monkeypatch):
    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "false")
    assert gate.should_track_interactive_request("/api/chat_stream") is False


# -- has_foreground_activity / track_interactive_request ----------------------

def test_no_activity_means_not_active():
    assert gate.has_foreground_activity() is False


def test_in_flight_request_counts_as_active():
    async def _run():
        async with gate.track_interactive_request("/api/chat_stream", "POST"):
            assert gate.has_foreground_activity() is True
        # Right after the request ends we're still inside the quiet window.
        assert gate.has_foreground_activity() is True

    asyncio.run(_run())


def test_activity_clears_after_the_quiet_window():
    async def _run():
        async with gate.track_interactive_request("/api/chat_stream", "POST"):
            pass
        # BACKGROUND_TASK_QUIET_MS=20 in the fixture above.
        await asyncio.sleep(0.05)
        assert gate.has_foreground_activity() is False

    asyncio.run(_run())


def test_active_chat_stream_counts_as_foreground_activity(monkeypatch):
    from routes import chat_routes
    monkeypatch.setattr(chat_routes, "_active_streams", {"sess-1": {"status": "streaming"}})
    assert gate.has_foreground_activity() is True


def test_running_agent_run_counts_as_foreground_activity(monkeypatch):
    from src import agent_runs

    class _FakeRun:
        status = "running"

    monkeypatch.setattr(agent_runs, "_RUNS", {"sess-1": _FakeRun()})
    assert gate.has_foreground_activity() is True


# -- wait_for_interactive_quiet ------------------------------------------------

def test_wait_returns_immediately_when_already_quiet():
    async def _run():
        waited = await gate.wait_for_interactive_quiet("test")
        assert waited is False

    asyncio.run(_run())


def test_wait_blocks_until_the_request_finishes():
    async def _run():
        async with gate.track_interactive_request("/api/chat_stream", "POST"):
            task = asyncio.create_task(gate.wait_for_interactive_quiet("test"))
            await asyncio.sleep(0.05)
            assert not task.done(), "must not return while a request is in flight"
        waited = await asyncio.wait_for(task, timeout=1)
        assert waited is True

    asyncio.run(_run())


def test_wait_blocks_while_a_chat_stream_is_live(monkeypatch):
    from routes import chat_routes
    monkeypatch.setattr(chat_routes, "_active_streams", {"sess-1": {"status": "streaming"}})

    async def _run():
        task = asyncio.create_task(gate.wait_for_interactive_quiet("test"))
        await asyncio.sleep(0.05)
        assert not task.done()
        monkeypatch.setattr(chat_routes, "_active_streams", {})
        waited = await asyncio.wait_for(task, timeout=1)
        assert waited is True

    asyncio.run(_run())
