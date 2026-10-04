"""A read-only, cached view of a coder run's transcript file.

Each claude_code (or OpenCode, or Antigravity) run writes a ``stream-json``
transcript into its run directory, ``RUNS_DIR/<job_id>/out.jsonl`` (written by
``src/agent_tools/claude_code_tool.py``). Until now nothing has read that file
on its own terms: ``devops_stats._parse_run_log`` re-reads and re-parses every
run on every page load, with only a process-lifetime memory cache, which is
wiped when the server restarts -- hence "first listing after restart is slow."

This module is the read-only viewer's backend:

  * reads only the single ``out.jsonl`` file in a run directory. It does not
    look inside ``subagents/`` or any sibling (PR 1 scope: "no subagents/*.jsonl"),
  * parses the transcript into counts of messages, tool calls, tool results,
    turns and thinking blocks, plus the structured content needed to render a
    detail view,
  * persists an on-disk summary keyed by the file's (size, mtime) so the first
    listing after a restart serves from cache instead of re-parsing,
  * ``resume_command()`` returns the exact CLI command a user could type to
    continue the session. Nothing in this module runs it or offers a resume
    button: PR 1 is read-only.

Cache layout follows ``services/search/cache.py`` (SHA-256 key, LRU cap, TTL)
but in its own ``DATA_DIR`` subdirectory, so a search-cache cleanup cannot
evict it.
"""

import hashlib
import json
import logging
import os
import time
from typing import Dict, List, Optional

from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

# ---- Rendering "safe caps" -----------------------------------------------
# A thinking/reasoning blob can be arbitrarily long (a whole file read back as
# text, a long reasoning chain). The detail view must not dump that verbatim
# into the DOM, so each of the following bounds what a summary carries. Named
# constants, not literals at each call site (CONTRIBUTING.md: "use named
# constants").
MAX_THINKING_CHARS = 4000     # per thinking block; a note marks the omission
MAX_TEXT_CHARS = 8000         # per assistant/user text block
MAX_TOOL_OUTPUT_CHARS = 2000  # per tool call's input hint and result output
MAX_BLOCKS = 600              # total blocks in the per-run detail payload
MAX_PREVIEW_CHARS = 240       # the one-line preview on the list rows

# ---- Persistent cache ------------------------------------------------------
SUMMARY_CACHE_DIR = os.path.join(DATA_DIR, "cache", "transcript_summaries")
SUMMARY_CACHE_MAX_ENTRIES = 400
SUMMARY_CACHE_TTL_S = 30 * 24 * 3600
_CACHE_VERSION = 1


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _clip(text: str, n: int) -> tuple:
    """(clipped_text, was_clipped) so a reader can tell what is not the whole."""
    text = str(text or "").replace("\r", " ")
    if len(text) <= n:
        return text, False
    return text[:n], True


