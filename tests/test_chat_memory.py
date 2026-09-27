""""Needs to know": a memory per chat, like Brain but per chat.

Items the model sees on every turn in that chat. The model suggests items and
the user decides; it adds directly only when asked. A new chat also sees the
user's recent chats' items (seen live on 2026-09-27: after a reload, "okay
check if he did merge it" went into a fresh, empty chat).
"""
import json
import os
import time

import pytest

from src import chat_memory as cm


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "MEMORY_FILE", str(tmp_path / "chat_memory.json"))
    monkeypatch.setattr(cm, "_chat_name", lambda sid: {"pr": "Will's PR"}.get(sid, ""))


def test_notes_from_the_first_version_become_items():
    with open(cm.MEMORY_FILE, "w") as f:
        json.dump({"pr": {"text": "Task: PR\nWaiting on: Will", "updated": time.time(), "by": "user"}}, f)
    items = cm.get("pr")["items"]
    assert [i["text"] for i in items] == ["Task: PR", "Waiting on: Will"]
    assert all(i["by"] == "you" and i["status"] == "active" for i in items)


def test_the_ai_suggests_and_the_user_decides():
    out = cm.run_tool('{"action": "suggest", "text": "Waiting on Will to merge PR #3"}', session_id="pr")
    sid = out["suggestion"]["id"]
    assert "Suggested for Needs to know (id " in out["output"]
    d = cm.get("pr")
    assert d["items"] == [] and d["suggested"][0]["text"] == "Waiting on Will to merge PR #3"
    # Not in the model's context until accepted.
    assert cm.context_text("pr") == ""
    cm.update("pr", sid, accept=True)
    assert cm.get("pr")["items"][0]["text"] == "Waiting on Will to merge PR #3"
    assert f"- [{sid}] Waiting on Will to merge PR #3" in cm.context_text("pr")
    # Declining removes it.
    other = cm.run_tool('{"action": "suggest", "text": "Use the fork"}', session_id="pr")["suggestion"]["id"]
    cm.remove("pr", other)
    assert cm.get("pr")["suggested"] == []
    # Suggesting something already kept does not ask twice.
    again = cm.run_tool('{"action": "suggest", "text": "waiting on will to merge pr #3"}', session_id="pr")
    assert "Already in Needs to know" in again["output"] and "suggestion" not in again


def test_add_edit_remove_and_the_tool():
    cm.add("pr", "Branch: will-scheduling-feature")                     # by you
    added = cm.run_tool('{"action": "add", "text": "Repo: GlooHackathon2026"}', session_id="pr")
    assert "Added to Needs to know" in added["output"]
    items = cm.get("pr")["items"]
    assert [(i["text"], i["by"]) for i in items] == [
        ("Branch: will-scheduling-feature", "you"), ("Repo: GlooHackathon2026", "ai")]
    cm.update("pr", items[0]["id"], text="Branch: will-scheduling-v2")
    assert cm.get("pr")["items"][0]["text"] == "Branch: will-scheduling-v2"
    listed = cm.run_tool('{"action": "list"}', session_id="pr")["output"]
    assert "will-scheduling-v2" in listed and "GlooHackathon2026" in listed
    gone = cm.run_tool(json.dumps({"action": "remove", "id": items[1]["id"]}), session_id="pr")
    assert gone["output"] == "Removed." and len(cm.get("pr")["items"]) == 1
    assert "only works inside a chat" in cm.run_tool("{}", session_id=None)["error"]


def test_a_new_chat_sees_recent_chats_items():
    cm.add("pr", "Waiting on Will to merge PR #3", owner="jaron")
    cm.add("theirs", "Another user's chat", owner="someone")
    cm.run_tool('{"action": "suggest", "text": "not accepted"}', session_id="pr")
    text = cm.context_text("fresh", owner="jaron", new_chat=True)
    assert "## Recent chats" in text and "\"Will's PR\" (chat pr" in text
    assert "merge PR #3" in text and "Another user's" not in text and "not accepted" not in text
    assert "Recent chats" not in cm.context_text("fresh", owner="jaron", new_chat=False)


def test_wired_everywhere():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    read = lambda *p: open(os.path.join(here, *p), encoding="utf-8").read()
    from src.agent_tools import TOOL_TAGS
    from src.tool_index import ALWAYS_AVAILABLE
    assert "chat_memory" in TOOL_TAGS and "chat_memory" in ALWAYS_AVAILABLE
    routes = read("routes", "chat_routes.py")
    for dec in ('@router.post("/api/chat/memory/{session_id}")',
                '@router.patch("/api/chat/memory/{session_id}/{item_id}")',
                '@router.delete("/api/chat/memory/{session_id}/{item_id}")'):
        assert dec in routes
    loop = read("src", "agent_loop.py")
    assert 'tool_output_data["suggestion"] = result["suggestion"]' in loop
    assert "window.chatNotes.suggestionPrompt(json.suggestion)" in read("static", "js", "chat.js")
    assert "Suggested for Needs to know" in read("static", "js", "chatRenderer.js")
