"""Talk to a chat that already lives on the shared opencode server.

Asked for 2026-11-02: "is there a way to go on a live call with my opencode from my phone? ... i want quick fast
responses, while smart too."

Odysseus's OpenCode tool (src/agent_tools/claude_code_tool.py) only starts new one-off `opencode run` jobs. The
user's real work lives in chats on the shared opencode server (opencode-serve, 127.0.0.1:4096, basic auth from
~/.config/opencode/server.env). This module is the client for those chats and what a voice call needs around it:

  list / resolve   the recent chats of every folder; a chat named by id, by title, or "the one in <folder>"
  send             POST /session/<id>/prompt_async (returns at once), then wait up to `wait_seconds` for the reply
  progress         the chat's tool steps since the last message, in plain words ("reading careers.html")
  stop             POST /session/<id>/abort
  bindings         which opencode chat an Odysseus chat is bound to, and whether the user armed it (see below)
  the watcher      after a send that is still going, one background task per chat follows it and puts short
                   spoken updates (at most one every UPDATE_MIN_S) and a one or two sentence summary in a queue
                   that the call page polls (routes/opencode_chat_routes.py) and speaks. The final summary is
                   also saved in the Odysseus chat, so the next turn knows what happened.

The agent's turn never waits for the run: a send returns within its wait with "still working" and the latest
steps, and the watcher carries on without the model, so the fast voice model is not spent on every update.

Sending into a chat is as strong as an approved coding run (the chat has its own shell and file access), so the
tool holds a send for the user's Approve unless the user armed that chat themselves: picking it in the call's
work-chat menu (an authenticated request from their own page) arms it for ARM_TTL_S. See the tool for the policy.

The server's password is read here and never logged or returned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# The first words of the note a voice call turn gets (routes/chat_routes.py VOICE_CALL_NOTE): how the agent loop
# knows this turn is spoken.
VOICE_CALL_MARK = "You are in a live voice call with the user."

DEFAULT_URL = "http://127.0.0.1:4096"
ENV_FILE = os.path.expanduser("~/.config/opencode/server.env")

WAIT_DEFAULT_S = 20.0
WAIT_CALL_DEFAULT_S = 8.0       # in a call the model's turn should come back quickly
WAIT_MAX_S = 55.0
POLL_S = 1.0                    # how often a send checks the chat while it waits
WATCH_POLL_S = 2.5              # how often the watcher checks a chat that is still going
UPDATE_MIN_S = 15.0             # a spoken progress update at most this often
WATCH_MAX_S = 60 * 60           # the watcher gives up on a chat that runs longer than this
WATCH_IDLE_S = 90.0             # ... or that shows no sign of life (not busy, no change) this long
CALL_ALIVE_S = 90.0             # a call page polled this recently: the chat is in a call
ARM_TTL_S = 4 * 3600            # how long the user's pick in the call menu lets sends through
QUEUE_KEEP = 40
QUEUE_TTL_S = 15 * 60
SUMMARY_CHARS = 280
LIST_LIMIT = 30

_transport: Optional[httpx.AsyncBaseTransport] = None      # tests put a fake server here
_lock = threading.RLock()


class OpenCodeError(Exception):
    """Why the opencode server could not do it, in words for the model and the person."""


# ── the server ────────────────────────────────────────────────────────────

def set_transport(t: Optional[httpx.AsyncBaseTransport]) -> None:
    global _transport
    _transport = t


def base_url() -> str:
    return (os.environ.get("ODYSSEUS_OPENCODE_URL") or DEFAULT_URL).rstrip("/")


def _credentials() -> Tuple[str, str]:
    """The server's login: the environment first, then the server.env file next to opencode's config."""
    user = os.environ.get("OPENCODE_SERVER_USERNAME") or ""
    pw = os.environ.get("OPENCODE_SERVER_PASSWORD") or ""
    if not pw or not user:
        path = os.environ.get("ODYSSEUS_OPENCODE_SERVER_ENV") or ENV_FILE
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    v = v.strip().strip("'\"")
                    if k.strip() == "OPENCODE_SERVER_PASSWORD":
                        pw = pw or v
                    elif k.strip() == "OPENCODE_SERVER_USERNAME":
                        user = user or v
        except OSError:
            pass
    return user or "opencode", pw


def _client(timeout: float = 10.0) -> httpx.AsyncClient:
    user, pw = _credentials()
    return httpx.AsyncClient(base_url=base_url(), auth=httpx.BasicAuth(user, pw) if pw else None,
                             timeout=timeout, transport=_transport)


async def _call(method: str, path: str, *, params: Optional[dict] = None, body: Optional[dict] = None,
                timeout: float = 10.0) -> Any:
    try:
        async with _client(timeout) as c:
            r = await c.request(method, path, params=params, json=body)
    except httpx.HTTPError as e:
        raise OpenCodeError(f"The opencode server at {base_url()} did not answer ({type(e).__name__}).")
    if r.status_code in (401, 403):
        raise OpenCodeError("The opencode server refused the login (check OPENCODE_SERVER_PASSWORD / server.env).")
    if r.status_code == 404:
        raise OpenCodeError("The opencode server has no such chat or endpoint (404).")
    if r.status_code >= 400:
        raise OpenCodeError(f"The opencode server answered {r.status_code}.")
    if r.status_code == 204 or not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ── chats ─────────────────────────────────────────────────────────────────

def ago(ms: Optional[float], now: Optional[float] = None) -> str:
    secs = max(0.0, (now if now is not None else time.time()) - (ms or 0) / 1000.0)
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= n:
            k = int(secs // n)
            return f"{k} {unit}{'' if k == 1 else 's'} ago"
    return "just now"


def _short_dir(path: str) -> str:
    home = os.path.expanduser("~")
    path = path or ""
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _chat_row(s: dict, busy: set) -> dict:
    t = s.get("time") or {}
    m = s.get("model") or {}
    return {
        "id": s.get("id") or "",
        "title": (s.get("title") or "").replace("\n", " ").strip() or "(untitled)",
        "directory": s.get("directory") or "",
        "folder": _short_dir(s.get("directory") or ""),
        "updated": t.get("updated") or t.get("created") or 0,
        "ago": ago(t.get("updated") or t.get("created")),
        "busy": (s.get("id") or "") in busy,
        "model": m.get("id") or "",
    }


async def busy_ids() -> set:
    try:
        st = await _call("GET", "/session/status")
    except OpenCodeError:
        return set()
    return {k for k, v in (st or {}).items() if isinstance(v, dict) and v.get("type") not in (None, "idle")}


async def list_chats(limit: int = LIST_LIMIT) -> List[dict]:
    """The newest top-level chats across all folders (subagent and archived chats left out), newest first."""
    try:
        rows = await _call("GET", "/experimental/session", params={"roots": "true", "limit": max(1, min(limit * 2, 100))})
    except OpenCodeError as e:
        if "404" not in str(e):
            raise
        rows = await _call("GET", "/session", params={"roots": "true"})
    rows = [s for s in (rows or []) if isinstance(s, dict) and s.get("id") and not s.get("parentID")
            and not (s.get("time") or {}).get("archived")]
    rows.sort(key=lambda s: -((s.get("time") or {}).get("updated") or 0))
    busy = await busy_ids()
    return [_chat_row(s, busy) for s in rows[:limit]]


_FILLER = {"the", "my", "our", "one", "chat", "chats", "session", "opencode", "open", "code", "in", "on", "at",
           "for", "to", "with", "folder", "directory", "project", "called", "named", "that", "this", "a", "an",
           "call", "talk", "go", "of", "from"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def _squash(s: str) -> str:
    return _norm(s).replace(" ", "")


def _core(ref: str) -> str:
    words = [w for w in _norm(ref).split() if w not in _FILLER]
    return " ".join(words)


def _folder_name(row: dict) -> str:
    return os.path.basename((row.get("directory") or "").rstrip("/"))


def resolve(ref: str, chats: List[dict]) -> Tuple[Optional[dict], str]:
    """The chat `ref` names, or (None, why). `ref` is an id, a title ("JaronDynamics"), "the one in <folder>",
    or "latest". Several equally good matches are an error that lists them, never a guess."""
    ref = (ref or "").strip()
    if not ref:
        return None, "Name a chat: its title, its id, or \"the one in <folder>\" (action 'list' shows them)."
    for c in chats:
        if c["id"] == ref:
            return c, ""
    if re.fullmatch(r"ses_[A-Za-z0-9]+", ref):
        starts = [c for c in chats if c["id"].startswith(ref)]
        return (starts[0], "") if len(starts) == 1 else (None, f"No chat with id {ref} among the recent ones.")
    low = _norm(ref)
    if low in ("latest", "last", "newest", "most recent", "the latest", "the last", "the most recent",
               "the latest chat", "my latest chat", "the last chat", "my last chat"):
        return (chats[0], "") if chats else (None, "There are no chats on the opencode server.")
    in_folder = re.search(r"\b(?:in|inside|from|under)\s+(.+)$", ref, re.I)
    folder_q = _core(in_folder.group(1)) if in_folder else ""
    core = _core(ref)
    tiers: List[List[dict]] = []
    if folder_q:
        fq = folder_q.replace(" ", "")
        tiers.append([c for c in chats if _squash(_folder_name(c)) == fq])
        tiers.append([c for c in chats if fq and (fq in _squash(c["directory"]))])
    if core:
        cq = core.replace(" ", "")
        tiers.append([c for c in chats if _squash(c["title"]) == cq])
        tiers.append([c for c in chats if _squash(_folder_name(c)) == cq])
        tiers.append([c for c in chats if cq in _squash(c["title"])])
        tiers.append([c for c in chats if cq in _squash(c["directory"])])
        words = set(core.split())
        if len(words) > 1:
            tiers.append([c for c in chats if words <= set(_norm(c["title"] + " " + c["directory"]).split())])
    for hits in tiers:
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            # The newest of several chats with one title is the one in use, but only when they are the same
            # folder; different folders are a real ambiguity.
            if len({h["directory"] for h in hits}) == 1:
                return hits[0], ""
            names = "; ".join(f"\"{h['title']}\" in {h['folder']} ({h['ago']})" for h in hits[:5])
            return None, f"More than one chat matches \"{ref}\": {names}. Say which one (by folder or id)."
    return None, f"No chat matches \"{ref}\". Action 'list' shows the recent ones."


async def find(ref: str) -> dict:
    chats = await list_chats()
    chat, why = resolve(ref, chats)
    if not chat:
        raise OpenCodeError(why)
    return chat


async def get_messages(chat: dict, limit: int = 60) -> List[dict]:
    rows = await _call("GET", f"/session/{chat['id']}/message",
                       params={"directory": chat.get("directory") or "", "limit": limit})
    return [m for m in (rows or []) if isinstance(m, dict) and isinstance(m.get("info"), dict)]


# ── reading progress ──────────────────────────────────────────────────────

def _rel(path: str, root: str) -> str:
    """A file as it is best said: relative to the chat's folder, or just its name when it is elsewhere."""
    path = str(path or "")
    if root and path.startswith(root.rstrip("/") + "/"):
        path = path[len(root.rstrip("/")) + 1:]
    elif path.startswith("/") or path.startswith("~"):
        path = os.path.basename(path.rstrip("/")) or path
    return path if len(path) <= 70 else "..." + path[-67:]


