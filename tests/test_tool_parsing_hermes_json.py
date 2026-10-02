"""Qwen/Hermes text-mode tool calls: bare JSON inside <tool_call> wrappers.

The canonical Qwen/Hermes text-mode form is a bare JSON object like
{"name": "bash", "arguments": {"command": "..."}} inside a <tool_call>
wrapper, which previously parsed to zero tool blocks (only <invoke> markup
inside the wrapper was recognized). Ported from the upstream fork's fix
(upstream commit 1976fe1b): a JSON-looking wrapper body is now parsed
through the same canonical function_call_to_tool_block converter as the XML
paths, and JSON-looking bodies fail closed instead of falling through to XML
scanning — XML-like text inside JSON argument values must stay data, and a
non-object "arguments" value is rejected rather than coerced.
"""
import src.agent_tools  # noqa: F401  (break agent_tools<->tool_parsing import cycle)
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks

ISSUE_PAYLOAD = '<tool_call>\n{"name": "bash", "arguments": {"command": "mkdir -p agent-test"}}\n</tool_call>'


def test_hermes_json_tool_call_payload_parses():
    blocks = parse_tool_blocks(ISSUE_PAYLOAD)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "bash"
    assert blocks[0].content == "mkdir -p agent-test"


def test_multiple_sequential_wrappers():
    text = (
        '<tool_call>\n{"name": "bash", "arguments": {"command": "ls"}}\n</tool_call>\n'
        'Now the second step:\n'
        '<tool_call>\n{"name": "bash", "arguments": {"command": "pwd"}}\n</tool_call>'
    )
    blocks = parse_tool_blocks(text)
    assert [(b.tool_type, b.content) for b in blocks] == [("bash", "ls"), ("bash", "pwd")]


def test_xml_inside_json_arguments_stays_data():
    # A valid JSON body whose argument values contain XML-like tool markup
    # must parse as the JSON-named tool; the embedded markup is content.
    text = (
        '<tool_call>{"name": "write_file", "arguments": '
        '{"path": "notes.txt", "content": "<bash>echo unsafe</bash>"}}</tool_call>'
    )
    blocks = parse_tool_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "write_file"
    assert "<bash>echo unsafe</bash>" in blocks[0].content
    assert all(b.tool_type != "bash" for b in blocks)


def test_malformed_json_body_never_falls_through_to_xml():
    # Fail-closed: a JSON-looking body that doesn't decode must not be
    # rescanned as XML, even when it contains well-formed tool markup.
    text = (
        '<tool_call>{"name": "write_file", "arguments": {broken json '
        '<invoke name="bash"><parameter name="command">echo unsafe</parameter></invoke>'
        '</tool_call>'
    )
    assert parse_tool_blocks(text) == []


def test_non_dict_arguments_rejected():
    # "arguments" must be an object; scalars/arrays are rejected, not coerced.
    for args in ('["ls"]', '"ls"', '1', 'null'):
        text = '<tool_call>{"name": "bash", "arguments": %s}</tool_call>' % args
        assert parse_tool_blocks(text) == [], f"arguments={args} should be rejected"


def test_strip_tool_blocks_removes_json_wrapper_spans():
    text = "Before.\n" + ISSUE_PAYLOAD + "\nAfter."
    cleaned = strip_tool_blocks(text)
    assert "tool_call" not in cleaned
    assert "mkdir -p agent-test" not in cleaned
    assert "Before." in cleaned
    assert "After." in cleaned


def test_xml_body_wrapper_regression():
    # The pre-existing XML wrapper form must keep parsing exactly as before.
    text = (
        '<tool_call><invoke name="bash">'
        '<parameter name="command">echo hi</parameter>'
        '</invoke></tool_call>'
    )
    blocks = parse_tool_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "bash"
    assert blocks[0].content == "echo hi"
