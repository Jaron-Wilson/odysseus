"""Google Tasks: task lists, the tasks in one, and adding a task."""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional

from src.google_workspace import connection
from src.google_workspace.common import clamp
from src.google_workspace.connection import GoogleError

logger = logging.getLogger(__name__)

API = "https://tasks.googleapis.com/tasks/v1"
_LIST_RE = re.compile(r"^[A-Za-z0-9_@-]{1,200}$")


def _list_id(raw) -> str:
    s = str(raw or "").strip() or "@default"
    if not _LIST_RE.match(s):
        raise GoogleError("That is not a Google Tasks list ID. Call google_tasks_list with tasklist='lists'.")
    return s


async def list_lists(user: Optional[str]) -> Dict:
    async with connection.client() as c:
        r = await connection.call(c, user, "GET", f"{API}/users/@me/lists", params={"maxResults": "100"})
    if r.status_code != 200:
        raise connection.explain(r, "tasks", "task lists")
    items = r.json().get("items") or []
    lines = [f"{i.get('title') or '(untitled)'} | id {i.get('id')}" for i in items]
    return {"output": "Task lists:\n" + ("\n".join(lines) if lines else "(none)"), "exit_code": 0}


async def list_tasks(user: Optional[str], tasklist: str = "", show_completed: bool = False, limit=50) -> Dict:
    if str(tasklist or "").strip().lower() == "lists":
        return await list_lists(user)
    lid = _list_id(tasklist)
    params = {"maxResults": str(clamp(limit, 50, 1, 100)),
              "showCompleted": "true" if show_completed else "false",
              "showHidden": "true" if show_completed else "false"}
    async with connection.client() as c:
        r = await connection.call(c, user, "GET", f"{API}/lists/{lid}/tasks", params=params)
    if r.status_code != 200:
        raise connection.explain(r, "tasks", "task list")
    items = r.json().get("items") or []
    lines = []
    for t in items:
        bits = [("[x] " if t.get("status") == "completed" else "[ ] ") + (t.get("title") or "(untitled)")]
        if t.get("due"):
            bits.append(f"due {t['due'][:10]}")
        if t.get("notes"):
            bits.append(" ".join(t["notes"].split())[:200])
        bits.append(f"id {t.get('id')}")
        lines.append(" | ".join(bits))
    return {"output": ("\n".join(lines) if lines else "No open tasks in that list."), "exit_code": 0}


def _due(text: str) -> str:
    """RFC 3339 for Google (it keeps only the date)."""
    text = str(text or "").strip()
    if not text:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return f"{text}T00:00:00.000Z"
    try:
        from routes.calendar_routes import parse_due_for_user
        dt = datetime.fromisoformat(parse_due_for_user(text).replace("Z", "+00:00"))
    except Exception:
        raise GoogleError(f"Could not read the due date: {text[:60]}")
    return f"{dt.astimezone(timezone.utc).date().isoformat() if dt.tzinfo else dt.date().isoformat()}T00:00:00.000Z"


async def create(user: Optional[str], title: str, notes: str = "", due: str = "", tasklist: str = "") -> Dict:
    title = " ".join(str(title or "").split())[:1000]
    if not title:
        raise GoogleError("What is the task? Pass title.")
    body: Dict = {"title": title}
    if notes:
        body["notes"] = str(notes)[:8000]
    d = _due(due)
    if d:
        body["due"] = d
    lid = _list_id(tasklist)
    async with connection.client() as c:
        r = await connection.call(c, user, "POST", f"{API}/lists/{lid}/tasks", json=body)
    if r.status_code not in (200, 201):
        raise connection.explain(r, "tasks", "add the task")
    t = r.json()
    return {"output": f"Added the task \"{title}\"" + (f", due {d[:10]}" if d else "") + f" (id {t.get('id')}).",
            "exit_code": 0}
