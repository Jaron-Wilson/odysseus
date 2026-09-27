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


def test_one_request_per_machine_while_it_waits(tmp_path, monkeypatch):
    """Seen live: each "try again" tried a screenshot and raised another
    approval request while the first was still unanswered."""
    monkeypatch.setattr(sca, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sca, "APPROVALS_FILE", str(tmp_path / "sc.json"))
    first = sca.request_grant("pc", "windows-desktop", "jaron", "screenshot", session_id="chat-a")
    again = sca.request_grant("pc", "windows-desktop", "jaron", "screenshot", session_id="chat-b")
    assert again["id"] == first["id"] and again.get("reused") and not first.get("reused")
    assert sca._load()[first["id"]]["session_id"] == "chat-b"      # approving resumes the latest chat
    # Another machine, or another user, gets its own request.
    assert sca.request_grant("laptop", "laptop", "jaron", "x")["id"] != first["id"]
    assert sca.request_grant("pc", "windows-desktop", "someone", "x")["id"] != first["id"]
    # Once answered, the next attempt is a new request.
    sca.set_status(first["id"], "denied", owner="jaron")
    assert sca.request_grant("pc", "windows-desktop", "jaron", "x")["id"] != first["id"]


def test_ssh_windows_rule():
    loop = open(os.path.join(HERE, "src", "agent_loop.py"), encoding="utf-8").read()
    assert "anything started over SSH (Start-Process, explorer, start) runs in a hidden session" in loop
    mgr = open(os.path.join(HERE, "src", "mcp_manager.py"), encoding="utf-8").read()
    assert 'if req.get("reused")' in mgr


def test_a_resumed_turn_gets_the_model_s_real_context(monkeypatch):
    """Seen live: turns resumed after an approval ran on a 32K default and
    dropped earlier messages, while qwen3.8-27b has 262K."""
    import asyncio
    from src import chat_queue, screen_control_resume as scr
    seen = {}

    async def fake_prepare(sess, sid, context):
        return context, 262144

    async def fake_loop(url, model, context, **kw):
        seen.update(kw)
        yield 'data: {"delta": "ok"}\n\n'

    monkeypatch.setattr(chat_queue, "_prepare", fake_prepare)

    class S:
        id, model, endpoint_url, headers, owner = "c1", "qwen3.8-27b", "http://llm", {}, "jaron"

    class SM:
        def add_message(self, *a): pass
        def save_sessions(self): pass

    async def run():
        async for _ in scr._resume_stream(S(), SM(), [{"role": "user", "content": "go"}], fake_loop):
            pass
    asyncio.run(run())
    assert seen["context_length"] == 262144


def test_screen_actions_are_capped_per_turn():
    from src import agent_loop as al
    loop = open(os.path.join(HERE, "src", "agent_loop.py"), encoding="utf-8").read()
    assert al.MAX_SCREEN_ACTIONS_PER_TURN == 10 and "click" in al._SCREEN_ACTS
    assert "_screen_acts > MAX_SCREEN_ACTIONS_PER_TURN" in loop
    assert "Never click taskbar icons or press Win+number" in loop
