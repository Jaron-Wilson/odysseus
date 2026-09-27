"""Screen-control approval: the turn waits for it, the popup shows, and a
grant just given survives a regenerate.

Seen live on 2026-09-27 (chat b98e1ca6): the turn asked for approval and
carried on anyway; the approval popup never showed in agent mode; and after
approving, the user pressed regenerate five seconds later (the approved run
had not shown on the page), whose Stop revoked the grant just given, so the
new turn asked for approval again.
"""
import os
import time

from src import screen_control_approvals as sca

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _grants(tmp_path, monkeypatch, ages):
    monkeypatch.setattr(sca, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sca, "APPROVALS_FILE", str(tmp_path / "sc.json"))
    now = time.time()
    data = {f"g{i}": {"status": "approved", "server_id": "pc", "owner": "jaron",
                      "decided_at": now - age, "expires": now + 900, "created": now - age}
            for i, age in enumerate(ages)}
    sca._save(data)


def test_stop_keeps_a_grant_just_given(tmp_path, monkeypatch):
    _grants(tmp_path, monkeypatch, [5, 600])
    assert sca.revoke(owner="jaron", keep_newer_than=120) == 1
    left = [k for k, v in sca._load().items() if v.get("status") == "approved"]
    assert left == ["g0"]                       # the one approved 5s ago
    # The explicit revoke still takes everything.
    assert sca.revoke(owner="jaron") == 1


def test_the_wiring():
    loop = open(os.path.join(HERE, "src", "agent_loop.py"), encoding="utf-8").read()
    routes = open(os.path.join(HERE, "routes", "chat_routes.py"), encoding="utf-8").read()
    i = loop.index('"ui_event": "screen_control_request"')
    assert "_awaiting_user = True" in loop[i:i + 700]
    assert routes.count('elif data.get("ui_event"):') == 2        # chat and agent relays
    assert "keep_newer_than=120" in routes
    assert "Never cycle through tabs" in loop
