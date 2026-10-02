"""
tool_parsing.py

Regex-based parsing of tool invocations from LLM response text.
Supports fenced code blocks, [TOOL_CALL] blocks, and XML-style <invoke> blocks.
"""

import ast
import bisect
import json
import logging
import re
from typing import List, Optional

from src.agent_tools import ToolBlock, TOOL_TAGS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Pattern 1: ```bash ... ``` fenced code blocks
_TOOL_BLOCK_RE = re.compile(
    r"```(" + "|".join(TOOL_TAGS) + r")\s*\n([\s\S]*?)```",
    re.IGNORECASE,
)

# Pattern 2: [TOOL_CALL] ... [/TOOL_CALL] blocks (some models use this format)
# Matches: {tool => "shell", args => {--command "ls -la"}} etc.
_TOOL_CALL_RE = re.compile(
    r"\[TOOL_CALL\]\s*\{([\s\S]*?)\}\s*\[/TOOL_CALL\]",
    re.IGNORECASE,
)
# Same delimiters as _TOOL_CALL_RE, split so they can be driven by
# _iter_delimited (a forward-only scan). The closer is `}\s*[/TOOL_CALL]`, so a
# present-but-unmatched `[/TOOL_CALL]` with no inner `}` ahead simply ends the
# scan instead of triggering re.finditer's O(n^2) rescan. See _iter_delimited.
_TOOL_CALL_OPEN_RE = re.compile(r"\[TOOL_CALL\]\s*\{", re.IGNORECASE)
_TOOL_CALL_CLOSE_RE = re.compile(r"\}\s*\[/TOOL_CALL\]", re.IGNORECASE)

# Pattern 3: XML-style tool calls (minimax, some other models)
# <minimax:tool_call><invoke name="bash"><parameter name="command">...</parameter></invoke></minimax:tool_call>
# Also handles: <tool_call><invoke ...>, <function_call><invoke ...>, plain <invoke ...>
_XML_TOOL_CALL_RE = re.compile(
    r"<(?:[\w]+:)?(?:tool_call|function_call)>\s*([\s\S]*?)</(?:[\w]+:)?(?:tool_call|function_call)>",
    re.IGNORECASE,
)
# _XML_TOOL_CALL_RE's delimiters, split for _iter_delimited's forward-only scan.
_XML_TOOL_CALL_OPEN_RE = re.compile(
    r"<(?:[\w]+:)?(?:tool_call|function_call)>\s*",
    re.IGNORECASE,
)
_XML_TOOL_CALL_CLOSE_RE = re.compile(
    r"</(?:[\w]+:)?(?:tool_call|function_call)>",
    re.IGNORECASE,
)
_XML_INVOKE_RE = re.compile(
    r'<invoke\s+name=["\'](\w+)["\']>\s*([\s\S]*?)</invoke>',
    re.IGNORECASE,
)
_XML_PARAM_RE = re.compile(
    r'<parameter\s+name=["\'](\w+)["\']>([\s\S]*?)</parameter>',
    re.IGNORECASE,
)
# Forward-only delimiters for the lazy XML patterns above, so untrusted "many
# openers, no closer" model output can't drive finditer's O(n^2) lazy rescan
# (CodeQL py/polynomial-redos). Consumed by _iter_xml_invoke.
_XML_INVOKE_OPEN_RE = re.compile(r'<invoke\s+name=["\'](\w+)["\']>\s*', re.IGNORECASE)
_XML_INVOKE_CLOSE_RE = re.compile(r'</invoke>', re.IGNORECASE)
# Split <parameter ...>...</parameter> delimiters: the parameter scan inside an
# invoke body is forward-only too, so a closed invoke stuffed with unclosed
# parameter openers can't drive finditer's O(n^2) rescan. See _iter_named_blocks.
_XML_PARAM_OPEN_RE = re.compile(r'<parameter\s+name=["\'](\w+)["\']>', re.IGNORECASE)
_XML_PARAM_CLOSE_RE = re.compile(r'</parameter>', re.IGNORECASE)
# `args => { ... }` opener (its closer is the last `}`, found with rfind) and the
# `<tag>` opener for tool_code XML params — both split out of greedy/backref
# patterns that finditer/re.search would otherwise rescan from every opener. See
# _parse_tool_call_block / _parse_tool_code_block.
_ARGS_BRACE_OPEN_RE = re.compile(r'args\s*(?:=>|:|=)\s*\{')
_TOOL_CODE_PARAM_OPEN_RE = re.compile(r"<(\w+)>")
_TOOL_CODE_PARAM_CLOSE_ANY_RE = re.compile(r"</(\w+)>")

# Pattern 4: <tool_code> blocks (MiniMax-M2.5 style)
# {tool => 'tool_name', args => '<param>value</param>'}
_TOOL_CODE_RE = re.compile(
    r"<tool_code>\s*\{([\s\S]*?)\}\s*</tool_code>",
    re.IGNORECASE,
)
# _TOOL_CODE_RE's delimiters, split for _iter_delimited's forward-only scan.
_TOOL_CODE_OPEN_RE = re.compile(r"<tool_code>\s*\{", re.IGNORECASE)
_TOOL_CODE_CLOSE_RE = re.compile(r"\}\s*</tool_code>", re.IGNORECASE)

