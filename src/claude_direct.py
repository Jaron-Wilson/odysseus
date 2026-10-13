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

- One turn at a time per session. A second message while one runs is
  refused, never run alongside - two writers to one session would interleave
  its transcript.
- The turn runs in its own task and saves itself when it ends, so closing
  the page or a phone going to sleep does not cut it off.
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

# session id -> the running turn, so a second message is refused.
_running: Dict[str, "asyncio.Task"] = {}


def is_on(chat_id: str) -> bool:
    a = claude_attach.get(chat_id)
    return bool(a and a.get("direct"))


def busy(session_id: str) -> bool:
    t = _running.get(session_id)
    return bool(t and not t.done())


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


async def _run_turn(chat_id: str, session_id: str, cwd: str, prompt: str,
                    out: "asyncio.Queue", save) -> None:
    """Runs one turn, puts SSE chunks on `out` (None when finished) and
    saves the reply through `save(text, metadata)` whatever happens to the
    page that asked."""
    from src.agent_tools.claude_code_tool import engine_cli, _cli_env

    started = time.time()
    text_parts: list = []
    cost = None
    new_id = ""
    error = ""

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
    proc = None
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
                            elif block.get("type") == "tool_use":
                                hint = _progress(ev)
                                if hint:
                                    put({"delta": hint + "\n", "thinking": True})
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
        except Exception as e:
            logger.exception("[claude-direct] turn failed")
            error = f"Could not run Claude Code: {e}"

    if error:
        put({"delta": ("\n\n" if text_parts else "") + error})
        text_parts.append(("\n\n" if text_parts else "") + error)
    if new_id and new_id != session_id:
        claude_attach.follow(chat_id, session_id, new_id)
    reply = "".join(text_parts).strip() or "(Claude Code finished without saying anything.)"
    metrics = {"response_time": round(time.time() - started, 2), "model": MODEL_LABEL}
    if cost is not None:
        metrics["cost_usd"] = cost
    try:
        save(reply, {"model": MODEL_LABEL, "claude_direct": {"session_id": new_id or session_id,
                                                             "cost_usd": cost}})
    except Exception:
        logger.exception("[claude-direct] could not save the reply")
    put({"type": "metrics", "data": metrics})
    try:
        out.put_nowait("data: [DONE]\n\n")
        out.put_nowait(None)
    except Exception:
        pass


def ccs_valid(sid: str) -> bool:
    from src import claude_code_sessions as ccs
    return ccs.valid_session_id(sid)


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
    if busy(sid):
        yield _sse({"delta": "Claude Code is still answering your last message in this session. "
                             "Send this again when that reply has finished."})
        yield "data: [DONE]\n\n"
        return

    sess.add_message(ChatMessage("user", message))
    session_manager.save_sessions()

    def save(reply: str, metadata: Dict) -> None:
        sess.add_message(ChatMessage("assistant", reply, metadata=metadata))
        session_manager.save_sessions()

    out: asyncio.Queue = asyncio.Queue()
    yield _sse({"type": "model_info", "model": MODEL_LABEL})
    task = asyncio.create_task(_run_turn(chat_id, sid, a.get("cwd") or "", message, out, save))
    _running[sid] = task
    task.add_done_callback(lambda _t, s=sid: _running.pop(s, None) if _running.get(s) is _t else None)
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
