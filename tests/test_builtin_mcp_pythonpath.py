"""Built-in Python MCP subprocesses must keep the parent's PYTHONPATH.

register_builtin_servers() and McpManager._reconnect_builtin() both used to
pass env={"PYTHONPATH": base_dir}, which replaces PYTHONPATH wholesale instead
of extending it. In a container or dev venv launch where PYTHONPATH already
carries the active environment's site-packages, the built-in MCP subprocess
lost that path entirely and failed to import its own dependencies - only the
app root survived. builtin_python_env() prepends the app root and keeps every
existing entry (deduplicated) instead of discarding them.
"""
import os

from src.builtin_mcp import builtin_python_env


def test_builtin_python_env_preserves_existing_pythonpath(monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(["/app/venv/lib/python3.13/site-packages", "/app", "/extra"]),
    )

    env = builtin_python_env("/app")

    assert env == {
        "PYTHONPATH": os.pathsep.join(["/app", "/app/venv/lib/python3.13/site-packages", "/extra"])
    }


def test_builtin_python_env_uses_app_root_without_existing_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)

    assert builtin_python_env("/srv/odysseus") == {"PYTHONPATH": "/srv/odysseus"}


def test_builtin_python_env_does_not_duplicate_the_app_root(monkeypatch):
    # The app root may already be on PYTHONPATH (e.g. re-registering after a
    # crash); it must appear once, not twice.
    monkeypatch.setenv("PYTHONPATH", "/app")

    assert builtin_python_env("/app") == {"PYTHONPATH": "/app"}


def test_register_builtin_servers_uses_builtin_python_env(monkeypatch):
    """register_builtin_servers() must route through builtin_python_env()
    rather than building its own PYTHONPATH dict, so the two spawn sites
    (initial registration and crash-reconnect) cannot drift apart."""
    import asyncio

    import src.builtin_mcp as builtin_mcp

    monkeypatch.setenv("PYTHONPATH", "/existing")
    seen_envs = []

    class FakeManager:
        async def connect_server(self, **kwargs):
            seen_envs.append(kwargs.get("env"))
            return True

    monkeypatch.setattr(builtin_mcp, "MCP_DISABLED", False)
    asyncio.run(builtin_mcp.register_builtin_servers(FakeManager()))

    assert seen_envs, "no built-in server attempted to connect"
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(builtin_mcp.__file__)))
    for env in seen_envs:
        assert env == builtin_mcp.builtin_python_env(base_dir)
        assert "/existing" in env["PYTHONPATH"].split(os.pathsep)


def test_reconnect_builtin_uses_builtin_python_env(monkeypatch):
    """McpManager._reconnect_builtin() must also route through
    builtin_python_env() so a crash-reconnected built-in server keeps the
    same PYTHONPATH behavior as the initial registration."""
    import asyncio

    from src.mcp_manager import McpManager

    monkeypatch.setenv("PYTHONPATH", "/existing")
    manager = McpManager()

    seen_envs = []

    async def fake_disconnect(_server_id):
        return None

    async def fake_connect_server(**kwargs):
        seen_envs.append(kwargs.get("env"))
        return True

    monkeypatch.setattr(manager, "disconnect_server", fake_disconnect)
    monkeypatch.setattr(manager, "connect_server", fake_connect_server)

    from src.builtin_mcp import _BUILTIN_SERVERS

    server_id = next(iter(_BUILTIN_SERVERS))
    assert asyncio.run(manager._reconnect_builtin(server_id)) is True

    assert seen_envs
    assert "/existing" in seen_envs[0]["PYTHONPATH"].split(os.pathsep)
