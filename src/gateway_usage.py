"""Usage log and dashboard for the OpenAI-compatible gateway (routes/openai_gateway_routes.py).

One row per /v1/chat/completions request, in <data>/gateway_usage.db (SQLite): when, who (the API token's name and
the client's User-Agent product, never the secret), the model asked for (auto, auto:fast, or an explicit id), the model
it was RESOLVED to, tokens, streamed or not, outcome and latency. Message content is never stored.

Tokens come from the upstream's own ``usage`` when it sends one; otherwise they are estimated (``estimated`` = 1)
from the request size and the characters streamed back.

The dashboard is served by scripts/gateway_usage_server.py (read-only, stdlib) and as JSON at GET /v1/usage.
"""
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_names: Dict[str, str] = {}

SCHEMA = """CREATE TABLE IF NOT EXISTS gateway_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    caller TEXT, token_name TEXT, client TEXT, owner TEXT,
    requested TEXT, resolved TEXT, route_reason TEXT,
    stream INTEGER, status INTEGER, ok INTEGER, error TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, cached_tokens INTEGER,
    estimated INTEGER, latency_ms INTEGER
)"""


def db_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "gateway_usage.db")


def _connect(path: Optional[str] = None, readonly: bool = False) -> sqlite3.Connection:
    p = path or db_path()
    if readonly:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    else:
        con = sqlite3.connect(p, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(SCHEMA)
        con.execute("CREATE INDEX IF NOT EXISTS gateway_requests_ts ON gateway_requests(ts)")
    con.row_factory = sqlite3.Row
    return con


# ── who is calling ─────────────────────────────────────────────────────────

def client_product(user_agent: str) -> str:
    """'opencode/1.2.3' -> 'opencode'; Python-urllib, curl and so on keep their name; empty -> 'unknown'."""
    m = re.match(r"\s*([A-Za-z0-9_.\-]+)", user_agent or "")
    return (m.group(1)[:40] if m else "unknown")


def token_name(token_id: Optional[str]) -> Optional[str]:
    """The API token's display name (not a secret), looked up once."""
    if not token_id:
        return None
    if token_id in _names:
        return _names[token_id]
    try:
        from core.database import ApiToken, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(ApiToken).filter(ApiToken.id == token_id).first()
            name = row.name if row else None
        finally:
            db.close()
    except Exception:
        name = None
    _names[token_id] = name or f"token-{str(token_id)[:6]}"
    return _names[token_id]


def caller_of(request) -> Dict[str, Optional[str]]:
    st = request.state
    tname = token_name(getattr(st, "api_token_id", None)) if getattr(st, "api_token", False) else None
    client = client_product(request.headers.get("user-agent") or "")
    who = f"{tname} / {client}" if tname else f"session / {client}"
    return {"caller": who, "token_name": tname, "client": client}


# ── recording ──────────────────────────────────────────────────────────────

def record(**f) -> None:
    """Never raises."""
    try:
        cols = ["caller", "token_name", "client", "owner", "requested", "resolved", "route_reason", "stream", "status",
                "ok", "error", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "estimated",
                "latency_ms"]
        vals = [f.get(c) for c in cols]
        with _lock:
            con = _connect()
            try:
                con.execute(f"INSERT INTO gateway_requests (ts,{','.join(cols)}) VALUES (?,{','.join('?' * len(cols))})",
                            [time.time()] + vals)
                con.commit()
            finally:
                con.close()
    except Exception:
        logger.warning("gateway usage: could not record a request", exc_info=True)


def usage_tokens(u: Any) -> Dict[str, int]:
    """prompt/completion/total/cached from an OpenAI-style usage object (zeros when absent)."""
    if not isinstance(u, dict):
        return {}
    p, c = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    det = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    cached = int(det.get("cached_tokens") or u.get("cache_read_input_tokens") or 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": int(u.get("total_tokens") or p + c),
            "cached_tokens": cached}


def content_chars(choice_delta: Dict) -> int:
    n = 0
    for k in ("content", "reasoning_content"):
        if isinstance(choice_delta.get(k), str):
            n += len(choice_delta[k])
    for tc in choice_delta.get("tool_calls") or []:
        n += len(((tc or {}).get("function") or {}).get("arguments") or "")
    return n


def rewrite_completion(data: bytes, resolved: Optional[str]) -> Tuple[bytes, Dict, int]:
    """(body with ``model`` set to ``resolved`` when given, usage tokens, completion characters). A body that is not
    a JSON object passes through untouched."""
    try:
        j = json.loads(data)
    except Exception:
        return data, {}, 0
    if not isinstance(j, dict):
        return data, {}, 0
    chars = sum(content_chars((c or {}).get("message") or {}) for c in j.get("choices") or [] if isinstance(c, dict))
    tok = usage_tokens(j.get("usage"))
    if resolved and "choices" in j:
        j["model"] = resolved
        return json.dumps(j).encode(), tok, chars
    return data, tok, chars


class SseTap:
    """Passes a passthrough SSE stream along, line by line, setting ``model`` on each chunk to ``resolved`` (when
    given), and noting the usage chunk and how much text went by. Bytes that are not whole ``data:`` JSON lines pass
    through unchanged."""

    def __init__(self, resolved: Optional[str]):
        self.resolved = resolved
        self.buf = b""
        self.tokens: Dict[str, int] = {}
        self.chars = 0

    def _line(self, line: bytes) -> bytes:
        if not line.startswith(b"data:") or b"[DONE]" in line:
            return line
        try:
            j = json.loads(line[5:])
        except Exception:
            return line
        if not isinstance(j, dict):
            return line
        if j.get("usage"):
            self.tokens = usage_tokens(j["usage"]) or self.tokens
        for c in j.get("choices") or []:
            if isinstance(c, dict):
                self.chars += content_chars(c.get("delta") or {})
        if self.resolved and "choices" in j:
            j["model"] = self.resolved
            return b"data: " + json.dumps(j).encode()
        return line

    def feed(self, chunk: bytes) -> bytes:
        self.buf += chunk
        if b"\n" not in self.buf:
            return b""
        head, _, self.buf = self.buf.rpartition(b"\n")
        return b"\n".join(self._line(l) for l in head.split(b"\n")) + b"\n"

    def flush(self) -> bytes:
        rest, self.buf = self.buf, b""
        return self._line(rest) if rest else b""


# ── reading ────────────────────────────────────────────────────────────────

AGG = ("COUNT(*) AS requests, SUM(ok) AS ok, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
       "COALESCE(SUM(completion_tokens),0) AS completion_tokens, COALESCE(SUM(total_tokens),0) AS total_tokens, "
       "COALESCE(SUM(cached_tokens),0) AS cached_tokens, SUM(estimated) AS estimated_rows, "
       "CAST(AVG(latency_ms) AS INT) AS avg_latency_ms")


def summary(days: int = 14, path: Optional[str] = None) -> Dict:
    p = path or db_path()
    out: Dict[str, Any] = {"days": days, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not os.path.exists(p):
        return {**out, "total": {}, "by_day": [], "by_resolved": [], "by_requested": [], "by_caller": [],
                "by_day_resolved": [], "recent": []}
    since = time.time() - days * 86400
    con = _connect(p, readonly=True)
    try:
        def q(sql, *a):
            return [dict(r) for r in con.execute(sql, (since,) + a)]
        day = "date(ts,'unixepoch','localtime')"
        out["total"] = q(f"SELECT {AGG} FROM gateway_requests WHERE ts>=?")[0]
        out["by_day"] = q(f"SELECT {day} AS day, {AGG} FROM gateway_requests WHERE ts>=? GROUP BY day ORDER BY day DESC")
        out["by_resolved"] = q(f"SELECT COALESCE(resolved,'(none)') AS resolved, {AGG} FROM gateway_requests WHERE ts>=? "
                               "GROUP BY resolved ORDER BY requests DESC")
        out["by_requested"] = q(f"SELECT COALESCE(requested,'(none)') AS requested, {AGG} FROM gateway_requests WHERE ts>=? "
                                "GROUP BY requested ORDER BY requests DESC")
        out["by_caller"] = q(f"SELECT COALESCE(caller,'(unknown)') AS caller, {AGG} FROM gateway_requests WHERE ts>=? "
                             "GROUP BY caller ORDER BY requests DESC")
        out["by_day_resolved"] = q(f"SELECT {day} AS day, COALESCE(resolved,'(none)') AS resolved, COUNT(*) AS requests, "
                                   "COALESCE(SUM(total_tokens),0) AS total_tokens FROM gateway_requests WHERE ts>=? "
                                   "GROUP BY day, resolved ORDER BY day DESC, requests DESC")
        out["recent"] = q("SELECT ts, caller, requested, resolved, stream, status, ok, prompt_tokens, completion_tokens, "
                          "estimated, latency_ms, error FROM gateway_requests WHERE ts>=? ORDER BY ts DESC LIMIT 40")
    finally:
        con.close()
    return out


def _n(v) -> str:
    return f"{int(v or 0):,}"


def _table(title: str, key: str, rows: List[Dict]) -> str:
    head = ("<tr><th>%s</th><th>requests</th><th>ok</th><th>prompt tok</th><th>completion tok</th><th>total tok</th>"
            "<th>cached</th><th>avg ms</th></tr>" % html.escape(key))
    body = "".join(
        "<tr><td>%s</td>%s</tr>" % (html.escape(str(r[key])), "".join(
            f"<td class=n>{_n(r.get(c))}</td>" for c in ("requests", "ok", "prompt_tokens", "completion_tokens",
                                                       "total_tokens", "cached_tokens", "avg_latency_ms")))
        for r in rows)
    return f"<h2>{html.escape(title)}</h2><table>{head}{body}</table>"


def render_html(s: Dict) -> str:
    t = s.get("total") or {}
    recent = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td><b>%s</b></td><td>%s</td><td class=n>%s</td><td class=n>%s</td>"
        "<td class=n>%s</td><td class=n>%s%s</td></tr>" % (
            datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S"), html.escape(r["caller"] or ""),
            html.escape(r["requested"] or ""), html.escape(r["resolved"] or ""),
            "stream" if r["stream"] else "", r["status"] or "", _n(r["prompt_tokens"]), _n(r["completion_tokens"]),
            _n(r["latency_ms"]), "" if r["ok"] else " ERR")
        for r in s.get("recent") or [])
    cards = "".join(f"<div class=card><div class=v>{v}</div><div>{k}</div></div>" for k, v in (
        ("requests", _n(t.get("requests"))), ("total tokens", _n(t.get("total_tokens"))),
        ("prompt tokens", _n(t.get("prompt_tokens"))), ("completion tokens", _n(t.get("completion_tokens")))))
    return f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=30>
<title>Gateway usage</title><style>
body{{font:14px system-ui,sans-serif;margin:24px;background:#111;color:#ddd}}h1{{font-size:20px}}h2{{font-size:15px;margin-top:26px}}
table{{border-collapse:collapse}}td,th{{padding:3px 10px;border-bottom:1px solid #333;text-align:left}}td.n,th{{text-align:right}}
th:first-child{{text-align:left}}.card{{display:inline-block;margin:0 12px 12px 0;padding:10px 16px;background:#1c1c1c;border-radius:6px}}
.v{{font-size:22px;color:#fff}}small{{color:#888}}</style>
<h1>Odysseus gateway usage <small>last {s.get('days')} days, refreshes every 30 s, <a href="/json" style="color:#8ac">/json</a></small></h1>
{cards}
{_table('By resolved model (what actually answered)', 'resolved', s.get('by_resolved') or [])}
{_table('By model asked for', 'requested', s.get('by_requested') or [])}
{_table('By caller (token name / client)', 'caller', s.get('by_caller') or [])}
{_table('By day', 'day', s.get('by_day') or [])}
<h2>Latest requests</h2><table><tr><th>time</th><th>caller</th><th>asked</th><th>resolved</th><th></th><th>status</th>
<th>prompt</th><th>completion</th><th>ms</th></tr>{recent}</table>
<p><small>Token counts are the upstream's own when it reports them, otherwise estimated.</small></p>"""
