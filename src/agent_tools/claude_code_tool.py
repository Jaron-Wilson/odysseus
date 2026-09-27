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
from src import claude_code_jobs
from src.constants import DATA_DIR

PROMPT_DIR = os.path.join(DATA_DIR, "claude_code_prompts")
PROGRESS_INTERVAL_S = 1.5
# Enough of the console to follow along. The whole transcript is kept on the
# job (Background tasks) and in the saved console.
PROGRESS_TAIL_LINES = 80
LINE_CHARS = 500
# The model when the agent names none. Left to the CLI, the default is
# whatever the user's own Claude settings say (often the most expensive
# model), and nothing said which one ran. Set ODYSSEUS_CLAUDE_CODE_MODEL to
# change it.
DEFAULT_MODEL = os.environ.get("ODYSSEUS_CLAUDE_CODE_MODEL", "sonnet").strip() or "sonnet"
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


def _summarize_opencode(event: dict) -> Optional[str]:
    """One console line for an OpenCode `run --format json` event.

    Its shape differs from Claude's: a flat {type, sessionID, part} where the
    interesting content hangs off `part` rather than a message envelope.
    """
    etype = event.get("type")
    part = event.get("part") or {}
    if etype == "text":
        return (part.get("text") or "").strip() or None
    if etype == "tool":
        state = part.get("state") or {}
        name = part.get("tool") or "tool"
        inp = state.get("input") or {}
        hint = inp.get("filePath") or inp.get("path") or inp.get("command") or inp.get("pattern") or ""
        return f"● {name}({str(hint).replace(chr(10), ' ')[:70]})"
    if etype == "step_finish":
        return None
    return None


def _clip(text, n: int) -> str:
    text = str(text).replace("\r", "")
    return text if len(text) <= n else text[:n - 1] + "…"


