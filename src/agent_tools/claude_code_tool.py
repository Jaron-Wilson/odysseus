"""Hand a coding task to the local Claude Code CLI, plan first, then execute.

Two phases, and the split is the whole point. `action:"plan"` runs the CLI in
its own plan mode with read-only tools: it reads the project and writes up what
it intends to do, changing nothing. The user approves, amends or rejects that
plan. Only then does `action:"execute"` resume the SAME CLI session with write
tools, so the agent never edits anything the user has not seen described first.

Resuming by session id is what makes the gate cheap: phase two already has all
of phase one's reading in context, so approval costs nothing to re-establish.

Claude Code is already authenticated for whoever runs this server, its
credentials living in the real ``$HOME/.claude``. That is also the catch:
subprocess tools here run with ``HOME`` rewritten to DATA_DIR, which would hide
those credentials, so the real home is restored for the child only.

The CLI's ``--output-format stream-json`` emits one JSON object per line, which
becomes the same ``{elapsed_s, tail}`` progress payload the bash tool uses — so
the existing UI renders a live console with no frontend work.
"""

import asyncio
import collections
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from src import claude_code_approvals as approvals

PROGRESS_INTERVAL_S = 1.5
PROGRESS_TAIL_LINES = 16
DEFAULT_TIMEOUT_S = 900
MAX_RESULT_CHARS = 20000

# Planning reads; it must not be able to write even if the CLI is talked into
# trying. Execution gets the tools needed to actually land the approved change.
PLAN_TOOLS = "Read,Glob,Grep"
EXECUTE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash,TodoWrite"


def _summarize(event: dict) -> Optional[str]:
    """One console line for a stream-json event, or None to show nothing."""
    etype = event.get("type")

    if etype == "system" and event.get("subtype") == "init":
        return f"· session {str(event.get('session_id'))[:8]} in {event.get('cwd', '?')}"

    if etype == "assistant":
        out = []
        for blk in (event.get("message") or {}).get("content") or []:
            btype = blk.get("type")
            if btype == "text":
                text = (blk.get("text") or "").strip()
                if text:
                    out.append(text)
            elif btype == "tool_use":
                name = blk.get("name", "tool")
                inp = blk.get("input") or {}
                hint = (
                    inp.get("file_path")
                    or inp.get("path")
                    or inp.get("command")
                    or inp.get("pattern")
                    or ""
                )
                hint = str(hint).replace("\n", " ")
                out.append(f"● {name}({hint[:70]})")
        return "\n".join(out) or None

    if etype == "result":
        return f"· finished in {event.get('duration_ms', 0) / 1000:.0f}s"

    # thinking_tokens, rate_limit_event, tool results: too noisy for a console.
    return None


class ClaudeCodeTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        progress_cb = (ctx or {}).get("progress_cb")

        try:
            args = json.loads(content) if content.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not args:
            # Bare text is the prompt — small models routinely skip the JSON.
            args = {"prompt": (content or "").strip()}

        prompt = (args.get("prompt") or args.get("task") or "").strip()
        if not prompt:
            return {"error": "prompt is required", "exit_code": 1}

        action = (args.get("action") or "plan").strip().lower()
        if action not in ("plan", "execute"):
            return {"error": "action must be 'plan' or 'execute'", "exit_code": 1}

        resume_id = (args.get("session_id") or "").strip()
        if action == "execute" and not resume_id:
            return {
                "error": (
                    "execute requires the session_id returned by a plan run. "
                    "Run action:'plan' first and let the user approve it."
                ),
                "exit_code": 1,
            }

        cli = shutil.which("claude")
        if not cli:
            return {
                "error": "claude CLI not found on PATH. Install Claude Code on this host.",
                "exit_code": 1,
            }

        # A working directory is mandatory: it is the only thing bounding what
        # the child can read or edit, and an unscoped run would inherit the
        # server's cwd.
        cwd = (args.get("cwd") or "").strip()
        if not cwd:
            return {
                "error": "cwd is required — name the directory Claude Code should work in",
                "exit_code": 1,
            }
        cwd_path = Path(cwd).expanduser().resolve()
        if not cwd_path.is_dir():
            return {"error": f"cwd is not a directory: {cwd_path}", "exit_code": 1}

        # The gate, enforced here rather than in the prompt: executing spends a
        # one-shot approval that only an authenticated request from the user's
        # browser can have granted. Calling execute straight after plan, without
        # the user answering, fails right here.
        if action == "execute":
            ok, why = approvals.consume_approval(resume_id, str(cwd_path))
            if not ok:
                return {
                    "error": f"refusing to execute: {why}",
                    "session_id": resume_id,
                    "exit_code": 1,
                }

        cmd = [cli, "-p", "--output-format", "stream-json", "--verbose"]

        if action == "plan":
            session_id = resume_id or str(uuid.uuid4())
            cmd += [
                "--session-id", session_id,
                "--permission-mode", "plan",
                "--allowedTools", args.get("allowed_tools") or PLAN_TOOLS,
            ]
        else:
            session_id = resume_id
            cmd += [
                "--resume", session_id,
                # Headless has nobody to answer a permission prompt:
                # --permission-prompts only offers "host" or "none", and
                # acceptEdits still prompts here, so the run exits 0 having
                # silently changed nothing. Execution is therefore only
                # reachable through the plan gate above — a human has already
                # read what this will do and said yes — and stays bounded by
                # --allowedTools and the required cwd.
                "--permission-mode", "bypassPermissions",
                "--allowedTools", args.get("allowed_tools") or EXECUTE_TOOLS,
            ]

        if args.get("model"):
            cmd += ["--model", str(args["model"])]

        # Start from a clean environment for the child. Anything inherited from
        # a surrounding Claude Code process (CLAUDECODE, CLAUDE_CODE_*, the
        # messaging socket) makes the CLI think it is a nested child session and
        # it then refuses its own edit tools — the run exits 0 having silently
        # changed nothing. Only relevant when this server was itself launched
        # from a Claude Code session, which is exactly the case while developing.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE_") and k != "CLAUDECODE"}
        # Undo the DATA_DIR HOME so the CLI finds its own credentials.
        env["HOME"] = str(Path.home())

        try:
            timeout = max(30, min(3600, int(args.get("timeout") or DEFAULT_TIMEOUT_S)))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd_path),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Prompt over stdin, never argv: no shell, no escaping, no length cap.
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            pass

        started = time.time()
        tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)
        final_text = ""
        is_error = False
        stderr_buf: list[str] = []

        async def _read_stdout():
            nonlocal final_text, is_error
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    tail.append(raw[:200])
                    continue
                if event.get("type") == "result":
                    final_text = event.get("result") or final_text
                    is_error = bool(event.get("is_error"))
                summary = _summarize(event)
                if summary:
                    for ln in summary.splitlines():
                        tail.append(ln)

        async def _read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_buf.append(line.decode("utf-8", errors="replace").rstrip())

        async def _emit_progress():
            while True:
                await asyncio.sleep(PROGRESS_INTERVAL_S)
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(tail),
                    })
                except Exception:
                    pass

        readers = [asyncio.create_task(_read_stdout()), asyncio.create_task(_read_stderr())]
        prog = asyncio.create_task(_emit_progress()) if progress_cb else None

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        except asyncio.CancelledError:
            try:
                proc.kill()
            except Exception:
                pass
            raise
        finally:
            if prog:
                prog.cancel()
            for r in readers:
                r.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        console = "\n".join(tail)
        if timed_out:
            return {
                "error": f"claude_code timed out after {timeout}s",
                "output": console[-MAX_RESULT_CHARS:],
                "session_id": session_id,
                "exit_code": 124,
            }
        if proc.returncode != 0 and not final_text:
            detail = "\n".join(stderr_buf[-10:]) or console
            return {
                "error": f"claude_code exited {proc.returncode}: {detail[:500]}",
                "session_id": session_id,
                "exit_code": proc.returncode or 1,
            }

        body = final_text or console
        result = {
            "action": action,
            "output": body[:MAX_RESULT_CHARS],
            "console": console[-4000:],
            "session_id": session_id,
            "cwd": str(cwd_path),
            "model": args.get("model") or "default",
            "elapsed_s": round(time.time() - started, 1),
            "exit_code": 1 if is_error else 0,
        }
        if action == "plan":
            try:
                approvals.record_plan(
                    session_id,
                    cwd=str(cwd_path),
                    plan=body,
                    owner=(ctx or {}).get("owner") or "",
                )
            except Exception as e:
                return {
                    "error": f"plan produced but could not be recorded for approval: {e}",
                    "output": body[:MAX_RESULT_CHARS],
                    "exit_code": 1,
                }
            result["nothing_changed"] = True
            result["approval"] = {
                "approve": f"[Approve plan](#claudecode-approve-{session_id})",
                "deny": f"[Deny](#claudecode-deny-{session_id})",
            }
            result["next_step"] = (
                "Show the plan to the user in full, then show these two links on their own line "
                "exactly as given so they can click one:\n"
                f"[Approve plan](#claudecode-approve-{session_id})  ·  [Deny](#claudecode-deny-{session_id})\n"
                "Tell them they can also just reply with changes they want instead of approving. "
                "Then STOP and wait. Calling execute before they click Approve will be refused by "
                "the server, so there is nothing to gain by trying."
            )
        return result
