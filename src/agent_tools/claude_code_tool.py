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
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from src import claude_code_approvals as approvals
from src import claude_code_jobs
from src import claude_code_agents
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

PROMPT_DIR = os.path.join(DATA_DIR, "claude_code_prompts")
# Scratch folders for runs whose project lives on another machine.
WORKSPACES_DIR = os.path.join(DATA_DIR, "workspaces")
PROGRESS_INTERVAL_S = 1.5
STREAM_LINE_LIMIT = 64 * 1024 * 1024
# Enough of the console to follow along. The whole transcript is kept on the
# job (Background tasks) and in the saved console.
PROGRESS_TAIL_LINES = 80
LINE_CHARS = 500
# The model when the agent names none. Left to the CLI, the default is
# whatever the user's own Claude settings say (often the most expensive
# model), and nothing said which one ran. Set ODYSSEUS_CLAUDE_CODE_MODEL to
# change it.
DEFAULT_MODEL = os.environ.get("ODYSSEUS_CLAUDE_CODE_MODEL", "sonnet").strip() or "sonnet"
# Every coding-agent engine, and the CLI each one runs. Antigravity is
# Google's (`agy`), signed in with the user's Google account; asked for
# 2026-09-30: "I have a google subscription that has alot of credits and
# models".
ENGINES = ("opencode", "claude", "antigravity")
ENGINE_BINARIES = {"opencode": "opencode", "claude": "claude", "antigravity": "agy"}
ENGINE_LABELS = {"opencode": "OpenCode", "claude": "Claude Code", "antigravity": "Antigravity"}
# The engine when the agent names none: OpenCode on this host's local models.
# Seen live: a request to draft video cuts went to Claude Code (the user's
# Claude plan) when the local 27B via OpenCode would have done. Claude is used
# when the user asks for it by name. ODYSSEUS_CODE_ENGINE=claude flips this.
DEFAULT_ENGINE = (os.environ.get("ODYSSEUS_CODE_ENGINE", "opencode").strip().lower() or "opencode")
if DEFAULT_ENGINE not in ENGINES:
    DEFAULT_ENGINE = "opencode"
OPENCODE_DEFAULT_LABEL = "local default (vllm3090/qwen3.8-27b)"
ANTIGRAVITY_DEFAULT_LABEL = "Antigravity default"


def engine_cli(engine: str) -> Optional[str]:
    """The CLI for an engine, or None when it is not installed. `agy`'s
    installer puts it in ~/.local/bin, which a service's PATH often lacks."""
    name = ENGINE_BINARIES.get(engine or "", "")
    if not name:
        return None
    found = shutil.which(name)
    if not found and engine == "antigravity":
        local = Path.home() / ".local" / "bin" / name
        if local.is_file() and os.access(local, os.X_OK):
            found = str(local)
    return found


_AGY_MODELS: Dict[str, object] = {"at": 0.0, "ids": []}
_MODEL_SLUG = re.compile(r"^[a-z0-9][a-z0-9.\-_/:]*[a-z0-9]$")


def antigravity_model_ids() -> list:
    """The model slugs `agy models` lists (cached for 10 minutes). Empty when
    agy is missing or signed out; then nothing is refused."""
    if time.time() - float(_AGY_MODELS["at"]) < 600:
        return list(_AGY_MODELS["ids"])
    ids: list = []
    cli = engine_cli("antigravity")
    if cli:
        try:
            out = subprocess.run([cli, "models"], capture_output=True, text=True, timeout=8,
                                 env=_cli_env(), stdin=subprocess.DEVNULL).stdout
            for line in out.splitlines():
                word = _strip_ansi(line).strip().lstrip("*-• ").split()
                if word and _MODEL_SLUG.match(word[0].lower()) and any(c.isdigit() for c in word[0]):
                    ids.append(word[0])
        except Exception as e:
            logger.debug("agy models failed: %s", e)
    _AGY_MODELS.update(at=time.time(), ids=ids)
    return ids


def opencode_model_ids() -> list:
    """The models an OpenCode run can use, as provider/model: its own config's
    and the Odysseus servers it is handed (src/opencode_providers.py). Empty
    when neither can be read (then nothing is refused)."""
    from src import opencode_providers
    return [mid for mid, _ in opencode_providers.models()[0]]


