"""Branches and subagents: threads of a chat that go their own way.

Asked for on 2026-10-10: "I want to be able to tell a chat to branch this
out, and then be able to have basically a subagent for the chat, like Claude
where I can say subagent this out, it can branch the chat, and have 'Threads'
so that I can visit different threads in each chat."

Both are child sessions (parent_session_id), like the side threads in
src/chat_threads.py, with a ChatThreadInfo row saying which kind they are:

- A branch is seeded with the conversation up to a message (the whole chat
  by default) and is then an ordinary conversation the user carries on.
- A subagent is seeded with the last few messages and a task, and runs on
  its own as a detached agent run (src/agent_runs.py), with the same agent
  loop and tools as any chat. The user keeps talking in the parent. When the
  run ends, its last reply is posted back into the parent as one message,
  which the parent's model reads like a merged side thread.

Limits: at most MAX_CONCURRENT subagents running per chat, subagents nest at
most MAX_SUBAGENT_DEPTH deep (a subagent of a chat may start one of its own,
and that one may not), and threads of any kind nest at most MAX_THREAD_DEPTH.
"""
import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from core.database import SessionLocal, ChatThreadInfo
from core.models import ChatMessage, in_context
from src import chat_threads

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


MAX_CONCURRENT = _env_int("ODYSSEUS_SUBAGENT_MAX_CONCURRENT", 3)
MAX_SUBAGENT_DEPTH = _env_int("ODYSSEUS_SUBAGENT_MAX_DEPTH", 2)
MAX_THREAD_DEPTH = _env_int("ODYSSEUS_THREAD_MAX_DEPTH", 4)
CONTEXT_MESSAGES = 6            # parent messages a subagent starts with
SEED_MSG_CHARS = 4000           # each, cut to this
RESULT_CHARS = 4000             # what is posted back to the parent
POST_BACK_WAIT_S = 900          # how long a result waits for the parent's reply to finish

RESULT_HEADER = "[Subagent {verb} · {name}]"
_VERBS = {"done": "finished", "failed": "failed", "stopped": "stopped"}
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
# The agent's own "_Note: ... context budget ..._" line is not the report.
_NOTE_RE = re.compile(r"(?m)^\s*_Note:.*?_\s*$\n?")

SUBAGENT_PROMPT = """[Subagent task from the chat "{parent}"]
{task}

You are a subagent working for that chat: the messages above are the end of \
its conversation, for context. Work on the task on your own with your tools; \
nobody is watching this thread, so do not ask questions, make reasonable \
choices and say what you assumed. End with a short final report: what you \
found or did, and anything left open. That report is posted back to the chat."""


