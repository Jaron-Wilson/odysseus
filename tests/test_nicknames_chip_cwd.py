"""Server nicknames, a background chip that names its chat, and no coding
runs in the Odysseus install.

Asked for on 2026-09-28:
- "can we add nicknames to models, I have 2 ollamas ... same name different
  servers", per chat.
- "1 background task running in this chat ... when it's actually in another chat".
- "also used wrong directory bro ... it's supposed to be on my laptop but it
  went on my server's home" (the run's cwd was /home/jaron/odysseus).
"""
import asyncio
import json
import os
import shutil
import subprocess

import pytest

from src.agent_tools import claude_code_tool as cct

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_odysseus_install_is_refused_and_workspaces_are_made(tmp_path, monkeypatch):
    for cwd in (HERE, os.path.join(HERE, "src")):
        out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps({"prompt": "x", "cwd": cwd}), {}))
        assert "running Odysseus install" in out["error"] and "workspaces" in out["error"]
    monkeypatch.setattr(cct, "WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setattr(cct.shutil, "which", lambda name: None)       # stop before spawning
    new = tmp_path / "workspaces" / "donate-giving"
    out = asyncio.run(cct.ClaudeCodeTool().execute(json.dumps({"prompt": "x", "cwd": str(new)}), {}))
    assert "CLI not found" in out["error"]            # got past the cwd checks
    loop = open(os.path.join(HERE, "src", "agent_loop.py"), encoding="utf-8").read()
    assert "never use the Odysseus install (/home/jaron/odysseus) as cwd" in loop


def test_the_chip_names_the_chat(monkeypatch):
    from src import claude_code_jobs as jobs
    from src import chat_queue
    monkeypatch.setattr(chat_queue, "_session_title", lambda sid: "Deploy Docker Container")
    j = jobs.Job(chat_session_id="c1", owner="", action="execute", cwd="/w", model="m",
                 engine="opencode", prompt="Carry out the approved plan.")
    assert j.public()["chat_name"] == "Deploy Docker Container"
    js = open(os.path.join(HERE, "static", "js", "bgTasks.js"), encoding="utf-8").read()
    assert "in \\u201c${_esc(first.chat_name)}\\u201d" in js and "'Go to chat'" in js
    assert "if (_currentSid() !== _chipSid) _renderChip(_chipJobs)" in js


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_favorites_are_per_server():
    src = open(os.path.join(HERE, "static", "js", "models.js"), encoding="utf-8").read()
    i = src.index("function _favKey(")
    j = src.index("// ── Usage tracking ──")
    script = """
      let store = [];
      const _loadFavorites = () => store.slice();
      const _saveFavorites = (l) => { store = l.slice(); };
    """ + src[i:j] + """
      const out = [];
      out.push(_toggleFavorite('llama3.1:8b', 'friend'));           // star the friend's one
      out.push(_isFavorite('llama3.1:8b', 'friend'), _isFavorite('llama3.1:8b', 'mine'));
      store = ['qwen3:14b'];                                          // an old-style favorite
      out.push(_isFavorite('qwen3:14b', 'mine'), _toggleFavorite('qwen3:14b', 'mine'), JSON.stringify(store));
      console.log(JSON.stringify(out));
    """
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert json.loads(r.stdout) == [True, True, False, True, False, "[]"], r.stderr


def test_rename_and_label_are_wired():
    read = lambda *p: open(os.path.join(HERE, *p), encoding="utf-8").read()
    admin = read("static", "js", "admin.js")
    assert "data-adm-rename-ep=" in admin and "JSON.stringify({ name: name.trim() })" in admin
    picker = read("static", "js", "modelPicker.js")
    assert "if (epName && (!isDefaultName || copies > 1)) displayName += ` \\u00b7 ${epName}`;" in picker
    assert "label.appendChild(document.createTextNode(displayName));" in picker   # a nickname is text