def _result_text(content) -> str:
    """A Claude ``tool_result``'s content: a string, or a list of text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("text"):
                parts.append(str(c["text"]))
        return "\n".join(parts)
    return ""


def _events(path: str) -> List[Dict]:
    """Every parseable JSON object on its own line, in file order. Blank and
    unparseable lines are skipped -- the CLI writes a clean stream, but a
    crash mid-write is exactly when the viewer is most wanted."""
    out: List[Dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError as err:
        logger.debug("transcript read failed: %s", err)
    return out


def _detect_engine(events: List[Dict]) -> str:
    """Which CLI wrote the transcript, from its own first lines."""
    for e in events[:8]:
        if e.get("event"):
            return "antigravity"
        if "sessionID" in e or e.get("type") in ("step_start", "step_finish", "step_update"):
            return "opencode"
        if e.get("type") in ("system", "assistant", "user") or (
                e.get("type") == "result" and isinstance(e.get("is_error"), bool)):
            return "claude"
        if e.get("type") in ("text", "tool", "error"):
            return "opencode"
    return "claude"


# ---- Per-engine block builders --------------------------------------------

def _build_claude(events: List[Dict], meta: Dict) -> List[Dict]:
    """Claude Code: ``assistant.message.content[]`` holds text | thinking |
    tool_use blocks; ``user`` lines carry the matching tool_results; one
    terminal ``result`` line holds status, cost, tokens, ``num_turns``."""
    blocks: List[Dict] = []
    tool_results: Dict[str, Dict] = {}
    assistant_ids = set()
    turns = 0
    final_text = ""
    is_error = False
    last_assistant_msg = ""

    for e in events:
        ts = _num(e.get("timestamp"))
        if ts:
            ts /= 1000.0
            if meta["first"] is None:
                meta["first"] = ts
            meta["last"] = ts
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "init":
            meta["model"] = meta.get("model") or str(e.get("model") or "")
            if not meta["session_id"]:
                meta["session_id"] = str(e.get("session_id") or "")
        elif kind == "assistant":
            msg = e.get("message") or {}
            if msg.get("id"):
                assistant_ids.add(str(msg["id"]))
            if msg.get("model") and not meta["model"]:
                meta["model"] = str(msg["model"])
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    text, clipped = _clip(b.get("thinking") or "", MAX_THINKING_CHARS)
                    blocks.append({"kind": "thinking", "text": text, "clipped": clipped,
                                   "full_len": len(str(b.get("thinking") or ""))})
                elif bt == "text":
                    text = str(b.get("text") or "").strip()
                    if text:
                        last_assistant_msg = text
                        text_c, clipped = _clip(text, MAX_TEXT_CHARS)
                        blocks.append({"kind": "text", "text": text_c, "clipped": clipped})
                elif bt == "tool_use":
                    name = str(b.get("name") or "tool")
                    inp = b.get("input") or {}
                    hint = ""
                    if isinstance(inp, dict):
                        for k in ("file_path", "path", "command", "pattern", "url",
                                  "query", "description", "prompt", "notebook_path"):
                            if inp.get(k):
                                hint = str(inp[k]); break
                        if not hint:
                            try:
                                hint = json.dumps(inp, ensure_ascii=False)
                            except (TypeError, ValueError):
                                hint = str(inp)
                    hint_c, _ = _clip(hint, MAX_TOOL_OUTPUT_CHARS)
                    blocks.append({"kind": "tool_call", "name": name, "hint": hint_c,
                                   "tool_id": str(b.get("id") or ""), "output": "",
                                   "tool_result_count": 0, "error": False})
        elif kind == "user":
            content = (e.get("message") or {}).get("content") or []
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tool_results[str(b.get("tool_use_id") or "")] = {
                            "text": _clip(_result_text(b.get("content")), MAX_TOOL_OUTPUT_CHARS)[0],
                            "error": bool(b.get("is_error")),
                        }
        elif kind == "result":
            is_error = bool(e.get("is_error"))
            final_text = str(e.get("result") or "")
            meta["cost_usd"] = _num(e.get("total_cost_usd")) or 0.0
            usage = e.get("usage") or {}
            meta["output_tokens"] = int(_num(usage.get("output_tokens")) or 0)
            dur = _num(e.get("duration_ms"))
            if dur:
                meta["duration_s"] = dur / 1000.0
            if isinstance(e.get("num_turns"), int):
                turns = max(turns, e["num_turns"])

    # Attach results to their calls by Claude's stable tool_use id, and count
    # results per call (the same tool may legitimately be called more than
    # once, but only the last result is kept here for display).
    by_id: Dict[str, Dict] = {}
    for b in blocks:
        if b.get("kind") == "tool_call":
            by_id.setdefault(b["tool_id"], b)
    for tid, r in tool_results.items():
        target = by_id.get(tid)
        if target is not None:
            target["output"] = r["text"]
            target["error"] = r["error"]
            target["tool_result_count"] += 1

    if not turns:
        # No ``result`` line (server died mid-run). The number of distinct
        # assistant messages is the best available turn count.
        turns = len(assistant_ids)
    meta.update({
        "status": ("failed" if is_error else "done") if events else "cut off",
        "is_error": is_error,
        "final_text": final_text or last_assistant_msg,
        "turns": turns,
    })
    return blocks


def _build_opencode(events: List[Dict], meta: Dict) -> List[Dict]:
    """OpenCode ``run --format json``: flat ``{type, sessionID, part}`` lines
    with text under ``part.text``, tool calls under ``part.tool``/``part.state``,
    token usage in ``step_finish.part.tokens``."""
    blocks: List[Dict] = []
    steps = 0
    last_reason = ""
    cost = 0.0
    out_tokens = 0.0
    reasoning_tokens = 0
    saw_error = False

    for e in events:
        ts = _num(e.get("timestamp"))
        if ts:
            ts /= 1000.0
            if meta["first"] is None:
                meta["first"] = ts
            meta["last"] = ts
        if not meta["session_id"] and e.get("sessionID"):
            meta["session_id"] = str(e["sessionID"])
        et = e.get("type")
        part = e.get("part") or {}
        if et == "step_start":
            steps += 1
        elif et == "step_finish":
            try:
                cost += float(part.get("cost") or 0)
            except (TypeError, ValueError):
                pass
            if part.get("reason"):
                last_reason = str(part["reason"])
            tokens = part.get("tokens") or {}
            reasoning = int(_num(tokens.get("reasoning")) or 0)
            output = int(_num(tokens.get("output")) or 0)
            out_tokens += output
            reasoning_tokens += reasoning
            if reasoning:
                blocks.append({"kind": "thinking", "text": f"[reasoning: {reasoning} tokens]",
                               "clipped": False, "full_len": reasoning})
        elif et == "text":
            text = str(part.get("text") or "").strip()
            if text:
                text_c, clipped = _clip(text, MAX_TEXT_CHARS)
                blocks.append({"kind": "text", "text": text_c, "clipped": clipped})
        elif et == "tool":
            state = part.get("state") or {}
            name = str(part.get("tool") or "tool")
            inp = state.get("input") or {}
            hint = ""
            if isinstance(inp, dict):
                for k in ("filePath", "path", "command", "pattern"):
                    if inp.get(k):
                        hint = str(inp[k]); break
            out = state.get("output") or state.get("error") or ""
            if not isinstance(out, str):
                try:
                    out = json.dumps(out, ensure_ascii=False)
                except (TypeError, ValueError):
                    out = str(out)
            err = bool(state.get("is_error")) or state.get("status") == "error"
            hint_c, _ = _clip(hint, MAX_TOOL_OUTPUT_CHARS)
            out_c, _ = _clip(out, MAX_TOOL_OUTPUT_CHARS)
            blocks.append({"kind": "tool_call", "name": name, "hint": hint_c,
                           "tool_id": "", "output": out_c,
                           "tool_result_count": 1 if (out_c or err) else 0,
                           "error": err})
    meta.update({
        "reasoning_tokens": reasoning_tokens,
        "status": ("done" if last_reason == "stop"
                   else "failed" if last_reason in ("error", "aborted")
                   else "cut off" if events else ""),
        "is_error": last_reason in ("error", "aborted"),
        "turns": steps,
        "cost_usd": cost,
        "output_tokens": out_tokens,
        "final_text": next((b["text"] for b in reversed(blocks) if b.get("kind") == "text"), ""),
    })
    return blocks


def _build_antigravity(events: List[Dict], meta: Dict) -> List[Dict]:
    """Antigravity ``--output-format stream-json``: ``{"event", "<event>": …}``
    lines; agent text arrives as ``text_delta`` updates on ``step_update``
    lines, one step per agent_response. A tool step is a ``tool`` step_type."""
    blocks: List[Dict] = []
    agent_idx: int = 0
    turns = 0
    out_tokens = 0
    duration = None
    final_text = ""
    is_error = False
    saw_result = False
    result_ok = False
    acc: Dict[int, Dict] = {}

    for e in events:
        if not meta["session_id"]:
            meta["session_id"] = str(e.get("conversation_id") or "")
        ts = _num(e.get("timestamp"))
        if ts:
            ts /= 1000.0
            if meta["first"] is None:
                meta["first"] = ts
            meta["last"] = ts
        ev = e.get("event")
        body = e.get(ev) if isinstance(e.get(ev), dict) else {}
        if ev == "step_update":
            stype = str(body.get("step_type") or "")
            idx = body.get("step_index")
            state = str(body.get("state") or "").upper()
            if stype == "agent_response":
                slot = acc.setdefault(idx if idx is not None else agent_idx,
                                      {"text": "", "thinking": 0, "done": False,
                                       "emitted": False})
                if body.get("text_delta"):
                    slot["text"] = slot.get("text", "") + str(body["text_delta"])
                usage = body.get("usage") or {}
                slot["thinking"] = int(usage.get("thinking_tokens") or slot.get("thinking") or 0)
                if state in ("DONE", "ERROR"):
                    slot["done"] = True
                    if idx in acc and not slot["emitted"]:
                        slot["emitted"] = True
                        turns += 1
                        th = int(slot.get("thinking") or 0)
                        if th:
                            blocks.append({"kind": "thinking",
                                           "text": f"[reasoning: {th} tokens]",
                                           "clipped": False, "full_len": th})
                        text = str(slot.get("text") or "").strip()
                        if text:
                            text_c, clipped = _clip(text, MAX_TEXT_CHARS)
                            blocks.append({"kind": "text", "text": text_c, "clipped": clipped})
                            final_text = text
            elif stype == "tool":
                info = body.get("tool_info") if isinstance(body.get("tool_info"), dict) else {}
                name = str(body.get("tool_name") or info.get("name") or "tool")
                params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
                hint = ""
                for k in ("CommandLine", "Command", "AbsolutePath", "TargetFile",
                          "Path", "SearchPath", "Query", "command", "path", "query", "url"):
                    if params.get(k):
                        hint = str(params[k]); break
                out = str(info.get("output") or "")
                err = state == "ERROR" or str(body.get("state") or "").lower() == "error"
                hint_c, _ = _clip(hint, MAX_TOOL_OUTPUT_CHARS)
                out_c, _ = _clip(out, MAX_TOOL_OUTPUT_CHARS)
                blocks.append({"kind": "tool_call", "name": name, "hint": hint_c,
                               "tool_id": "", "output": out_c,
                               "tool_result_count": 1 if (out_c or err) else 0,
                               "error": err})
        elif ev == "result":
            st = str(body.get("status") or "").upper()
            saw_result = True
            result_ok = st == "SUCCESS"
            is_error = not result_ok
            final_text = str(body.get("response") or "") or final_text
            usage = body.get("usage") or {}
            out_tokens = int(_num(usage.get("output_tokens")) or 0)
            duration = _num(body.get("duration_seconds"))
            if isinstance(body.get("num_turns"), int) and body["num_turns"]:
                turns = max(turns, body["num_turns"])
    meta.update({
        "status": "done" if saw_result and result_ok else
                  "failed" if saw_result or is_error else
                  "cut off" if events else "",
        "is_error": is_error,
        "turns": turns,
        "output_tokens": out_tokens,
        "duration_s": duration,
        "final_text": final_text,
    })
    return blocks


_BUILDERS = {"claude": _build_claude, "opencode": _build_opencode,
             "antigravity": _build_antigravity}


# ---- Public entry points --------------------------------------------------

def parse_run(run_dir: str) -> Optional[Dict]:
    """Parse ``<run_dir>/out.jsonl`` into a structured summary.

    Returns ``None`` when there is no transcript (a run directory that never
    started) or it cannot be read. Never raises -- the route layer treats a
    missing summary as "no detail available for that row."

    Only the single ``out.jsonl`` file is read. A sibling ``status.jsonl``
    (progress), ``prompt.txt``, ``err.log``, or a ``subagents/`` tree is not;
    the scope of PR 1 is the main transcript, read-only.
    """
    path = os.path.join(run_dir, "out.jsonl")
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if size == 0:
        return None

    events = _events(path)
    if not events:
        return None

    engine = _detect_engine(events)
    builder = _BUILDERS.get(engine, _build_claude)

    meta: Dict = {
        "engine": engine, "model": "", "session_id": "",
        "first": None, "last": mtime,
        "status": "", "is_error": False, "final_text": "",
        "cost_usd": 0.0, "output_tokens": 0, "reasoning_tokens": 0,
        "duration_s": None, "turns": 0,
    }
    try:
        blocks = builder(events, meta)
    except Exception as e:
        logger.debug("transcript parse %s failed: %s", run_dir, e)
        return None

    truncated = len(blocks) > MAX_BLOCKS
    if truncated:
        blocks = blocks[-MAX_BLOCKS:]

    if meta["first"] is None:
        try:
            meta["first"] = os.path.getctime(path)
        except OSError:
            meta["first"] = mtime

    # Counts are derived from the blocks, not guessed at length.
    n_text = sum(1 for b in blocks if b.get("kind") == "text")
    n_tool = sum(1 for b in blocks if b.get("kind") == "tool_call")
    n_result = sum(1 for b in blocks if b.get("kind") == "tool_call" and b.get("tool_result_count"))
    n_think = sum(1 for b in blocks if b.get("kind") == "thinking")
    counts = {"messages": n_text, "tool_calls": n_tool, "results": n_result,
              "thinking_blocks": n_think,
              "turns": meta.get("turns") or (n_text + n_tool)}

    preview = next((b.get("text") or "" for b in blocks if b.get("kind") == "text"), "")
    preview, _ = _clip(preview, MAX_PREVIEW_CHARS)

    return {
        "engine": engine,
        "model": meta.get("model") or "",
        "session_id": meta.get("session_id") or "",
        "status": meta.get("status") or "",
        "is_error": bool(meta.get("is_error")),
        "final_text": meta.get("final_text") or "",
        "first": meta.get("first"),
        "last": meta.get("last"),
        "duration_s": meta.get("duration_s"),
        "cost_usd": meta.get("cost_usd") or 0.0,
        "output_tokens": meta.get("output_tokens") or 0,
        "reasoning_tokens": meta.get("reasoning_tokens") or 0,
        "counts": counts,
        "preview": preview,
        "truncated": truncated,
        "blocks": blocks,
        "resume_command": resume_command(engine, meta.get("session_id") or ""),
    }


def _resume_cmd(engine: str, session_id: str) -> str:
    """The CLI command a user could type by hand to continue the session.

    Display-only: this module -- and PR 1 -- never runs it and never offers a
    resume button. The strings match what ``src/agent_tools/claude_code_tool.py``
    composes for its own resume calls (the user must add a new prompt
    themselves when running these in a real shell; they are the same flags the
    app already uses, so copy-paste lands exactly where the app would have).

      Claude:      ``claude --resume <session_id>``
      OpenCode:    ``opencode --session <session_id>``
      Antigravity: ``agy --conversation <session_id>``
    """
    if not session_id:
        return ""
    engine = (engine or "").lower()
    if engine == "claude":
        return f"claude --resume {session_id}"
    if engine == "opencode":
        return f"opencode --session {session_id}"
    if engine == "antigravity":
        return f"agy --conversation {session_id}"
    return ""


def resume_command(engine: str, session_id: str) -> str:
    """Public alias; the dict key in the summary is spelled ``resume_command``."""
    return _resume_cmd(engine, session_id)


# ---- Persistence ----------------------------------------------------------
# A process-lifetime cache is what devops_stats has today. It is why the first
# listing after a restart is slow: the moment the old process is gone the new
# one re-reads every ``out.jsonl``. Caching the summary on disk, keyed by the
# file's (size, mtime), turns that first call into a file read instead of a
# full parse.

def _summary_key(run_dir: str, size: int, mtime: float) -> str:
    raw = f"{_CACHE_VERSION}|{os.path.realpath(run_dir)}|{size}|{int(mtime * 1000)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> str:
    try:
        os.makedirs(SUMMARY_CACHE_DIR, exist_ok=True)
    except OSError as e:
        logger.warning("transcript summary cache unavailable: %s", e)
        return ""
    return os.path.join(SUMMARY_CACHE_DIR, f"{key}.summary.json")


def _evict(max_entries: int = SUMMARY_CACHE_MAX_ENTRIES,
           ttl_s: int = SUMMARY_CACHE_TTL_S) -> None:
    """LRU+TTL over the on-disk summaries. Mirrors the shape of
    services/search/cache.py:cleanup_cache; never raises -- the cache is a
    convenience and a broken directory must not take the viewer with it."""
    try:
        names = os.listdir(SUMMARY_CACHE_DIR)
    except OSError:
        return
    now = time.time()
    rows = []
    for name in names:
        if not name.endswith(".summary.json"):
            continue
        full = os.path.join(SUMMARY_CACHE_DIR, name)
        try:
            rows.append((os.path.getmtime(full), full))
        except OSError:
            continue
    rows.sort(reverse=True)
    fresh = [r for r in rows if now - r[0] < ttl_s]
    keep_count = min(len(fresh), max_entries)
    for _, drop in fresh[keep_count:] + rows[len(fresh):]:
        try:
            os.unlink(drop)
        except OSError:
            pass


def _write_summary(path: str, data: Dict) -> None:
    if not path:
        return
    atomic_write_json(path, {"_version": _CACHE_VERSION, **data})


def _read_summary(path: str) -> Optional[Dict]:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("_version") != _CACHE_VERSION:
        return None
    d.pop("_version", None)
    return d


def _sum_stat(run_dir: str) -> Optional[tuple]:
    path = os.path.join(run_dir, "out.jsonl")
    try:
        return os.path.getsize(path), os.path.getmtime(path)
    except OSError:
        return None


def get_summary(run_dir: str) -> Optional[Dict]:
    """The cached view of a run; parses only when the on-disk cache is cold.

    Returns ``None`` when the run directory has no transcript yet, or the
    summary cannot be produced. The caller (a route) treats that as "no detail
    available" for that row.
    """
    stat = _sum_stat(run_dir)
    if stat is None:
        return None
    size, mtime = stat
    if size == 0:
        return None
    key = _summary_key(run_dir, size, mtime)
    cp = _cache_path(key)
    if cp:
        cached = _read_summary(cp)
        if cached is not None:
            return cached
    parsed = parse_run(run_dir)
    if parsed is None:
        return None
    if cp:
        try:
            _write_summary(cp, parsed)
        except OSError as e:
            logger.debug("transcript summary write failed: %s", e)
    # Prune lazily; the cost is one listdir and it is amortised over many
    # summary reads.
    try:
        _evict()
    except Exception:
        logger.exception("transcript summary eviction failed")
    return parsed


def invalidate(run_dir: str) -> None:
    """Drop the cached summary for a run directory, if any. Kept for tests
    and for a future "transcripts: refresh" admin action."""
    stat = _sum_stat(run_dir)
    if stat is None:
        return
    key = _summary_key(run_dir, *stat)
    cp = _cache_path(key)
    if cp and os.path.isfile(cp):
        try:
            os.unlink(cp)
        except OSError:
            pass


def clear_cache() -> int:
    """Wipe the on-disk summary cache. Returns the number of files removed."""
    removed = 0
    try:
        for name in os.listdir(SUMMARY_CACHE_DIR):
            if name.endswith(".summary.json"):
                try:
                    os.unlink(os.path.join(SUMMARY_CACHE_DIR, name))
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed
