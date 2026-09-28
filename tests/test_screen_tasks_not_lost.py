"""Screen tasks: no surprise windows, no blind screenshot loops, and a
question asked while the page is closed still reaches the user.

Seen live on 2026-09-27 (chat b98e1ca6): a lookup hit a private repo, so the
agent opened Chrome on the PC unasked (and Chrome Remote Desktop, by a loose
name match), took screenshot after screenshot that it could not see (they
went to the page only), then asked a question nobody saw.
"""
import asyncio
import os

from src import agent_runs, chat_queue
from src import agent_loop as al

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = {"data": "aGVsbG8=", "mimeType": "image/png"}


def test_a_vision_model_is_shown_the_screenshot(monkeypatch):
    import src.chat_helpers as ch
    monkeypatch.setattr(ch, "model_supports_vision", lambda m, u="": True)
    shots = []
    msgs = [{"role": "assistant", "content": "x"},
            {"role": "user", "content": "[Tool execution results]\n\n[Screenshot captured (image/png)]"}]
    al._attach_screenshot(msgs, IMG, "qwen3.8-27b", "http://llm", shots)
    parts = msgs[-1]["content"]
    assert parts[0]["type"] == "text" and parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,aGVsbG8="
    assert set(parts[1]) == {"type", "image_url"}          # nothing extra sent to the API
    # The next screenshot replaces it: only the newest is attached.
    msgs.append({"role": "user", "content": "[Tool execution results]\n\nnext"})
    al._attach_screenshot(msgs, IMG, "qwen3.8-27b", "http://llm", shots)
    assert not any(p.get("type") == "image_url" for p in msgs[1]["content"])
    assert any(p.get("type") == "image_url" for p in msgs[-1]["content"])


def test_a_blind_model_is_told_it_cannot_see(monkeypatch):
    import src.chat_helpers as ch
    monkeypatch.setattr(ch, "model_supports_vision", lambda m, u="": False)
    msgs = [{"role": "tool", "content": "[Screenshot captured (image/png)]", "tool_call_id": "1"}]
    al._attach_screenshot(msgs, IMG, "tiny-text-model", "http://llm", [])
    assert msgs[-1]["role"] == "user" and "cannot see images" in msgs[-1]["content"]


def test_opening_apps_needs_the_screen_control_grant():
    from src.screen_control_approvals import is_sensitive
    for tool in ("launch_app", "focus_app", "vscode_open"):
        assert is_sensitive(f"mcp__19d772b0__{tool}"), tool
    assert not is_sensitive("mcp__19d772b0__list_apps")


def test_a_question_reaches_the_user_when_no_page_is_watching(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_queue, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(chat_queue, "QUEUE_FILE", str(tmp_path / "q.json"))
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "Will's PR")
    sent = []

    async def fake_send(sid, notify, heading, body, *, kind="done", **kw):
        sent.append((sid, notify, heading, body, kind))
        return {"sent": 1}

    monkeypatch.setattr(chat_queue, "send_notification", fake_send)
    phone = {"note": "", "registered": True, "name": "pixel-8a"}

    # A page is watching and the user answers: nothing is sent.
    monkeypatch.setattr(chat_queue, "QUESTION_GRACE_S", 0.01)
    monkeypatch.setattr(agent_runs, "has_watchers", lambda sid: True)
    monkeypatch.setattr(chat_queue, "_last_role", lambda sid: "user")
    assert asyncio.run(chat_queue.notify_question("c1", "Which PR?", phone)) is None
    assert sent == []
    # A page is connected but nobody answers (a phone tab in the background):
    # it goes out after the grace period.
    monkeypatch.setattr(chat_queue, "_last_role", lambda sid: "assistant")
    asyncio.run(chat_queue.notify_question("c1", "Which PR?", phone))
    assert len(sent) == 1
    sent.clear()

    # Nobody is: it goes to the device the message came from.
    monkeypatch.setattr(agent_runs, "has_watchers", lambda sid: False)
    asyncio.run(chat_queue.notify_question("c1", "Which PR in GlooHackathon2026?", phone))
    sid, notify, heading, body, kind = sent[-1]
    assert notify == {"device": "pixel-8a"} and heading == "Question: Will's PR"
    assert body == "Which PR in GlooHackathon2026?" and kind == "question"

    # The bell's target wins when one is set.
    chat_queue.set_notify("c1", {"label": "all devices"})
    asyncio.run(chat_queue.notify_question("c1", "Which PR?", phone))
    assert sent[-1][1] == {"label": "all devices"}


def test_the_rules_and_guards_are_in_place():
    src = open(os.path.join(HERE, "src", "agent_loop.py"), encoding="utf-8").read()
    assert "Never do it on your own initiative, including when a lookup fails" in src
    assert "A new Chrome window, or the one you already have open?" in src
    assert "third screenshot in a row" in src
    assert "_cq.notify_question(" in src
    assert "_attach_screenshot(messages, _round_images[-1]" in src


def test_a_question_can_be_shown_again():
    js = open(os.path.join(HERE, "static", "js", "chat.js"), encoding="utf-8").read()
    render = open(os.path.join(HERE, "static", "js", "chatRenderer.js"), encoding="utf-8").read()
    assert "export function showAskUserCard(" in js
    assert js.count("_leaveAskUserRelaunch(_aq)") == 2          # the x and the backdrop
    assert "showAskUserCard(json.data || {});" in js
    assert "again.className = 'ask-user-relaunch';" in render and "ev.tool === 'ask_user'" in render
