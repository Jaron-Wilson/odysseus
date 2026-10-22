"""Shared bits for the mod system tests (tests/test_mods_*.py)."""
import asyncio
import json
import os
import socket
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELLO = os.path.join(ROOT, "examples", "hello-mod")


@pytest.fixture
def mods_dir(tmp_path, monkeypatch):
    """A fresh mods folder, and no route from a mod to a real core."""
    from src.mods import registry, runtime, signing, events
    monkeypatch.setenv("ODYSSEUS_MODS_DIR", str(tmp_path / "mods"))
    # A mod started by a test must never reach a running Odysseus on 7000.
    monkeypatch.setenv("ODYSSEUS_INTERNAL_BASE", "http://127.0.0.1:9")
    monkeypatch.setattr(signing, "_frame_secret", None)
    registry._token_index = None
    runtime._states.clear()
    events._runtime_subs.clear()
    events._history.clear()
    yield tmp_path / "mods"
    registry._token_index = None
    runtime._states.clear()


def add_mod(manifest_overrides=None, *, enabled=True, permissions=None, mod_id="test-mod", access="admins"):
    """Put a mod straight into the registry (no install), returns (entry, token)."""
    from src.mods import manifest as mf, registry, signing, paths
    raw = {
        "id": mod_id, "name": "Test Mod", "version": "1.0.0",
        "runtime": {"type": "process", "command": ["python", "-m", "nothing"]},
        "permissions": permissions if permissions is not None else [],
    }
    raw.update(manifest_overrides or {})
    man = mf.validate(raw)
    token = signing.new_token()
    with registry._lock:
        data = registry._load()
        data["mods"][mod_id] = {"id": mod_id, "name": man["name"], "version": man["version"], "manifest": man,
                                "granted_permissions": man["permissions"], "enabled": enabled, "access": access,
                                "token": registry._encrypt(token), "source": {"type": "path", "path": "/x"}}
        registry._save(data)
    os.makedirs(paths.src_dir(mod_id), exist_ok=True)
    return registry.get(mod_id), token


def mark_running(mod_id, port):
    from src.mods import runtime
    st = runtime.state(mod_id)
    st.status = "running"
    st.port = port
    st.want_running = True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UvicornThread:
    """Serve an ASGI app on a real port (streaming needs a real socket)."""

    def __init__(self, app):
        import uvicorn
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    lifespan="off", ws="websockets", proxy_headers=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


def run(coro):
    return asyncio.run(coro)


def write_tools_cache(mod_id, tools):
    from src.mods import paths
    os.makedirs(paths.mod_dir(mod_id), exist_ok=True)
    with open(os.path.join(paths.mod_dir(mod_id), "tools.json"), "w") as f:
        json.dump({"tools": tools}, f)
