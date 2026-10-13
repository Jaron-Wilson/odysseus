"""Direct mode: a chat whose replies come straight from its attached Claude
Code session, with no chat model in between.

Asked for on 2026-10-13: "any way we could put this in a seperate chat on my
odysseus server so i can chat with it and it uses this claude code session
only to respond?" - and "can we make a remote control?". `/claude attach
<id>` alone hands the session to the chat's model as a tool, which decides
when to use it and rewords what comes back. `/claude direct` turns that off
for the chat: each message is sent to the session as its next prompt
(`claude -p --resume <id>`, in the session's own folder) and Claude's words
are the reply, as they stream.

- A background agent (`claude --bg`, what `claude agents` lists) holds its
  session, and the CLI will not resume a session whose agent is running -
  only fork a copy (seen on the first direct turn, 2026-10-13). Jaron: "well
  i have all these chats as claude agents though". So a turn takes the agent
  over the way chat agents are parked (src/claude_agent_view.py): an idle
  agent is stopped, which keeps its conversation, the turn runs on the real
  session, and it is parked again afterwards, so `claude agents` and
  `claude attach` still find it. A busy agent - working on something - is
  never interrupted: the message is refused until it is done.
- One turn at a time per session. A second message while one runs is
  refused, never run alongside - two writers to one session would interleave
  its transcript. The refusal tells the page, which shows the running reply.
- The turn runs in its own task and saves itself as it goes, so closing
  the page or a phone going to sleep does not cut it off. "i dont have the
  claude code report back when i refresh the page or switch devices": a
  turn can run for an hour, so the reply is saved as a placeholder when it
  starts and updated every few seconds (_Live), the chat route runs it as a
  detached run (src/agent_runs.py) so a reload reattaches with
  /api/chat/resume, and /api/claude_direct/<chat>/live reads the text so far.
  A restart marks a reply left running as interrupted, keeping its text
  (mark_interrupted_on_startup). A long turn sends one push when it is done.
- Headless, nobody can answer a permission prompt, so it runs in Claude
  Code's "auto" permission mode: the same classifier-gated mode an
  interactive session uses, which refuses what it judges risky instead of
  asking. CLAUDE_DIRECT_PERMISSION_MODE changes it.
- Admin only, like attaching (routes/claude_sessions_routes.py).
"""

import asyncio
import json
import logging
import os
import time
from typing import AsyncGenerator, Dict, Optional

from src import claude_attach

logger = logging.getLogger(__name__)

PERMISSION_MODE = os.environ.get("CLAUDE_DIRECT_PERMISSION_MODE", "auto")
# A real turn can be a long piece of work (a build, a deploy, a test suite).
TURN_TIMEOUT_S = 3600
MODEL_LABEL = "Claude Code (direct)"
# How often the reply so far is written to the chat while a turn runs.
LIVE_SAVE_EVERY_S = 4.0
LIVE_SAVE_EVERY_CHARS = 1500
# A turn that took longer than this sends a push when it is done.
LONG_TURN_S = 120

BUSY_TEXT = ("Claude Code is still answering your last message in this session. "
             "Send this again when that reply has finished.")

# session id -> the running turn, so a second message is refused.
_running: Dict[str, "asyncio.Task"] = {}
# chat id -> the reply being written (_Live), for the read endpoint.
_live: Dict[str, "_Live"] = {}
# Strong refs to the done-notification tasks.
_bg: set = set()


def is_on(chat_id: str) -> bool:
    a = claude_attach.get(chat_id)
    return bool(a and a.get("direct"))


def busy(session_id: str) -> bool:
    t = _running.get(session_id)
    return bool(t and not t.done())


def chat_busy(chat_id: str) -> bool:
    """A direct turn is running in this chat."""
    if chat_id in _live:
        return True
    a = claude_attach.get(chat_id)
    return bool(a and busy(a.get("session_id") or ""))


def command(cli: str, session_id: str) -> list:
    """The CLI call for one turn. The prompt goes in on stdin."""
    return [cli, "-p", "--output-format", "stream-json", "--verbose",
            "--resume", session_id, "--permission-mode", PERMISSION_MODE]


