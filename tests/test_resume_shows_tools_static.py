"""A chat opened on a second device shows tool calls while the reply runs.

Seen live on 2026-09-27: the same chat open on the PC (which sent the
message) and on the laptop (attached mid-reply through /api/chat/resume).
The PC showed every tool call; the laptop showed plain text until the run
ended, because resumeStream only noted that tools were used. It now collects
them in the saved shape and draws them with the history renderer. Checked in
a real browser when it was built; this keeps the wiring in place.
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resume_src():
    with open(os.path.join(HERE, "static", "js", "chat.js"), encoding="utf-8") as f:
        js = f.read()
    start = js.index("export async function resumeStream(sessionId)")
    return js[start:js.index("\n  }\n", start)]


def test_resume_collects_tool_events_and_draws_them():
    src = _resume_src()
    assert "} else if (json.type === 'tool_output') {" in src
    assert "liveTools.push(ev);" in src
    assert "} else if (json.type === 'agent_step') {" in src
    assert "chatRenderer.addMessage('assistant', ''" in src
    assert "tool_events: events, round_texts: roundTexts" in src


def test_live_nodes_are_cleared_before_the_final_reload():
    src = _resume_src()
    clear_at = src.index("for (const n of liveNodes) n.remove();\n    if (leftSession)")
    reload_at = src.index("sessionModule.selectSession(sessionId)")
    assert clear_at < reload_at


def test_live_redraws_skip_the_entry_animation():
    assert "n.classList.add('resume-live')" in _resume_src()
    with open(os.path.join(HERE, "static", "style.css"), encoding="utf-8") as f:
        assert ".resume-live, .resume-live * { animation: none !important; }" in f.read()
