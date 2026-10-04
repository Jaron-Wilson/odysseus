"""Claude Code's own sessions on this host, read from its transcripts.

Claude Code writes each session as JSONL under ~/.claude/projects/<cwd with
"/" as "-">/<session uuid>.jsonl, one event per line: user and assistant
messages (with tool_use and tool_result blocks), titles, summaries and
bookkeeping. This module lists them and turns one into readable turns for
the Claude sessions page (static/js/claudeSessions.js,
routes/claude_sessions_routes.py). Read-only: nothing here writes there.

Some transcripts are hundreds of MB, so nothing reads a whole file per
request. The list reads a little of each file's head and tail and counts
messages once per file, then only the bytes appended since (the files only
grow). A transcript is paged backwards from the end by byte offset, and a
live one is followed forwards from the last offset the page saw.

Only names that are really there are opened: a project must be one of the
directories under the projects dir and a session a UUID, so a request can't
point this anywhere else.
"""

import json
import os
import re
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple

# A session counts as live when its transcript changed this recently.
LIVE_S = 120

# How much of a file the list reads for its titles, cwd and branch.
HEAD_BYTES = 512 * 1024
TAIL_BYTES = 256 * 1024
# Where a user prompt is looked for at the start of a file. Sessions can
# open with large attachments, so this is more than HEAD_BYTES.
FIRST_PROMPT_BYTES = 4 * 1024 * 1024

# Paging a transcript: at most this many turns, from at most this many bytes.
PAGE_TURNS = 200
PAGE_BYTES = 8 * 1024 * 1024
# Lines longer than this (a whole file pasted in, a huge tool result) are not
# parsed; the page says one was left out.
MAX_LINE = 6 * 1024 * 1024

# What a turn keeps of each part.
TEXT_CAP = 20000
RESULT_CAP = 2000
INPUT_SUMMARY_CAP = 200
TITLE_CAP = 120

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Claude Code names a project dir after its cwd: every "/" and "." becomes "-".
PROJECT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$|^-[A-Za-z0-9._-]{0,254}$")

# Wrappers Claude Code puts around things the user did not type.
_NOT_A_PROMPT = ("<local-command-caveat>", "<command-name>", "<local-command-stdout>",
                 "<system-reminder>", "<bash-input>", "<bash-stdout>", "<task-notification>",
                 "Caveat: ")