def _tool_hint(name: str, inp: dict) -> str:
    """What a tool call is acting on, in a few words."""
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "url", "query",
                "description", "prompt", "notebook_path"):
        if inp.get(key):
            return _clip(str(inp[key]).replace("\n", " ⏎ "), 240)
    return _clip(json.dumps(inp, ensure_ascii=False), 160) if inp else ""


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _summarize(event: dict) -> Optional[str]:
    """Console lines for a stream-json event, or None to show nothing.

    Verbose on purpose: the reply text in full, each tool call with what it
    acts on, the first lines of each tool result, and a glimpse of thinking,
    so the console reads like the CLI's own screen rather than a spinner.
    """
    etype = event.get("type")

    if etype == "system" and event.get("subtype") == "init":
        model = event.get("model")
        return (f"· session {str(event.get('session_id'))[:8]} in {event.get('cwd', '?')}"
                + (f" · model {model}" if model else ""))

    if etype == "assistant":
        out = []
        for blk in (event.get("message") or {}).get("content") or []:
            btype = blk.get("type")
            if btype == "text":
                text = (blk.get("text") or "").strip()
                if text:
                    out.append(text)
            elif btype == "thinking":
                thought = " ".join((blk.get("thinking") or "").split())
                if thought:
                    out.append(f"✻ {_clip(thought, 300)}")
            elif btype == "tool_use":
                name = blk.get("name", "tool")
                out.append(f"● {name}({_tool_hint(name, blk.get('input') or {})})")
        return "\n".join(out) or None

    if etype == "user":
        out = []
        for blk in (event.get("message") or {}).get("content") or []:
            if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                continue
            lines = [ln for ln in _result_text(blk.get("content")).splitlines() if ln.strip()]
            if not lines:
                out.append("  ⎿ (no output)")
                continue
            mark = "  ⎿ error: " if blk.get("is_error") else "  ⎿ "
            out.append(mark + _clip(lines[0], 200))
            out.extend("    " + _clip(ln, 200) for ln in lines[1:4])
            if len(lines) > 4:
                out.append(f"    … {len(lines) - 4} more lines")
        return "\n".join(out) or None

    if etype == "result":
        bits = [f"· finished in {event.get('duration_ms', 0) / 1000:.0f}s"]
        if event.get("num_turns"):
            bits.append(f"{event['num_turns']} turns")
        if event.get("total_cost_usd") is not None:
            bits.append(f"${event['total_cost_usd']:.2f}")
        return " · ".join(bits)

    # thinking_tokens and rate_limit_event are counted or ignored.
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

        engine = (args.get("engine") or "claude").strip().lower()
        if engine not in ("claude", "opencode"):
            return {"error": "engine must be 'claude' or 'opencode'", "exit_code": 1}

        cli = shutil.which("opencode" if engine == "opencode" else "claude")
        if not cli:
            return {
                "error": (f"{engine} CLI not found on PATH. Install it on this host."),
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

        # OpenCode ships the same split as a pair of agents: `plan` is read-only
        # (verified — it refuses to edit and says so) and `build` writes, so the
        # approval gate above applies to it unchanged. Its prompt rides as a
        # positional argument; exec involves no shell, so nothing needs quoting.
        prompt_via_stdin = True
        if engine == "opencode":
            # OpenCode mints its own ses_… id, which the event stream reports.
            session_id = resume_id or ""
            cmd = [cli, "run", "--format", "json", "--dir", str(cwd_path),
                   "--agent", "build" if action == "execute" else "plan"]
            if args.get("model"):
                cmd += ["--model", str(args["model"])]
            if resume_id:
                cmd += ["--session", resume_id]
            cmd.append(prompt)
            prompt_via_stdin = False
        else:
            cmd = [cli, "-p", "--output-format", "stream-json", "--verbose"]

        if engine == "opencode":
            pass
        elif action == "ask":
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

        # The model is always chosen here and always shown, never left to the
        # CLI's own default: an unnamed run used to land on the most
        # expensive model with nothing saying so. An execute carries on with
        # the model its plan was written with, unless told otherwise.
        model = str(args.get("model") or "").strip()
        if engine != "opencode":
            if not model and action == "execute":
                model = str((approvals.get(resume_id) or {}).get("model") or "")
            model = model or DEFAULT_MODEL
            cmd += ["--model", model]
        model_label = model or "opencode default"

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
        # Claude takes the prompt over stdin, never argv: no shell, no escaping,
        # no length cap. OpenCode takes it positionally, so just close stdin.
        try:
            if prompt_via_stdin:
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

        # Registered so the run can be sent to the background from its tool
        # card and watched in Background tasks (src/claude_code_jobs.py).
        job = claude_code_jobs.register(
            chat_session_id=(ctx or {}).get("session_id") or "",
            owner=(ctx or {}).get("owner") or "",
            action=action, cwd=str(cwd_path), model=model_label, engine=engine,
            prompt=prompt)
        job.proc = proc
        job.pid = proc.pid
        job.cli_session_id = session_id or ""

        # What was actually spawned, stated up front. Without this the user sees
        # an opaque spinner and has to go hunting in `ps` to find out whether a
        # process exists at all, what it may touch, or how to kill it.
        if engine == "opencode":
            _grant = f"--agent {'build' if action == 'execute' else 'plan'}"
        else:
            _grant = (
                f"--permission-mode {'plan' if action in ('plan', 'ask') else 'bypassPermissions'}"
                f" --allowedTools {args.get('allowed_tools') or (PLAN_TOOLS if action == 'plan' else ASK_TOOLS if action == 'ask' else EXECUTE_TOOLS)}"
            )
        banner = (
            f"$ {engine} {_grant}\n"
            f"  pid {proc.pid} · session {(session_id or 'pending')[:12]} · cwd {cwd_path}\n"
            f"  model {model_label} · kill with: kill {proc.pid}"
        )
        job.banner = banner

        def _add(ln: str) -> None:
            ln = _clip(ln, LINE_CHARS)
            tail.append(ln)
            job.lines.append(ln)

        def _tail_text() -> str:
            head = banner
            if thinking_tokens:
                head += f"\n  thinking… {thinking_tokens} tokens"
            body = "\n".join(tail)
            return head + ("\n" + body if body else "")

        def _full_console() -> str:
            head = banner + (f"\n  thinking… {thinking_tokens} tokens" if thinking_tokens else "")
            body = "\n".join(job.lines)
            return head + ("\n" + body if body else "")

        def _progress() -> dict:
            return {"elapsed_s": round(time.time() - started, 1), "tail": _tail_text(),
                    "job_id": job.id, "can_background": not job.detached,
                    "model": model_label}

        if progress_cb:
            # Emit once immediately; the periodic loop only starts after a delay
            # and a long thinking phase would otherwise show nothing at all.
            try:
                await progress_cb(_progress())
            except Exception:
                pass

        async def _read_stdout():
            nonlocal final_text, is_error, thinking_tokens, session_id
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
                    _add(_strip_ansi(raw))
                    continue
                if engine == "opencode":
                    # OpenCode allocates the session itself, so the id is only
                    # knowable from the stream — and it is what a later
                    # --session resume, and the approval record, both key on.
                    if not session_id and event.get("sessionID"):
                        session_id = event["sessionID"]
                        job.cli_session_id = session_id
                    summary = _summarize_opencode(event)
                    if summary:
                        for ln in summary.splitlines():
                            _add(ln)
                        if event.get("type") == "text":
                            final_text = (final_text + "\n" + summary).strip()
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
                        _add(ln)

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
                    await progress_cb(_progress())
                except Exception:
                    pass

        readers = [asyncio.create_task(_read_stdout()), asyncio.create_task(_read_stderr())]
        prog = asyncio.create_task(_emit_progress()) if progress_cb else None

        async def _finalize(timed_out: bool) -> Dict:
            """The tool result, once the CLI has exited (or been killed)."""
            # Keep the banner in the saved console so the pid, cwd and tool grant
            # are still on the record after the run ends, not just while it streams.
            console = _full_console()
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
                detail = "\n".join(stderr_buf[-10:]) or console[-2000:]
                hint = ""
                if args.get("model"):
                    # The common cause by far: an invented id like claude-opus-4.
                    # The CLI's own message does not always make that obvious.
                    hint = (
                        f" — note model was set to {args['model']!r}; valid values are "
                        "'opus', 'sonnet', 'haiku' or a full id such as 'claude-opus-5'. "
                        "Retry without `model` to use the default."
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
                "console": console[-12000:],
                "session_id": session_id,
                "cwd": str(cwd_path),
                "model": model_label,
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
                        model=model,
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
                    "approve": f"[Approve plan · runs on {model_label}](#claudecode-approve-{session_id})",
                    "deny": f"[Deny](#claudecode-deny-{session_id})",
                }
                result["next_step"] = (
                    "Show the plan to the user in full. If the result has a `pdf` field, show that "
                    "link too so they can read it as a paginated document. Then show these two links "
                    "on their own line exactly as given so they can click one:\n"
                    f"[Approve plan · runs on {model_label}](#claudecode-approve-{session_id})  ·  [Deny](#claudecode-deny-{session_id})\n"
                    f"Say plainly that approving runs Claude Code on the model `{model_label}`. "
                "Tell them they can also just reply with changes they want instead of approving, "
                "including a different model (for example 'haiku' for small changes). "
                    "Then STOP and wait. Calling execute before they click Approve will be refused by "
                    "the server, so there is nothing to gain by trying."
                )
            return result

        def _job_status(result: Dict) -> str:
            code = result.get("exit_code")
            return "done" if code == 0 else "timed_out" if code == 124 else "failed"

        async def _drain_readers() -> None:
            # The CLI has exited, so the pipes are at EOF: let the readers take
            # the last lines rather than cancelling them mid-buffer.
            try:
                await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=5)
            except asyncio.TimeoutError:
                for r in readers:
                    r.cancel()
                await asyncio.gather(*readers, return_exceptions=True)

        wait_task = asyncio.create_task(proc.wait())
        detach_task = asyncio.create_task(job.detach_event.wait())
        try:
            done, _ = await asyncio.wait({wait_task, detach_task}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # The chat's Stop, while the run is still in the foreground.
            detach_task.cancel()
            if prog:
                prog.cancel()
            try:
                proc.kill()
            except Exception:
                pass
            for r in readers:
                r.cancel()
            claude_code_jobs.finish(job, "stopped")
            raise

        if detach_task in done and wait_task not in done:
            # Sent to the background: the chat's turn ends now, the CLI keeps
            # going, and its result is posted into the chat when it finishes.
            if prog:
                prog.cancel()
            remaining = max(30.0, timeout - (time.time() - started))
            try:
                from src import chat_queue
                job.notify = chat_queue.get(job.chat_session_id).get("notify")
            except Exception:
                job.notify = None

            async def _carry_on():
                timed_out = False
                try:
                    await asyncio.wait_for(wait_task, timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except Exception:
                        pass
                await _drain_readers()
                try:
                    result = await _finalize(timed_out)
                except Exception as e:
                    result = {"error": f"could not finish the background run: {e}", "exit_code": 1}
                claude_code_jobs.finish(job, _job_status(result), result)
                await _post_background_result(job, result)

            job.task = asyncio.create_task(_carry_on())
            return {
                "action": action,
                "background": True,
                "job_id": job.id,
                "session_id": session_id,
                "cwd": str(cwd_path),
                "model": model_label,
                "console": _tail_text()[-4000:],
                "output": (
                    f"Moved to the background as job `{job.id}` (Claude Code, {model_label}, "
                    f"in {cwd_path}). It keeps running; its result will be posted in this chat "
                    "when it finishes, and it can be watched or stopped under Background tasks.\n"
                    "Do NOT wait or poll for it. Tell the user it is running in the background, "
                    "then carry on."
                ),
                "exit_code": 0,
            }

        detach_task.cancel()
        timed_out = wait_task not in done
        if timed_out:
            wait_task.cancel()
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        if prog:
            prog.cancel()
        await _drain_readers()
        result = await _finalize(timed_out)
        claude_code_jobs.finish(job, _job_status(result), result)
        return result


async def _post_background_result(job, result: Dict) -> None:
    """Put a backgrounded run's result into the chat it came from, and send
    the chat's done notification if one was asked for."""
    sid = job.chat_session_id
    if not sid:
        return
    ok = result.get("exit_code") == 0
    head = (f"**Background Claude Code job `{job.id}` "
            f"{'finished' if ok else 'stopped' if job.status == 'stopped' else 'failed'}** "
            f"({job.action}, {job.model}, {round((job.finished or time.time()) - job.started)}s, "
            f"in `{job.cwd}`)")
    body = result.get("output") or result.get("error") or ""
    parts = [head, "", body.strip()]
    if result.get("pdf"):
        parts += ["", result["pdf"]]
    approval = result.get("approval") or {}
    if approval:
        parts += ["", f"{approval.get('approve')}  ·  {approval.get('deny')}"]
    content = "\n".join(parts).strip()
    try:
        from src.ai_interaction import get_session_manager
        from core.models import ChatMessage
        sm = get_session_manager()
        if sm:
            sm.add_message(sid, ChatMessage("assistant", content, metadata={
                "source": "claude_code_background",
                "model": job.model,
                "tool_events": [{
                    "round": 1, "tool": "claude_code",
                    "command": f"background job {job.id}: {job.prompt}",
                    "output": (result.get("console") or "")[-12000:],
                    "exit_code": result.get("exit_code"),
                }],
                "round_texts": ["", content],
            }))
            sm.save_sessions()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Could not post background job %s to %s", job.id, sid)
    if job.notify is not None:
        try:
            from src import chat_queue
            await chat_queue.send_done_notification(sid, job.notify, failed=not ok)
        except Exception:
            pass
