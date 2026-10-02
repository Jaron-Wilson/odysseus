"""A screen-control call waiting on approval is not a failed tool.

The gate in src/mcp_manager.py answers exit_code 1 with needs_approval, and
the turn stops until the user answers the modal. The card used to say
"failed", which read as the agent going ahead without asking.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_live_and_saved_tool_events_carry_needs_approval():
    src = _read("src/agent_loop.py")
    assert 'tool_output_data["needs_approval"] = True' in src
    assert 'tool_event["needs_approval"] = True' in src


def test_both_renderers_say_waiting_for_approval_not_failed():
    for rel, var in (("static/js/chat.js", "json"), ("static/js/chatRenderer.js", "ev")):
        js = _read(rel)
        assert f"{var}.needs_approval ? 'waiting for approval' : 'failed'" in js, rel
        assert f"{var}.needs_approval ? ' waiting' : ' error'" in js, rel
