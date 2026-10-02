"""Qwen chat-template turn markers (`<|assistant|>`, `|end|`, etc.) occasionally
leak into visible text around a tool call and must be stripped for display.
The `end` branch requires at least one pipe around the marker so this never
eats a lone `end` that closes a Ruby/Lua/shell block, or the plain word
"assistant" in unrelated prose. Ported from the upstream fork's fix
(upstream commit f1e96d10), which tightened an earlier version where both
pipes were optional and silently deleted legitimate code/prose.
"""
import src.agent_tools  # noqa: F401  (break agent_tools<->tool_parsing import cycle)
from src.tool_parsing import strip_tool_blocks


def test_pipe_delimited_end_marker_is_stripped():
    assert strip_tool_blocks("Here is the answer. |end|") == "Here is the answer."
    assert strip_tool_blocks("Here is the answer. end|") == "Here is the answer."
    assert strip_tool_blocks("Here is the answer. /|end|") == "Here is the answer."


def test_role_marker_is_stripped():
    assert strip_tool_blocks("<|assistant|>Here is the answer.") == "Here is the answer."
    assert strip_tool_blocks("Here is the answer.</|end|>") == "Here is the answer."


def test_bare_end_in_code_is_not_eaten():
    # Ruby/Lua/shell code closing a block with a lone `end` must survive.
    ruby = "def foo\n  puts 'hi'\nend"
    assert strip_tool_blocks(ruby) == ruby


def test_bare_assistant_word_without_pipes_still_strips():
    # Matches upstream behavior: an unadorned "assistant" token on its own
    # (as chat templates emit before the real reply) still strips, since
    # that branch was not part of the #5547 regression being fixed.
    assert strip_tool_blocks("assistant Here is the answer.") == "Here is the answer."
