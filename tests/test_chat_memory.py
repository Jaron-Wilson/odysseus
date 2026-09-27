""""Needs to know" notes per chat, and recent chats' notes in a new chat.

Seen live on 2026-09-27: after a page reload, "okay check if he did merge
it" went into a fresh, empty chat instead of the one about the PR, and the
model had no idea who "he" was.
"""
import os
import time

import pytest

from src import chat_memory as cm


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "MEMORY_FILE", str(tmp_path / "chat_memory.json"))
    monkeypatch.setattr(cm, "_chat_name", lambda sid: {"pr": "Will's PR", "old": "Old thing"}.get(sid, ""))


def test_set_append_get_and_clear():
    cm.set_text("pr", "Task: PR for will-scheduling-feature", owner="jaron")
    cm.append("pr", "Waiting on: Will to merge PR #3")
    rec = cm.get("pr")
    assert rec["text"] == "Task: PR for will-scheduling-feature\nWaiting on: Will to merge PR #3"
    assert rec["by"] == "model" and rec["updated"]
    cm.set_text("pr", "x" * 5000)
    assert len(cm.get("pr")["text"]) == cm.MAX_CHARS and cm.get("pr")["text"].startswith("…")
    cm.set_text("pr", "")
    assert cm.get("pr")["text"] == ""


def test_the_chat_sees_its_own_notes():
    cm.set_text("pr", "Task: PR for will-scheduling-feature")
    text = cm.context_text("pr")
    assert text.startswith("## Needs to know (this chat)") and "will-scheduling-feature" in text
    assert "Recent chats" not in text
    assert cm.context_text("empty") == ""


def test_a_new_chat_sees_recent_chats_notes():
    cm.set_text("pr", "Waiting on: Will to merge PR #3", owner="jaron")
    cm.set_text("old", "Something from long ago", owner="jaron")
    cm.set_text("theirs", "Another user's chat", owner="someone")
    data = cm._load()
    data["old"]["updated"] = time.time() - 48 * 3600          # too old
    cm._save(data)
    text = cm.context_text("fresh", owner="jaron", new_chat=True)
    assert "## Recent chats" in text
    assert "\"Will's PR\" (chat pr" in text and "merge PR #3" in text
    assert "long ago" not in text and "Another user's" not in text
    # An established chat does not get them.
    assert "Recent chats" not in cm.context_text("fresh", owner="jaron", new_chat=False)


def test_the_tool():
    out = cm.run_tool('{"action": "set", "text": "Task: rebrand"}', session_id="c1")
    assert out["notes"] == "Task: rebrand"
    out = cm.run_tool('{"action": "append", "text": "Branch: rebrand-2"}', session_id="c1")
    assert out["notes"] == "Task: rebrand\nBranch: rebrand-2"
    assert "Branch: rebrand-2" in cm.run_tool('{"action": "get"}', session_id="c1")["output"]
    assert cm.run_tool("plain text becomes the notes", session_id="c2")["notes"] == "plain text becomes the notes"
    assert "only works inside a chat" in cm.run_tool("{}", session_id=None)["error"]


def test_wired_everywhere():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    read = lambda *p: open(os.path.join(here, *p), encoding="utf-8").read()
    from src.agent_tools import TOOL_TAGS
    from src.tool_index import ALWAYS_AVAILABLE
    assert "chat_memory" in TOOL_TAGS and "chat_memory" in ALWAYS_AVAILABLE
    assert 'elif tool == "chat_memory":' in read("src", "tool_execution.py")
    assert 'untrusted_context_message("chat notes", _notes)' in read("src", "agent_loop.py")
    assert '"/api/chat/memory/{session_id}"' in read("routes", "chat_routes.py")
    assert 'id="chat-notes-btn"' in read("static", "index.html")