def _cli_env() -> dict:
    """The environment a coding-agent CLI runs in (see run())."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_") and k != "CLAUDECODE"}
    # Undo the DATA_DIR HOME so the CLI finds its own credentials.
    env["HOME"] = str(Path.home())
    # Odysseus's own model servers, for OpenCode (Claude ignores it).
    try:
        from src import opencode_providers
        extra = opencode_providers.config_content()
        if extra:
            env["OPENCODE_CONFIG_CONTENT"] = extra
    except Exception as e:
        logger.debug("OpenCode providers skipped: %s", e)
    return env
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


def engine_label(engine: str) -> str:
    """What the user sees for a run: the engine that actually ran it. The
    tool is named claude_code for history's sake, but most runs are OpenCode,
    and a card saying "Claude" for those misleads."""
    return ENGINE_LABELS.get(engine or "", "OpenCode")


def _summarize_antigravity(event: dict) -> Optional[str]:
    """One console line for an `agy --output-format stream-json` event:
    {"event": "init" | "step_update" | "result", "<event>": {...}}. A step
    streams its text as text_delta; the line is written once, when the step
    is DONE, so a streamed answer is not repeated line by line."""
    if event.get("event") != "step_update":
        return None
    step = event.get("step_update") or {}
    if str(step.get("state") or "").upper() != "DONE":
        return None
    kind = str(step.get("step_type") or "")
    if kind == "agent_response":
        return None                                  # the text itself: _Stream collects it
    hint = ""
    for key in ("command", "path", "file_path", "target", "query", "url", "title"):
        if step.get(key):
            hint = str(step[key]); break
    return f"● {kind or 'step'}({hint.replace(chr(10), ' ')[:70]})"


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

        env = _cli_env()

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

    async def _job_status(self, job_id: Optional[str], chat_id: str = "") -> Dict:
        """Report on background sessions, so the user can just ask how it is going.

        Odysseus's own runs first (the job ids its cards, Background tasks and
        "Bring back" use): asked about job fd81af50, this said "no background
        session matching fd81af50", because it only read the Claude CLI's
        sessions (2026-09-29). Then the CLI's own view, whose ids are the ones
        `claude attach` / `logs` / `stop` accept.
        """
        own = self._own_job_status(job_id, chat_id)
        if own:
            return own
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
        env = _cli_env()

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

    def _chat_agents(self, chat_id: str) -> Dict:
        """The Claude Code agents chats have used, so a chat can carry one on
        with from_chat. The calling chat's own agents are marked."""
        agents = claude_code_agents.list_all()
        if not agents:
            return {"output": "No chat has a Claude Code agent yet.", "agents": [], "exit_code": 0}
        busy = {j.cli_session_id for j in claude_code_jobs.list_jobs() if j.status == "running"}
        lines = []
        for a in agents[:25]:
            ago = int(time.time() - a.get("last_used", 0))
            ago_s = f"{ago // 60}m ago" if ago < 3600 else f"{ago // 3600}h ago" if ago < 86400 else f"{ago // 86400}d ago"
            mine = " (this chat)" if a["chat_id"] == chat_id else ""
            state = " · busy" if a["session_id"] in busy else ""
            lines.append(
                f"- chat \"{a.get('chat_name') or '?'}\" `{a['chat_id'][:8]}`{mine}{state}: "
                f"{a.get('engine')} {a.get('model')} in {a.get('cwd')}, {ago_s}. "
                f"Last: {a.get('last_prompt') or '?'}")
        lines.append("Carry one on with {\"action\": \"ask\" or \"plan\", \"from_chat\": \"<chat id>\", ...}.")
        return {"output": "\n".join(lines), "agents": agents[:25], "exit_code": 0}

    async def _list_agents(self) -> Dict:
        """Report the Claude Code sessions running on this host."""
        cli = shutil.which("claude")
        if not cli:
            return {"error": "claude CLI not found on PATH.", "exit_code": 1}
        env = _cli_env()
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

    @staticmethod
    def _own_job_status(job_id: Optional[str], chat_id: str) -> Optional[Dict]:
        if job_id:
            job = claude_code_jobs.get(job_id)
            jobs = [job] if job else []
        else:
            jobs = [j for j in claude_code_jobs.list_jobs("")
                    if j.status == "running" and (not chat_id or j.chat_session_id == chat_id)]
        if not jobs:
            return None
        lines = []
        for j in jobs:
            took = round(((j.finished or time.time()) - j.started) / 60, 1)
            st = claude_code_jobs.shown_status(j.status, j.agent_status) or {}
            on = f" on {j.model}" if j.model else ""
            detail = f" \u00b7 {st['detail']}" if st.get("detail") else ""
            lines.append(f"- `{j.id}` {j.status} \u2014 {engine_label(j.engine)} {j.action}{on}, {took} min{detail}")
            tail = [l for l in list(j.lines)[-6:] if l.strip()]
            if tail:
                lines.append("  last output: " + " | ".join(t.strip()[:160] for t in tail))
        running = [j for j in jobs if j.status == "running"]
        hint = (" To follow one here, call claude_code with action 'attach' and its job_id. "
                "Do not start it again." if running else "")
        return {"output": "\n".join(lines) + ("\n" + hint.strip() if hint else ""), "exit_code": 0,
                "jobs": [j.public() for j in jobs]}

    async def _attach(self, job_id: str, ctx: dict) -> Dict:
        """Follow a background run in this chat turn again ("bring it back"):
        its live output streams into the card here and its result is returned
        to the chat, which carries on from it. Asked for: "after pushing a
        task to background let me pull it back into the chat"."""
        job = claude_code_jobs.get(job_id)
        if not job:
            return {"error": f"no run {job_id!r} (it may have finished long ago)", "exit_code": 1}
        progress_cb = ctx.get("progress_cb")
        if job.status == "running":
            job.attached = True
            try:
                while job.status == "running" and job.attached:
                    if progress_cb:
                        body = "\n".join(list(job.lines)[-PROGRESS_TAIL_LINES:])
                        try:
                            await progress_cb({
                                "elapsed_s": round(time.time() - job.started, 1),
                                "tail": job.banner + ("\n" + body if body else ""),
                                "job_id": job.id, "can_background": True, "model": job.model,
                                "engine_label": engine_label(job.engine),
                                "agent_status": job.agent_status})
                        except Exception:
                            pass
                    await asyncio.sleep(PROGRESS_INTERVAL_S)
            except asyncio.CancelledError:
                # The chat's Stop stops a run brought back into the chat, as it
                # does one that never left: "the inchat does not do it"
                # (2026-09-29), only Background tasks' Stop did. A restart
                # leaves it running, to be picked up again.
                job.attached = False
                if not _restarting():
                    claude_code_jobs.kill_run(job)
                    claude_code_jobs.finish(job, "stopped")
                raise
            if job.status == "running":    # sent to the background again
                return {"background": True, "job_id": job.id, "exit_code": 0,
                        "output": (f"Sent back to the background as job `{job.id}`. Its result will be "
                                   "posted in this chat when it finishes. Do NOT wait for it.")}
        result = dict(job.result or {"output": "The run finished without a result.", "exit_code": 1})
        result["job_id"] = job.id
        result["brought_back"] = True
        result.setdefault("engine_label", engine_label(job.engine))
        # Keep the run's own next step: for a plan it says to show the Approve
        # and Deny links. Replacing it lost them - seen live, a brought-back
        # plan showed as text with nothing to approve it by.
        own = result.get("next_step") or ("Report what it did, then carry on with the "
                                          "conversation where the user left off.")
        result["next_step"] = "This run was brought back from the background. " + own
        return result

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
        # Switched off for this chat by the user (src/chat_prefs.py). Checked
        # here too, not only by leaving the tool out of the list, because
        # queued sends and approval resumes reach tools by other paths.
        try:
            from src import chat_prefs
            if not chat_prefs.claude_code_allowed((ctx or {}).get("session_id") or ""):
                return {"error": ("Coding agents (OpenCode, Claude Code and Antigravity) are all switched off for this chat by the user. Do not "
                                  "try again or work around it (no claude/opencode/agy through bash "
                                  "either): do the work with your own tools, or tell the user "
                                  "they can switch them back on with the coding-agent button."),
                        "disabled": True, "exit_code": 1}
        except Exception:
            pass
        if action not in ("plan", "execute", "ask", "list", "status", "agents", "attach"):
            return {"error": "action must be 'plan', 'execute', 'ask', 'list', 'status', 'agents' or 'attach'",
                    "exit_code": 1}
        if action == "attach":
            return await self._attach(str(args.get("job_id") or ""), ctx or {})

        # Reads: no prompt, no directory, nothing spawned.
        if action == "list":
            return await self._list_agents()
        if action == "agents":
            return self._chat_agents((ctx or {}).get("session_id") or "")
        if action == "status":
            return await self._job_status(args.get("job_id"), (ctx or {}).get("session_id") or "")

        prompt = (args.get("prompt") or args.get("task") or "").strip()
        if not prompt:
            return {"error": "prompt is required", "exit_code": 1}

        resume_id = (args.get("session_id") or "").strip()
        # A check is not a plan: "its asked me about 12 times for plans"
        # (2026-09-29). A chat sent read-only status checks and questions as
        # plans, and each one asked the user to approve a plan that changed
        # nothing. Those run as 'ask', which needs no approval.
        if action == "plan" and not resume_id and _is_read_only_check(prompt):
            logger.info("[claude_code] a read-only check sent as a plan runs as ask")
            action = "ask"
        # A list of shell commands is a chore, not coding work: seen live, a
        # branch rename (six git commands) went through an OpenCode plan and
        # approval. Refused so the agent runs them itself with bash.
        if action in ("plan", "ask") and not resume_id and _is_command_list(prompt):
            return {"error": ("Not started: this is a list of shell commands, not coding work. "
                              "Run them yourself with bash (ssh to the machine if needed), one "
                              "command per step, and report what each did."),
                    "chore": True, "exit_code": 1}
        if action == "execute" and not resume_id:
            return {
                "error": (
                    "execute requires the session_id returned by a plan run. "
                    "Run action:'plan' first and let the user approve it."
                ),
                "exit_code": 1,
            }

        engine = str(args.get("engine") or "").strip().lower()
        if not engine and action == "execute":
            # Carry on with the engine the approved plan was written with.
            engine = str((approvals.get(resume_id) or {}).get("engine") or "")
        engine = engine or DEFAULT_ENGINE
        # The engine and model the user picked when approving (the Approve
        # dialog). Asked for: "when approving the plans, let me manually
        # switch from each type of coder, ie: opencode, claude code, and the
        # models too". A plan's session only exists for the engine that wrote
        # it, so another engine starts fresh, handed the approved plan.
        run_fresh, plan_text, plan_engine = False, "", ""
        if action == "execute":
            appr = approvals.get(resume_id) or {}
            plan_engine = str(appr.get("engine") or "")
            chosen = str(appr.get("run_engine") or "").lower()
            if chosen in ENGINES:
                run_fresh = bool(plan_engine) and chosen != plan_engine
                engine = chosen
            if appr.get("run_model"):
                args["model"] = str(appr["run_model"])
            plan_text = str(appr.get("plan") or "")
        if engine not in ENGINES:
            return {"error": "engine must be 'opencode', 'claude' or 'antigravity'", "exit_code": 1}
        # Plans are written on OpenCode (the local model) unless the user
        # named Claude themselves. Seen live: the agent asked for Claude and
        # opus on its own for a plan, spending the user's Claude plan on
        # read-only recon. The approved run carries on with the plan's engine.
        engine_note = ""
        if (action == "plan" and engine == "claude" and DEFAULT_ENGINE == "opencode"
                and not _user_named_claude((ctx or {}).get("session_id") or "")):
            engine = "opencode"
            args.pop("model", None)                  # a Claude model name means nothing to OpenCode
            if resume_id and not resume_id.startswith("ses_"):
                resume_id = ""                       # a Claude session cannot continue on OpenCode
            engine_note = ("Planned on OpenCode (the local model): plans use it unless the user "
                           "asks for Claude by name. Say so when showing the plan.")
        # One engine switched off for this chat (src/chat_prefs.py). A fresh
        # plan or ask moves to the other engine if that one is allowed; an
        # approved plan cannot change engine, so its run is refused.
        chat_for_prefs = (ctx or {}).get("session_id") or ""
        try:
            from src import chat_prefs
            engine_ok = chat_prefs.engine_allowed(chat_for_prefs, engine)
            # The first other engine this chat allows, OpenCode first (the local model).
            others = [e for e in ENGINES if e != engine]
            other = next((e for e in others if chat_prefs.engine_allowed(chat_for_prefs, e)), others[0])
            other_ok = chat_prefs.engine_allowed(chat_for_prefs, other)
        except Exception:
            engine_ok, other, other_ok = True, "", False
        if not engine_ok:
            if action in ("plan", "ask") and other_ok:
                engine_note = (f"{engine_label(engine)} is switched off for this chat, so this runs on "
                               f"{engine_label(other)}. Say so.")
                engine = other
                args.pop("model", None)                 # a model name for one engine means nothing to the other
                # Sessions never cross engines, and Claude's and Antigravity's
                # ids look alike (UUIDs), so the old one is always dropped.
                resume_id = ""
            else:
                return {"error": (f"{engine_label(engine)} is switched off for this chat by the user"
                                  + (", and this approved plan was written for it, so it cannot run on "
                                     f"{engine_label(other)}." if action == "execute" and other_ok else ".")
                                  + " Do not work around it. Tell the user; they can change it with the "
                                    "coding-agent button in the chat bar."),
                        "disabled": True, "exit_code": 1}

        # One agent per chat (src/claude_code_agents.py): an ask or plan in a
        # chat carries on that chat's agent for the folder, so it keeps what it
        # already read. `from_chat` carries on another chat's agent instead.
        chat_id = (ctx or {}).get("session_id") or ""
        agent_note = ""
        agent_from = ""
        if action in ("ask", "plan") and not resume_id:
            from_chat = str(args.get("from_chat") or "").strip()
            if from_chat:
                matches = claude_code_agents.find_chat(from_chat)
                if not matches:
                    return {"error": (f"no chat matching {from_chat!r} has a Claude Code agent. "
                                      "Use action 'agents' to see which chats do."), "exit_code": 1}
                if len(matches) > 1:
                    names = ", ".join(f"{claude_code_agents._chat_name(c) or '?'} ({c[:8]})"
                                      for c in matches[:6])
                    return {"error": f"{from_chat!r} matches several chats: {names}. Pass the chat id.",
                            "exit_code": 1}
                agent = claude_code_agents.for_chat(
                    matches[0], cwd=str(args.get("cwd") or ""), engine=engine)
                if not agent:
                    return {"error": ("that chat has no Claude Code agent"
                                      + (" for this folder" if args.get("cwd") else "")
                                      + f" (engine {engine}). Use action 'agents' to see its agents."),
                            "exit_code": 1}
                resume_id = agent["session_id"]
                args["cwd"] = args.get("cwd") or agent["cwd"]
                agent_from = matches[0]
                agent_note = (f"Carrying on the Claude Code agent from the chat "
                              f"\"{claude_code_agents._chat_name(agent_from) or agent_from[:8]}\".")
            elif chat_id and args.get("cwd") and not args.get("new_agent"):
                agent = claude_code_agents.for_chat(chat_id, cwd=str(args["cwd"]), engine=engine)
                if agent:
                    resume_id = agent["session_id"]
                    agent_from = chat_id
                    agent_note = ("Carrying on this chat's Claude Code agent, which keeps what it "
                                  "already read. Pass new_agent:true for a fresh one.")

        # One run at a time per agent: two chats driving the same CLI session
        # at once would interleave their turns in one transcript. And never a
        # second run of an approved plan: seen live, a restart mid-run and a
        # second Approve started another CLI doing the same work beside the
        # first. Runs that outlived a restart are back in the registry
        # (reattach_runs); the process check catches anything untracked.
        if resume_id:
            busy = claude_code_jobs.running_for(
                cli_session_id=resume_id,
                plan_id=resume_id if action == "execute" else "",
                job_id=str((approvals.get(resume_id) or {}).get("run_id") or "") if action == "execute" else "")
            if busy and action == "execute" and busy.plan_id == resume_id:
                return {"error": (f"this approved plan is already running as job {busy.id} "
                                  f"(started {round(time.time() - busy.started)}s ago). Do NOT start it "
                                  "again: its result will be posted in this chat when it finishes. Tell "
                                  "the user it is running and can be watched under Background tasks."),
                        "job_id": busy.id, "already_running": True, "exit_code": 1}
            if busy:
                return {"error": (f"that Claude Code agent is busy with job {busy.id} "
                                  f"({busy.action}, started from another run). Wait for it, watch "
                                  "it under Background tasks, or pass new_agent:true for a fresh agent."),
                        "job_id": busy.id, "exit_code": 1}
            pid = _os_process_for(resume_id)
            if pid:
                return {"error": (f"a {engine} process (pid {pid}) is already working in this session "
                                  "on this host. Do NOT start another: wait for it to finish, or pass "
                                  "new_agent:true for a fresh agent."),
                        "pid": pid, "already_running": True, "exit_code": 1}

        cli = engine_cli(engine)
        if not cli:
            return {
                "error": (f"{engine_label(engine)} CLI not found on PATH ({ENGINE_BINARIES[engine]}). "
                          + ("Ask the user to install it (curl -fsSL https://antigravity.google/cli/install.sh | bash) "
                             "and sign in once by running agy." if engine == "antigravity" else "Install it on this host.")),
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
        # Never the running Odysseus install itself. Seen live: a build-mode
        # run for a project on the laptop was started in /home/jaron/odysseus
        # (the agent reached the laptop over ssh), with write access to the
        # server's own code. A project on another machine gets a scratch
        # workspace here instead, created on first use.
        odysseus_root = Path(__file__).resolve().parents[2]
        if cwd_path == odysseus_root or odysseus_root in cwd_path.parents:
            return {"error": (
                f"refusing cwd {cwd_path}: that is the running Odysseus install on this server, not "
                "a project. For a project on this server, name its own folder. For a project on "
                "another machine (the laptop, the PC), use a scratch workspace such as "
                f"{Path(WORKSPACES_DIR) / '<project name>'} (created for you) and reach the "
                "machine over ssh from there. To change Odysseus ITSELF (a new feature), work in "
                f"its own copy, {Path(WORKSPACES_DIR) / 'odysseus'}: clone it there if missing "
                "(git clone <origin of /home/jaron/odysseus>), branch from origin/dev, commit, push, "
                "and open a PR against dev with gh; the user merges it. Never edit the live "
                "install."), "exit_code": 1}
        workspaces = Path(WORKSPACES_DIR).resolve()
        if not cwd_path.exists() and workspaces in cwd_path.parents:
            cwd_path.mkdir(parents=True, exist_ok=True)
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
        if action == "execute" and run_fresh:
            prompt = (f"The user approved the plan below (written by {engine_label(plan_engine)} for this "
                      f"folder) and chose you to carry it out.\n\n<approved-plan>\n{plan_text}\n"
                      f"</approved-plan>\n\n{prompt}")
        if action == "execute":
            prompt = prompt.rstrip() + STATUS_INSTRUCTIONS
        prompt_via_stdin = True
        if engine == "opencode" and args.get("model"):
            # OpenCode only knows the models in its own config. Seen
            # 2026-09-29: "totoro/qwen3.8:27b" (an Odysseus endpoint OpenCode
            # has no provider for) exited 1 with nothing useful, and the
            # Claude-only hint sent the agent off to change the server's
            # default model. Refuse it here, naming the ones that work.
            known = opencode_model_ids()
            if known and str(args["model"]) not in known:
                if action == "execute" and resume_id:
                    approvals.restore_approval(resume_id)     # nothing ran: the approval still stands
                return {
                    "error": (f"OpenCode has no model {str(args['model'])!r}. Use one of: "
                              f"{', '.join(known)}, or leave `model` out for its default. Do not "
                              "change Odysseus settings to get a model: ask the user to add it to "
                              "OpenCode (~/.config/opencode/opencode.json)."),
                    "exit_code": 2,
                }
        if engine == "antigravity" and args.get("model"):
            known = antigravity_model_ids()
            if known and str(args["model"]) not in known:
                if action == "execute" and resume_id:
                    approvals.restore_approval(resume_id)     # nothing ran: the approval still stands
                return {"error": (f"Antigravity has no model {str(args['model'])!r}. Use one of: "
                                  f"{', '.join(known)}, or leave `model` out for its default."),
                        "exit_code": 2}
        if engine == "antigravity":
            # agy mints the conversation id and reports it in its init event.
            # Headless there is nobody to approve a tool, so without
            # --dangerously-skip-permissions anything needing approval (writes,
            # commands) is soft-denied, and --sandbox restricts the terminal:
            # plans and asks stay read-only. An approved execute may write.
            session_id = "" if run_fresh else (resume_id or "")
            cmd = [cli, "-p", prompt, "--output-format", "stream-json"]
            if args.get("model"):
                cmd += ["--model", str(args["model"])]
            if resume_id and not run_fresh:
                cmd += ["--conversation", resume_id]
            cmd += ["--dangerously-skip-permissions"] if action == "execute" else ["--sandbox"]
            prompt_via_stdin = False
        elif engine == "opencode":
            # OpenCode mints its own ses_… id, which the event stream reports.
            session_id = "" if run_fresh else (resume_id or "")
            cmd = [cli, "run", "--format", "json", "--dir", str(cwd_path),
                   "--agent", "build" if action == "execute" else "plan"]
            if args.get("model"):
                cmd += ["--model", str(args["model"])]
            if resume_id and not run_fresh:
                cmd += ["--session", resume_id]
            cmd.append(prompt)
            prompt_via_stdin = False
        else:
            cmd = [cli, "-p", "--output-format", "stream-json", "--verbose"]

        if engine in ("opencode", "antigravity"):
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
            session_id = str(uuid.uuid4()) if run_fresh else resume_id
            cmd += [
                *(["--session-id", session_id] if run_fresh else ["--resume", session_id]),
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
        # Run limits: from the agent's arguments, else what the user chose when
        # approving the plan.
        limits = normalize_limits({k: args.get(k) for k in ("max_turns", "max_cost_usd", "take_your_time")})
        if (not any(limits.values())) and action == "execute":
            limits = normalize_limits((approvals.get(resume_id) or {}).get("limits"))
        if engine == "claude":
            if not model and action == "execute" and plan_engine == "claude":
                # The plan's own model, when it was a Claude plan: an OpenCode
                # model name means nothing to Claude.
                model = str((approvals.get(resume_id) or {}).get("model") or "")
            model = model or DEFAULT_MODEL
            cmd += ["--model", model]
            if limits.get("max_cost_usd"):
                # The CLI's own hard stop, at the point where the wrap-up starts.
                cmd += ["--max-budget-usd", f"{BUDGET_WRAP_AT * limits['max_cost_usd']:.2f}"]
        model_label = model or (ANTIGRAVITY_DEFAULT_LABEL if engine == "antigravity" else OPENCODE_DEFAULT_LABEL)
        run_label = f"{engine_label(engine)} · {model_label}"

        # Detached mode: hand the run to bg_jobs and return now, so a refactor
        # that takes ten minutes does not hold the chat open. The monitor
        # re-invokes the agent with the output once it finishes.
        # --bg is Claude's own; other engines run here and can be sent to the
        # background from the card instead.
        if args.get("background") and engine == "claude":
            return await self._launch_background(
                cmd, prompt, cwd_path, session_id, action,
                (ctx or {}).get("session_id"))

        # Start from a clean environment for the child. Anything inherited from
        # a surrounding Claude Code process (CLAUDECODE, CLAUDE_CODE_*, the
        # messaging socket) makes the CLI think it is a nested child session and
        # it then refuses its own edit tools — the run exits 0 having silently
        # changed nothing. Only relevant when this server was itself launched
        # from a Claude Code session, which is exactly the case while developing.
        env = _cli_env()

        try:
            timeout = max(30, min(3600, int(args.get("timeout") or DEFAULT_TIMEOUT_S)))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        if limits.get("take_your_time"):
            timeout = TAKE_YOUR_TIME_TIMEOUT_S
        if engine == "antigravity":
            # agy stops waiting after 5 minutes by default; the run's own
            # timeout (and the stall check) decide instead.
            cmd += ["--print-timeout", f"{int(timeout) + 60}s"]

        # Registered before the CLI starts, so its run directory exists to
        # write into. An approved plan's run takes the id the approval was
        # given, which is what the chat already shows for it.
        run_id = ""
        if action == "execute":
            run_id = str((approvals.get(resume_id) or {}).get("run_id") or "")
        job = claude_code_jobs.register(
            job_id=run_id,
            chat_session_id=chat_id, owner=(ctx or {}).get("owner") or "",
            action=action, cwd=str(cwd_path), model=model_label, engine=engine,
            prompt=prompt)
        job.cli_session_id = session_id or ""
        job.plan_id = resume_id if action == "execute" else ""
        job.spec = {
            "action": action, "session_id": session_id or "", "cwd": str(cwd_path),
            "model": model, "model_label": model_label, "engine": engine,
            "run_label": run_label, "args_model": str(args.get("model") or ""),
            "chat_id": chat_id, "owner": (ctx or {}).get("owner") or "",
            "prompt": prompt, "agent_note": agent_note, "timeout": timeout, "limits": limits,
            "engine_note": engine_note,
        }

        # The CLI runs in its own session, writing to files rather than pipes
        # into this process, so a server restart neither kills it nor loses
        # its output: the restarted server reattaches (reattach_runs). The
        # shell wrapper records the exit code, which nothing else would be
        # around to collect. Claude takes the prompt on stdin (no argv length
        # cap, nothing to escape); OpenCode takes it positionally.
        os.makedirs(job.run_dir, exist_ok=True)
        env["ODYSSEUS_STATUS_FILE"] = os.path.join(job.run_dir, "status.jsonl")
        prompt_path = os.path.join(job.run_dir, "prompt.txt")
        if prompt_via_stdin:
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write(prompt)
        wrapped = ["/bin/sh", "-c", '"$@"; echo $? > "$0/exit"', job.run_dir, *cmd]
        try:
            with open(os.path.join(job.run_dir, "out.jsonl"), "ab") as out_f, \
                 open(os.path.join(job.run_dir, "err.log"), "ab") as err_f, \
                 open(prompt_path if prompt_via_stdin else os.devnull, "rb") as in_f:
                proc = await asyncio.create_subprocess_exec(
                    *wrapped, cwd=str(cwd_path), env=env,
                    stdin=in_f, stdout=out_f, stderr=err_f, start_new_session=True)
        except Exception as e:
            claude_code_jobs.finish(job, "failed", {"error": str(e), "exit_code": 1})
            return {"error": f"could not start {engine}: {e}", "exit_code": 1}
        job.proc = proc
        job.pid = proc.pid

        started = time.time()
        job.spec["started"] = started
        tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)
        stream = _Stream(engine, session_id or "")

        # What was actually spawned, stated up front. Without this the user sees
        # an opaque spinner and has to go hunting in `ps` to find out whether a
        # process exists at all, what it may touch, or how to kill it.
        if engine == "opencode":
            _grant = f"--agent {'build' if action == 'execute' else 'plan'}"
        elif engine == "antigravity":
            _grant = "--dangerously-skip-permissions" if action == "execute" else "--sandbox (read-only: tools needing approval are denied)"
        else:
            _grant = (
                f"--permission-mode {'plan' if action in ('plan', 'ask') else 'bypassPermissions'}"
                f" --allowedTools {args.get('allowed_tools') or (PLAN_TOOLS if action == 'plan' else ASK_TOOLS if action == 'ask' else EXECUTE_TOOLS)}"
            )
        banner = (
            f"$ {ENGINE_BINARIES.get(engine, engine)} {_grant}\n"
            f"  job {job.id} · pid {proc.pid} · session {(session_id or 'pending')[:12]} · cwd {cwd_path}\n"
            f"  model {model_label} · kill with: kill -- -{proc.pid}"
            + (f"\n  limits: {_limit_text(limits)}" if any(limits.values()) else "")
        )
        job.banner = banner
        claude_code_jobs.save()

        def _add(ln: str) -> None:
            ln = _clip(ln, LINE_CHARS)
            tail.append(ln)
            job.lines.append(ln)

        def _tail_text() -> str:
            head = banner
            if stream.thinking_tokens:
                head += f"\n  thinking… {stream.thinking_tokens} tokens"
            body = "\n".join(tail)
            return head + ("\n" + body if body else "")

        def _progress() -> dict:
            return {"elapsed_s": round(time.time() - started, 1), "tail": _tail_text(),
                    "job_id": job.id, "can_background": not job.detached,
                    "model": model_label, "engine_label": engine_label(engine),
                    "usage": {"turns": stream.turns, "cost_usd": round(stream.cost, 2), "limits": limits},
                    "agent_status": job.agent_status}

        if progress_cb:
            # Emit once immediately; the periodic loop only starts after a delay
            # and a long thinking phase would otherwise show nothing at all.
            try:
                await progress_cb(_progress())
            except Exception:
                pass

        async def _emit_progress():
            while True:
                await asyncio.sleep(PROGRESS_INTERVAL_S)
                try:
                    await progress_cb(_progress())
                except Exception:
                    pass

        wait_task = asyncio.create_task(proc.wait())
        out_tail = _FileTail(os.path.join(job.run_dir, "out.jsonl"))
        follow_task = asyncio.create_task(
            _follow(job, stream, _add, wait_task.done, out_tail))
        prog = asyncio.create_task(_emit_progress()) if progress_cb else None
        detach_task = asyncio.create_task(job.detach_event.wait())
        try:
            done, _ = await asyncio.wait({follow_task, detach_task}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            detach_task.cancel()
            if prog:
                prog.cancel()
            if _restarting():
                # A deploy restart, not the user: the run keeps going and the
                # next server reattaches to it and posts its result.
                job.detached = True
                claude_code_jobs.save()
                raise
            # The chat's Stop, while the run is still in the foreground.
            claude_code_jobs.kill_run(job)
            follow_task.cancel()
            claude_code_jobs.finish(job, "stopped")
            raise

        if detach_task in done and follow_task not in done:
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
            claude_code_jobs.save()

            async def _carry_on():
                timed_out = False
                try:
                    await asyncio.wait_for(asyncio.shield(follow_task), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    claude_code_jobs.kill_run(job)
                    try:
                        await asyncio.wait_for(follow_task, timeout=10)
                    except Exception:
                        pass
                try:
                    result = await _build_result(job, stream, _exit_code(job), timed_out)
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
                    f"Moved to the background as job `{job.id}` ({run_label}, "
                    f"in {cwd_path}). It keeps running; its result will be posted in this chat "
                    "when it finishes, and it can be watched or stopped under Background tasks.\n"
                    "Do NOT wait or poll for it. Tell the user it is running in the background, "
                    "then carry on."
                ),
                "exit_code": 0,
            }

        detach_task.cancel()
        timed_out = follow_task not in done
        if timed_out:
            claude_code_jobs.kill_run(job)
            try:
                await asyncio.wait_for(follow_task, timeout=10)
            except Exception:
                pass
        if prog:
            prog.cancel()
        result = await _build_result(job, stream, _exit_code(job), timed_out)
        claude_code_jobs.finish(job, _job_status(result), result)
        return result


# ── the run, independent of the chat turn that started it ────────────────
# Module level so a run picked up again after a restart finishes exactly as a
# live one would (reattach_runs).

FOLLOW_POLL_S = 0.25


class _Stream:
    """The CLI's event stream, turned into console lines and the final result."""

    def __init__(self, engine: str, session_id: str = ""):
        self.engine = engine
        self.session_id = session_id
        self.final_text = ""
        self.is_error = False
        self.thinking_tokens = 0
        # For run limits: turns taken and dollars spent so far. Claude reports
        # the exact total only at the end (result.total_cost_usd); until then
        # it is estimated from each message's token usage.
        self.turns = 0
        self.cost_exact: Optional[float] = None
        self.result_subtype = ""
        self._msg_usage: Dict[str, tuple] = {}
        self._agy_steps: set = set()
        self._agy_text: Dict[object, str] = {}

    @property
    def cost(self) -> float:
        if self.cost_exact is not None:
            return self.cost_exact
        return sum(_price(model, u) for model, u in self._msg_usage.values())

    def feed(self, raw: str) -> list:
        raw = (raw or "").strip()
        if not raw:
            return []
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return [_strip_ansi(raw)]
        if not isinstance(event, dict):
            return [_strip_ansi(raw)]
        if self.engine == "antigravity":
            kind = event.get("event")
            body = event.get(kind) if isinstance(event.get(kind), dict) else {}
            if not self.session_id:
                self.session_id = str(event.get("conversation_id") or body.get("conversation_id") or "")
            if kind == "step_update":
                idx = body.get("step_index")
                if idx is not None and idx not in self._agy_steps:
                    self._agy_steps.add(idx)
                    self.turns += 1
                if body.get("step_type") == "agent_response" and body.get("text_delta"):
                    self._agy_text[idx] = self._agy_text.get(idx, "") + str(body["text_delta"])
                    if str(body.get("state") or "").upper() == "DONE":
                        text = self._agy_text.get(idx, "").strip()
                        self.final_text = (self.final_text + "\n" + text).strip()
                        return text.splitlines()
                    return []
                if str(body.get("state") or "").upper() == "DONE":
                    usage = body.get("usage") or {}
                    self.thinking_tokens += int(usage.get("thinking_tokens") or 0)
            elif kind == "result":
                # The whole answer, authoritative over the streamed pieces.
                self.final_text = str(body.get("response") or "").strip() or self.final_text
                status = str(body.get("status") or "").upper()
                self.is_error = status not in ("", "SUCCESS")
                self.result_subtype = status.lower()
                if body.get("error"):
                    # Kept even after partial text: a quota or sign-in failure is the news.
                    self.final_text = (self.final_text + f"\n\nAntigravity error: {body['error']}").strip()
                if isinstance(body.get("num_turns"), int):
                    self.turns = max(self.turns, body["num_turns"])
                return [f"Antigravity error: {body['error']}"] if body.get("error") else []
            summary = _summarize_antigravity(event)
            return summary.splitlines() if summary else []
        if self.engine == "opencode":
            # OpenCode allocates the session itself, so the id is only
            # knowable from the stream — and it is what a later --session
            # resume, and the approval record, both key on.
            if not self.session_id and event.get("sessionID"):
                self.session_id = event["sessionID"]
            if event.get("type") == "step_start":
                self.turns += 1
            elif event.get("type") == "step_finish":
                try:
                    self.cost_exact = (self.cost_exact or 0.0) + float((event.get("part") or {}).get("cost") or 0)
                except (TypeError, ValueError):
                    pass
            summary = _summarize_opencode(event)
            if not summary:
                return []
            if event.get("type") == "text":
                self.final_text = (self.final_text + "\n" + summary).strip()
            return summary.splitlines()
        if event.get("type") == "assistant":
            msg = event.get("message") or {}
            mid = msg.get("id") or ""
            if mid and mid not in self._msg_usage:
                self.turns += 1
            if mid:
                self._msg_usage[mid] = (msg.get("model") or "", msg.get("usage") or {})
        if event.get("type") == "result":
            self.final_text = event.get("result") or self.final_text
            self.is_error = bool(event.get("is_error"))
            self.result_subtype = str(event.get("subtype") or "")
            if isinstance(event.get("total_cost_usd"), (int, float)):
                self.cost_exact = float(event["total_cost_usd"])
            if isinstance(event.get("num_turns"), int):
                self.turns = max(self.turns, event["num_turns"])
        elif event.get("subtype") == "thinking_tokens":
            # Counted rather than printed: it is the only signal during a long
            # silent reasoning phase, but one line per tick would flood it.
            self.thinking_tokens = event.get("estimated_tokens") or self.thinking_tokens
        summary = _summarize(event)
        return summary.splitlines() if summary else []


class _FileTail:
    """Complete lines appended to a file since the last read. No line length
    limit: seen live, stream-json events of 110 KB+ (a whole file read) broke
    a line-limited pipe reader and the finished run was never noticed."""

    def __init__(self, path: str):
        self.path = path
        self.pos = 0
        self.buf = b""

    def lines(self, final: bool = False) -> list:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except FileNotFoundError:
            data = b""
        self.buf += data
        parts = self.buf.split(b"\n")
        self.buf = parts.pop()
        if final and self.buf:
            parts.append(self.buf)
            self.buf = b""
        return [p.decode("utf-8", errors="replace") for p in parts]


async def _follow(job, stream: "_Stream", add, exited, tail: "_FileTail") -> None:
    """Feed the run's output to `stream` until `exited()` is true."""
    status_tail = _FileTail(os.path.join(job.run_dir, "status.jsonl"))
    last_heard = time.time()
    while True:
        done = exited()                    # checked first, so the last read gets everything
        for raw in tail.lines(final=done):
            last_heard = time.time()
            for ln in stream.feed(raw):
                add(ln)
        for raw in status_tail.lines(final=done):
            last_heard = time.time()
            _take_status(job, raw)
        if not done:
            _check_limits(job, stream, add)
            _check_stalled(job, add, last_heard)
        if stream.session_id and job.cli_session_id != stream.session_id:
            job.cli_session_id = stream.session_id
            job.spec["session_id"] = stream.session_id
            claude_code_jobs.save()
        if done:
            return
        await asyncio.sleep(FOLLOW_POLL_S)


STATUS_INSTRUCTIONS = """

--- Status for the user ---
Keep the user posted while you work, the way a progress line does. At the start and after each
step, append ONE line of JSON to the file named in $ODYSSEUS_STATUS_FILE, for example:
  echo '{"state":"working","detail":"Deploying the API worker"}' >> "$ODYSSEUS_STATUS_FILE"
state is "working", "needs_input" (you are blocked on the user: say exactly what you need), or
"done" (with a one-line result). Keep detail under 12 words. This is for the user's screen only:
it does not replace your final report."""
STATUS_STATES = ("working", "needs_input", "done", "failed")


def _take_status(job, raw: str) -> None:
    """One line the agent appended to status.jsonl."""
    raw = (raw or "").strip()
    if not raw:
        return
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        d = {"state": "working", "detail": raw}
    if not isinstance(d, dict):
        return
    state = str(d.get("state") or "working").strip().lower().replace(" ", "_")
    if state not in STATUS_STATES:
        state = "working"
    detail = _clip(str(d.get("detail") or d.get("text") or "").strip(), 160)
    st = {"state": state, "detail": detail, "at": time.time()}
    prev = job.agent_status
    job.agent_status = st
    job.timeline.append(st)
    del job.timeline[:-50]
    claude_code_jobs.save()
    # Blocked on the user: tell them where they will see it (push, Modes,
    # the desktop overlay), once per distinct request.
    if state == "needs_input" and not (prev and prev.get("state") == "needs_input"
                                       and prev.get("detail") == detail):
        try:
            t = asyncio.get_running_loop().create_task(_notify_needs_input(job, detail))
            _BG_TASKS.add(t)
            t.add_done_callback(_BG_TASKS.discard)
        except RuntimeError:
            pass


async def _notify_needs_input(job, detail: str) -> None:
    if not job.chat_session_id:
        return
    try:
        from src import chat_queue
        notify = chat_queue.get(job.chat_session_id).get("notify") or {}
        title = chat_queue._session_title(job.chat_session_id)
        await chat_queue.send_notification(
            job.chat_session_id, notify,
            f"{engine_label(job.engine)} needs you" + (f": {title}" if title else ""),
            detail or "It is waiting on you.", kind="question")
    except Exception as e:
        logger.warning("Could not send the needs-input notification for %s: %s", job.id, e)


# ── run limits: turns, budget, or take your time ─────────────────────────
# Asked for: "set how many turns it should take, total cost (try to stay under;
# when hit, finish up right away!) or select take your time and it does
# unlimited". A limit reached stops the run and resumes the same session for
# one short wrap-up turn, so the work is summarized instead of cut off.
BUDGET_WRAP_AT = 0.8            # of the budget: leaves room for the wrap-up
TAKE_YOUR_TIME_TIMEOUT_S = 4 * 3600
# Dollars per million tokens: input, output, cache read, cache write.
# Calibrated against a real run (claude-opus-5-5, 12.3M cache reads + 277K
# cache writes + 115K output = $6.98 reported). Only an estimate: the CLI's own
# --max-budget-usd enforces the real spend on Claude runs.
_PRICES = {"haiku": (1.0, 5.0, 0.1, 2.0), "sonnet": (3.0, 15.0, 0.3, 6.0), "opus": (3.0, 15.0, 0.3, 6.0)}
WRAP_UP_PROMPT = (
    "STOP: you have reached the {reason} the user set for this run. Do not start anything new "
    "and do not change any more files. In a few short lines, report: what is done, what is left, "
    "and anything half-finished the user should know about.")


def _price(model: str, usage: dict) -> float:
    m = (model or "").lower()
    rate = next((v for k, v in _PRICES.items() if k in m), _PRICES["opus"])
    u = usage or {}
    return (rate[0] * float(u.get("input_tokens") or 0) + rate[1] * float(u.get("output_tokens") or 0)
            + rate[2] * float(u.get("cache_read_input_tokens") or 0)
            + rate[3] * float(u.get("cache_creation_input_tokens") or 0)) / 1_000_000


def normalize_limits(raw) -> Dict:
    """{"max_turns", "max_cost_usd", "take_your_time"} from a request or the
    agent's arguments; anything missing or invalid means no such limit."""
    raw = raw if isinstance(raw, dict) else {}
    out = {"max_turns": None, "max_cost_usd": None, "take_your_time": bool(raw.get("take_your_time"))}
    if out["take_your_time"]:
        return out
    try:
        t = int(raw.get("max_turns") or 0)
        out["max_turns"] = t if 1 <= t <= 1000 else None
    except (TypeError, ValueError):
        pass
    try:
        c = float(raw.get("max_cost_usd") or 0)
        out["max_cost_usd"] = round(c, 2) if 0 < c <= 1000 else None
    except (TypeError, ValueError):
        pass
    return out


def _limit_text(limits: Dict) -> str:
    if not limits:
        return ""
    if limits.get("take_your_time"):
        return "take your time (no turn or cost limit)"
    parts = []
    if limits.get("max_turns"):
        parts.append(f"{limits['max_turns']} turns")
    if limits.get("max_cost_usd"):
        parts.append(f"${limits['max_cost_usd']:.2f}")
    return " · ".join(parts)


# A run that writes nothing for this long is stuck (a model server that
# stopped answering), not thinking. Seen 2026-09-29: two runs sat silent for
# 20 and 40 minutes, and the chat showed a bare tool call the whole time
# ("there should not ever ... just be a plain old tool call at the bottom of
# the page"). OpenCode writes a line per finished step, so a slow model can
# be quiet for minutes while it thinks: the limit is well past that.
STALL_S = int(os.environ.get("ODYSSEUS_CODER_STALL_S", "900"))


def _check_stalled(job, add, last_heard: float) -> None:
    if job.spec.get("stalled") or job.status != "running" or STALL_S <= 0:
        return
    quiet = time.time() - last_heard
    if quiet < STALL_S:
        return
    job.spec["stalled"] = max(1, round(quiet / 60))
    add(f"\u25a0 No output for {job.spec['stalled']} minutes: the model server looks stuck. Stopping the run.")
    claude_code_jobs.save()
    claude_code_jobs.kill_run(job)


def _check_limits(job, stream: "_Stream", add) -> None:
    """Stop the run once a limit is reached; the wrap-up follows in _build_result."""
    limits = (job.spec or {}).get("limits") or {}
    if job.spec.get("limit_hit") or job.status != "running" or limits.get("take_your_time"):
        return
    reason = ""
    if limits.get("max_turns") and stream.turns >= limits["max_turns"]:
        reason = f"turn limit ({limits['max_turns']} turns)"
    elif limits.get("max_cost_usd") and stream.cost >= BUDGET_WRAP_AT * limits["max_cost_usd"]:
        reason = f"budget (${limits['max_cost_usd']:.2f})"
    if not reason:
        return
    job.spec["limit_hit"] = reason
    add(f"\u25a0 Reached the {reason}: stopping and asking for a short wrap-up.")
    claude_code_jobs.save()
    claude_code_jobs.kill_run(job)


async def _wrap_up(job, reason: str) -> str:
    """One short turn in the same session: what is done, what is left."""
    spec = job.spec
    engine = spec.get("engine", job.engine)
    sid = job.cli_session_id or spec.get("session_id") or ""
    cli = engine_cli(engine)
    if not cli or not sid:
        return ""
    prompt = WRAP_UP_PROMPT.format(reason=reason)
    if engine == "antigravity":
        cmd = [cli, "-p", prompt, "--output-format", "stream-json", "--conversation", sid,
               "--sandbox", "--print-timeout", "170s"]
        if spec.get("args_model"):
            cmd += ["--model", spec["args_model"]]
        stdin_data = None
    elif engine == "opencode":
        cmd = [cli, "run", "--format", "json", "--dir", spec.get("cwd") or job.cwd,
               "--agent", "plan", "--session", sid]
        if spec.get("args_model"):
            cmd += ["--model", spec["args_model"]]
        cmd.append(prompt)
        stdin_data = None
    else:
        cmd = [cli, "-p", "--output-format", "stream-json", "--verbose", "--resume", sid,
               "--permission-mode", "plan", "--allowedTools", "Read"]
        if spec.get("model"):
            cmd += ["--model", spec["model"]]
        cost_cap = ((spec.get("limits") or {}).get("max_cost_usd") or 0) * (1 - BUDGET_WRAP_AT)
        if cost_cap:
            cmd += ["--max-budget-usd", f"{max(0.05, cost_cap):.2f}"]
        stdin_data = prompt.encode()
    env = _cli_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=spec.get("cwd") or job.cwd, env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=STREAM_LINE_LIMIT, start_new_session=True)
        out, _ = await asyncio.wait_for(proc.communicate(stdin_data), timeout=180)
    except Exception as e:
        logger.warning("Wrap-up for job %s failed: %s", job.id, e)
        return ""
    s = _Stream(engine, sid)
    for line in out.decode("utf-8", "replace").splitlines():
        for ln in s.feed(line):
            job.lines.append(_clip(ln, LINE_CHARS))
    return (s.final_text or "").strip()