class ThreadError(Exception):
    """A thread could not be made; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── the ChatThreadInfo rows ─────────────────────────────────────────────
def _row_dict(row) -> dict:
    return {"thread_id": row.thread_id, "parent_id": row.parent_id, "kind": row.kind or "side",
            "status": row.status, "task": row.task, "result": row.result,
            "posted_back": bool(row.posted_back),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}


def info(thread_id: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        row = db.query(ChatThreadInfo).filter(ChatThreadInfo.thread_id == thread_id).first()
        return _row_dict(row) if row else None
    finally:
        db.close()


def infos_for_parent(parent_id: str) -> Dict[str, dict]:
    db = SessionLocal()
    try:
        rows = db.query(ChatThreadInfo).filter(ChatThreadInfo.parent_id == parent_id).all()
        return {r.thread_id: _row_dict(r) for r in rows}
    finally:
        db.close()


def save_info(thread_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.query(ChatThreadInfo).filter(ChatThreadInfo.thread_id == thread_id).first()
        if row is None:
            row = ChatThreadInfo(thread_id=thread_id, parent_id=fields.pop("parent_id", ""))
            db.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


def kind_of(thread_id: str) -> str:
    row = info(thread_id)
    return row["kind"] if row else "side"


# ── where a chat sits ───────────────────────────────────────────────────
def _get(sm, sid: str):
    try:
        return sm.get_session(sid)
    except KeyError:
        return None


def ancestry(sm, sid: str) -> List[str]:
    """This chat's id, then its parent's, up to the top-level chat."""
    out, seen = [], set()
    cur = sid
    while cur and cur not in seen and len(out) < 32:
        seen.add(cur)
        out.append(cur)
        s = sm.sessions.get(cur) if hasattr(sm, "sessions") else None
        if s is None:
            s = _get(sm, cur)
        cur = getattr(s, "parent_session_id", None) if s else None
    return out


def depth(sm, sid: str) -> int:
    return len(ancestry(sm, sid)) - 1


def subagent_depth(sm, sid: str) -> int:
    """How many subagents deep this chat is (itself included)."""
    return sum(1 for a in ancestry(sm, sid) if kind_of(a) == "subagent")


def status_of(thread_id: str, row: Optional[dict]) -> str:
    """running / done / failed / stopped for a subagent; running / idle for
    the others (running while a reply is being written in it)."""
    from src import agent_runs
    if agent_runs.is_active(thread_id):
        return "running"
    if row and row.get("kind") == "subagent":
        st = row.get("status") or "done"
        # Saved as running but no run: a restart cut it off.
        return "stopped" if st == "running" else st
    return "idle"


def running_subagents(sm, parent_id: str) -> List[str]:
    from src import agent_runs
    return [tid for tid, row in infos_for_parent(parent_id).items()
            if row["kind"] == "subagent" and agent_runs.is_active(tid)]


# ── making threads ──────────────────────────────────────────────────────
def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _meta(m) -> dict:
    return m.metadata if isinstance(getattr(m, "metadata", None), dict) else {}


def _new_thread(sm, parent, *, kind: str, name: str, seed: list, anchor: Optional[str],
                endpoint_url: Optional[str] = None, model: Optional[str] = None,
                headers: Optional[dict] = None, task: Optional[str] = None,
                status: Optional[str] = None):
    thread_id = str(uuid.uuid4())
    thread = sm.create_session(
        thread_id, name, endpoint_url or parent.endpoint_url, model or parent.model, rag=False,
        owner=getattr(parent, "owner", None),
        parent_session_id=parent.id, thread_anchor_id=anchor)
    hdrs = headers if headers is not None else getattr(parent, "headers", None)
    if hdrs:
        thread.headers = dict(hdrs)
    for m in seed:
        meta = {k: v for k, v in _meta(m).items()
                if k not in ("_db_id", "timestamp", "excluded", "references")}
        meta["thread_seed"] = True
        content = m.content
        if kind == "subagent" and isinstance(content, str) and len(content) > SEED_MSG_CHARS:
            content = _THINK_RE.sub("", content).strip()[:SEED_MSG_CHARS] + "\n[...]"
        thread.add_message(ChatMessage(m.role, content, metadata=meta))
    save_info(thread_id, parent_id=parent.id, kind=kind, status=status, task=task,
              created_at=datetime.now(timezone.utc).replace(tzinfo=None))
    try:
        from core.database import get_session_mode, set_session_mode
        set_session_mode(thread_id, "agent" if kind == "subagent" else (get_session_mode(parent.id) or "agent"))
    except Exception:
        logger.debug("thread mode not set", exc_info=True)
    return thread


def _anchor_index(hist, anchor_msg_id: Optional[str]) -> int:
    if not anchor_msg_id:
        return len(hist) - 1
    idx = next((i for i, m in enumerate(hist) if _meta(m).get("_db_id") == anchor_msg_id), None)
    if idx is None:
        raise ThreadError("Message not found", 404)
    return idx


def _check_depth(sm, parent_id: str) -> None:
    if depth(sm, parent_id) + 1 > MAX_THREAD_DEPTH:
        raise ThreadError(f"Threads nest at most {MAX_THREAD_DEPTH} deep", 400)


def branch(sm, parent_id: str, *, anchor_msg_id: Optional[str] = None, title: Optional[str] = None,
           drop_trailing_user: bool = False) -> dict:
    """A new thread holding the conversation up to `anchor_msg_id` (the
    whole chat when omitted), which the user then carries on by itself."""
    parent = _get(sm, parent_id)
    if parent is None:
        raise ThreadError("Session not found", 404)
    _check_depth(sm, parent_id)
    hist = list(parent.history)
    idx = _anchor_index(hist, anchor_msg_id)
    seed = [m for m in hist[: idx + 1] if m.role in ("user", "assistant") and in_context(m)]
    if drop_trailing_user and seed and seed[-1].role == "user":
        seed.pop()        # the model was asked to branch: that request stays behind
    last_user = next((_text(m.content) for m in reversed(seed) if m.role == "user"), "")
    title = " ".join((title or "").split())[:80]
    name = title or ("Branch: " + chat_threads._label(last_user, 48) if last_user else f"Branch of {parent.name}")
    anchor = _meta(hist[idx]).get("_db_id") if hist and idx >= 0 else None
    thread = _new_thread(sm, parent, kind="branch", name=name, seed=seed, anchor=anchor)
    try:
        sm.save_sessions()
    except Exception:
        pass
    logger.info("[threads] branch %s from %s (%d messages)", thread.id[:8], parent_id[:8], len(seed))
    return {"id": thread.id, "name": name, "kind": "branch", "parent_session_id": parent_id,
            "anchor_msg_id": anchor, "seeded": len(seed)}


def spawn(sm, parent_id: str, task: str, *, title: Optional[str] = None, model: Optional[str] = None,
          owner: Optional[str] = None, anchor_msg_id: Optional[str] = None, agent_loop=None) -> dict:
    """Start a subagent on `task` in a new thread of `parent_id`, running in
    the background. Needs a running event loop (agent_runs.start)."""
    from src import agent_runs
    task = (task or "").strip()
    if not task:
        raise ThreadError("Say what the subagent should do", 400)
    parent = _get(sm, parent_id)
    if parent is None:
        raise ThreadError("Session not found", 404)
    _check_depth(sm, parent_id)
    if subagent_depth(sm, parent_id) + 1 > MAX_SUBAGENT_DEPTH:
        raise ThreadError(
            f"Subagents nest at most {MAX_SUBAGENT_DEPTH} deep: do this one yourself", 400)
    running = running_subagents(sm, parent_id)
    if len(running) >= MAX_CONCURRENT:
        raise ThreadError(
            f"{len(running)} subagents are already running in this chat (the most is "
            f"{MAX_CONCURRENT}): wait for one to finish or stop one", 429)
    url = hdrs = model_id = None
    if model and str(model).strip():
        from src.ai_interaction import _resolve_model
        try:
            url, model_id, hdrs = _resolve_model(str(model).strip(), owner=owner or getattr(parent, "owner", None))
        except ValueError as e:
            raise ThreadError(str(e), 400)
    hist = list(parent.history)
    ctx = [m for m in hist if m.role in ("user", "assistant") and in_context(m)
           and _text(m.content).strip()][-CONTEXT_MESSAGES:]
    anchor = anchor_msg_id or next((_meta(m).get("_db_id") for m in reversed(hist)
                                    if _meta(m).get("_db_id")), None)
    title = " ".join((title or "").split())[:80]
    name = title or chat_threads._label(task, 50)
    thread = _new_thread(sm, parent, kind="subagent", name=name, seed=ctx, anchor=anchor,
                         endpoint_url=url, model=model_id, headers=hdrs, task=task, status="running")
    thread.add_message(ChatMessage("user", SUBAGENT_PROMPT.format(parent=parent.name, task=task),
                                   metadata={"source": "subagent_task"}))
    try:
        sm.save_sessions()
    except Exception:
        pass
    from src.screen_control_resume import _resume_stream
    agent_runs.start(thread.id, _resume_stream(
        thread, sm, thread.get_context_messages(), agent_loop=agent_loop, source="subagent"))
    logger.info("[threads] subagent %s started in %s: %s", thread.id[:8], parent_id[:8], task[:80])
    return {"id": thread.id, "name": name, "kind": "subagent", "status": "running",
            "parent_session_id": parent_id, "anchor_msg_id": anchor, "model": thread.model}


# ── when a subagent's run ends ──────────────────────────────────────────
def _final_reply(thread) -> str:
    """The subagent's last reply, after its task message."""
    for m in reversed(thread.history):
        if m.role == "user" and _meta(m).get("source") == "subagent_task":
            break
        if m.role == "assistant":
            text = _NOTE_RE.sub("", _THINK_RE.sub("", _text(m.content))).strip()
            if text:
                return text
    return ""


