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
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from src import claude_code_approvals as approvals
from src.constants import DATA_DIR

PROMPT_DIR = os.path.join(DATA_DIR, "claude_code_prompts")
PROGRESS_INTERVAL_S = 1.5
PROGRESS_TAIL_LINES = 16
# 900s killed a real rebrand at ~15 minutes, after it had already written every
# file — the work survived but the run was recorded as a timeout and the
# approval was spent. Refactors across a large codebase genuinely take this long.
DEFAULT_TIMEOUT_S = 2400
MAX_RESULT_CHARS = 20000

# Planning and asking read; neither may write even if the CLI is talked into
# trying. Execution gets the tools needed to land the approved change. Task is
# included so the agent can fan work out to its own subagents, which is most of
# the value on a large codebase.
PLAN_TOOLS = "Read,Glob,Grep,Task"
ASK_TOOLS = "Read,Glob,Grep,Task"
EXECUTE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash,TodoWrite,Task"


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def _strip_ansi(s: str) -> str:
    """Drop terminal escapes. `claude --bg` and `claude logs` both colour their
    output, and the raw codes make the id unparseable and the logs unreadable."""
    return _ANSI_RE.sub("", s or "")


def _die_with_parent() -> None:
    """Ask the kernel to SIGKILL this child if the server process goes away.

    Linux-only (PR_SET_PDEATHSIG = 1); a no-op anywhere else, which just
    restores the previous orphaning behaviour rather than breaking the call.
    """
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 9, 0, 0, 0)
    except Exception:
        pass


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
    async def _launch_background(self, cmd, prompt, cwd_path, cli_session_id,
                                 action, chat_session_id) -> Dict:
        """Dispatch via the CLI's own `--bg` and return the short id.

        Deliberately not bg_jobs: a `--bg` session is a first-class background
        session, so it shows up in `claude agents` and the user can
        `claude attach <id>` into it, read `claude logs <id>` or `claude stop
        <id>`. A `-p` run detached by other means registers as an interactive
        session, which attach refuses — the thing the user actually hit.
        """
        # --bg owns the backgrounding, and stream-json has no reader here.
        drop = {"-p", "--output-format", "stream-json", "--verbose"}
        argv = [c for c in cmd if c not in drop]
        argv.insert(1, "--bg")

        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE_") and k != "CLAUDECODE"}
        env["HOME"] = str(Path.home())

        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd_path), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=120)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {"error": "claude --bg did not return an id within 120s", "exit_code": 124}

        text = _strip_ansi((out or b"").decode("utf-8", "replace")).strip()
        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace")[:300] or text[:300]
            return {"error": f"claude --bg exited {proc.returncode}: {detail}", "exit_code": 1}

        # Anchor on the id the CLI tells the user to attach to. Scanning for a
        # bare token instead picks up a word out of the help text it prints
        # underneath — the first attempt came back with the id "this".
        m = (re.search(r"claude attach\s+([0-9a-f]{6,})", text)
             or re.search(r"backgrounded\s*·?\s*([0-9a-f]{6,})", text)
             or re.search(r"\b([0-9a-f]{8})\b", text))
        short_id = m.group(1) if m else ""

        return {
            "action": action,
            "background": True,
            "job_id": short_id,
            "session_id": cli_session_id,
            "cwd": str(cwd_path),
            "launch_output": text[:500],
            "output": (
                f"Claude Code is running in the background as `{short_id}` in {cwd_path}.\n"
                f"The user can watch or take it over with `claude attach {short_id}`, "
                f"read output with `claude logs {short_id}`, or halt it with "
                f"`claude stop {short_id}`. It also appears in `claude agents`.\n"
                "Do NOT wait or poll — carry on, and use action:'status' if the user asks "
                "how it is going."
            ),
            "exit_code": 0,
        }

    async def _job_status(self, job_id: Optional[str]) -> Dict:
        """Report on background sessions, so the user can just ask how it is going.

        Reads the CLI's own view rather than a local job table, so the ids here
        are the same ones `claude attach` / `logs` / `stop` accept.
        """
        listing = await self._list_agents()
        if listing.get("exit_code") != 0:
            return listing
        sessions = [s for s in listing.get("sessions") or []
                    if s.get("kind") == "background"]
        if job_id:
            sessions = [s for s in sessions
                        if str(s.get("id", "")).startswith(job_id)
                        or str(s.get("sessionId", "")).startswith(job_id)]
            if not sessions:
                return {"error": f"no background session matching {job_id}", "exit_code": 1}
        if not sessions:
            return {"output": "No background Claude Code sessions are running.", "exit_code": 0}

        cli = shutil.which("claude")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE_") and k != "CLAUDECODE"}
        env["HOME"] = str(Path.home())

        lines = []
        for s in sessions:
            sid = s.get("id") or ""
            state = s.get("state") or s.get("status") or "?"
            lines.append(f"- `{sid}` {state} — {s.get('name') or '?'} ({s.get('cwd')})")
            if cli and sid:
                try:
                    p = await asyncio.create_subprocess_exec(
                        cli, "logs", sid, env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL)
                    o, _ = await asyncio.wait_for(p.communicate(), timeout=30)
                    clean = _strip_ansi((o or b"").decode("utf-8", "replace"))
                    # Cursor-positioning leftovers and spinner frames survive
                    # escape-stripping as junk lines; keep only real text.
                    # `claude logs` replays a TUI buffer, so most lines are
                    # spinner frames and cursor droppings. Keep lines that look
                    # like prose rather than animation.
                    tail = [
                        t.strip() for t in clean.splitlines()
                        if len(t.strip()) > 8
                        and not t.strip().startswith("[")
                        and sum(c.isalnum() or c.isspace() for c in t) > len(t) * 0.6
                    ][-4:]
                    for t in tail:
                        lines.append(f"      {t[:140]}")
                except Exception:
                    pass
        lines.append("Attach with `claude attach <id>`, stop with `claude stop <id>`.")
        return {"output": "\n".join(lines), "sessions": sessions, "exit_code": 0}

    async def _list_agents(self) -> Dict:
        """Report the Claude Code sessions running on this host."""
        cli = shutil.which("claude")
        if not cli:
            return {"error": "claude CLI not found on PATH.", "exit_code": 1}
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE_") and k != "CLAUDECODE"}
        env["HOME"] = str(Path.home())
        proc = await asyncio.create_subprocess_exec(
            cli, "agents", "--json",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {"error": "listing agents timed out", "exit_code": 124}
        if proc.returncode != 0:
            return {
                "error": f"claude agents --json exited {proc.returncode}: "
                         f"{err.decode('utf-8', 'replace')[:300]}",
                "exit_code": proc.returncode or 1,
            }
        try:
            sessions = json.loads(out.decode("utf-8", "replace") or "[]")
        except json.JSONDecodeError as e:
            return {"error": f"could not parse agent list: {e}", "exit_code": 1}

        lines = [
            f"- {s.get('name') or s.get('id')} — {s.get('state', '?')}"
            f" ({s.get('kind', '?')}, {s.get('cwd', '?')})"
            for s in sessions
        ]
        return {
            "output": "\n".join(lines) or "No Claude Code sessions running.",
            "sessions": sessions,
            "count": len(sessions),
            "exit_code": 0,
        }

    async def execute(self, content: str, ctx: dict) -> Dict:
        progress_cb = (ctx or {}).get("progress_cb")

        try:
            args = json.loads(content) if content.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not args:
            # Bare text is the prompt — small models routinely skip the JSON.
            args = {"prompt": (content or "").strip()}

        action = (args.get("action") or "plan").strip().lower()
        if action not in ("plan", "execute", "ask", "list", "status"):
            return {"error": "action must be 'plan', 'execute', 'ask', 'list' or 'status'",
                    "exit_code": 1}

        # Reads: no prompt, no directory, nothing spawned.
        if action == "list":
            return await self._list_agents()
        if action == "status":
            return await self._job_status(args.get("job_id"))

        prompt = (args.get("prompt") or args.get("task") or "").strip()
        if not prompt:
            return {"error": "prompt is required", "exit_code": 1}

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

        if action == "ask":
            # Conversation, not change: resume (or open) a session with
            # read-only tools. Needs no approval precisely because it cannot
            # write, which is what makes free back-and-forth reasonable.
            session_id = resume_id or str(uuid.uuid4())
            cmd += (["--resume", session_id] if resume_id else ["--session-id", session_id])
            cmd += [
                "--permission-mode", "plan",
                "--allowedTools", args.get("allowed_tools") or ASK_TOOLS,
            ]
        elif action == "plan":
            session_id = resume_id or str(uuid.uuid4())
            cmd += (["--resume", session_id] if resume_id else ["--session-id", session_id])
            cmd += [
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

        # Detached mode: hand the run to bg_jobs and return now, so a refactor
        # that takes ten minutes does not hold the chat open. The monitor
        # re-invokes the agent with the output once it finishes.
        if args.get("background"):
            return await self._launch_background(
                cmd, prompt, cwd_path, session_id, action,
                (ctx or {}).get("session_id"))

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
            # Die with the server. Without this a restart reparents the CLI to
            # init, where it keeps burning CPU and tokens on a run nothing is
            # reading any more, and the user has no way to stop it.
            preexec_fn=_die_with_parent,
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
        thinking_tokens = 0

        # What was actually spawned, stated up front. Without this the user sees
        # an opaque spinner and has to go hunting in `ps` to find out whether a
        # process exists at all, what it may touch, or how to kill it.
        banner = (
            f"$ claude -p --permission-mode {'plan' if action in ('plan', 'ask') else 'bypassPermissions'}"
            f" --allowedTools {args.get('allowed_tools') or (PLAN_TOOLS if action == 'plan' else ASK_TOOLS if action == 'ask' else EXECUTE_TOOLS)}\n"
            f"  pid {proc.pid} · session {session_id[:8]} · cwd {cwd_path}\n"
            f"  model {args.get('model') or 'default'} · kill with: kill {proc.pid}"
        )

        def _tail_text() -> str:
            head = banner
            if thinking_tokens:
                head += f"\n  thinking… {thinking_tokens} tokens"
            body = "\n".join(tail)
            return head + ("\n" + body if body else "")

        if progress_cb:
            # Emit once immediately; the periodic loop only starts after a delay
            # and a long thinking phase would otherwise show nothing at all.
            try:
                await progress_cb({"elapsed_s": 0.0, "tail": _tail_text()})
            except Exception:
                pass

        async def _read_stdout():
            nonlocal final_text, is_error, thinking_tokens
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
                elif event.get("subtype") == "thinking_tokens":
                    # Counted rather than printed: it is the only signal during
                    # a long silent reasoning phase, but one line per tick would
                    # flood the console.
                    thinking_tokens = event.get("estimated_tokens") or thinking_tokens
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
                        "tail": _tail_text(),
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

        # Keep the banner in the saved console so the pid, cwd and tool grant
        # are still on the record after the run ends, not just while it streams.
        console = _tail_text()
        if timed_out:
            restored = action == "execute" and approvals.restore_approval(session_id)
            return {
                "error": (
                    f"claude_code timed out after {timeout}s"
                    + (" — the approval has been restored, so this can be retried "
                       "with a longer `timeout` without planning again." if restored else "")
                ),
                "output": console[-MAX_RESULT_CHARS:],
                "session_id": session_id,
                "approval_restored": restored,
                "exit_code": 124,
            }
        if proc.returncode != 0 and not final_text:
            if action == "execute":
                approvals.restore_approval(session_id)
            detail = "\n".join(stderr_buf[-10:]) or console
            hint = ""
            if args.get("model"):
                # The common cause by far: an invented id like claude-opus-4.
                # The CLI's own message does not always make that obvious.
                hint = (
                    f" — note model was set to {args['model']!r}; valid values are "
                    "'opus', 'sonnet', 'haiku' or a full id such as 'claude-opus-5'. "
                    "Retry without `model` to use the CLI default."
                )
            return {
                "error": f"claude_code exited {proc.returncode}: {detail[:400]}{hint}",
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
            # A plan is the thing the user actually reads before saying yes, so
            # give them a paginated copy in the house style. Cosmetic: a render
            # failure still leaves the plan text in the reply.
            try:
                from src.doc_pdf import render_markdown_pdf
                pdf_path, pdf_err = await render_markdown_pdf(
                    body,
                    f"plan-{session_id}",
                    running_title=f"Plan · {cwd_path.name} · jaronwilson.dev",
                )
            except Exception as e:
                pdf_path, pdf_err = None, str(e)
            if pdf_path:
                result["pdf"] = f"[Download plan PDF](/api/claude_code/plan/{session_id}/pdf)"
                result["pdf_path"] = pdf_path
            elif pdf_err:
                result["pdf_error"] = pdf_err

            result["nothing_changed"] = True
            result["approval"] = {
                "approve": f"[Approve plan](#claudecode-approve-{session_id})",
                "deny": f"[Deny](#claudecode-deny-{session_id})",
            }
            result["next_step"] = (
                "Show the plan to the user in full. If the result has a `pdf` field, show that "
                "link too so they can read it as a paginated document. Then show these two links "
                "on their own line exactly as given so they can click one:\n"
                f"[Approve plan](#claudecode-approve-{session_id})  ·  [Deny](#claudecode-deny-{session_id})\n"
                "Tell them they can also just reply with changes they want instead of approving. "
                "Then STOP and wait. Calling execute before they click Approve will be refused by "
                "the server, so there is nothing to gain by trying."
            )
        return result