def _sse(obj: Dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _progress(event: Dict) -> Optional[str]:
    """A short line for what Claude is doing (a tool call), or None."""
    try:
        from src.agent_tools.claude_code_tool import _summarize
        line = _summarize(event)
    except Exception:
        line = None
    return line or None


class _Live:
    """The reply as it is written, kept on the chat so a refresh or another
    device sees it.

    A placeholder assistant message is saved when the turn starts and
    updated in place: every LIVE_SAVE_EVERY_S, or sooner once
    LIVE_SAVE_EVERY_CHARS of new text have come in, and once more at the end
    with the whole reply. `_live` holds the same for the read endpoint."""

    def __init__(self, chat_id: str, session_id: str, session_manager, msg, started: float) -> None:
        self.chat_id = chat_id
        self.session_id = session_id
        self.sm = session_manager
        self.msg = msg
        self.text = ""
        self.activity = ""
        self.started = started
        self._saved_at = started
        self._saved_len = 0
        self._dirty = False
        _live[chat_id] = self

    def snapshot(self) -> Dict:
        return {"running": True, "text": self.text, "activity": self.activity,
                "started_at": self.started, "session_id": self.session_id,
                "message_id": (self.msg.metadata or {}).get("_db_id"),
                "elapsed_s": round(time.time() - self.started, 1)}

    def update(self, text: Optional[str] = None, activity: Optional[str] = None) -> None:
        if text is not None:
            self.text = text
        if activity is not None:
            self.activity = activity
        self._dirty = True
        if (time.time() - self._saved_at >= LIVE_SAVE_EVERY_S
                or len(self.text) - self._saved_len >= LIVE_SAVE_EVERY_CHARS):
            self._write()

    def flush(self) -> None:
        """Write what came in since the last write. A turn can go quiet for
        minutes after a message (a long tool call), so this runs on a timer
        too, not only when the next event arrives."""
        if self._dirty and time.time() - self._saved_at >= LIVE_SAVE_EVERY_S:
            self._write()

    def _write(self) -> None:
        self._dirty = False
        self._saved_at = time.time()
        self._saved_len = len(self.text)
        cd = dict((self.msg.metadata or {}).get("claude_direct") or {})
        cd.update({"running": True, "activity": self.activity, "updated_at": self._saved_at})
        self.msg.metadata["claude_direct"] = cd
        self.msg.content = self.text
        _persist(self.sm, self.chat_id, self.msg)

    def finish(self, text: str, metadata: Dict) -> None:
        try:
            keep = {k: v for k, v in (self.msg.metadata or {}).items() if k in ("_db_id", "timestamp")}
            self.msg.metadata = {**keep, **metadata}
            self.msg.content = text
            _persist(self.sm, self.chat_id, self.msg)
        finally:
            if _live.get(self.chat_id) is self:
                _live.pop(self.chat_id, None)


def _persist(session_manager, chat_id: str, msg) -> None:
    """Write a changed message back: the same row, not a new message."""
    try:
        fn = getattr(session_manager, "update_message", None)
        if fn:
            fn(chat_id, msg)
        session_manager.save_sessions()
    except Exception:
        logger.exception("[claude-direct] could not save the reply so far")


async def _run_turn(chat_id: str, session_id: str, cwd: str, prompt: str,
                    out: "asyncio.Queue", live: "_Live") -> None:
    """Runs one turn, puts SSE chunks on `out` (None when finished) and
    saves the reply into `live`'s message as it goes and at the end,
    whatever happens to the page that asked."""
    from src.agent_tools.claude_code_tool import engine_cli, _cli_env

    started = live.started
    text_parts: list = []
    cost = None
    new_id = ""
    error = ""
    stopped = ""

    def put(obj: Dict) -> None:
        try:
            out.put_nowait(_sse(obj))
        except Exception:
            pass

    cli = engine_cli("claude")
    if not cli:
        error = "The Claude Code CLI (`claude`) is not installed on this host."
    elif not cwd or not os.path.isdir(cwd):
        error = f"The session's folder {cwd or '(none)'} is not on this host, so it cannot run."
    was_parked = False
    proc = None

    async def flusher() -> None:
        while True:
            await asyncio.sleep(max(LIVE_SAVE_EVERY_S, 0.5))
            live.flush()

    flushing = asyncio.create_task(flusher())
    try:
        # Take a background agent over for this turn (see the module notes).
        if not error:
            try:
                from src import claude_agent_view
                agent = await asyncio.to_thread(claude_agent_view.find, session_id)
                if agent and agent.get("pid"):
                    if str(agent.get("status") or "") == "busy":
                        error = (f"That session's agent ({agent.get('id')}) is busy working right now. "
                                 "Send this again when it has finished, so the two never write at once.")
                    else:
                        why = await asyncio.to_thread(claude_agent_view.unpark, session_id)
                        if why:
                            error = f"Could not take the session over: {why}"
                        else:
                            was_parked = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[claude-direct] agent check failed: %s", e)
        if not error:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command(cli, session_id), cwd=cwd, env=_cli_env(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()

                async def read() -> None:
                    nonlocal cost, new_id
                    async for raw in proc.stdout:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if ev.get("session_id") and ccs_valid(ev["session_id"]):
                            new_id = ev["session_id"]
                        if ev.get("type") == "assistant":
                            for block in (ev.get("message") or {}).get("content") or []:
                                if block.get("type") == "text" and block.get("text"):
                                    # Separate one message from the next, as the
                                    # terminal does.
                                    chunk = ("\n\n" if text_parts else "") + block["text"]
                                    text_parts.append(chunk)
                                    put({"delta": chunk})
                                    live.update(text="".join(text_parts))
                                elif block.get("type") == "tool_use":
                                    hint = _progress(ev)
                                    if hint:
                                        put({"delta": hint + "\n", "thinking": True})
                                        live.update(activity=hint)
                        elif ev.get("type") == "result":
                            cost = ev.get("total_cost_usd")
                            if ev.get("is_error") and not text_parts:
                                put({"delta": str(ev.get("result") or "Claude Code reported an error.")})
                                text_parts.append(str(ev.get("result") or ""))

                await asyncio.wait_for(read(), timeout=TURN_TIMEOUT_S)
                rc = await proc.wait()
                if rc != 0 and not text_parts:
                    tail = (await proc.stderr.read()).decode("utf-8", "replace").strip()[-800:]
                    error = f"Claude Code exited with code {rc}." + (f"\n\n```\n{tail}\n```" if tail else "")
            except asyncio.TimeoutError:
                error = f"The turn ran past {TURN_TIMEOUT_S // 60} minutes and was stopped."
                if proc and proc.returncode is None:
                    proc.kill()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[claude-direct] turn failed")
                error = f"Could not run Claude Code: {e}"
    except asyncio.CancelledError:
        # Stop, or the server stopping every turn before a restart
        # (agent_runs.stop_all): end the CLI and keep what was written.
        from src import agent_runs
        stopped = "restart" if agent_runs.restarting else "stopped"
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        error = ("The server restarted during this turn, so it was stopped here."
                 if stopped == "restart" else "Stopped.")
    flushing.cancel()

    if error:
        put({"delta": ("\n\n" if text_parts else "") + error})
        text_parts.append(("\n\n" if text_parts else "") + error)
    if new_id and new_id != session_id:
        claude_attach.follow(chat_id, session_id, new_id)
    if was_parked:
        # Back where it was: a background agent `claude attach` opens.
        try:
            from src import claude_agent_view
            await asyncio.to_thread(claude_agent_view.park, new_id or session_id, cwd)
        except Exception as e:
            logger.warning("[claude-direct] could not park the agent again: %s", e)
    reply = "".join(text_parts).strip() or "(Claude Code finished without saying anything.)"
    elapsed = round(time.time() - started, 2)
    metrics = {"response_time": elapsed, "model": MODEL_LABEL}
    if cost is not None:
        metrics["cost_usd"] = cost
    cd = {"session_id": new_id or session_id, "cost_usd": cost, "running": False,
          "started_at": started, "finished_at": time.time()}
    if stopped:
        cd["interrupted"] = stopped
    try:
        live.finish(reply, {**metrics, "claude_direct": cd})
    except Exception:
        logger.exception("[claude-direct] could not save the reply")
    put({"type": "metrics", "data": metrics})
    try:
        out.put_nowait("data: [DONE]\n\n")
        out.put_nowait(None)
    except Exception:
        pass
    if not stopped and elapsed >= LONG_TURN_S:
        _notify_done(chat_id, reply)


def _notify_done(chat_id: str, reply: str) -> None:
    """One push when a long turn is done, so the report is not missed. The
    chat's bell (src/chat_queue.py) sends its own when the run ends, so this
    one is left out when that is set."""
    try:
        from src import chat_queue
        if chat_queue.get(chat_id).get("notify") is not None:
            return
        title = chat_queue._session_title(chat_id)
        heading = f"Claude Code finished: {title}" if title else "Claude Code finished"
        body = chat_queue._preview(reply) or "The report is ready."
        t = asyncio.get_running_loop().create_task(
            chat_queue.send_notification(chat_id, {}, heading, body, kind="done"))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    except Exception:
        logger.exception("[claude-direct] could not send the done notification")


def ccs_valid(sid: str) -> bool:
    from src import claude_code_sessions as ccs
    return ccs.valid_session_id(sid)


async def busy_reply(chat_id: str) -> AsyncGenerator[str, None]:
    """The answer to a second message while a turn runs: refused, and the
    page is told so it can show the running reply instead."""
    live = _live.get(chat_id)
    yield _sse({"type": "claude_direct_busy", "message": BUSY_TEXT,
                "live": live.snapshot() if live else None})
    yield _sse({"delta": BUSY_TEXT})
    yield "data: [DONE]\n\n"


async def stream(chat_id: str, sess, message: str, session_manager) -> AsyncGenerator[str, None]:
    """The chat's reply to `message`, from its attached session."""
    from core.models import ChatMessage

    a = claude_attach.get(chat_id)
    if not a:
        yield _sse({"delta": "No Claude Code session is attached to this chat any more. "
                             "Attach one with /claude attach <id>, or turn direct mode off with /claude direct off."})
        yield "data: [DONE]\n\n"
        return
    sid = a["session_id"]
    if busy(sid) or chat_id in _live:
        async for chunk in busy_reply(chat_id):
            yield chunk
        return

    sess.add_message(ChatMessage("user", message))
    # The reply's placeholder, saved now and filled in as it streams.
    started = time.time()
    msg = ChatMessage("assistant", "", metadata={
        "model": MODEL_LABEL,
        "claude_direct": {"session_id": sid, "running": True, "started_at": started}})
    sess.add_message(msg)
    session_manager.save_sessions()
    live = _Live(chat_id, sid, session_manager, msg, started)

    out: asyncio.Queue = asyncio.Queue()
    yield _sse({"type": "model_info", "model": MODEL_LABEL})
    task = asyncio.create_task(_run_turn(chat_id, sid, a.get("cwd") or "", message, out, live))
    _running[sid] = task
    task.add_done_callback(lambda _t, s=sid: _running.pop(s, None) if _running.get(s) is _t else None)
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(out.get(), timeout=15)
            except asyncio.TimeoutError:
                # A long tool call says nothing for minutes; keep the line open.
                yield ": heartbeat\n\n"
                continue
            if chunk is None:
                return
            yield chunk
    except asyncio.CancelledError:
        # Stop, or a restart stopping every run: the turn stops too and
        # saves what it has (closing the page does not get here, the run is
        # detached; see agent_runs).
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait({task}, timeout=15)
            except Exception:
                pass
        raise


def live_state(chat_id: str, sess=None) -> Dict:
    """The chat's direct reply: the one being written, or the last saved one."""
    live = _live.get(chat_id)
    if live:
        return live.snapshot()
    for m in reversed(getattr(sess, "history", None) or []):
        meta = (m.metadata if hasattr(m, "metadata") else m.get("metadata")) or {}
        role = m.role if hasattr(m, "role") else m.get("role")
        if role != "assistant" or not isinstance(meta.get("claude_direct"), dict):
            continue
        cd = meta["claude_direct"]
        content = m.content if hasattr(m, "content") else m.get("content")
        out = {"running": False, "text": content if isinstance(content, str) else "",
               "started_at": cd.get("started_at"), "finished_at": cd.get("finished_at"),
               "session_id": cd.get("session_id"), "message_id": meta.get("_db_id"),
               "interrupted": cd.get("interrupted") or ""}
        if cd.get("running"):
            # Saved as running but no turn is: it was cut off.
            out["interrupted"] = "restart"
        return out
    return {"running": False, "text": ""}


def mark_interrupted_on_startup() -> int:
    """A restart ends every turn, but a crash or a kill leaves its reply
    saved as running. Mark those interrupted, keeping their text, so no page
    waits on them forever. Called once at startup."""
    from core.database import SessionLocal, ChatMessage as DbChatMessage
    db = SessionLocal()
    fixed = []
    try:
        rows = db.query(DbChatMessage).filter(
            DbChatMessage.role == "assistant",
            DbChatMessage.meta_data.like('%"claude_direct"%'),
            DbChatMessage.meta_data.like('%"running": true%'),
        ).all()
        for row in rows:
            try:
                meta = json.loads(row.meta_data or "{}")
            except (json.JSONDecodeError, ValueError):
                continue
            cd = meta.get("claude_direct")
            if not isinstance(cd, dict) or not cd.get("running"):
                continue
            cd["running"] = False
            cd["interrupted"] = "restart"
            meta["claude_direct"] = cd
            meta.setdefault("model", MODEL_LABEL)
            row.meta_data = json.dumps(meta)
            if not (row.content or "").strip():
                row.content = "(Claude Code had not said anything yet when the server restarted.)"
            fixed.append((row.session_id, row.id, row.content, meta))
        if fixed:
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("[claude-direct] could not check for replies left running")
        return 0
    finally:
        db.close()
    # A chat already loaded in memory gets the same change.
    try:
        from core.models import get_session_manager_instance
        sm = get_session_manager_instance()
        for chat_id, msg_id, content, meta in fixed:
            sess = (getattr(sm, "sessions", None) or {}).get(chat_id) if sm else None
            for m in getattr(sess, "history", None) or []:
                if (getattr(m, "metadata", None) or {}).get("_db_id") == msg_id:
                    m.content = content
                    m.metadata = {**meta, "_db_id": msg_id}
    except Exception:
        pass
    if fixed:
        logger.info("[claude-direct] marked %d reply(s) left running as interrupted", len(fixed))
    return len(fixed)