_COMMAND_RE = re.compile(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>(?:.*?<command-args>(.*?)</command-args>)?", re.S)


def projects_dir() -> str:
    return os.path.expanduser(os.getenv("CLAUDE_PROJECTS_DIR") or "~/.claude/projects")


def jobs_dir() -> str:
    return os.path.expanduser(os.getenv("CLAUDE_JOBS_DIR") or "~/.claude/jobs")


# ── Path safety ─────────────────────────────────────────────────────────

def valid_project(name: str) -> bool:
    return (isinstance(name, str) and bool(PROJECT_RE.match(name))
            and name not in (".", "..") and "/" not in name and "\\" not in name)


def valid_session_id(sid: str) -> bool:
    return isinstance(sid, str) and bool(UUID_RE.match(sid))


def project_names() -> List[str]:
    root = projects_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    # A symlinked dir could lead anywhere, so it isn't a project here.
    return sorted(n for n in names if valid_project(n) and os.path.isdir(os.path.join(root, n))
                  and not os.path.islink(os.path.join(root, n)))


def transcript_path(project: str, session_id: str) -> Optional[str]:
    """The transcript's path, or None unless the project is a real project
    dir, the id a UUID and the file a regular file inside the projects dir."""
    if not valid_project(project) or not valid_session_id(session_id):
        return None
    if project not in project_names():
        return None
    root = os.path.realpath(projects_dir())
    path = os.path.realpath(os.path.join(root, project, session_id + ".jsonl"))
    if os.path.dirname(os.path.dirname(path)) != root or not os.path.isfile(path):
        return None
    return path


def project_label(project: str) -> str:
    """A readable guess at the cwd from the dir name (lossy: "-" stood for
    "/", "." and "-" alike). The real cwd comes from the transcript."""
    return "/" + project.lstrip("-").replace("-", "/") if project.startswith("-") else project


# ── Reading lines ───────────────────────────────────────────────────────

def _kind_hint(head: bytes) -> str:
    """The event's type from the start of its line, without parsing it.
    Assistant lines put "message" (with its role) before "type"."""
    if b'"type":"user"' in head or b'"role":"user"' in head:
        return "user"
    if b'"role":"assistant"' in head or b'"type":"assistant"' in head:
        return "assistant"
    if b'"type":"system"' in head:
        return "system"
    if b'"type":"summary"' in head:
        return "summary"
    return ""


def _loads(line: bytes) -> Optional[dict]:
    try:
        d = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return d if isinstance(d, dict) else None


def lines_backward(f, end: int, block: int = 256 * 1024) -> Iterator[Tuple[int, Optional[bytes], int]]:
    """(offset, line, length) for each complete line ending at or before
    `end`, last first. `line` is None for lines over MAX_LINE, which are
    stepped over without being read in."""
    pos = end
    carry = b""          # the end of a line whose start is still further back
    carry_len = 0        # its full length so far, carry may be cut short
    while pos > 0:
        start = max(0, pos - block)
        f.seek(start)
        chunk = f.read(pos - start)
        pos = start
        while True:
            i = chunk.rfind(b"\n")
            if i < 0:
                carry_len += len(chunk)
                if carry_len <= MAX_LINE:
                    carry = chunk + carry
                else:
                    carry = b""
                break
            tail = chunk[i + 1:]
            length = len(tail) + carry_len
            if length:
                line = (tail + carry) if length <= MAX_LINE else None
                yield start + i + 1, line, length
            carry, carry_len = b"", 0
            chunk = chunk[:i]
    if carry_len:
        yield 0, (carry if carry_len <= MAX_LINE else None), carry_len


def lines_forward(f, start: int, end: int) -> Iterator[Tuple[int, Optional[bytes], int]]:
    """(offset, line, length) for each complete line from `start` to `end`.
    A last line without its newline yet is left for next time."""
    f.seek(start)
    pos = start
    while pos < end:
        raw = f.readline(MAX_LINE + 1)
        if not raw:
            return
        if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
            # Too long to show: skip to its end.
            skipped = len(raw)
            while True:
                more = f.readline(1024 * 1024)
                skipped += len(more)
                if not more or more.endswith(b"\n"):
                    break
            if not more or not more.endswith(b"\n"):
                return
            yield pos, None, skipped - 1
            pos += skipped
            continue
        if not raw.endswith(b"\n"):
            return
        yield pos, raw[:-1], len(raw) - 1
        pos += len(raw)


# ── Turning events into turns ──────────────────────────────────────────

def _cap(s: str, n: int) -> Tuple[str, bool]:
    s = s or ""
    return (s, False) if len(s) <= n else (s[:n].rstrip() + "…", True)


def _one_line(s: str) -> str:
    return " ".join(str(s).split())


def _block_text(content) -> str:
    """The text in a message's content: a string or a list of blocks."""
    if isinstance(content, str):
        return content
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict):
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                out.append(b["text"])
            elif b.get("type") == "image":
                out.append("[image]")
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def summarize_input(name: str, inp) -> str:
    """One short line for a tool call: its command, path, pattern or query."""
    if not isinstance(inp, dict):
        return _cap(_one_line(inp or ""), INPUT_SUMMARY_CAP)[0]
    for key in ("description", "command", "file_path", "pattern", "path", "url", "query",
                "prompt", "skill", "subject", "notebook_path", "task_id", "message"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            text = v
            if key == "description" and isinstance(inp.get("command"), str):
                text = f"{v}: {inp['command']}"
            elif key == "pattern" and isinstance(inp.get("path"), str):
                text = f"{v} in {inp['path']}"
            return _cap(_one_line(text), INPUT_SUMMARY_CAP)[0]
    try:
        return _cap(_one_line(json.dumps(inp, ensure_ascii=False)), INPUT_SUMMARY_CAP)[0]
    except (TypeError, ValueError):
        return ""


def _prompt_text(text: str) -> Optional[str]:
    """What the user typed, or None for wrappers around something else. A
    slash command shows as the command."""
    t = (text or "").strip()
    if not t:
        return None
    m = _COMMAND_RE.search(t)
    if m:
        args = (m.group(2) or "").strip()
        return f"{m.group(1)} {args}".strip()
    if t.startswith(_NOT_A_PROMPT):
        return None
    return t


def parse_event(d: dict) -> List[Dict]:
    """The readable turns in one transcript event (often none)."""
    kind = d.get("type")
    ts = d.get("timestamp") or ""
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    side = bool(d.get("isSidechain"))
    out: List[Dict] = []
    if kind == "user":
        content = msg.get("content")
        if d.get("isCompactSummary"):
            text, cut = _cap(_block_text(content), TEXT_CAP)
            return [{"kind": "summary", "text": text, "truncated": cut, "ts": ts}]
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    res = b.get("content")
                    text = _block_text(res) if not isinstance(res, str) else res
                    text, cut = _cap(text, RESULT_CAP)
                    out.append({"kind": "tool_result", "id": b.get("tool_use_id") or "",
                                "text": text, "truncated": cut, "is_error": bool(b.get("is_error")), "ts": ts})
        if d.get("isMeta"):
            return out
        prompt = _prompt_text(_block_text(content))
        if prompt:
            text, cut = _cap(prompt, TEXT_CAP)
            out.append({"kind": "user", "text": text, "truncated": cut, "ts": ts, "sidechain": side})
        return out
    if kind == "assistant":
        for b in msg.get("content") if isinstance(msg.get("content"), list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                text, cut = _cap(b["text"], TEXT_CAP)
                out.append({"kind": "assistant", "text": text, "truncated": cut, "ts": ts,
                            "model": msg.get("model") or "", "sidechain": side})
            elif b.get("type") == "tool_use":
                name = b.get("name") or "tool"
                out.append({"kind": "tool", "id": b.get("id") or "", "name": name,
                            "summary": summarize_input(name, b.get("input")), "ts": ts})
        return out
    if kind == "system":
        sub = d.get("subtype")
        if sub == "compact_boundary":
            return [{"kind": "system", "text": "Conversation compacted", "ts": ts}]
        if sub in ("away_summary", "informational", "scheduled_task_fire") and isinstance(d.get("content"), str):
            return [{"kind": "system", "text": _cap(d["content"], 1000)[0], "ts": ts}]
        return []
    if kind == "summary" and isinstance(d.get("summary"), str):
        return [{"kind": "summary", "text": _cap(d["summary"], TEXT_CAP)[0], "ts": ts}]
    return []


def parse_line(line: Optional[bytes], length: int = 0) -> List[Dict]:
    if line is None:
        return [{"kind": "system", "text": f"(an event of {length // 1024} KB is too large to show)", "ts": ""}]
    if not _kind_hint(line[:800]):
        return []
    d = _loads(line)
    return parse_event(d) if d else []


# ── One session ─────────────────────────────────────────────────────────

def read_page(path: str, before: Optional[int] = None, limit: int = PAGE_TURNS) -> Dict:
    """The last `limit` turns ending at byte offset `before` (the end of the
    file when None). `start` is where the next older page ends; 0 means
    this reaches the beginning. `end` is where to follow from."""
    limit = max(1, min(int(limit or PAGE_TURNS), 1000))
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        end = size
        if before is None:
            # A last line still being written is left for the next poll.
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    for off, _line, _n in lines_backward(f, size):
                        end = off
                        break
            before = end
        before = max(0, min(int(before), size))
        groups: List[List[Dict]] = []
        count = 0
        start = before
        scanned = 0
        for off, line, length in lines_backward(f, before):
            start = off
            scanned += length + 1
            turns = parse_line(line, length)
            if turns:
                groups.append(turns)
                count += len(turns)
            if count >= limit or scanned >= PAGE_BYTES:
                break
        else:
            start = 0
    turns = [t for g in reversed(groups) for t in g]
    return {"turns": turns, "start": start, "end": end, "size": size}


def read_after(path: str, after: int) -> Dict:
    """Turns appended since byte offset `after`. `reset` when the file is
    shorter than that (rewritten), and the page should start over."""
    size = os.path.getsize(path)
    after = max(0, int(after or 0))
    if after > size:
        return {"turns": [], "end": size, "size": size, "reset": True}
    turns: List[Dict] = []
    end = after
    with open(path, "rb") as f:
        for off, line, length in lines_forward(f, after, size):
            turns.extend(parse_line(line, length))
            end = off + length + 1
            if len(turns) >= 1000:
                break
    return {"turns": turns, "end": end, "size": size, "reset": False}


# ── The list ────────────────────────────────────────────────────────────

_CACHE: Dict[str, Dict] = {}
_LOCK = threading.Lock()


def _head_info(f, size: int) -> Dict:
    """cwd, branch and the first prompt, from the start of the file."""
    info: Dict = {}
    f.seek(0)
    for _off, line, _n in lines_forward(f, 0, min(size, FIRST_PROMPT_BYTES)):
        if line is None:
            continue
        hint = _kind_hint(line[:800])
        if hint != "user" and info.get("cwd"):
            continue
        d = _loads(line) if hint or b'"cwd"' in line[:2000] else None
        if not d:
            continue
        if d.get("cwd") and not info.get("cwd"):
            info["cwd"] = d["cwd"]
        if d.get("type") == "user" and not d.get("isMeta") and not d.get("isCompactSummary"):
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            p = _prompt_text(_block_text(msg.get("content")))
            if p and not p.startswith("/"):
                info["first_prompt"] = _cap(_one_line(p), 300)[0]
                break
    return info


def _tail_info(f, size: int) -> Dict:
    """The latest titles, cwd, branch and time, from the end of the file."""
    info: Dict = {}
    start = max(0, size - TAIL_BYTES)
    f.seek(start)
    data = f.read(size - start)
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]
    for line in reversed(lines):
        if not line.strip():
            continue
        head = line[:400]
        want = (b'"custom-title"' in head or b'"ai-title"' in head or b'"summary"' in head
                or b'"last-prompt"' in head or b'"agent-name"' in head
                or not info.get("cwd") or not info.get("last_ts"))
        if not want:
            continue
        d = _loads(line)
        if not d:
            continue
        t = d.get("type")
        if t == "custom-title" and d.get("customTitle"):
            info.setdefault("custom_title", d["customTitle"])
        elif t == "ai-title" and d.get("aiTitle"):
            info.setdefault("ai_title", d["aiTitle"])
        elif t == "agent-name" and d.get("agentName"):
            info.setdefault("agent_name", d["agentName"])
        elif t == "summary" and d.get("summary"):
            info.setdefault("summary", d["summary"])
        elif t == "last-prompt" and d.get("lastPrompt"):
            info.setdefault("last_prompt", _cap(_one_line(d["lastPrompt"]), 300)[0])
        if d.get("cwd") and not info.get("cwd"):
            info["cwd"] = d["cwd"]
            info["git_branch"] = d.get("gitBranch") or ""
            info["kind"] = d.get("sessionKind") or ""
        if d.get("timestamp") and not info.get("last_ts"):
            info["last_ts"] = d["timestamp"]
    return info


def _count_messages(f, start: int, end: int) -> Tuple[int, int]:
    """User and assistant events in the complete lines from `start` to `end`,
    from the start of each line only. Returns (count, where the last
    complete line ends), so a half-written line is counted next time."""
    count = 0
    f.seek(start)
    pos = done_to = start
    head = b""
    while pos < end:
        chunk = f.read(min(4 * 1024 * 1024, end - pos))
        if not chunk:
            break
        i = 0
        while True:
            j = chunk.find(b"\n", i)
            if len(head) < 800:
                head += chunk[i:(j if j >= 0 else len(chunk))][:800 - len(head)]
            if j < 0:
                break
            if _kind_hint(head) in ("user", "assistant"):
                count += 1
            head = b""
            done_to = pos + j + 1
            i = j + 1
        pos += len(chunk)
    return count, done_to


def _job_for(session_id: str) -> Optional[Dict]:
    """A `claude --bg` job's state, when this session is one."""
    path = os.path.join(jobs_dir(), session_id[:8], "state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    scan = st.get("linkScanPath") or ""
    if scan and not scan.endswith(session_id + ".jsonl"):
        return None
    return {"state": st.get("state") or "", "name": st.get("name") or "",
            "detail": _cap(_one_line(st.get("detail") or ""), 300)[0]}


def session_info(project: str, path: str, now: Optional[float] = None) -> Optional[Dict]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    sid = os.path.basename(path)[:-len(".jsonl")]
    with _LOCK:
        c = _CACHE.get(path)
        if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime:
            info = c["info"]
        else:
            try:
                with open(path, "rb") as f:
                    if not c or st.st_size < c["counted_to"] or c.get("ino") != st.st_ino:
                        c = {"count": 0, "counted_to": 0, "head": _head_info(f, st.st_size), "ino": st.st_ino}
                    elif not c["head"].get("first_prompt") and c["counted_to"] < FIRST_PROMPT_BYTES:
                        c["head"] = _head_info(f, st.st_size)
                    n, upto = _count_messages(f, c["counted_to"], st.st_size)
                    c["count"] += n
                    c["counted_to"] = upto
                    tail = _tail_info(f, st.st_size)
            except OSError:
                return None
            head = c["head"]
            info = {
                "cwd": tail.get("cwd") or head.get("cwd") or "",
                "git_branch": tail.get("git_branch") or "",
                "kind": tail.get("kind") or "",
                "custom_title": tail.get("custom_title") or "",
                "ai_title": tail.get("ai_title") or "",
                "summary": tail.get("summary") or "",
                "first_prompt": head.get("first_prompt") or "",
                "last_prompt": tail.get("last_prompt") or "",
                "message_count": c["count"],
            }
            c.update(size=st.st_size, mtime=st.st_mtime, info=info)
            _CACHE[path] = c
    now = time.time() if now is None else now
    title = (info["custom_title"] or info["ai_title"] or info["summary"]
             or info["first_prompt"] or info["last_prompt"] or "Untitled session")
    out = dict(info)
    out.update({
        "id": sid, "project": project, "title": _cap(_one_line(title), TITLE_CAP)[0],
        "cwd": info["cwd"] or project_label(project),
        "last_activity": st.st_mtime, "size": st.st_size,
        "live": now - st.st_mtime <= LIVE_S,
        "job": _job_for(sid),
    })
    return out


def list_sessions(now: Optional[float] = None) -> List[Dict]:
    """Every session in every project, live ones first, then newest."""
    root = projects_dir()
    out = []
    seen = set()
    for project in project_names():
        try:
            names = os.listdir(os.path.join(root, project))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl") or not valid_session_id(name[:-6]):
                continue
            path = os.path.join(root, project, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            seen.add(path)
            info = session_info(project, path, now=now)
            if info:
                out.append(info)
    with _LOCK:
        for p in [p for p in _CACHE if p not in seen]:
            _CACHE.pop(p, None)
    out.sort(key=lambda s: (not s["live"], -s["last_activity"]))
    return out
