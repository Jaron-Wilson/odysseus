"""Argument coercion: strings from text-mode tool calls become schema types."""
import asyncio
import json

from src.tool_arg_coercion import coerce_args, format_problems
from src.agent_tools import parse_tool_blocks

LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "folder": {"type": "string"},
        "max_results": {"type": "integer"},
        "ratio": {"type": "number"},
        "unread_only": {"type": "boolean"},
        "uids": {"type": "array", "items": {"type": "string"}},
        "ids": {"type": "array", "items": {"type": "integer"}},
        "opts": {"type": "object"},
    },
}


def test_numeric_strings():
    out, bad = coerce_args({"max_results": "30", "ratio": "1.5"}, LIST_SCHEMA)
    assert out == {"max_results": 30, "ratio": 1.5} and not bad
    assert isinstance(out["max_results"], int)
    out, _ = coerce_args({"max_results": "30.0", "ratio": "2"}, LIST_SCHEMA)
    assert out["max_results"] == 30 and out["ratio"] == 2


def test_booleans():
    out, bad = coerce_args({"unread_only": "false"}, LIST_SCHEMA)
    assert out["unread_only"] is False and not bad
    out, _ = coerce_args({"unread_only": "True"}, LIST_SCHEMA)
    assert out["unread_only"] is True


def test_json_string_array_and_object():
    out, bad = coerce_args({"uids": '["1", "2"]', "opts": '{"a": 1}', "ids": "[1, 2]"}, LIST_SCHEMA)
    assert out["uids"] == ["1", "2"] and out["opts"] == {"a": 1} and out["ids"] == [1, 2] and not bad


def test_single_value_to_list_and_item_coercion():
    out, _ = coerce_args({"uids": "42", "ids": "7"}, LIST_SCHEMA)
    assert out["uids"] == ["42"] and out["ids"] == [7]
    out, _ = coerce_args({"ids": ["1", "2"]}, LIST_SCHEMA)
    assert out["ids"] == [1, 2]


def test_string_param_given_number_and_unknown_args_untouched():
    out, _ = coerce_args({"folder": 5, "extra": "1"}, LIST_SCHEMA)
    assert out["folder"] == "5" and out["extra"] == "1"


def test_already_correct_is_unchanged():
    args = {"max_results": 5, "unread_only": True, "folder": "INBOX"}
    out, bad = coerce_args(args, LIST_SCHEMA)
    assert out == args and not bad


def test_impossible_gives_expected_type_and_example():
    out, bad = coerce_args({"max_results": "lots", "unread_only": "maybe"}, LIST_SCHEMA)
    assert {b["name"] for b in bad} == {"max_results", "unread_only"}
    msg = format_problems("list_emails", bad)
    assert "max_results" in msg and "integer" in msg and "Example" in msg and "30" in msg
    assert "boolean" in msg


def test_bool_is_not_an_integer():
    _, bad = coerce_args({"max_results": True}, LIST_SCHEMA)
    assert bad


def test_text_tool_call_parser_strings_reach_the_tool_typed():
    """XML invoke calls carry every value as a string; the converter must
    hand the tool real integers and booleans."""
    text = ('<invoke name="list_emails">'
            '<parameter name="max_results">30</parameter>'
            '<parameter name="unread_only">false</parameter>'
            '<parameter name="folder">INBOX</parameter>'
            '</invoke>')
    blocks = parse_tool_blocks(text)
    assert blocks, "invoke block should parse"
    args = json.loads(blocks[0].content)
    assert args["max_results"] == 30 and isinstance(args["max_results"], int)
    assert args["unread_only"] is False
    assert args["folder"] == "INBOX"


def test_native_function_call_with_string_args():
    from src.tool_schemas import function_call_to_tool_block
    block = function_call_to_tool_block("list_emails", json.dumps({"max_results": "30", "unread_only": "true"}))
    args = json.loads(block.content)
    assert args == {"max_results": 30, "unread_only": True}


def test_mcp_call_tool_coerces_and_reports(monkeypatch):
    from src.mcp_manager import McpManager
    mgr = McpManager.__new__(McpManager)
    mgr._tools = {"email": [{"name": "list_emails", "input_schema": LIST_SCHEMA}]}
    mgr._sessions = {}
    mgr._connections = {}
    seen = {}

    async def fake_do_call(session, name, arguments):
        seen.update(arguments)
        return {"stdout": "ok", "stderr": "", "exit_code": 0}

    mgr._do_call = fake_do_call
    mgr._sessions["email"] = object()
    r = asyncio.run(mgr.call_tool("mcp__email__list_emails", {"max_results": "30", "unread_only": "false"}))
    assert r["exit_code"] == 0 and seen == {"max_results": 30, "unread_only": False}
    r = asyncio.run(mgr.call_tool("mcp__email__list_emails", {"max_results": "many"}))
    assert r["exit_code"] == 1 and "integer" in r["error"] and "Example" in r["error"]
