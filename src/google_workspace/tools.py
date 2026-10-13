"""The agent's google_* tools (names, schemas and products in products.py).

Every call is scoped to the chat's owner and their own Google connection.
A tool whose product is off is hidden from the model (disabled_for) and
refuses to run anyway (run_tool checks again), so switching a product off in
Settings > Google takes effect on the next turn.
"""

import json
import logging
import os
from typing import Dict, Optional, Set

from src.google_workspace import connection
from src.google_workspace import products as P
from src.google_workspace.connection import GoogleError

logger = logging.getLogger(__name__)


def disabled_for(owner: Optional[str]) -> Set[str]:
    """google_* tools to hide this turn: those whose product is off."""
    try:
        on = set(connection.enabled_products(owner))
    except Exception:
        on = set()
    return {name for name, t in P.TOOLS.items() if t["product"] and t["product"] not in on}


def status_text(owner: Optional[str]) -> str:
    st = connection.status(owner)
    if not st["configured"]:
        return ("No Google OAuth client is set up on this server yet, so no Google product can be connected. "
                "An admin adds one in Settings > Google (steps in docs/google.md).")
    on = [p for p in st["products"] if p["enabled"]]
    lost = [p for p in st["products"] if p.get("needs_reconnect")]
    off = [p for p in st["products"] if not p["enabled"] and not p.get("needs_reconnect")]
    lines = []
    if on:
        lines.append(f"Connected as {st['email'] or 'a Google account'}: " + ", ".join(p["label"] for p in on) + ".")
    else:
        lines.append("No Google product is connected.")
    if lost:
        lines.append("Needs reconnect (Google no longer holds the permission): " +
                     ", ".join(p["label"] for p in lost) + ". The user presses Reconnect on its card in "
                     "Settings > Google and keeps every box ticked on Google's consent screen.")
    if off:
        lines.append("Not connected: " + ", ".join(p["label"] for p in off) +
                     ". The user turns them on with Connect on each card in Settings > Google.")
    return " ".join(lines)


def _args(content: str) -> Dict:
    raw = (content or "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except ValueError:
        return {"query": raw}
    return d if isinstance(d, dict) else {}


def _read_upload(path: str, is_admin: bool) -> bytes:
    if not is_admin:
        raise GoogleError("Only an admin can upload files from this server; pass content instead.")
    from src.tool_execution import _resolve_tool_path
    try:
        real = _resolve_tool_path(path)
    except ValueError as e:
        raise GoogleError(str(e))
    if not os.path.isfile(real):
        raise GoogleError(f"No such file: {path}")
    from src.google_workspace.drive import MAX_UPLOAD
    if os.path.getsize(real) > MAX_UPLOAD:
        raise GoogleError(f"Too big to upload from here (at most {MAX_UPLOAD // (1024 * 1024)} MB).")
    with open(real, "rb") as f:
        return f.read()


async def _dispatch(tool: str, a: Dict, owner: Optional[str], is_admin: bool) -> Dict:
    from src.google_workspace import contacts, docs, drive, sheets, slides, tasks
    if tool == "google_drive_search":
        return await drive.search(owner, a.get("query") or "", a.get("kind") or "", a.get("folder_id") or "",
                                  a.get("limit") or 20, a.get("page_token") or "")
    if tool == "google_drive_read":
        return await drive.read(owner, a.get("file") or a.get("file_id") or a.get("id") or "",
                                a.get("offset") or 0, a.get("max_chars"), bool(a.get("details_only")))
    if tool == "google_drive_upload":
        if a.get("path"):
            data = _read_upload(str(a["path"]), is_admin)
            name = a.get("name") or os.path.basename(str(a["path"]))
        else:
            if a.get("content") is None:
                raise GoogleError("Pass content (the file's text) or path.")
            data, name = str(a.get("content")).encode("utf-8"), a.get("name") or ""
        convert = str(a.get("convert") or "").lower()
        return await drive.upload(owner, name, data, a.get("mime_type") or "", a.get("folder_id") or "",
                                  convert if convert in ("doc", "sheet") else "")
    if tool == "google_drive_create_folder":
        return await drive.create_folder(owner, a.get("name") or "", a.get("parent_id") or "")
    if tool == "google_doc_read":
        return await docs.read(owner, a.get("document") or a.get("id") or "", a.get("offset") or 0, a.get("max_chars"))
    if tool == "google_doc_create":
        return await docs.create(owner, a.get("title") or "", a.get("content") or "")
    if tool == "google_doc_edit":
        return await docs.edit(owner, a.get("document") or a.get("id") or "", a.get("action") or "",
                               a.get("text") or "", a.get("index"), a.get("find") or "", a.get("replace") or "",
                               a.get("match_case", True) is not False)
    if tool == "google_sheet_read":
        return await sheets.read(owner, a.get("spreadsheet") or a.get("id") or "", a.get("range") or "",
                                 bool(a.get("formulas")), a.get("max_rows") or 100)
    if tool == "google_sheet_write":
        return await sheets.write(owner, a.get("spreadsheet") or a.get("id") or "", a.get("range") or "",
                                  a.get("rows") or a.get("values"))
    if tool == "google_sheet_append":
        return await sheets.append(owner, a.get("spreadsheet") or a.get("id") or "", a.get("range") or "",
                                   a.get("rows") or a.get("values"))
    if tool == "google_sheet_create":
        return await sheets.create(owner, a.get("title") or "", a.get("tabs"), a.get("rows"))
    if tool == "google_slides_read":
        return await slides.read(owner, a.get("presentation") or a.get("id") or "", a.get("offset") or 0,
                                 a.get("max_chars"))
    if tool == "google_slides_create":
        return await slides.create(owner, a.get("title") or "", a.get("subtitle") or "", a.get("slides"))
    if tool == "google_tasks_list":
        return await tasks.list_tasks(owner, a.get("tasklist") or "", bool(a.get("show_completed")),
                                      a.get("limit") or 50)
    if tool == "google_task_create":
        return await tasks.create(owner, a.get("title") or "", a.get("notes") or "", a.get("due") or "",
                                  a.get("tasklist") or "")
    if tool == "google_contacts_search":
        return await contacts.search(owner, a.get("query") or "", a.get("limit") or 10)
    raise GoogleError(f"Unknown Google tool {tool}.")


async def run_tool(tool: str, content: str, owner: Optional[str] = None, is_admin: bool = False) -> Dict:
    spec = P.TOOLS.get(tool)
    if not spec:
        return {"error": f"Unknown Google tool {tool}.", "exit_code": 1}
    if tool == "google_status":
        return {"output": status_text(owner), "exit_code": 0}
    product = spec["product"]
    if not connection.is_enabled(owner, product):
        label = P.PRODUCTS[product]["label"]
        return {"error": f"{label} is not connected. The user can connect it in Settings > Google.",
                "exit_code": 1}
    try:
        return await _dispatch(tool, _args(content), owner, is_admin)
    except GoogleError as e:
        return {"error": str(e), "exit_code": 1}
