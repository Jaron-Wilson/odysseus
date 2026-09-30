"""The DevOps page: what is running, how fast the models are, which coder does best.

Asked for on 2026-09-29: "can we get a devops page, ie: show everything
happening, how many tokens/s or tokens/h etc average speed, most liked
coder, etc."

- Chat speed comes from each reply's own record (chat_messages.metadata:
  output_tokens, tokens_per_second, time_to_first_token, response_time,
  model), so it covers every reply the server has kept.
- Coder runs come from claude_code_history.jsonl (written when a run
  finishes) and, for runs from before that file existed, from each run's
  own log in claude_code_runs/ (a Claude run ends with a "result" line, an
  OpenCode run with "step_finish" lines).
- There is no like button, so "most liked" is the coder picked most, and
  "most reliable" the one whose runs finish OK most often.
"""
import json
import logging
import os
import statistics
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

WINDOWS_H = (1, 24, 24 * 7, 24 * 30)
MIN_RUNS_TO_RANK = 3
_LOG_CACHE: Dict[str, tuple] = {}          # run dir -> (mtime, parsed)


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _ts(v) -> float:
    """A chat_messages timestamp (naive UTC) as epoch seconds."""
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v).replace("Z", ""))
        except ValueError:
            return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


# ---- chat replies -------------------------------------------------------------

def _replies(since: float, owner: str = "") -> List[Dict]:
    from sqlalchemy import text
    from core.database import engine
    cutoff = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = ("SELECT m.metadata, m.timestamp, m.session_id, s.name FROM chat_messages m "
           "LEFT JOIN sessions s ON s.id = m.session_id "
           "WHERE m.role = 'assistant' AND m.timestamp >= :cutoff")
    params = {"cutoff": cutoff}
    if owner:
        sql += " AND s.owner = :owner"
        params["owner"] = owner
    out = []
    with engine.connect() as conn:
        for meta, ts, sid, name in conn.execute(text(sql), params):
            try:
                d = json.loads(meta) if meta else {}
            except ValueError:
                continue
            if not isinstance(d, dict) or not d.get("output_tokens"):
                continue
            out.append({
                "t": _ts(ts), "session_id": sid, "chat": name or "",
                "model": str(d.get("model") or d.get("requested_model") or "unknown"),
                "out": _num(d.get("output_tokens")) or 0.0,
                "in": _num(d.get("input_tokens")) or 0.0,
                "tps": _num(d.get("tokens_per_second")),
                "ttft": _num(d.get("time_to_first_token")),
                "secs": _num(d.get("response_time")),
            })
    return out


def _speed(rows: List[Dict]) -> Optional[float]:
    """Average generation speed, weighted by tokens: all tokens over all the
    time spent writing them, so one long reply counts for what it is."""
    toks = secs = 0.0
    for r in rows:
        if r["tps"] and r["tps"] > 0 and r["out"]:
            toks += r["out"]
            secs += r["out"] / r["tps"]
    return round(toks / secs, 1) if secs else None