def _clean_phrase(s: str, n: int = 70) -> str:
    s = re.sub(r"\s+", " ", re.sub(r"[`*_#<>]", "", str(s or ""))).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "..."


def step_words(tool: str, inp: Any, root: str = "") -> Optional[str]:
    """One tool call in plain words ("reading careers.html"), or None for bookkeeping not worth saying.
    Never a command line or file content: a bash step is the tool's own description at most."""
    inp = inp if isinstance(inp, dict) else {}
    tool = (tool or "").lower()
    path = inp.get("filePath") or inp.get("path") or inp.get("file") or ""
    if tool in ("todowrite", "todoread", "todo"):
        return None
    if tool == "read":
        return f"reading {_rel(path, root)}" if path else "reading a file"
    if tool == "edit":
        return f"editing {_rel(path, root)}" if path else "editing a file"
    if tool == "write":
        return f"writing {_rel(path, root)}" if path else "writing a file"
    if tool in ("patch", "apply_patch"):
        return "applying a patch"
    if tool == "bash":
        d = _clean_phrase(inp.get("description") or "", 60)
        return f"running a command ({d})" if d else "running a command"
    if tool == "grep":
        return "searching the code"
    if tool in ("glob", "list", "ls"):
        return "looking through the files"
    if tool == "webfetch":
        return "fetching a web page"
    if tool in ("websearch", "codesearch"):
        return "searching the web"
    if tool == "task":
        d = _clean_phrase(inp.get("description") or "", 60)
        return f"asking a helper agent ({d})" if d else "asking a helper agent"
    if tool == "skill":
        return "loading a skill"
    return f"using {_clean_phrase(tool, 30)}" if tool else None