def _exit_code(job) -> int:
    """The CLI's exit code, as the wrapper recorded it. Missing means the run
    was killed before it could say (Stop, a timeout, or a reboot)."""
    try:
        with open(os.path.join(job.run_dir, "exit"), "r") as f:
            return int(f.read().strip() or -9)
    except (FileNotFoundError, ValueError):
        rc = getattr(job.proc, "returncode", None)
        return rc if rc not in (None, 0) else -9


def _stderr_tail(job, n: int = 10) -> list:
    try:
        with open(os.path.join(job.run_dir, "err.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16000))
            text = f.read().decode("utf-8", errors="replace")
        return [ln.rstrip() for ln in text.splitlines() if ln.strip()][-n:]
    except OSError:
        return []


def _job_status(result: Dict) -> str:
    code = result.get("exit_code")
    return "done" if code == 0 else "timed_out" if code == 124 else "failed"


async def _build_result(job, stream: "_Stream", returncode: int, timed_out: bool) -> Dict:
    """The tool result, once the CLI has exited (or been killed)."""
    spec = job.spec
    action = spec.get("action", job.action)
    session_id = stream.session_id or spec.get("session_id", "")
    cwd = spec.get("cwd", job.cwd)
    model_label = spec.get("model_label", job.model)
    run_label = spec.get("run_label", model_label)
    timeout = spec.get("timeout", DEFAULT_TIMEOUT_S)
    started = spec.get("started", job.started)
    # Keep the banner in the saved console so the pid, cwd and tool grant are
    # still on the record after the run ends, not just while it streams.
    head = job.banner + (f"\n  thinking… {stream.thinking_tokens} tokens" if stream.thinking_tokens else "")
    body_lines = "\n".join(job.lines)
    console = head + ("\n" + body_lines if body_lines else "")
    final_text = stream.final_text

    limits = spec.get("limits") or {}
    reason = spec.get("limit_hit") or ""
    if not reason and limits.get("max_cost_usd") and "budget" in (stream.result_subtype or "").lower():
        reason = f"budget (${limits['max_cost_usd']:.2f})"          # the CLI's own stop
    if reason:
        summary = await _wrap_up(job, reason)
        spent = f"{stream.turns} turns, about ${stream.cost:.2f}"
        body = (f"**Stopped at the {reason} you set** ({spent}).\n\n"
                + (summary or final_text or "It stopped before it could summarize; see the console."))
        return {"action": action, "output": body[:MAX_RESULT_CHARS], "console": console[-12000:],
                "session_id": session_id, "cwd": cwd, "model": model_label, "limit_hit": reason,
                "usage": {"turns": stream.turns, "cost_usd": round(stream.cost, 2), "limits": limits},
                "engine_label": engine_label(spec.get("engine", job.engine)), "exit_code": 0,
                "job_id": job.id}
    if spec.get("stalled"):
        restored = action == "execute" and approvals.restore_approval(session_id)
        return {
            "error": (
                f"Stopped: {engine_label(spec.get('engine', job.engine))} wrote nothing for "
                f"{spec['stalled']} minutes on {model_label}, so the model server is stuck or overloaded. "
                + ("The approval is restored, so it can run again. " if restored else "")
                + "Tell the user this in your reply, and suggest another model (a different server) "
                  "or trying again later. Do not retry on the same model by yourself."),
            "output": console[-MAX_RESULT_CHARS:],
            "session_id": session_id, "approval_restored": restored, "stalled": True,
            "exit_code": 1,
        }
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
    if returncode != 0 and not final_text:
        if action == "execute":
            approvals.restore_approval(session_id)
        detail = "\n".join(_stderr_tail(job)) or console[-2000:]
        hint = ""
        if spec.get("args_model") and spec.get("engine", job.engine) == "antigravity":
            hint = (f" — note model was set to {spec['args_model']!r}; Antigravity's models are "
                    f"{', '.join(antigravity_model_ids()) or 'those `agy models` lists'}. "
                    "Retry without `model` to use its default.")
        elif spec.get("args_model") and spec.get("engine", job.engine) == "opencode":
            hint = (f" — note model was set to {spec['args_model']!r}; OpenCode's models are "
                    f"{', '.join(opencode_model_ids()) or 'those in ~/.config/opencode/opencode.json'}. "
                    "Retry without `model` to use its default.")
        elif spec.get("args_model"):
            # The common cause by far: an invented id like claude-opus-4. The
            # CLI's own message does not always make that obvious.
            hint = (
                f" — note model was set to {spec['args_model']!r}; valid values are "
                "'opus', 'sonnet', 'haiku' or a full id such as 'claude-opus-5'. "
                "Retry without `model` to use the default."
            )
        return {
            "error": f"claude_code exited {returncode}: {detail[:400]}{hint}",
            "session_id": session_id,
            "exit_code": returncode or 1,
        }

    body = final_text or console
    result = {
        "action": action,
        "engine_label": engine_label(spec.get("engine", job.engine)),
        "output": body[:MAX_RESULT_CHARS],
        "console": console[-12000:],
        "session_id": session_id,
        "cwd": cwd,
        "model": model_label,
        "elapsed_s": round(time.time() - started, 1),
        "exit_code": 1 if stream.is_error else 0,
    }
    if job.id:
        result["job_id"] = job.id
    if spec.get("agent_note"):
        result["agent"] = spec["agent_note"]
    if spec.get("engine_note"):
        result["engine_note"] = spec["engine_note"]
    chat_id = spec.get("chat_id", "")
    if chat_id and session_id and not stream.is_error:
        try:
            claude_code_agents.record(
                chat_id, session_id=session_id, cwd=cwd, engine=spec.get("engine", job.engine),
                model=model_label, action=action, prompt=spec.get("prompt", ""), summary=body)
        except Exception:
            pass
    plan_ref = session_id
    if action == "plan":
        try:
            plan_ref = session_id + "~" + approvals.record_plan(
                session_id,
                cwd=cwd,
                plan=body,
                owner=spec.get("owner", ""),
                model=spec.get("model", ""),
                engine=spec.get("engine", job.engine),
                chat_session_id=chat_id,
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
                running_title=f"Plan · {Path(cwd).name} · jaronwilson.dev",
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
            "approve": f"[Approve plan · runs on {run_label}](#claudecode-approve-{plan_ref})",
            "deny": f"[Deny](#claudecode-deny-{plan_ref})",
        }
        result["next_step"] = (
            "Show the plan to the user in full. If the result has a `pdf` field, show that "
            "link too so they can read it as a paginated document. Then show these two links "
            "on their own line exactly as given so they can click one:\n"
            f"[Approve plan · runs on {run_label}](#claudecode-approve-{plan_ref})  ·  [Deny](#claudecode-deny-{plan_ref})\n"
            f"Say plainly that approving runs it on {run_label}. "
            "Tell them they can also just reply with changes they want instead of approving, "
            "including a different model (for example 'haiku' for small changes). "
            "Then STOP and wait. Approving starts the run by itself, in this chat; calling "
            "execute before they click Approve will be refused by the server."
        )
        # Nobody may be looking: say a plan is waiting, where they will see it.
        t = asyncio.create_task(_notify_plan(session_id, chat_id, run_label, body))
        _BG_TASKS.add(t)
        t.add_done_callback(_BG_TASKS.discard)
    return result


_BG_TASKS: set = set()
# With the chat open, the plan is probably being read: wait this long before
# also sending a notification, and skip it if the plan was answered meanwhile.
PLAN_NOTIFY_GRACE_S = 120.0
PLAN_TURN_WAIT_S = 20 * 60

_CLAUDE_NAMED = re.compile(r"\b(claude(?![_\w])|opus|sonnet|haiku|fable)\b", re.I)


def _user_named_claude(chat_id: str) -> bool:
    """Whether the user's own last messages in the chat ask for Claude."""
    if not chat_id:
        return False
    try:
        from src.ai_interaction import get_session_manager
        sess = get_session_manager().get_session(chat_id)
    except Exception:
        return False
    seen = 0
    for msg in reversed(getattr(sess, "history", None) or []):
        if getattr(msg, "role", "") != "user":
            continue
        if (getattr(msg, "metadata", None) or {}).get("source") in (
                "claude_code_plan_approved", "screen_control_approved"):
            continue                                 # our own notes, not the user
        if _CLAUDE_NAMED.search(str(getattr(msg, "content", "") or "")):
            return True
        seen += 1
        if seen >= 2:
            break
    return False


async def _notify_plan(plan_id: str, chat_id: str, run_label: str, plan: str) -> None:
    """Tell the user a plan is waiting for Approve or Deny: push, the Modes
    listener and the desktop overlay (chat_queue.send_notification)."""
    if not chat_id:
        return
    try:
        from src import agent_runs, chat_queue
        # Only once the chat shows the plan: the turn that ran it is still
        # writing it up (a local model takes minutes). Seen live: the
        # notification came first, and the chat it opened looked empty
        # until the reply landed two minutes later.
        waited = 0.0
        while agent_runs.is_active(chat_id) and waited < PLAN_TURN_WAIT_S:
            await asyncio.sleep(2.0)
            waited += 2.0
        if agent_runs.has_watchers(chat_id):
            await asyncio.sleep(PLAN_NOTIFY_GRACE_S)
        if (approvals.get(plan_id) or {}).get("status") != "pending":
            return
        notify = chat_queue.get(chat_id).get("notify") or {}
        title = chat_queue._session_title(chat_id)
        first = next((ln.strip("#*- ").strip() for ln in (plan or "").splitlines() if ln.strip()), "")
        await chat_queue.send_notification(
            chat_id, notify, f"Plan ready: {title}" if title else "Plan ready",
            f"Approve or deny (runs on {run_label}). {first}"[:220], kind="plan")
    except Exception as e:
        logger.warning("Could not send the plan notification for %s: %s", chat_id, e)


async def _reattached(job) -> None:
    """Follow a run that outlived the server that started it, then finish it
    as the live run would have, posting the result into its chat."""
    stream = _Stream(job.engine, job.cli_session_id)

    def add(ln: str) -> None:
        job.lines.append(_clip(ln, LINE_CHARS))

    exit_path = os.path.join(job.run_dir, "exit")

    def exited() -> bool:
        return os.path.exists(exit_path) or not claude_code_jobs.pid_alive(job.pid, job.run_dir)

    tail = _FileTail(os.path.join(job.run_dir, "out.jsonl"))
    timeout = float(job.spec.get("timeout") or DEFAULT_TIMEOUT_S)
    remaining = max(5.0, float(job.spec.get("started") or job.started) + timeout - time.time())
    timed_out = False
    try:
        await asyncio.wait_for(_follow(job, stream, add, exited, tail), timeout=remaining)
    except asyncio.TimeoutError:
        timed_out = True
        claude_code_jobs.kill_run(job)
        for _ in range(40):
            if exited():
                break
            await asyncio.sleep(0.25)
        for raw in tail.lines(final=True):
            for ln in stream.feed(raw):
                add(ln)
    try:
        result = await _build_result(job, stream, _exit_code(job), timed_out)
    except Exception as e:
        result = {"error": f"could not finish the run: {e}", "exit_code": 1}
    claude_code_jobs.finish(job, _job_status(result), result)
    await _post_background_result(job, result)


def reattach_runs() -> int:
    """After a restart: pick up every recorded run, live or finished while
    the server was down, and see it through. Call from the running loop."""
    n = 0
    for rec in claude_code_jobs.load_records():
        if claude_code_jobs.get(rec["id"]):
            continue
        sp = rec.get("server_pid")
        if sp and sp != os.getpid() and claude_code_jobs.pid_alive(sp):
            continue                       # another server process still follows it
        if time.time() - float(rec.get("started") or 0) > claude_code_jobs.MAX_REATTACH_AGE_S:
            continue
        if not os.path.isdir(os.path.join(claude_code_jobs.RUNS_DIR, rec["id"])):
            continue
        job = claude_code_jobs.adopt(rec)
        job.task = asyncio.create_task(_reattached(job))
        n += 1
        logger.info("Reattached Claude Code job %s (pid %s, chat %s)",
                    job.id, job.pid, (job.chat_session_id or "-")[:8])
    claude_code_jobs.save()
    return n


_CMD_LINE_RE = re.compile(r"^\s*(?:[-*>\d.)]+\s*)?`?\$?\s*(git|gh|ssh|scp|cd|ls|mv|cp|rm|mkdir|npx|npm|wrangler|curl|docker|systemctl)\b")
_CODE_WORK_RE = re.compile(r"\b(implement|refactor|rewrite|write (?:the |a |new )?(?:code|function|test|component|module|script)|"
                           r"fix (?:the |a )?bug|add (?:a |the )?(?:feature|endpoint|route|page|test|function)|debug|"
                           r"build (?:a |the )?(?:feature|page|component|api)|design)\b", re.I)


_CHECK_RE = re.compile(
    r"\b(read[- ]only (status )?check|status check|sanity check|just check|check only|"
    r"do not (write|change|edit|modify) anything|no changes|nothing to change|"
    r"answer (these|the following|this|\d+|one|two|three|four|five|six) questions?)\b", re.I)
_PLAN_WORD_RE = re.compile(r"\bplan(s|ning|ned)?\b|\bdesign\b|\bimplement", re.I)


def _is_read_only_check(prompt: str) -> bool:
    """A prompt that asks for facts, not a change: it says it is a check (or
    questions to answer) and never speaks of a plan or of building anything."""
    head = (prompt or "")[:600]
    return bool(_CHECK_RE.search(head)) and not _PLAN_WORD_RE.search(prompt or "")


def _restarting() -> bool:
    try:
        from src import agent_runs
        return bool(agent_runs.restarting)
    except Exception:
        return False


def _is_command_list(prompt: str) -> bool:
    """Mostly shell commands (3 or more) and no coding work asked for."""
    lines = [ln for ln in (prompt or "").splitlines() if ln.strip()]
    cmds = sum(1 for ln in lines if _CMD_LINE_RE.match(ln))
    return cmds >= 3 and not _CODE_WORK_RE.search(prompt or "")


REATTACH_SWEEP_S = 20


async def reattach_forever() -> None:
    """Keep picking up runs no live server follows. Seen live: during a
    restart the old process started a run after the new one had already done
    its startup reattach, then exited; the run finished with nobody to post
    its result."""
    while True:
        await asyncio.sleep(REATTACH_SWEEP_S)
        try:
            reattach_runs()
        except Exception as e:
            logger.warning("Claude Code reattach sweep failed: %s", e)


def _os_process_for(session_id: str) -> Optional[int]:
    """A claude/opencode process on this host already working in CLI session
    `session_id`, tracked or not."""
    if not session_id or not os.path.isdir("/proc"):
        return None
    me = os.getpid()
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if not args:
            continue
        head = " ".join(os.path.basename(a) for a in args[:2]).lower()
        if "claude" not in head and "opencode" not in head and "agy" not in head:
            continue
        if any(a == session_id or a.endswith(f"/{session_id}.jsonl") for a in args):
            return int(d)
    return None


async def _post_background_result(job, result: Dict) -> None:
    """Put a backgrounded run's result into the chat it came from, and send
    the chat's done notification if one was asked for."""
    sid = job.chat_session_id
    if not sid or job.attached:
        return                          # brought back: the chat turn following it reports it
    ok = result.get("exit_code") == 0
    name = engine_label(job.engine)
    head = (f"**{name if job.reattached else 'Background ' + name} job `{job.id}` "
            f"{'finished' if ok else 'stopped' if job.status == 'stopped' else 'failed'}** "
            f"({job.action}, {job.model}, {round((job.finished or time.time()) - job.started)}s, "
            f"in `{job.cwd}`"
            + ("; it kept running through a server restart" if job.reattached else "") + ")")
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
                    "round": 1, "tool": "claude_code", "label": engine_label(job.engine),
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
    _wake_chat(job, sid, "finished" if ok else "stopped" if job.status == "stopped" else "failed")


BG_DONE_PROMPT = (
    "[Background job {verb} \u00b7 job {job_id} \u00b7 {engine}]\n\n"
    "The {engine} job `{job_id}` that was running in the background has {verb}; its result is "
    "posted just above. Tell the user in a few lines what it did (or why it stopped) and whether "
    "anything needs them. If they had already asked in this chat for a next step once it was "
    "done, carry that on now. Otherwise do not start new work."
)


def _wake_chat(job, sid: str, verb: str) -> None:
    """Start a short turn in the chat so its model reads the result and tells
    the user, the way a person would ping back. Asked for: "once an agent is
    done it does not ping the chat, I have to reiterate or ask if it's done
    when in background tasks I can see it finished". Queued once behind a
    reply that is already running there."""
    prompt = BG_DONE_PROMPT.format(verb=verb, job_id=job.id, engine=engine_label(job.engine))
    try:
        from src.screen_control_resume import start_turn
        if start_turn(sid, prompt, note_source="claude_code_background_done",
                      reply_source="claude_code_background_report"):
            return
        from src import chat_queue
        chat_queue.add(sid, prompt, key=f"bg-done:{job.id}",
                       label=f"Report {engine_label(job.engine)} job {job.id} ({verb})")
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Could not tell chat %s that job %s %s", sid[:8], job.id, verb)