# Pattern 5: DeepSeek DSML markup leaking into content. When deepseek
# models can't emit structured tool_calls (e.g. we sent no tool schemas
# that round, or the API didn't parse them), they fall back to raw
# markup using fullwidth-pipe delimiters:
#   <｜｜DSML｜｜tool_calls>
#     <｜｜DSML｜｜invoke name="web_search">
#       <｜｜DSML｜｜parameter name="query" string="true">QUERY</｜｜DSML｜｜parameter>
#     </｜｜DSML｜｜invoke>
#   </｜｜DSML｜｜tool_calls>
# We normalize it into the standard <invoke>/<parameter> form so the
# existing XML parser + stripper handle it (parse → execute; strip →
# never show the garbage to the user). The pipe run is tolerant of
# fullwidth (U+FF5C) and ascii '|' in any count.
_DSML_PIPES = r"[｜|]+"
def _normalize_dsml(text: str) -> str:
    if not isinstance(text, str):
        return ""
    if "DSML" not in text:
        return text
    t = text
    t = re.sub(rf"<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*tool_calls\s*>", "<tool_call>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*tool_calls\s*>", "</tool_call>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*invoke\s+name=", "<invoke name=", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*invoke\s*>", "</invoke>", t, flags=re.IGNORECASE)
    # parameter open tag — drop any extra attrs (e.g. string="true").
    t = re.sub(rf'<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*parameter\s+name=(["\'][^"\']+["\'])[^>]*>',
               r"<parameter name=\1>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*parameter\s*>", "</parameter>", t, flags=re.IGNORECASE)
    return t

# Map model tool names to our tool types
_TOOL_NAME_MAP = {
    "shell": "bash",
    "bash": "bash",
    "terminal": "bash",
    "command": "bash",
    "execute": "bash",
    "run": "bash",
    "python": "python",
    "code": "python",
    "search": "web_search",
    "web_search": "web_search",
    "websearch": "web_search",
    "google_search": "web_search",
    "google_search_retrieval": "web_search",
    "google_search_grounding": "web_search",
    "web_fetch": "web_fetch",
    "webfetch": "web_fetch",
    "fetch_url": "web_fetch",
    "fetch": "web_fetch",
    "read": "read_file",
    "read_file": "read_file",
    "cat": "read_file",
    "write": "write_file",
    "write_file": "write_file",
    "save": "write_file",
    "document": "update_document",
    "update_document": "update_document",
    "create_document": "create_document",
    "edit": "edit_document",
    "edit_document": "edit_document",
    "search_chats": "search_chats",
    "search_conversations": "search_chats",
    "find_chat": "search_chats",
    "chat_with_model": "chat_with_model",
    "ask_model": "chat_with_model",
    "chat_model": "chat_with_model",
    "create_session": "create_session",
    "new_session": "create_session",
    "list_sessions": "list_sessions",
    "send_to_session": "send_to_session",
    "message_session": "send_to_session",
    "pipeline": "pipeline",
    "chain": "pipeline",
    "manage_session": "manage_session",
    "session_control": "manage_session",
    "manage_memory": "manage_memory",
    "memory": "manage_memory",
    "manage_tasks": "manage_tasks",
    "tasks": "manage_tasks",
    "schedule": "manage_tasks",
    "list_models": "list_models",
    "models": "list_models",
    "available_models": "list_models",
    "ui_control": "ui_control",
    "ui": "ui_control",
    "control": "ui_control",
    "api_call": "api_call",
    "api": "api_call",
    "integration": "api_call",
    "ask_teacher": "ask_teacher",
    "teacher": "ask_teacher",
    "manage_skills": "manage_skills",
    "skills": "manage_skills",
    "skill": "manage_skills",
    "suggest_document": "suggest_document",
    "suggest": "suggest_document",
    "review_document": "suggest_document",
    "manage_endpoints": "manage_endpoints",
    "endpoints": "manage_endpoints",
    "manage_mcp": "manage_mcp",
    "mcp_servers": "manage_mcp",
    "manage_webhooks": "manage_webhooks",
    "webhooks": "manage_webhooks",
    "manage_tokens": "manage_tokens",
    "tokens": "manage_tokens",
    "manage_documents": "manage_documents",
    "documents": "manage_documents",
    "manage_research": "manage_research",
    "list_research": "manage_research",
    "read_research": "manage_research",
    "open_research": "manage_research",
    "delete_research": "manage_research",
    "manage_settings": "manage_settings",
    "settings": "manage_settings",
    "preferences": "manage_settings",
    "manage_notes": "manage_notes",
    "notes": "manage_notes",
    "todo": "manage_notes",
    "todos": "manage_notes",
}

_MISFENCED_WEB_TOOL_NAMES = {
    "web_search": "web_search",
    "websearch": "web_search",
    "google_search": "web_search",
    "google_search_retrieval": "web_search",
    "google_search_grounding": "web_search",
    "web_fetch": "web_fetch",
    "webfetch": "web_fetch",
    "fetch_url": "web_fetch",
}

_RAW_WEB_JSON_TOOL_RE = re.compile(
    r"\b(?:web_search|websearch|google_search|google_search_retrieval|google_search_grounding)\b",
    re.IGNORECASE,
)
_RAW_WEB_JSON_ALLOWED_KEYS = {"query", "queries", "time_filter", "freshness", "max_pages"}

# Pattern 4b: Gemma-style <|tool_call|> call:tool_name{args} <tool_call|>
_GEMMA_TOOL_CALL_RE = re.compile(
    r"<\|?tool_call\|?>\s*call:([\w\d_-]+)\s*(\{[\s\S]*?\})\s*<\|?tool_call\|?>",
    re.IGNORECASE,
)

# Qwen chat-template turn markers that occasionally leak into visible text
# around a tool call, e.g. `<|assistant|>`, `</|end|>`. At least one pipe is
# required around a bare `end`/`assistant` marker so this never eats a lone
# `end` closing a Ruby/Lua/shell block, or the ordinary word "assistant" in
# prose — only `|end`, `end|`, `|end|` and `/|end|` strip.
_QWEN_ROLE_MARKER_RE = re.compile(r"</?\|(?:assistant|assistan|user|system|tool)\|>?|</\|end\|>?", re.IGNORECASE)
_QWEN_BARE_MARKER_RE = re.compile(
    r"(?:^|[\t\r\n ])(?:/?\|end\||\|end|end\|)(?=[\t\r\n ]|$)|"
    r"(?:^|[\t\r\n ])assistan(?:t)?(?=[\t\r\n ]|$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Parsing functions
# ---------------------------------------------------------------------------

def _literal_string(value) -> Optional[str]:
    """Return a string from a small literal AST node, or None."""
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        return None
    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _parse_misfenced_web_lookup(content: str) -> Optional[ToolBlock]:
    """Recover simple web_search/web_fetch calls wrapped in python/bash fences.

    Some local fenced-tool models write:

        ```python
        web_search("latest python release")
        ```

    That is an intended tool call, not Python code. Keep this intentionally
    narrow: only a single bare function call to a known web tool alias converts.
    """
    try:
        module = ast.parse(content.strip(), mode="exec")
    except SyntaxError:
        return None
    if len(module.body) != 1 or not isinstance(module.body[0], ast.Expr):
        return None
    call = module.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None

    mapped = _MISFENCED_WEB_TOOL_NAMES.get(call.func.id.lower())
    if mapped not in ("web_search", "web_fetch"):
        return None
    if len(call.args) > 1:
        return None

    args = {}
    if call.args:
        key = "url" if mapped == "web_fetch" else "query"
        value = _literal_string(call.args[0])
        if not value:
            return None
        args[key] = value

    allowed = {"query", "queries", "url", "time_filter", "freshness", "max_pages"}
    for keyword in call.keywords:
        if keyword.arg not in allowed:
            return None
        key = "query" if keyword.arg == "queries" else keyword.arg
        value = _literal_string(keyword.value)
        if value is not None:
            args[key] = value
            continue
        try:
            parsed = ast.literal_eval(keyword.value)
        except (ValueError, SyntaxError, TypeError):
            return None
        if key == "max_pages" and isinstance(parsed, int):
            args[key] = parsed
            continue
        return None

    if mapped == "web_search":
        query = args.get("query")
        if not query:
            return None
        payload = {"query": query}
        for key in ("time_filter", "freshness", "max_pages"):
            if key in args:
                payload[key] = args[key]
        if len(payload) == 1:
            return ToolBlock("web_search", query)
        return ToolBlock("web_search", json.dumps(payload))

    url = args.get("url")
    if not url:
        return None
    return ToolBlock("web_fetch", url)


def _parse_misfenced_read_file_lookup(content: str, *, allow_shell_style: bool = False) -> Optional[ToolBlock]:
    """Recover simple read_file calls wrapped in python/bash fences.

    Mirrors `_parse_misfenced_web_lookup` for the other common miswrite: a
    model fences `read_file("path.py")` as if it were code instead of using
    the real tool-call syntax. `allow_shell_style` additionally recovers a
    bare `read_file <path>` line (optionally with a JSON args blob), which
    only makes sense inside a ```bash fence.
    """
    stripped = content.strip()
    if not stripped:
        return None

    try:
        module = ast.parse(stripped, mode="exec")
    except SyntaxError:
        module = None
    if module and len(module.body) == 1 and isinstance(module.body[0], ast.Expr):
        call = module.body[0].value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            if call.func.id.lower() != "read_file" or len(call.args) > 1:
                return None
            args = {}
            if call.args:
                path = _literal_string(call.args[0])
                if not path:
                    return None
                args["path"] = path
            allowed = {"path", "file", "file_path", "offset", "limit"}
            for keyword in call.keywords:
                if keyword.arg not in allowed:
                    return None
                key = "path" if keyword.arg in ("file", "file_path") else keyword.arg
                if key == "path":
                    path = _literal_string(keyword.value)
                    if not path:
                        return None
                    args["path"] = path
                    continue
                try:
                    value = ast.literal_eval(keyword.value)
                except (ValueError, SyntaxError, TypeError):
                    return None
                if not isinstance(value, int) or value < 0:
                    return None
                args[key] = value
            if not args.get("path"):
                return None
            from src.tool_schemas import function_call_to_tool_block
            return function_call_to_tool_block("read_file", json.dumps(args))

    if not allow_shell_style:
        return None
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    match = re.fullmatch(r"read_file\s+(.+)", lines[0], re.IGNORECASE)
    if not match:
        return None
    path = match.group(1).strip()
    if not path:
        return None
    if path.startswith("{"):
        try:
            args = json.loads(path)
        except json.JSONDecodeError:
            return None
        if not isinstance(args, dict):
            return None
        normalized = {}
        raw_path = args.get("path") or args.get("file") or args.get("file_path")
        if isinstance(raw_path, str) and raw_path.strip():
            normalized["path"] = raw_path.strip()
        for key in ("offset", "limit"):
            value = args.get(key)
            if isinstance(value, int) and value >= 0:
                normalized[key] = value
        if not normalized.get("path"):
            return None
        from src.tool_schemas import function_call_to_tool_block
        return function_call_to_tool_block("read_file", json.dumps(normalized))
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "'\"":
        path = path[1:-1].strip()
    if not path:
        return None
    return ToolBlock("read_file", path)


def _coerce_raw_web_query(value) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _raw_web_json_to_tool_block(payload) -> Optional[ToolBlock]:
    if not isinstance(payload, dict):
        return None
    if set(payload) - _RAW_WEB_JSON_ALLOWED_KEYS:
        return None

    query = _coerce_raw_web_query(payload.get("query"))
    if not query:
        query = _coerce_raw_web_query(payload.get("queries"))
    if not query:
        return None

    content = {"query": query}
    for key in ("time_filter", "freshness"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip().lower() in ("day", "week", "month", "year"):
            content[key] = value.strip().lower()

    max_pages = payload.get("max_pages")
    if isinstance(max_pages, int) and 1 <= max_pages <= 10:
        content["max_pages"] = max_pages

    if len(content) == 1:
        return ToolBlock("web_search", query)
    return ToolBlock("web_search", json.dumps(content))


def _parse_raw_web_json_lookup(text: str):
    """Recover local text-model web_search calls emitted as prose + bare JSON.

    Some non-native tool models leak the intended call as:

        Need to do web_search for ...
        {"query": "...", "time_filter": "week"}

    Keep this narrower than fenced/tool markup: it only runs when a known web
    tool name appears shortly before a JSON object shaped like web_search args.
    Returns `(ToolBlock, (start, end))` — the span is used by `strip_tool_blocks`
    to remove exactly the matched JSON, or `None` if nothing matched.
    """
    if not isinstance(text, str):
        return None

    decoder = json.JSONDecoder()
    for mention in _RAW_WEB_JSON_TOOL_RE.finditer(text):
        search_start = mention.end()
        search_end = min(len(text), search_start + 1200)
        for brace in re.finditer(r"\{", text[search_start:search_end]):
            start = search_start + brace.start()
            try:
                parsed, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            block = _raw_web_json_to_tool_block(parsed)
            if block:
                return block, (start, start + end)
    return None


def _parse_gemma_tool_call(tool_name: str, body: str) -> Optional[ToolBlock]:
    """Parse a Gemma-style call:tool_name{...} block into a ToolBlock."""
    tool_name = tool_name.strip().lower().replace("-", "_")
    body = body.strip()
    if not body:
        return None

    # Replace custom Gemma string delimiters with standard quotes
    body = body.replace('<|"|>', '"').replace('<|"', '"').replace('"|>', '"')

    # Try standard JSON parsing
    params = {}
    try:
        params = json.loads(body)
        if not isinstance(params, dict):
            params = {}
    except json.JSONDecodeError:
        # Try unquoted keys repair: e.g. {query: "..."} -> {"query": "..."}
        try:
            repaired = re.sub(r'([{,]\s*)(\w+)\s*:', r'\1"\2":', body)
            params = json.loads(repaired)
            if not isinstance(params, dict):
                params = {}
        except Exception:
            # Simple regex key-value extraction fallback
            params = {}
            for m in re.finditer(r'(\w+)\s*:\s*["\']?(.*?)["\']?(?=\s*,\s*\w+\s*:|\s*\})', body):
                k = m.group(1)
                v = m.group(2).strip()
                params[k] = v

    from src.tool_schemas import function_call_to_tool_block
    return function_call_to_tool_block(tool_name, json.dumps(params))


def _looks_like_json_body(body: str) -> bool:
    """True when a <tool_call> wrapper body is JSON, not XML markup."""
    return body.lstrip()[:1] in ("{", "[")


def _parse_json_tool_call_body(body: str) -> Optional[ToolBlock]:
    """Parse a Qwen/Hermes text-mode wrapper body: bare JSON inside <tool_call>.

      <tool_call>
      {"name": "bash", "arguments": {"command": "mkdir -p agent-test"}}
      </tool_call>

    Strict by design: the body must decode to an object with a string "name",
    and "arguments" — when present — must itself be an object. Anything else
    returns None rather than being coerced, so a malformed call is dropped
    instead of dispatching with mangled arguments. `raw_decode` tolerates
    trailing chatter after the JSON object; the trailing text is never
    scanned for tool markup. Conversion goes through
    `function_call_to_tool_block` so aliases and per-tool argument formatting
    stay identical to the XML invoke path.
    """
    stripped = body.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    if "arguments" in parsed and not isinstance(parsed["arguments"], dict):
        return None
    args = parsed.get("arguments", {})
    from src.tool_schemas import function_call_to_tool_block
    return function_call_to_tool_block(name.strip().lower(), json.dumps(args))


def _parse_tool_call_block(raw: str) -> Optional[ToolBlock]:
    """Parse a [TOOL_CALL] block into a ToolBlock.

    Handles formats like:
      {tool => "shell", args => {--command "ls -la"}}
      {tool: "shell", command: "ls -la"}
    """
    # Try to extract tool name
    tool_match = re.search(r'tool\s*(?:=>|:|=)\s*["\']?(\w+)["\']?', raw, re.IGNORECASE)
    if not tool_match:
        return None

    tool_name = tool_match.group(1).lower()
    # Fall back to the raw name when it's a real tool but not in the alias
    # map, so known tools (e.g. manage_calendar) aren't silently dropped.
    mapped = _TOOL_NAME_MAP.get(tool_name) or (tool_name if tool_name in TOOL_TAGS else None)
    if not mapped:
        return None

    # Extract the command/content — try several patterns
    content = None

    # Pattern: --command "value" or --command 'value'
    cmd_match = re.search(r'--command\s+["\'](.+?)["\']', raw, re.DOTALL)
    if cmd_match:
        content = cmd_match.group(1)

    # Pattern: command => "value" or command: "value"
    if not content:
        cmd_match = re.search(r'command\s*(?:=>|:|=)\s*["\'](.+?)["\']', raw, re.DOTALL)
        if cmd_match:
            content = cmd_match.group(1)

    # Pattern: args => {content} — extract everything inside the nested braces.
    # Find the opener, then take through the LAST `}` (rfind). Equivalent to the
    # greedy `\{([\s\S]*)\}` capture, but the bounded opener + rfind avoids
    # re.search rescanning from every `args:{` opener when no `}` is reachable
    # (CodeQL py/polynomial-redos).
    if not content:
        am = _ARGS_BRACE_OPEN_RE.search(raw)
        close = raw.rfind('}')
        if am and close >= am.end():
            inner = raw[am.end():close].strip()
            # Strip quotes and key prefixes
            inner = re.sub(r'^--?\w+\s+', '', inner)
            inner = inner.strip('\'"')
            if inner:
                content = inner

    # Pattern: query/path/code => "value"
    if not content:
        for key in ("query", "path", "code", "content", "text", "file"):
            m = re.search(rf'{key}\s*(?:=>|:|=)\s*["\'](.+?)["\']', raw, re.DOTALL)
            if m:
                content = m.group(1)
                break

    # Last resort: take everything after the tool declaration
    if not content:
        rest = raw[tool_match.end():].strip()
        rest = re.sub(r'^[,;]\s*', '', rest)
        rest = rest.strip('{} \t\n\'"')
        if rest:
            content = rest

    if content:
        return ToolBlock(mapped, content.strip())
    return None


def _parse_xml_invoke(name, body) -> Optional[ToolBlock]:
    """Parse an <invoke name="tool"><parameter ...>...</parameter></invoke> call.

    Delegates content-shaping to function_call_to_tool_block — the SAME
    converter used for native function calls — so the full tool set (every
    name in TOOL_TAGS, plus email + MCP tools) and the correct per-tool
    content format are handled in ONE place. The previous version duplicated
    a partial, hand-maintained tool-name map plus a `key: value` serializer:
    any tool missing from that map (e.g. `manage_calendar`) was silently
    dropped, and JSON-arg tools got an unparseable `k: v` blob. Both bugs
    made deepseek's DSML `create_event` calls vanish with no execution.
    """
    # Lowercase the tool name: models often emit capitalized invoke names
    # (e.g. <invoke name="Bash">) and function_call_to_tool_block matches
    # case-sensitively against the lowercase _TOOL_NAME_MAP / TOOL_TAGS, so a
    # raw capitalized name would be silently dropped.
    tool_name = name.lower()
    params = {}
    for pname, pval in _iter_named_blocks(body, _XML_PARAM_OPEN_RE, _XML_PARAM_CLOSE_RE):
        params[pname] = pval.strip()
    # Local import to avoid a circular import at module load.
    from src.tool_schemas import function_call_to_tool_block
    return function_call_to_tool_block(tool_name, json.dumps(params))


def _parse_tool_code_block(raw: str) -> Optional[ToolBlock]:
    """Parse a <tool_code>{tool => 'name', args => '...'}</tool_code> block (MiniMax style)."""
    # Extract tool name
    tool_match = re.search(r"tool\s*=>\s*['\"](\S+?)['\"]", raw)
    if not tool_match:
        return None
    tool_name = tool_match.group(1).lower().replace('-', '_')
    # Strip MCP prefixes like "mcp__server__" or "cli-mcp-server-"
    for prefix in ("mcp__", "cli_mcp_server_", "desktop_commander_", "mcp_code_executor_"):
        if tool_name.startswith(prefix):
            tool_name = tool_name[len(prefix):]
            break

    mapped = _TOOL_NAME_MAP.get(tool_name)

    # Extract args content
    args_match = re.search(r"args\s*=>\s*['\"]?\s*([\s\S]*?)\s*['\"]?\s*$", raw, re.DOTALL)
    args_body = args_match.group(1).strip().strip("'\"") if args_match else ""

    # Parse XML params inside args (e.g. <command>ls</command>). Forward-only
    # backref scan so a `<x><x>...` opener flood can't drive the O(n^2) lazy
    # rescan (CodeQL py/polynomial-redos); see _iter_backref_blocks.
    xml_params = {}
    for pname, pval in _iter_backref_blocks(args_body, _TOOL_CODE_PARAM_OPEN_RE, _TOOL_CODE_PARAM_CLOSE_ANY_RE):
        xml_params[pname] = pval.strip()

    # When the model gave structured params, hand them to the canonical
    # converter (same as native calls + <invoke>) so the full tool set and
    # correct per-tool content format apply — not a partial map + k:v blob.
    if xml_params:
        from src.tool_schemas import function_call_to_tool_block
        block = function_call_to_tool_block(mapped or tool_name, json.dumps(xml_params))
        if block:
            return block

    # No structured params: args_body is a raw single value (e.g. a bash
    # command). Keep the freeform special-casing for the simple tools.
    if mapped:
        if mapped == "bash":
            content = xml_params.get("command", args_body)
        elif mapped == "python":
            content = xml_params.get("code", args_body)
        elif mapped == "web_search":
            content = xml_params.get("query", args_body)
        elif mapped == "web_fetch":
            content = xml_params.get("url", args_body)
        elif mapped in ("read_file", "write_file"):
            content = xml_params.get("path", xml_params.get("file_path", args_body))
        else:
            content = "\n".join(f"{k}: {v}" for k, v in xml_params.items()) if xml_params else args_body
        if content:
            return ToolBlock(mapped, content.strip())
    elif tool_name and args_body:
        # Unknown tool — try as MCP tool call
        content = "\n".join(f"{k}: {v}" for k, v in xml_params.items()) if xml_params else args_body
        return ToolBlock(tool_name, content.strip())
    return None


def _iter_delimited(text, open_re, close_re):
    """Yield ``(match_start, inner_start, inner_end, match_end)`` for each
    non-overlapping ``open_re ... close_re`` pair, scanning strictly forward.

    For the lazy, non-nesting delimiters here this is equivalent to
    ``re.finditer`` of ``open_re([\\s\\S]*?)close_re`` (each opener pairs with
    the first closer after it; the next scan resumes past that closer), but it
    runs in O(n): the moment an opener has no reachable closer, no later opener
    can have one either, so we stop. ``re.finditer`` instead retries from every
    opener and rescans to end-of-string each time -> O(n^2) on attacker-
    controlled "many openers, no closer" model output (CodeQL py/polynomial-redos).

    A whole-string "is the closer present?" guard is not enough: a stale closer
    placed before an opener flood, or a closer with no matching inner delimiter
    (e.g. `[/TOOL_CALL]` but no `}`), keeps the guard true while every opener
    still rescans. Pairing each opener only with a closer *after* it closes both
    holes.
    """
    pos = 0
    while True:
        om = open_re.search(text, pos)
        if om is None:
            return
        cm = close_re.search(text, om.end())
        if cm is None:
            return
        yield om.start(), om.end(), cm.start(), cm.end()
        pos = cm.end()


def _strip_delimited(text: str, open_re, close_re) -> str:
    """Remove every ``open_re ... close_re`` span (forward-only; see
    _iter_delimited). Equivalent to ``open_re([\\s\\S]*?)close_re`` ``re.sub('')``
    for these delimiters, without the O(n^2) rescan on unclosed openers."""
    spans = list(_iter_delimited(text, open_re, close_re))
    if not spans:
        return text
    out = []
    last = 0
    for match_start, _inner_start, _inner_end, match_end in spans:
        out.append(text[last:match_start])
        last = match_end
    out.append(text[last:])
    return "".join(out)


def _iter_named_blocks(text, open_re, close_re):
    """Forward-only equivalent of ``open_re([\\s\\S]*?)close_re`` finditer where
    open_re captures a name in group 1: yield ``(name, body)``, pairing each
    opener with the first ``close_re`` after it. O(n) once no closer is reachable
    from an opener, no later opener has one either (see _iter_delimited), so
    untrusted opener floods can't drive the lazy O(n^2) rescan."""
    pos = 0
    while True:
        om = open_re.search(text, pos)
        if om is None:
            return
        cm = close_re.search(text, om.end())
        if cm is None:
            return
        yield om.group(1), text[om.end():cm.start()]
        pos = cm.end()


def _iter_xml_invoke(text):
    """Forward-only ``<invoke name="..">...</invoke>`` scan (see _iter_named_blocks)."""
    return _iter_named_blocks(text, _XML_INVOKE_OPEN_RE, _XML_INVOKE_CLOSE_RE)


def _iter_backref_blocks(text, open_re, close_any_re, ci=False):
    """Forward-only equivalent of an ``<tag>([\\s\\S]*?)</tag>`` backreference
    finditer (same-name open/close): yield ``(name, body)``, pairing each opener
    with the nearest following matching closer and skipping an opener whose
    closer is unreachable.

    Every closer is indexed by tag name in one linear pass, then each opener
    binary-searches its own name's closer positions. A flood of distinct unclosed
    tag names therefore stays O(n log n) rather than the lazy backref's O(n^2)
    suffix rescan (CodeQL py/polynomial-redos); per-name memoization alone left
    that distinct-name case quadratic. ``close_any_re`` matches ANY closer and
    captures its tag name in group 1; ``ci`` lowercases names for matching, since
    a case-insensitive backref closer needs case-folded comparison."""
    norm = (lambda s: s.lower()) if ci else (lambda s: s)
    closer_starts = {}
    closer_ends = {}
    for cm in close_any_re.finditer(text):
        k = norm(cm.group(1))
        closer_starts.setdefault(k, []).append(cm.start())
        closer_ends.setdefault(k, []).append(cm.end())
    om = open_re.search(text)
    while om is not None:
        name = om.group(1)
        k = norm(name)
        resume = om.end()
        starts = closer_starts.get(k)
        if starts:
            i = bisect.bisect_left(starts, om.end())
            if i < len(starts):
                yield name, text[om.end():starts[i]]
                resume = closer_ends[k][i]
        om = open_re.search(text, resume)


def parse_tool_blocks(text: str, skip_fenced: bool = False) -> List[ToolBlock]:
    """Extract executable tool blocks from LLM response text.

    Supports multiple formats:
    1. ```bash ... ``` fenced code blocks (standard)
    2. [TOOL_CALL] ... [/TOOL_CALL] blocks (some models)
    3. XML-style <tool_call>/<invoke> blocks, including a Hermes/Qwen
       text-mode body that is bare JSON (`{"name": ..., "arguments": {...}}`)
       instead of <invoke> markup
    4. <tool_code> blocks (MiniMax-M2.5 style)
    4b. Gemma 3/4 `<|tool_call|>call:name{...}<|tool_call|>` tokens
    5. DeepSeek DSML markup (normalized to <invoke> first)
    6. Non-native local model fallback: prose mentioning web_search followed by
       bare JSON args, e.g. {"query":"...", "time_filter":"week"}

    `skip_fenced`: when True, Pattern 1 (fenced ```bash/```python/```json code
    blocks) is not matched at all. Native function-calling models (GPT/Claude/
    Grok/Qwen3/DeepSeek-V, etc.) commonly write illustrative fenced examples in
    prose; for those models we trust the structured tool_calls channel for real
    invocations and treat a bare fence as display text rather than an action
    (issue #3222). Patterns 2-5 — explicit [TOOL_CALL]/<invoke>/<tool_code>/DSML
    markup that leaked into content as text — stay fully active regardless,
    since that markup is never an illustrative example and dropping it would
    silently lose real calls (e.g. DeepSeek-V falling back to DSML when it
    can't emit structured tool_calls).
    """
    blocks = []

    # Normalize DeepSeek DSML markup into standard <invoke> form so the
    # XML patterns below catch it.
    text = _normalize_dsml(text)

    # Pattern 1: fenced code blocks (skipped when `skip_fenced` — see docstring).
    if not skip_fenced:
        for m in _TOOL_BLOCK_RE.finditer(text):
            tag = m.group(1).lower()
            content = m.group(2).strip()
            if not content:
                continue
            # If a code block's content is an <invoke> XML call (some models wrap
            # tool calls in ```python or ```xml fences), parse the invoke instead.
            if '<invoke' in content:
                for inv_name, inv_body in _iter_xml_invoke(content):
                    block = _parse_xml_invoke(inv_name, inv_body)
                    if block:
                        blocks.append(block)
                # This fenced block is <invoke> markup, not literal code. Whether or
                # not any call converted, never fall through to append the raw XML as
                # a python/bash block — e.g. a hyphenated/namespaced tool name that
                # _XML_INVOKE_RE's \w+ can't match would otherwise be executed as code.
                continue
            if tag in ("python", "bash"):
                block = (_parse_misfenced_web_lookup(content)
                         or _parse_misfenced_read_file_lookup(content, allow_shell_style=(tag == "bash")))
                if block:
                    blocks.append(block)
                    continue
            blocks.append(ToolBlock(tag, content))

    # Pattern 2: [TOOL_CALL] blocks (only if no fenced blocks found)
    # _iter_delimited scans the delimiter-bounded formats forward-only so
    # untrusted "many openers, no closer" output can't drive the O(n^2)
    # finditer rescan (ReDoS); see its docstring.
    if not blocks:
        for _ms, inner_start, inner_end, _me in _iter_delimited(
            text, _TOOL_CALL_OPEN_RE, _TOOL_CALL_CLOSE_RE
        ):
            block = _parse_tool_call_block(text[inner_start:inner_end])
            if block:
                blocks.append(block)

    # Pattern 3: XML-style <tool_call>/<invoke> blocks
    if not blocks:
        # Try wrapped: <tool_call><invoke ...>...</invoke></tool_call>
        # A wrapper body that is JSON (Qwen/Hermes text mode: a bare
        # {"name": ..., "arguments": {...}} object instead of <invoke> markup)
        # is parsed as JSON or dropped — never scanned by the XML iterator
        # below, so XML-like text inside a JSON argument value stays data
        # instead of selecting a different tool.
        json_body_seen = False
        for _ms, inner_start, inner_end, _me in _iter_delimited(
            text, _XML_TOOL_CALL_OPEN_RE, _XML_TOOL_CALL_CLOSE_RE
        ):
            body = text[inner_start:inner_end]
            if _looks_like_json_body(body):
                json_body_seen = True
                block = _parse_json_tool_call_body(body)
                if block:
                    blocks.append(block)
                continue
            for inv_name, inv_body in _iter_xml_invoke(body):
                block = _parse_xml_invoke(inv_name, inv_body)
                if block:
                    blocks.append(block)
        # Try bare <invoke> without wrapper. Skipped when a JSON wrapper body
        # was seen but produced no block: this rescan covers the full text,
        # wrapper bodies included, and <invoke> markup inside a (possibly
        # malformed) JSON payload must stay data rather than dispatch.
        if not blocks and not json_body_seen:
            for inv_name, inv_body in _iter_xml_invoke(text):
                block = _parse_xml_invoke(inv_name, inv_body)
                if block:
                    blocks.append(block)

    # Pattern 4: <tool_code> blocks (MiniMax-M2.5 style)
    if not blocks:
        for _ms, inner_start, inner_end, _me in _iter_delimited(
            text, _TOOL_CODE_OPEN_RE, _TOOL_CODE_CLOSE_RE
        ):
            block = _parse_tool_code_block(text[inner_start:inner_end])
            if block:
                blocks.append(block)

    # Pattern 4b: Gemma-style <|tool_call|> call:tool_name{args} <tool_call|>
    if not blocks:
        for m in _GEMMA_TOOL_CALL_RE.finditer(text):
            tool_name = m.group(1)
            body = m.group(2)
            block = _parse_gemma_tool_call(tool_name, body)
            if block:
                blocks.append(block)

    # Pattern 6: local text-model web_search call leaked as prose + bare JSON.
    if not blocks and not skip_fenced:
        raw_web_json = _parse_raw_web_json_lookup(text)
        if raw_web_json:
            blocks.append(raw_web_json[0])

    return blocks


def strip_tool_blocks(text: str, skip_fenced: bool = False) -> str:
    """Remove executable tool blocks from text for clean display.

    `skip_fenced`: when True, fenced ```bash/```python/```json code blocks
    (Pattern 1) are left intact instead of being stripped. This must mirror
    whatever `skip_fenced` value `parse_tool_blocks` was called with for the
    same response: if a fence wasn't executed as a tool call (because it's an
    illustrative example from a native function-calling model), it shouldn't
    vanish from the persisted/displayed text either — otherwise the example
    streams once and then disappears on reload (issue #3222 follow-up).
    Patterns 2-5 + DSML markup are always stripped, since that markup should
    never reach the user regardless of whether it converted to a tool call.
    """
    # Normalize DSML first so its markup gets stripped by the <invoke>
    # / <tool_call> removers below instead of leaking to the user.
    text = _normalize_dsml(text)
    cleaned = text if skip_fenced else _TOOL_BLOCK_RE.sub('', text)
    # Forward-only removal mirrors parse_tool_blocks: _strip_delimited pairs each
    # opener with a later closer and stops when none is reachable, so untrusted
    # output can't drive the O(n^2) lazy-rescan (ReDoS); see _iter_delimited.
    cleaned = _strip_delimited(cleaned, _TOOL_CALL_OPEN_RE, _TOOL_CALL_CLOSE_RE)
    cleaned = _strip_delimited(cleaned, _XML_TOOL_CALL_OPEN_RE, _XML_TOOL_CALL_CLOSE_RE)
    cleaned = _strip_delimited(cleaned, _TOOL_CODE_OPEN_RE, _TOOL_CODE_CLOSE_RE)
    cleaned = _GEMMA_TOOL_CALL_RE.sub('', cleaned)
    cleaned = _QWEN_ROLE_MARKER_RE.sub('', cleaned)
    cleaned = _QWEN_BARE_MARKER_RE.sub(' ', cleaned)
    if not skip_fenced:
        raw_web_json = _parse_raw_web_json_lookup(cleaned)
        if raw_web_json:
            _, (start, end) = raw_web_json
            cleaned = cleaned[:start] + cleaned[end:]
    # Strip bare <invoke> blocks not wrapped in <tool_call>
    cleaned = re.sub(r'<invoke\s+name=["\'].*?</invoke>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()