def _mean(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(statistics.fmean(vals), 2) if vals else None


def chat_stats(hours: float, owner: str = "", now: Optional[float] = None) -> Dict:
    now = now or time.time()
    rows = _replies(now - hours * 3600, owner)
    out_tokens = sum(r["out"] for r in rows)
    last_hour = sum(r["out"] for r in rows if r["t"] >= now - 3600)
    by_model: Dict[str, List[Dict]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    models = sorted(({
        "model": m, "replies": len(rs), "output_tokens": int(sum(r["out"] for r in rs)),
        "tokens_per_s": _speed(rs), "median_tokens_per_s": (
            round(statistics.median([r["tps"] for r in rs if r["tps"]]), 1)
            if any(r["tps"] for r in rs) else None),
        "first_token_s": _mean(r["ttft"] for r in rs),
        "reply_s": _mean(r["secs"] for r in rs),
    } for m, rs in by_model.items()), key=lambda x: -x["output_tokens"])
    ranked = [m for m in models if m["replies"] >= MIN_RUNS_TO_RANK and m["tokens_per_s"]]
    # Buckets for the chart: hours for a day or two, days beyond that.
    step = 3600 if hours <= 48 else 86400
    start = now - hours * 3600
    n = max(1, int(round(hours * 3600 / step)))
    buckets = [{"t": start + i * step, "output_tokens": 0, "replies": 0, "_rows": []} for i in range(n)]
    for r in rows:
        i = min(n - 1, max(0, int((r["t"] - start) // step)))
        b = buckets[i]
        b["output_tokens"] += int(r["out"])
        b["replies"] += 1
        b["_rows"].append(r)
    for b in buckets:
        b["tokens_per_s"] = _speed(b.pop("_rows"))
    return {
        "hours": hours, "replies": len(rows), "output_tokens": int(out_tokens),
        "input_tokens": int(sum(r["in"] for r in rows)),
        "tokens_per_hour": round(out_tokens / hours) if hours else None,
        "tokens_last_hour": int(last_hour),
        "tokens_per_s": _speed(rows),
        "first_token_s": _mean(r["ttft"] for r in rows),
        "reply_s": _mean(r["secs"] for r in rows),
        "models": models,
        "fastest_model": max(ranked, key=lambda m: m["tokens_per_s"])["model"] if ranked else None,
        "busiest_model": models[0]["model"] if models else None,
        "bucket_s": step, "series": buckets,
    }


# ---- coder runs ---------------------------------------------------------------

def _parse_run_log(run_dir: str) -> Dict:
    """What a run's own log says: engine, model, outcome, tokens, cost."""
    path = os.path.join(run_dir, "out.jsonl")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _LOG_CACHE.get(run_dir)
    if hit and hit[0] == mtime:
        return hit[1]
    info: Dict = {"engine": "", "model": "", "status": "", "out": 0, "cost": 0.0,
                  "first": None, "last": mtime}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(j, dict):
                    continue
                kind = j.get("type")
                stamp = _num(j.get("timestamp"))
                if stamp:
                    stamp /= 1000.0
                    info["first"] = info["first"] or stamp
                    info["last"] = stamp
                if j.get("event") in ("init", "step_update", "result") and not kind:
                    # Antigravity (agy --output-format stream-json).
                    info["engine"] = "antigravity"
                    body = j.get(j["event"]) if isinstance(j.get(j["event"]), dict) else {}
                    if j["event"] == "result":
                        info["status"] = "done" if str(body.get("status") or "").upper() == "SUCCESS" else "failed"
                        info["out"] = int(_num((body.get("usage") or {}).get("output_tokens")) or 0)
                        if _num(body.get("duration_seconds")):
                            info["secs"] = _num(body.get("duration_seconds"))
                    continue
                if kind == "system" and j.get("subtype") == "init":
                    info["engine"] = "claude"
                    info["model"] = j.get("model") or info["model"]
                elif kind == "result":
                    info["engine"] = "claude"
                    info["status"] = "failed" if j.get("is_error") else "done"
                    info["cost"] = _num(j.get("total_cost_usd")) or 0.0
                    u = j.get("usage") or {}
                    info["out"] = int(_num(u.get("output_tokens")) or 0)
                    dur = _num(j.get("duration_ms"))
                    if dur:
                        info["secs"] = dur / 1000.0
                elif kind in ("step_start", "step_finish", "tool_use", "text"):
                    info["engine"] = info["engine"] or "opencode"
                    if kind == "step_finish":
                        part = j.get("part") or {}
                        tok = part.get("tokens") or {}
                        info["out"] += int(_num(tok.get("output")) or 0) + int(_num(tok.get("reasoning")) or 0)
                        info["cost"] += _num(part.get("cost")) or 0.0
                        info["status"] = "done" if part.get("reason") == "stop" else ""
                elif kind == "error":
                    info["engine"] = info["engine"] or "opencode"
                    info["status"] = "failed"
    except OSError:
        return {}
    if not info["first"]:
        # A Claude log has no timestamps: its length says when it began.
        info["first"] = (info["last"] - info["secs"]) if info.get("secs") else os.path.getctime(path)
    _LOG_CACHE[run_dir] = (mtime, info)
    return info


def _history() -> Dict[str, Dict]:
    from src import claude_code_jobs as jobs
    out: Dict[str, Dict] = {}
    try:
        with open(jobs.HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    out[r["id"]] = r
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


def coder_runs(since: float, owner: str = "") -> List[Dict]:
    """Every coder run started since `since`, newest first."""
    from src import claude_code_jobs as jobs
    hist = _history()
    live = {j.id: j for j in jobs.list_jobs("")}
    ids = set(hist) | set(live)
    try:
        ids |= {d for d in os.listdir(jobs.RUNS_DIR) if os.path.isdir(os.path.join(jobs.RUNS_DIR, d))}
    except OSError:
        pass
    runs = []
    for rid in ids:
        h = hist.get(rid) or {}
        j = live.get(rid)
        log = _parse_run_log(os.path.join(jobs.RUNS_DIR, rid))
        if not h and not j and not log.get("engine"):
            continue                               # an empty log: it never started
        started = (j.started if j else None) or _num(h.get("started")) or log.get("first")
        if not started or started < since:
            continue
        run_owner = (j.owner if j else "") or h.get("owner") or ""
        if owner and run_owner and run_owner != owner:
            continue
        status = (j.status if j else "") or h.get("status") or log.get("status") or "cut off"
        finished = (j.finished if j else None) or _num(h.get("finished"))
        if status == "running":
            finished = None
        elif not finished:
            finished = log.get("last")
        secs = log.get("secs") or ((finished - started) if finished else time.time() - started)
        engine = (j.engine if j else "") or h.get("engine") or log.get("engine") or "claude"
        model = (j.model if j else "") or h.get("model") or log.get("model") or ""
        runs.append({
            "id": rid, "engine": engine, "model": model,
            "action": (j.action if j else "") or h.get("action") or "",
            "status": status, "started": started, "finished": finished,
            "seconds": round(max(0.0, secs), 1), "output_tokens": int(log.get("out") or 0),
            "cost_usd": round(log.get("cost") or 0.0, 4),
            "chat_session_id": (j.chat_session_id if j else "") or h.get("chat_session_id") or "",
        })
    runs.sort(key=lambda r: -r["started"])
    return runs


ENGINE_NAMES = {"claude": "Claude Code", "opencode": "OpenCode", "antigravity": "Antigravity", "codex": "Codex"}


def coder_stats(hours: float, owner: str = "", now: Optional[float] = None) -> Dict:
    now = now or time.time()
    runs = coder_runs(now - hours * 3600, owner)
    groups: Dict[tuple, List[Dict]] = {}
    for r in runs:
        groups.setdefault((r["engine"], r["model"]), []).append(r)
    coders = []
    for (eng, model), rs in groups.items():
        ended = [r for r in rs if r["status"] != "running"]
        ok = [r for r in ended if r["status"] == "done"]
        busy = sum(r["seconds"] for r in ended)
        toks = sum(r["output_tokens"] for r in ended)
        coders.append({
            "engine": eng, "name": ENGINE_NAMES.get(eng, eng or "?"), "model": model,
            "runs": len(rs), "running": len(rs) - len(ended), "done": len(ok),
            "failed": sum(1 for r in ended if r["status"] in ("failed", "timed_out")),
            "cut_off": sum(1 for r in ended if r["status"] in ("stopped", "cut off")),
            "success_rate": round(len(ok) / len(ended), 3) if ended else None,
            "avg_minutes": round(busy / len(ended) / 60, 1) if ended else None,
            "output_tokens": toks, "tokens_per_s": round(toks / busy, 1) if busy and toks else None,
            "cost_usd": round(sum(r["cost_usd"] for r in rs), 2),
        })
    coders.sort(key=lambda c: (-c["runs"], c["name"]))
    ranked = [c for c in coders if c["success_rate"] is not None and c["runs"] - c["running"] >= MIN_RUNS_TO_RANK]
    best = max(ranked, key=lambda c: (c["success_rate"], c["runs"])) if ranked else None
    return {
        "runs": len(runs), "coders": coders,
        "favorite": coders[0] if coders else None,
        "most_reliable": best,
        "recent": runs[:15],
    }


# ---- happening now ------------------------------------------------------------

def _chat_names(ids: List[str]) -> Dict[str, str]:
    if not ids:
        return {}
    try:
        from sqlalchemy import bindparam, text
        from core.database import engine
        q = text("SELECT id, name FROM sessions WHERE id IN :ids").bindparams(bindparam("ids", expanding=True))
        with engine.connect() as conn:
            return {i: n or "" for i, n in conn.execute(q, {"ids": list(ids)})}
    except Exception:
        return {}


def live(now: Optional[float] = None) -> Dict:
    now = now or time.time()
    from src import agent_runs, chat_queue
    from src import claude_code_jobs as jobs
    replying = [(sid, agent_runs._RUNS[sid]) for sid in agent_runs.active_sessions() if sid in agent_runs._RUNS]
    coder_jobs = [j for j in jobs.list_jobs("") if j.status == "running"]
    shells: List[Dict] = []
    try:
        from src import bg_jobs
        shells = [r for r in bg_jobs.refresh().values() if r.get("status") == "running"]
    except Exception:
        pass
    queued = 0
    try:
        queued = sum(len(e.get("items") or []) for e in chat_queue._load().values())
    except Exception:
        pass
    names = _chat_names([sid for sid, _ in replying] + [j.chat_session_id for j in coder_jobs]
                        + [r.get("session_id") or "" for r in shells])
    return {
        "replies": [{"session_id": sid, "chat": names.get(sid, ""),
                     "seconds": round(now - getattr(r, "started", now)),
                     "watchers": len(r.subscribers)} for sid, r in replying],
        "coders": [{"id": j.id, "engine": j.engine, "name": ENGINE_NAMES.get(j.engine, j.engine),
                    "model": j.model, "action": j.action, "session_id": j.chat_session_id,
                    "chat": names.get(j.chat_session_id, ""), "seconds": round(now - j.started),
                    "prompt": jobs.display_prompt(j.prompt)[:160],
                    "status": (jobs.shown_status(j.status, j.agent_status) or {}).get("detail", "")}
                   for j in coder_jobs],
        "shell_jobs": [{"id": r.get("id") or "", "command": str(r.get("command") or "")[:160],
                        "session_id": r.get("session_id") or "", "chat": names.get(r.get("session_id") or "", ""),
                        "seconds": round(now - (_num(r.get("started_at")) or now))} for r in shells],
        "queued_messages": queued,
    }


def snapshot(hours: float = 24, owner: str = "") -> Dict:
    hours = float(hours) if hours in WINDOWS_H else 24.0
    now = time.time()
    out = {"now": now, "hours": hours, "windows": list(WINDOWS_H)}
    for key, fn in (("live", lambda: live(now)), ("chat", lambda: chat_stats(hours, owner, now)),
                    ("coders", lambda: coder_stats(hours, owner, now))):
        try:
            out[key] = fn()
        except Exception as e:
            logger.warning("DevOps %s stats failed: %s", key, e)
            out[key] = {"error": str(e)[:200]}
    return out