def _msg_time(m: dict, key: str = "created") -> float:
    return float((m.get("info", {}).get("time") or {}).get(key) or 0)


def _text_of(parts: List[dict]) -> str:
    return "".join(str(p.get("text") or "") for p in parts if p.get("type") == "text" and not p.get("ignored")).strip()


_ERR_NAMES = {"MessageAbortedError": "it was stopped", "ContextOverflowError": "the conversation got too long",
              "ProviderAuthError": "the model login failed", "APIError": "the model API failed"}


def _error_words(err: Any) -> str:
    if not isinstance(err, dict):
        return ""
    name = str(err.get("name") or "")
    msg = _clean_phrase((err.get("data") or {}).get("message") or "", 120)
    base = _ERR_NAMES.get(name) or name or "an error"
    return base + (f": {msg}" if msg and name not in _ERR_NAMES else "")


class Progress:
    """What a chat has done since its newest user message."""

    def __init__(self):
        self.user_id = ""
        self.user_text = ""
        self.started = 0.0
        self.steps: List[Tuple[str, str]] = []       # (part id, words), oldest first
        self.current = ""                            # the tool that is running now, in words
        self.text = ""                               # the newest reply text
        self.done = False
        self.error = ""
        self.has_assistant = False

    @property
    def phrases(self) -> List[str]:
        out: List[str] = []
        for _pid, w in self.steps:
            if not out or out[-1] != w:
                out.append(w)
        return out


def progress_of(msgs: List[dict], root: str = "", after_ids: Optional[set] = None) -> Progress:
    """Progress since the newest user message not in `after_ids` (the messages before a send), or simply the
    newest user message. `msgs` are oldest first, as the server returns them."""
    p = Progress()
    msgs = sorted(msgs, key=lambda m: _msg_time(m))
    idx = -1
    for i, m in enumerate(msgs):
        if m["info"].get("role") == "user" and (after_ids is None or m["info"].get("id") not in after_ids):
            idx = i
            if after_ids is not None:
                break           # the first new one: it is ours
    if idx < 0:
        return p
    u = msgs[idx]
    p.user_id = u["info"].get("id") or ""
    p.user_text = _text_of(u.get("parts") or [])
    p.started = _msg_time(u)
    last_done = False
    for m in msgs[idx + 1:]:
        info = m["info"]
        if info.get("role") != "assistant":
            continue
        if info.get("parentID") and info["parentID"] != p.user_id and after_ids is None:
            # Replies to a later user message belong to the next round, which `idx` would have found.
            continue
        p.has_assistant = True
        last_done = bool((info.get("time") or {}).get("completed")) and info.get("finish") not in ("tool-calls", "unknown")
        if info.get("error"):
            p.error = _error_words(info["error"])
        for part in m.get("parts") or []:
            t = part.get("type")
            if t == "tool":
                state = part.get("state") or {}
                if state.get("status") == "pending" and not state.get("input"):
                    continue            # announced by the model, not started: no file or detail yet
                words = step_words(part.get("tool") or "", state.get("input"), root)
                if not words:
                    continue
                p.steps.append((part.get("id") or "", words))
                if state.get("status") in ("running", "pending"):
                    p.current = words
                    last_done = False
                elif p.current == words:
                    p.current = ""
            elif t == "text" and not part.get("ignored"):
                txt = str(part.get("text") or "").strip()
                if txt:
                    p.text = txt
    p.done = bool(p.has_assistant and last_done and not p.current)
    return p


# ── words for the ear ─────────────────────────────────────────────────────

_FENCE = re.compile(r"```.*?(```|$)", re.S)


