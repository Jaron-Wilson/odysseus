"""connect_all_enabled() must connect configured servers concurrently, each
bounded by its own timeout, instead of one at a time.

Before this fix, connect_all_enabled() looped over the enabled servers and
awaited connect_server() for each in turn, so a single slow or hung server
delayed every server configured after it, and the whole startup connect was
wrapped in one shared timeout in app.py. Now each server gets its own
asyncio.wait_for() timeout and all of them run under asyncio.gather(), so a
hung server only costs its own timeout and never blocks the others.
"""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.mcp_manager import McpManager


class _FakeQuery:
    def __init__(self, servers):
        self._servers = servers

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._servers


class _FakeDb:
    def __init__(self, servers):
        self._servers = servers

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._servers)

    def close(self):
        pass


def _fake_server(sid, name):
    return SimpleNamespace(
        id=sid, name=name, transport="stdio", command=f"cmd-{sid}",
        args=json.dumps([]), env=json.dumps({}), url=None,
    )


def _patch_db(monkeypatch, servers):
    """connect_all_enabled() does ``from src.database import McpServer,
    SessionLocal`` at call time, so patch whatever module is already
    registered at sys.modules["src.database"] (real or the conftest-installed
    stub used for fully isolated unit tests) rather than re-importing it,
    adding McpServer if the stub doesn't define one."""
    db = sys.modules["src.database"]
    monkeypatch.setattr(db, "McpServer", MagicMock(), raising=False)
    monkeypatch.setattr(db, "SessionLocal", lambda: _FakeDb(servers), raising=False)


@pytest.mark.asyncio
async def test_connect_all_enabled_runs_servers_concurrently(monkeypatch):
    """Three servers that each take ~0.3s must finish in about 0.3s total, not
    the ~0.9s a sequential loop would take."""
    servers = [_fake_server(i, f"server{i}") for i in (1, 2, 3)]
    _patch_db(monkeypatch, servers)

    manager = McpManager()

    async def fake_connect_server(**_kwargs):
        await asyncio.sleep(0.3)
        return True

    monkeypatch.setattr(manager, "connect_server", fake_connect_server)

    start = asyncio.get_event_loop().time()
    await manager.connect_all_enabled()
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < 0.6, f"connects did not overlap: took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_one_hung_server_does_not_block_or_delay_the_others(monkeypatch):
    """A server whose connect never returns must time out on its own and must
    not prevent the other, healthy servers from connecting and completing
    quickly."""
    servers = [_fake_server(1, "fast1"), _fake_server(2, "hung"), _fake_server(3, "fast2")]
    _patch_db(monkeypatch, servers)

    manager = McpManager()
    completed = []

    async def fake_connect_server(server_id, **_kwargs):
        if server_id == 2:
            await asyncio.sleep(999)  # never finishes on its own
        else:
            await asyncio.sleep(0.05)
            completed.append(server_id)
        return True

    monkeypatch.setattr(manager, "connect_server", fake_connect_server)

    start = asyncio.get_event_loop().time()
    # Use a short per-server timeout so the test doesn't actually wait 20s.
    await asyncio.gather(*(manager._connect_with_timeout(srv, timeout=0.2) for srv in servers))
    elapsed = asyncio.get_event_loop().time() - start

    assert set(completed) == {1, 3}
    assert elapsed < 1.0, f"the hung server blocked the others: took {elapsed:.2f}s"
    # The hung server's own timeout is recorded instead of being left silently
    # "connecting" forever.
    assert manager.get_server_status(2)["status"] == "error"
    assert "timed out" in manager.get_server_status(2)["error"].lower()


@pytest.mark.asyncio
async def test_connect_all_enabled_isolates_one_failing_server(monkeypatch):
    """A server whose connect_server() raises must not stop the others from
    being attempted (asyncio.gather over independent tasks, not a sequential
    loop that would abort partway through)."""
    servers = [_fake_server(1, "ok1"), _fake_server(2, "broken"), _fake_server(3, "ok2")]
    _patch_db(monkeypatch, servers)

    manager = McpManager()
    attempted = []

    async def fake_connect_server(server_id, **_kwargs):
        attempted.append(server_id)
        if server_id == 2:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(manager, "connect_server", fake_connect_server)

    await manager.connect_all_enabled()

    assert set(attempted) == {1, 2, 3}
