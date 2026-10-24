"""Tests for the background hosting-service tracker.

Verifies:
  - Launch registration & process identity tracking
  - Port & readiness discovery (localhost labeling, reachable URLs)
  - Process exits & restart reconciliation
  - Secret redaction (tokens, API keys, passwords, URL credentials)
  - Access control and ownership isolation
  - PID-reuse safeguards (refusal to kill unrelated process upon PID reuse)
  - Safe confirmation-backed stopping
  - API routes & agent tool execution
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.hosting_routes import setup_hosting_routes
from src import hosting_tracker
from src.agent_tools.services_tools import ManageServicesTool


@pytest.fixture(autouse=True)
def setup_tmp_store(tmp_path, monkeypatch):
    """Isolate hosting tracker persistence to a temporary directory."""
    services_dir = tmp_path / "hosting_services"
    services_file = tmp_path / "hosting_services.json"
    monkeypatch.setattr(hosting_tracker, "SERVICES_DIR", services_dir)
    monkeypatch.setattr(hosting_tracker, "SERVICES_FILE", services_file)
    services_dir.mkdir(parents=True, exist_ok=True)
    yield


def test_secret_redaction_masks_credentials():
    sample = (
        "standalone_key sk-abcdef12345678901234567890\n"
        "standalone_ghp ghp_1234567890abcdefghijklmnopqrstuv\n"
        "export GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuv\n"
        "python -m server --port 8080 --token secret_tok_1234 --password 'mypassword'\n"
        "curl -H 'Authorization: Bearer secret_bearer_token' https://admin:supersecret@example.com"
    )
    redacted = hosting_tracker.redact_secrets(sample)

    assert "sk-***REDACTED***" in redacted
    assert "ghp_***REDACTED***" in redacted
    assert "secret_tok_1234" not in redacted
    assert "mypassword" not in redacted
    assert "secret_bearer_token" not in redacted
    assert "supersecret" not in redacted
    assert "--port 8080" in redacted
    assert "https://admin:***@example.com" in redacted


def test_launch_and_register_records_identity():
    # Launch a tiny Python background HTTP server
    svc = hosting_tracker.launch_service(
        f"{sys.executable} -m http.server 0",
        owner="alice",
        name="Test Web Server",
    )

    try:
        assert svc["id"].startswith("srv_")
        assert svc["owner"] == "alice"
        assert svc["name"] == "Test Web Server"
        assert svc["pid"] is not None

        # Process start time token was captured
        store = hosting_tracker._load_store()
        rec = store[svc["id"]]
        assert rec["process_start_time"] is not None
        assert rec["status"] in ("starting", "ready")

        # Let it bind and check reconciliation
        time.sleep(0.4)
        reconciled = hosting_tracker.get_service(svc["id"], user="alice")
        assert reconciled is not None
        assert reconciled["port"] is not None
        assert reconciled["port"] > 0
        assert reconciled["url"] == f"http://localhost:{reconciled['port']}"
        assert reconciled["bind_label"] in ("Localhost only", "Local / All interfaces")
        assert reconciled["readiness"] in ("ready", "starting")
    finally:
        hosting_tracker.stop_service(svc["id"], user="alice", confirm=True)


def test_register_service_validates_running_process():
    # Spawning a real process
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        svc = hosting_tracker.register_service(
            proc.pid,
            name="External Sleeper",
            command="sleep 10",
            owner="bob",
            port=9999,
        )
        assert svc["id"].startswith("srv_")
        assert svc["pid"] == proc.pid
        assert svc["owner"] == "bob"
        assert svc["port"] == 9999

        # Reject registering a dead PID
        with pytest.raises(ValueError, match="not currently running"):
            hosting_tracker.register_service(99999999, name="Ghost", command="ghost")
    finally:
        proc.terminate()
        proc.wait()


def test_safe_stopping_requires_confirmation():
    svc = hosting_tracker.launch_service(
        f"{sys.executable} -c 'import time; time.sleep(10)'",
        owner="alice",
        name="Sleeper",
    )
    try:
        with pytest.raises(ValueError, match="requires explicit confirmation"):
            hosting_tracker.stop_service(svc["id"], user="alice", confirm=False)
    finally:
        hosting_tracker.stop_service(svc["id"], user="alice", confirm=True)


def test_pid_reuse_prevention_refuses_to_kill():
    # Launch service
    svc = hosting_tracker.launch_service(
        f"{sys.executable} -c 'import time; time.sleep(10)'",
        owner="alice",
        name="Protected Process",
    )
    pid = svc["pid"]

    try:
        # Simulate PID reuse by modifying expected process_start_time to a bogus token
        store = hosting_tracker._load_store()
        store[svc["id"]]["process_start_time"] = "99999999_bogus_token"
        hosting_tracker._save_store(store)

        # Attempt to stop: must refuse to signal the PID!
        with pytest.raises(RuntimeError, match="Process ID has been reused by an unrelated system process"):
            hosting_tracker.stop_service(svc["id"], user="alice", confirm=True)

        # Verify that the actual process is STILL ALIVE (was not killed!)
        assert hosting_tracker.pid_alive(pid) is True

        # And verify the service record was safely marked dead
        store_after = hosting_tracker._load_store()
        assert store_after[svc["id"]]["status"] == "dead"
    finally:
        # Clean up the actual process directly
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def test_restart_reconciliation_detects_exit():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    svc = hosting_tracker.register_service(
        proc.pid,
        name="Quick Exiter",
        command="sleep 0.2",
        owner="alice",
    )
    proc.wait()

    # Process is now dead; run reconciliation
    reconciled_store = hosting_tracker.reconcile_services()
    rec = reconciled_store[svc["id"]]
    assert rec["status"] in ("dead", "stopped")
    assert rec["ended_at"] is not None


def test_ownership_and_access_control():
    svc_alice = hosting_tracker.launch_service(
        f"{sys.executable} -c 'import time; time.sleep(5)'",
        owner="alice",
        name="Alice App",
    )
    try:
        # Bob cannot see Alice's service
        bob_list = hosting_tracker.list_services(user="bob")
        assert not any(s["id"] == svc_alice["id"] for s in bob_list)
        assert hosting_tracker.get_service(svc_alice["id"], user="bob") is None

        # Bob cannot stop Alice's service
        with pytest.raises(KeyError):
            hosting_tracker.stop_service(svc_alice["id"], user="bob", confirm=True)

        # Alice can see her service
        alice_list = hosting_tracker.list_services(user="alice")
        assert any(s["id"] == svc_alice["id"] for s in alice_list)

        # Admin user can see and stop Alice's service
        with patch("src.hosting_tracker.owner_is_admin_or_single_user", return_value=True):
            admin_list = hosting_tracker.list_services(user="admin")
            assert any(s["id"] == svc_alice["id"] for s in admin_list)
            stopped = hosting_tracker.stop_service(svc_alice["id"], user="admin", confirm=True)
            assert stopped["status"] == "stopped"
    finally:
        try:
            hosting_tracker.stop_service(svc_alice["id"], user="alice", confirm=True)
        except Exception:
            pass


def test_logs_redaction():
    svc = hosting_tracker.launch_service(
        f"{sys.executable} -u -c 'print(\"App listening on http://localhost:8080 with token=secret12345678\", flush=True); import time; time.sleep(5)'",
        owner="alice",
        name="Logging App",
    )
    try:
        time.sleep(0.5)
        logs = hosting_tracker.get_logs(svc["id"], user="alice")
        assert "secret12345678" not in logs
        assert "***REDACTED***" in logs
        assert "http://localhost:8080" in logs
    finally:
        hosting_tracker.stop_service(svc["id"], user="alice", confirm=True)


def test_api_routes():
    app = FastAPI()
    app.include_router(setup_hosting_routes())

    client = TestClient(app)

    # Launch service via API
    with patch("routes.hosting_routes.get_current_user", return_value="alice"):
        resp = client.post("/api/services/launch", json={
            "command": f"{sys.executable} -c 'import time; time.sleep(10)'",
            "name": "API Service",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        svc_id = data["service"]["id"]

        try:
            # List services
            list_resp = client.get("/api/services")
            assert list_resp.status_code == 200
            assert any(s["id"] == svc_id for s in list_resp.json()["services"])

            # Get service
            get_resp = client.get(f"/api/services/{svc_id}")
            assert get_resp.status_code == 200
            assert get_resp.json()["service"]["name"] == "API Service"

            # Logs
            logs_resp = client.get(f"/api/services/{svc_id}/logs")
            assert logs_resp.status_code == 200

            # Stop without confirm fails
            fail_stop = client.post(f"/api/services/{svc_id}/stop", json={"confirm": False})
            assert fail_stop.status_code == 400

            # Stop with confirm succeeds
            stop_resp = client.post(f"/api/services/{svc_id}/stop", json={"confirm": True})
            assert stop_resp.status_code == 200
            assert stop_resp.json()["service"]["status"] == "stopped"
        finally:
            try:
                hosting_tracker.stop_service(svc_id, user="alice", confirm=True)
            except Exception:
                pass


@pytest.mark.asyncio
async def test_manage_services_agent_tool():
    tool = ManageServicesTool()
    ctx = {"owner": "alice", "session_id": "ses_123"}

    # Launch via tool
    res = await tool.execute(
        f'{{"action": "launch", "command": "{sys.executable} -c \'import time; time.sleep(10)\'", "name": "Agent Svc"}}',
        ctx
    )
    assert res["exit_code"] == 0
    svc_id = res["service_id"]

    try:
        # List via tool
        list_res = await tool.execute('{"action": "list"}', ctx)
        assert list_res["exit_code"] == 0
        assert svc_id in list_res["output"]

        # Status via tool
        status_res = await tool.execute(f'{{"action": "status", "service_id": "{svc_id}"}}', ctx)
        assert status_res["exit_code"] == 0
        assert "Agent Svc" in status_res["output"]

        # Stop via tool
        stop_res = await tool.execute(f'{{"action": "stop", "service_id": "{svc_id}"}}', ctx)
        assert stop_res["exit_code"] == 0
        assert "Stopped hosting service" in stop_res["output"]
    finally:
        try:
            hosting_tracker.stop_service(svc_id, user="alice", confirm=True)
        except Exception:
            pass