def speakable(text: str) -> str:
    """Reply text as plain prose: code blocks dropped, markdown marks and links reduced to their words."""
    t = _FENCE.sub(" ", text or "")
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "", t)
    t = re.sub(r"[*_~|>]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def summary_of(text: str, max_sentences: int = 2, max_chars: int = SUMMARY_CHARS) -> str:
    """The first one or two sentences of a reply, short enough to say out loud."""
    plain = speakable(text)
    if not plain:
        return ""
    out, size = [], 0
    for s in re.split(r"(?<=[.!?])\s+", plain):
        s = s.strip()
        if not s:
            continue
        if out and size + len(s) > max_chars:
            break
        out.append(s)
        size += len(s) + 1
        if len(out) >= max_sentences:
            break
    got = " ".join(out)
    if len(got) > max_chars:
        got = got[: max_chars - 3].rstrip(" ,;:") + "..."
    return got


def describe_progress(p: Progress, limit: int = 4) -> str:
    """For the model: the steps so far in plain words, then the newest text."""
    phr = p.phrases
    lines = []
    if phr:
        shown = phr[-limit:]
        lines.append("Steps so far (newest last): " + "; ".join(shown) + ("" if len(phr) <= limit else f" (and {len(phr) - limit} earlier)") + ".")
    if p.current:
        lines.append(f"Right now it is {p.current}.")
    txt = summary_of(p.text, 3, 420)
    if txt:
        lines.append("Its latest words: " + txt)
    return "\n".join(lines) or "No steps yet."


# ── sending ───────────────────────────────────────────────────────────────

def clamp_wait(wait: Any, in_call: bool = False) -> float:
    try:
        w = float(wait)
    except (TypeError, ValueError):
        w = WAIT_CALL_DEFAULT_S if in_call else WAIT_DEFAULT_S
    if in_call:
        w = min(w, 15.0)
    return max(0.0, min(w, WAIT_MAX_S))


async def _state(chat: dict, after_ids: Optional[set] = None) -> Tuple[Progress, bool, str]:
    """(progress, busy now, what it waits on) from the server."""
    msgs = await get_messages(chat)
    prog = progress_of(msgs, chat.get("directory") or "", after_ids)
    busy = chat["id"] in await busy_ids()
    return prog, busy, await waiting_on(chat)


async def waiting_on(chat: dict) -> str:
    """A permission or question the chat is blocked on (the person has to answer it in opencode), or ""."""
    for path, noun in (("/permission", "permission"), ("/question", "answer")):
        try:
            rows = await _call("GET", path, params={"directory": chat.get("directory") or ""})
        except OpenCodeError:
            continue
        for r in rows or []:
            if isinstance(r, dict) and r.get("sessionID") == chat["id"]:
                what = _clean_phrase(r.get("permission") or "", 40) if noun == "permission" else ""
                return f"it is waiting for your {noun}" + (f" to use {what}" if what else "") + " in opencode"
    return ""


async def post_prompt(chat: dict, text: str) -> None:
    await _call("POST", f"/session/{chat['id']}/prompt_async", params={"directory": chat.get("directory") or ""},
                body={"parts": [{"type": "text", "text": text}]})


async def abort(chat: dict) -> bool:
    r = await _call("POST", f"/session/{chat['id']}/abort", params={"directory": chat.get("directory") or ""})
    return r is not False


async def send_and_wait(chat: dict, text: str, wait: float, *, owner_chat: str = "", in_call: bool = False) -> Dict:
    """Send `text` into the chat and wait up to `wait` seconds for the reply. Returns
    {"status": "done"|"working"|"sent", "was_busy", "progress", "waiting_on"}; a run still going gets a watcher."""
    before = await get_messages(chat)
    seen = {m["info"].get("id") for m in before}
    was_busy = chat["id"] in await busy_ids()
    await post_prompt(chat, text)
    deadline = time.monotonic() + wait
    prog = Progress()
    blocked = ""
    while True:
        await asyncio.sleep(min(POLL_S, max(0.0, deadline - time.monotonic())) if wait else 0)
        prog, busy, blocked = await _state(chat, seen)
        if prog.done and not busy:
            return {"status": "done", "was_busy": was_busy, "progress": prog, "waiting_on": ""}
        if blocked or time.monotonic() >= deadline:
            break
    status = "working" if (prog.user_id or was_busy) else "sent"
    result = {"status": status, "was_busy": was_busy, "progress": prog, "waiting_on": blocked}
    if owner_chat:
        start_watch(owner_chat, chat, seen, started_with=prog)
    return result


# ── bindings: which opencode chat an Odysseus chat talks to ──────────────

def _bind_file() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "opencode_chat_bindings.json")