def on_run_finished(session_id: str, run_status: str) -> None:
    """agent_runs calls this when any run ends. For a subagent's first run:
    record how it went and post the result back to its parent."""
    row = info(session_id)
    if not row or row["kind"] != "subagent" or row["posted_back"]:
        return
    from core.models import get_session_manager_instance
    sm = get_session_manager_instance()
    if sm is None:
        return
    thread = _get(sm, session_id)
    reply = _final_reply(thread) if thread else ""
    if run_status == "stopped":
        status = "stopped"
    elif run_status == "done" and reply:
        status = "done"
    else:
        status = "failed"
    result = reply or {"stopped": "Stopped before it wrote anything.",
                       "failed": "The run ended without a reply (see the thread for what happened)."}.get(status, "")
    if len(result) > RESULT_CHARS:
        result = result[:RESULT_CHARS].rstrip() + "\n[...] (the rest is in the thread)"
    save_info(session_id, status=status, result=result,
              finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
    logger.info("[threads] subagent %s %s", session_id[:8], status)
    _schedule_post_back(sm, session_id, row["parent_id"])


def _schedule_post_back(sm, thread_id: str, parent_id: str) -> None:
    """Post now, or once the parent's own reply is written, so the result
    does not land in the middle of a turn."""
    from src import agent_runs
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None or not agent_runs.is_active(parent_id):
        post_back(sm, thread_id)
        return

    async def _later():
        waited = 0.0
        while agent_runs.is_active(parent_id) and waited < POST_BACK_WAIT_S:
            await asyncio.sleep(1.0)
            waited += 1.0
        post_back(sm, thread_id)

    loop.create_task(_later())


def post_back(sm, thread_id: str) -> bool:
    row = info(thread_id)
    if not row or row["posted_back"]:
        return False
    parent = _get(sm, row["parent_id"])
    thread = _get(sm, thread_id)
    if parent is None:
        return False
    name = thread.name if thread else "subagent"
    status = row["status"] or "done"
    content = (RESULT_HEADER.format(verb=_VERBS.get(status, status), name=name)
               + "\n\n" + (row["result"] or ""))
    save_info(thread_id, posted_back=True)
    parent.add_message(ChatMessage("user", content, metadata={
        "source": "subagent_result", "thread_id": thread_id, "status": status}))
    try:
        sm.save_sessions()
    except Exception:
        pass
    return True


# ── the tools the chat model calls ──────────────────────────────────────
def _args(content: str, main: str) -> dict:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
    return {main: raw} if raw else {}


def _sm_or_error():
    from core.models import get_session_manager_instance
    return get_session_manager_instance()


def _owns(sm, session_id: str, owner: Optional[str]) -> bool:
    s = _get(sm, session_id)
    return s is not None and (not owner or not getattr(s, "owner", None) or s.owner == owner)


def run_branch_tool(content: str, *, session_id: Optional[str], owner: Optional[str] = None) -> Dict:
    """branch_thread: put the conversation so far into a new thread."""
    sm = _sm_or_error()
    if not session_id or sm is None or not _owns(sm, session_id, owner):
        return {"error": "branch_thread only works inside a chat", "exit_code": 1}
    args = _args(content, "title")
    try:
        t = branch(sm, session_id, title=args.get("title"), drop_trailing_user=True)
    except ThreadError as e:
        return {"error": f"branch_thread: {e}", "exit_code": 1}
    return {
        "output": (f"Branched into a new thread: [{t['name']}](#session-{t['id']}) "
                   f"({t['seeded']} messages copied). It is in this chat's Threads panel. "
                   "Give the user that link; the branch is theirs to carry on, so do not "
                   "continue the work here."),
        "thread": t, "exit_code": 0,
    }


def run_subagent_tool(content: str, *, session_id: Optional[str], owner: Optional[str] = None) -> Dict:
    """spawn_subagent: hand a task to a subagent running in the background."""
    sm = _sm_or_error()
    if not session_id or sm is None or not _owns(sm, session_id, owner):
        return {"error": "spawn_subagent only works inside a chat", "exit_code": 1}
    args = _args(content, "task")
    try:
        t = spawn(sm, session_id, str(args.get("task") or ""), title=args.get("title"),
                  model=args.get("model"), owner=owner)
    except ThreadError as e:
        return {"error": f"spawn_subagent: {e}", "exit_code": 1}
    return {
        "output": (f"Subagent started in the background: [{t['name']}](#session-{t['id']}) "
                   f"on {t['model']}. It works on its own; its report is posted back to this "
                   "chat when it finishes. Tell the user it is running and end your turn: do "
                   "not wait for it, poll it, or do the same work yourself."),
        "thread": t, "exit_code": 0,
    }
