"""
agent_tools.py — Facade module.

Re-exports tool parsing, schemas, execution, and implementations
for backward compatibility. All importers continue to work unchanged.

Sub-modules:
  - tool_parsing.py: regex patterns, parse/strip functions
  - tool_schemas.py: FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
  - tool_execution.py: execute_tool_block, format_tool_result, MCP helpers
  - tool_implementations.py: all do_* tool functions
"""

import logging
from collections import namedtuple

from src.tool_utils import _truncate, get_mcp_manager, set_mcp_manager

logger = logging.getLogger(__name__)

from .subprocess_tools import BashTool, PythonTool
from .web_tools import WebSearchTool, WebFetchTool
from .filesystem_tools import ReadFileTool, WriteFileTool, EditFileTool, LsTool, GlobTool, GrepTool
from .document_tools import CreateDocumentTool, UpdateDocumentTool, EditDocumentTool, SuggestDocumentTool, ManageDocumentTool
from .claude_code_tool import ClaudeCodeTool
from .notify_tool import NotifyDeviceTool
from .devices_tool import ManageDevicesTool
from .bg_job_tools import ManageBgJobsTool

TOOL_HANDLERS = {
    "bash": BashTool().execute,
    "claude_code": ClaudeCodeTool().execute,
    "notify_device": NotifyDeviceTool().execute,
    "manage_devices": ManageDevicesTool().execute,
    "python": PythonTool().execute,
    "web_search": WebSearchTool().execute,
    "web_fetch": WebFetchTool().execute,
    "read_file": ReadFileTool().execute,
    "write_file": WriteFileTool().execute,
    "edit_file": EditFileTool().execute,
    "ls": LsTool().execute,
    "glob": GlobTool().execute,
    "grep": GrepTool().execute,
    "create_document": CreateDocumentTool().execute,
    "update_document": UpdateDocumentTool().execute,
    "edit_document": EditDocumentTool().execute,
    "suggest_document": SuggestDocumentTool().execute,
    "manage_documents": ManageDocumentTool().execute,
    "manage_bg_jobs": ManageBgJobsTool().execute,
}

# ---------------------------------------------------------------------------
# Constants (re-exported for backward compatibility — single source of truth
# is src.constants; always prefer importing from there for new code)
# ---------------------------------------------------------------------------
MAX_AGENT_ROUNDS = 50
SHELL_TIMEOUT = 60
PYTHON_TIMEOUT = 30

# Tool types that trigger execution
TOOL_TAGS = {"bash", "python", "web_search", "web_fetch", "read_file", "write_file", "edit_file",
             "grep", "glob", "ls", "manage_bg_jobs",
             "create_document", "update_document", "edit_document",
             "search_chats",
             "chat_with_model", "create_session", "list_sessions",
             "send_to_session",
             "pipeline",
             "manage_session", "manage_memory", "list_models",
             "ui_control", "generate_image", "ask_user", "update_plan", "propose_plan",
             "manage_tasks", "api_call", "ask_teacher", "manage_skills",
             "suggest_document",
             "manage_endpoints", "manage_mcp", "manage_webhooks",
             "manage_tokens", "manage_documents", "manage_settings",
             "manage_notes", "manage_calendar",
             "resolve_contact", "manage_contact", "list_email_accounts", "send_email", "list_emails",
             "read_email", "reply_to_email", "bulk_email", "archive_email",
             "delete_email", "mark_email_read",
             # Cookbook tools (LLM serving + downloads). Without these
             # entries, native function calls to e.g. list_served_models
             # are rejected as "Unknown function call" before reaching
             # the dispatcher — silent failure for the whole cookbook
             # surface.
             "download_model", "serve_model",
             "list_served_models", "stop_served_model",
             "list_downloads", "cancel_download",
             "search_hf_models", "list_cached_models",
             "list_serve_presets", "serve_preset", "adopt_served_model",
             "list_cookbook_servers",
             # Other tools the agent reaches for that were also missing.
             "edit_image", "trigger_research", "manage_research",
             "claude_code", "notify_device", "manage_devices",
             "chat_memory",
             # Threads of a chat: a branch, or a subagent run in the
             # background (src/chat_subagents.py).
             "branch_thread", "spawn_subagent",
             # The merged PRs (src/whats_new.py), read-only.
             "whats_new",
             # Generic loopback to any UI-button endpoint (cookbook,
             # gallery, email folders, etc.) — agent uses this when
             # there's no named tool wrapper for the action.
             "app_api"}
# The user's Company from a chat (src/agent_tools/company_chat_tools.py).
from .company_chat_tools import ALL_TOOLS as _COMPANY_CHAT_TOOLS  # noqa: E402
TOOL_TAGS |= _COMPANY_CHAT_TOOLS
# Game streaming from the PC: Sunshine apps, launch link, stop, Steam sync
# (src/agent_tools/game_tools.py).
from .game_tools import ALL_TOOLS as _GAME_TOOLS  # noqa: E402
TOOL_TAGS |= _GAME_TOOLS
# Company agents' tools (src/company_tools.py); only offered in a Company run.
from src.company_tools import TOOL_NAMES as _COMPANY_TOOL_NAMES  # noqa: E402
TOOL_TAGS |= set(_COMPANY_TOOL_NAMES)

# Google products: Drive, Docs, Sheets, Slides, Tasks, Contacts
# (src/google_workspace/products.py).
from src.google_workspace.products import ALL_TOOLS as _GOOGLE_TOOLS  # noqa: E402
TOOL_TAGS |= _GOOGLE_TOOLS

ToolBlock =namedtuple("ToolBlock", ["tool_type", "content"])

# ---------------------------------------------------------------------------
# Re-exports from sub-modules
# ---------------------------------------------------------------------------

# Parsing
from src.tool_parsing import (  # noqa: E402, F401
    parse_tool_blocks,
    strip_tool_blocks,
    _TOOL_NAME_MAP,
    _TOOL_BLOCK_RE,
    _TOOL_CALL_RE,
    _XML_TOOL_CALL_RE,
    _XML_INVOKE_RE,
    _XML_PARAM_RE,
)

# Schemas
from src.tool_schemas import (  # noqa: E402, F401
    FUNCTION_TOOL_SCHEMAS,
    function_call_to_tool_block,
)

# Execution
from src.tool_execution import (  # noqa: E402, F401
    execute_tool_block,
    format_tool_result,
)

# Document functions
from .document_tools import (
    set_active_document, 
    set_active_model
)

# Implementations
from src.tool_implementations import (  # noqa: E402, F401
    do_search_chats,
    do_manage_skills,
    do_manage_tasks,
    do_manage_endpoints,
    do_manage_mcp,
    do_manage_webhooks,
    do_manage_tokens,
    do_manage_settings,
    do_api_call,
)