def _load_bindings() -> Dict[str, dict]:
    try:
        with open(_bind_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _save_bindings(d: Dict[str, dict]) -> None:
    from core.atomic_io import atomic_write_json
    atomic_write_json(_bind_file(), d, indent=1)


def get_binding(owner_chat: str) -> Optional[dict]:
    with _lock:
        b = _load_bindings().get(owner_chat or "")
    return dict(b) if isinstance(b, dict) and b.get("id") else None


def bind(owner_chat: str, chat: dict, *, armed: bool) -> dict:
    """Make `chat` this Odysseus chat's default opencode chat. `armed` is the user's own pick (the call menu):
    sends to it then skip the per-message Approve for ARM_TTL_S. A bind made by the agent never arms."""
    now = time.time()
    with _lock:
        d = _load_bindings()
        old = d.get(owner_chat) or {}
        keep_arm = (not armed and old.get("id") == chat["id"] and now < float(old.get("armed_until") or 0))
        d[owner_chat] = {"id": chat["id"], "title": chat["title"], "directory": chat["directory"],
                         "folder": chat.get("folder") or _short_dir(chat["directory"]), "at": now,
                         "armed_until": (now + ARM_TTL_S) if armed else (float(old["armed_until"]) if keep_arm else 0)}
        for k in [k for k, v in d.items() if now - float(v.get("at") or 0) > 7 * 86400]:
            d.pop(k, None)
        _save_bindings(d)
        return dict(d[owner_chat])


def unbind(owner_chat: str) -> bool:
    with _lock:
        d = _load_bindings()
        if owner_chat not in d:
            return False
        d.pop(owner_chat)
        _save_bindings(d)
    return True


def is_armed(owner_chat: str, chat_id: str) -> bool:
    b = get_binding(owner_chat)
    return bool(b and b["id"] == chat_id and time.time() < float(b.get("armed_until") or 0))


# ── the call, and what the watcher says ──────────────────────────────────

_calls: Dict[str, float] = {}                 # Odysseus chat id -> last time a call page was heard from
_queues: Dict[str, List[dict]] = {}           # Odysseus chat id -> updates to speak
_seq = 0
_watchers: Dict[str, "asyncio.Task"] = {}     # opencode chat id -> its watcher
_touched: Dict[str, set] = {}                 # Odysseus chat id -> opencode chat ids it has sent into


def note_call(owner_chat: str) -> None:
    if owner_chat:
        _calls[owner_chat] = time.time()


def in_call(owner_chat: str) -> bool:
    return bool(owner_chat) and time.time() - _calls.get(owner_chat, 0.0) < CALL_ALIVE_S


def push(owner_chat: str, kind: str, text: str) -> dict:
    """Queue something for the call page to say. kind: "progress" or "done" (or "problem")."""
    global _seq
    with _lock:
        _seq += 1
        item = {"seq": _seq, "kind": kind, "text": text, "at": time.time()}
        q = _queues.setdefault(owner_chat, [])
        q.append(item)
        cut = time.time() - QUEUE_TTL_S
        _queues[owner_chat] = [i for i in q if i["at"] >= cut][-QUEUE_KEEP:]
    return item


def updates_after(owner_chat: str, after: int = 0) -> Tuple[List[dict], int]:
    """(the queued updates newer than `after`, the cursor to ask with next time)."""
    with _lock:
        if after > _seq:
            after = 0                   # the server restarted since the page last asked
        items = [dict(i) for i in _queues.get(owner_chat, []) if i["seq"] > after]
        return items, (items[-1]["seq"] if items else after)


def working_chats(owner_chat: str) -> List[str]:
    return [cid for cid, t in _watchers.items() if not t.done() and getattr(t, "owner_chat", "") == owner_chat]


def remember_sent(owner_chat: str, chat_id: str) -> None:
    _touched.setdefault(owner_chat, set()).add(chat_id)


def sent_before(owner_chat: str, chat_id: str) -> bool:
    return chat_id in _touched.get(owner_chat, set())


def update_line(new_phrases: List[str], current: str, first: bool) -> str:
    """One spoken progress line from what changed. Never code, never a command line."""
    if current:
        return (f"OpenCode is {current}." if first else f"Now {current}.")
    if new_phrases:
        return f"Just finished {new_phrases[-1]}."
    return ""


def final_line(title: str, p: Progress, blocked: str = "") -> Tuple[str, str]:
    """(kind, what to say) when a run ends: one or two sentences, plain words."""
    name = f"\"{title}\"" if title else "OpenCode"
    if p.error and "stopped" in p.error:
        return "done", f"OpenCode stopped in {name}."
    if p.error:
        return "problem", f"OpenCode hit a problem in {name}: {p.error}."
    said = re.sub(r"^(?:done|all done|finished)\b[.!:,]*\s*", "", summary_of(p.text), flags=re.I)
    if said:
        return "done", f"OpenCode finished in {name}. {said}"
    n = len(p.phrases)
    return "done", f"OpenCode finished in {name}" + (f" after {n} steps." if n > 1 else ".")


def _post_to_chat(owner_chat: str, text: str, title: str, kind: str) -> None:
    """Save the final summary in the Odysseus chat, so the next turn and a reload know what happened."""
    try:
        from src.ai_interaction import get_session_manager
        from core.models import ChatMessage
        sm = get_session_manager()
        if not sm:
            return
        sm.add_message(owner_chat, ChatMessage("assistant", text, metadata={
            "source": "opencode_chat_" + kind, "opencode_chat": title}))
        sm.save_sessions()
    except Exception:
        logger.warning("Could not post the OpenCode summary to chat %s", (owner_chat or "")[:8], exc_info=True)


def start_watch(owner_chat: str, chat: dict, seen: set, started_with: Optional[Progress] = None) -> None:
    """Follow a chat that is still working, in the background, until it finishes. One per opencode chat."""
    old = _watchers.get(chat["id"])
    if old and not old.done():
        old.cancel()
    try:
        task = asyncio.get_running_loop().create_task(_watch(owner_chat, chat, seen, started_with))
    except RuntimeError:
        return
    task.owner_chat = owner_chat            # type: ignore[attr-defined]
    _watchers[chat["id"]] = task


def stop_watch(chat_id: str) -> None:
    t = _watchers.pop(chat_id, None)
    if t and not t.done():
        t.cancel()


async def _watch(owner_chat: str, chat: dict, seen: set, prog0: Optional[Progress]) -> None:
    title = chat.get("title") or ""
    announced = set(pid for pid, _ in (prog0.steps if prog0 else []))
    last_say = 0.0
    said_any = False
    last_current = ""
    last_block = ""
    t0 = time.monotonic()
    last_change = t0
    last_sig = None
    try:
        while time.monotonic() - t0 < WATCH_MAX_S:
            await asyncio.sleep(WATCH_POLL_S)
            try:
                prog, busy, blocked = await _state(chat, seen)
            except OpenCodeError as e:
                if time.monotonic() - last_change > WATCH_IDLE_S:
                    push(owner_chat, "problem", f"I lost touch with OpenCode: {e}")
                    return
                continue
            sig = (len(prog.steps), prog.current, len(prog.text), prog.done, bool(blocked))
            if sig != last_sig:
                last_sig, last_change = sig, time.monotonic()
            if prog.done and not busy:
                kind, say = final_line(title, prog)
                _post_to_chat(owner_chat, say, title, kind)
                push(owner_chat, kind, say)
                return
            if blocked and last_block != blocked:
                last_block = blocked
                push(owner_chat, "progress", f"OpenCode is stuck: {blocked}.")
                last_say = time.monotonic()
                continue
            if not busy and time.monotonic() - last_change > WATCH_IDLE_S:
                # Idle, no sign of an end (aborted from the opencode side, or no reply ever came).
                msg = f"OpenCode went quiet in \"{title}\" without finishing."
                _post_to_chat(owner_chat, msg, title, "problem")
                push(owner_chat, "problem", msg)
                return
            fresh = [(pid, w) for pid, w in prog.steps if pid not in announced]
            if not fresh and not (prog.current and prog.current != last_current):
                continue
            if time.monotonic() - last_say < UPDATE_MIN_S or not in_call(owner_chat):
                continue
            line = update_line([w for _p, w in fresh], prog.current, first=not said_any)
            announced.update(pid for pid, _ in fresh)
            last_current = prog.current
            if line:
                push(owner_chat, "progress", line)
                said_any = True
                last_say = time.monotonic()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("OpenCode watcher for %s failed", chat.get("id"), exc_info=True)
    finally:
        if _watchers.get(chat["id"]) is asyncio.current_task():
            _watchers.pop(chat["id"], None)
