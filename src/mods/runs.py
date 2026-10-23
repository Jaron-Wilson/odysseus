"""Agent runs for mods (permission `agent.run`).

A mod asks the core to run the ordinary agent loop (src/agent_loop.py) on a
model, in a chat the core creates, with the tools the mod picks: its own
tools forced into the run whatever the words, plus core tools it names.
This is how a mod like Company runs its agents without reaching into the
core.

    POST   /api/mods/v1/agent/runs              start a run (below)
    GET    /api/mods/v1/agent/runs              this mod's live and recent runs
    GET    /api/mods/v1/agent/runs/<id>         status, reply text, tool calls, usage
    GET    /api/mods/v1/agent/runs/<id>/events  the run's chat events (SSE), replayed from the start
    DELETE /api/mods/v1/agent/runs/<id>         stop it

Start body:

    {"user": "alice",                  whose run (as for notify); default the primary admin
     "model": "qwen3:8b@Box",          a spec from GET llm/models (required)
     "local_only": true,               refuse a model that is not local (src/mods/llm.py)
     "title": "Run Oct 22 10:15",      the run's chat title
     "folder": "Company",              the chat folder (default: the mod's name)
     "workspace": {"session_id": "...", "title": "Ada · Acme"},
                                       a parent chat the run is a thread of (made when
                                       the id is missing or gone; the answer names it)
     "system": "...",                  the system prompt
     "messages": [{"role": "user", "content": "..."}],   or "prompt": "..."
     "tools": {"mod": true,            this mod's tools: true (all), a list, or false
               "core": ["bash", "read_file"],   core tools to offer (omit: picked per message)
               "deny": ["web_search"],          core tools never offered (ask_user always is)
               "other_mods": false},            other mods' tools by keyword (default no)
     "coding_agent": "opencode",       the one coding engine its chat may use (optional)
     "max_rounds": 20,                 1-200
     "max_cost_usd": 0.5,              stop when the run cost this much
     "scope": "c/3f2a/a/91bc",         usage scope path (src/mods/usage_api.py)
     "approved_plan": "1. ...",        pin a plan the person approved
     "gate": "auto",                   auto: a local model waits for Odysseus to be quiet;
                                       always / never
     "checkpoint": "/mod/runs/checkpoint",   a path on the mod, POSTed between rounds
     "metadata": {...}}                echoed back (4 KB)

The answer is {id, session_id, workspace_id, status}. Model calls are
recorded under the scope path plus "run-<id>"; the stored budgets of every
scope above it are enforced between rounds.

Between agent rounds the core stops the run when the mod asked
(DELETE), its max_cost_usd or a budget is reached, or the checkpoint path
answers {"stop": "<reason>"}: the core POSTs it {run_id, round, cost_usd,
metadata} as user odysseus-core and waits at most 10 s (no answer: go on).

The mod's tools are called with an `X-Odysseus-Run: <run id>` header during
its runs, so a tool knows which run (and so which agent) is calling.

When a run ends the core POSTs an `agent.run.finished` event to the mod's
events webhook (as other events, src/mods/events.py), with the run's view
as data.

Runs survive a restart of the core. They are kept in DATA_DIR/mods/runs.json
(the view of each, not its text). When the core starts, every run that was
still queued or running is marked `interrupted` ("Odysseus restarted": the
agent loop died with the process), and each mod that owns one gets
`agent.run.finished` with that run's view plus `"restarted": true`, as soon as
its process is up. The mod decides: resume (start a new run for the same work,
usually with the old run's `metadata` and a note of what was done), re-queue it,
or give up. A mod that was down for the notice reads the same thing from
GET /api/mods/v1/agent/runs (the interrupted runs stay listed for six hours),
so it can reconcile when it starts. The run's chat (`session_id`) is kept.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.mods import registry, signing, usage_api
from src.mods import llm as mod_llm
from src.mods import tools as mod_tools

logger = logging.getLogger(__name__)

MAX_LIVE_PER_MOD = int(os.getenv("ODYSSEUS_MOD_RUNS_PER_MOD", "4") or 4)
MAX_LIVE_TOTAL = int(os.getenv("ODYSSEUS_MOD_RUNS_TOTAL", "8") or 8)
KEEP_FINISHED_S = 6 * 3600
MAX_KEPT = 300
CHECKPOINT_TIMEOUT_S = 10
MAX_BODY = 200_000
LIVE = ("queued", "running")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,80}$")
_FOLDER_RE = re.compile(r"[^\w .,()&+-]")

_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar("odysseus_mod_run", default=None)
_RUNS: Dict[str, "ModRun"] = {}
_tasks: set = set()


class RunError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ModRun:
    def __init__(self, mod_id: str, owner: str) -> None:
        self.id = uuid.uuid4().hex
        self.mod_id = mod_id
        self.owner = owner
        self.status = "queued"
        self.error: Optional[str] = None
        self.stop_reason: Optional[str] = None
        self.stop_requested = False
        self.session_id = ""
        self.workspace_id: Optional[str] = None
        self.spec = ""
        self.model_id = ""
        self.url = ""
        self.headers: Dict[str, str] = {}
        self.local = False
        self.gate = False
        self.context_length = 0
        self.scope = ""
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.text = ""
        self.tools: List[Dict[str, Any]] = []
        self.round = 1
        self.model_error: Optional[str] = None
        self.loop_error: Optional[str] = None
        self.usage: Dict[str, Any] = {}
        self.metadata: Dict[str, Any] = {}
        self.req: Dict[str, Any] = {}
        self.task: Optional[asyncio.Task] = None
        self.restarted = False         # interrupted because Odysseus restarted (src/mods/runs.py recover)
        self.notified = False          # the mod was told so after the restart
        self.tool_calls_before = 0     # a recovered run: how many tool calls it made (the calls are not kept)

    @property
    def run_scope(self) -> str:
        return (self.scope + "/" if self.scope else "") + f"run-{self.id}"

    def view(self, full: bool = True) -> Dict[str, Any]:
        out = {"id": self.id, "status": self.status, "error": self.error, "stop_reason": self.stop_reason,
               "session_id": self.session_id, "workspace_id": self.workspace_id, "model": self.spec,
               "local": self.local, "scope": self.scope, "rounds": self.round,
               "tool_calls": len(self.tools) or self.tool_calls_before, "usage": self.usage,
               "metadata": self.metadata, "created": self.created, "started": self.started,
               "finished": self.finished}
        if self.restarted:
            out["restarted"] = True
        if full:
            out["text"] = self.text[-20000:]
            out["tools"] = [dict(t, output=(str(t.get("output") or "")[:2000])) for t in self.tools[-100:]]
        return out


# ── The run context (read by src/agent_loop.py and src/mods/tools.py) ────

@contextmanager
def run_context(**val) -> Iterator[Dict[str, Any]]:
    token = _ctx.set(val)
    try:
        yield val
    finally:
        try:
            _ctx.reset(token)
        except ValueError:
            _ctx.set(None)


def current() -> Optional[Dict[str, Any]]:
    """The mod run this code runs in: {mod_id, run_id, tools, other_mods}, or None."""
    return _ctx.get()


def forced_tools(owner: Optional[str]) -> List[str]:
    """Qualified names of the running mod's own tools for this run."""
    c = _ctx.get()
    if not c or not c.get("tools"):
        return []
    want = c["tools"]
    out = []
    for entry, t in mod_tools.enabled_tools(owner):
        if entry["id"] != c["mod_id"]:
            continue
        if want is True or t["name"] in want:
            out.append(mod_tools.qualified(entry["id"], t["name"]))
    return out


def run_header(mod_id: str) -> Optional[str]:
    """The X-Odysseus-Run value for a call of `mod_id`'s tool, if it is that mod's run."""
    c = _ctx.get()
    return c["run_id"] if c and c.get("mod_id") == mod_id else None


# ── Validation ───────────────────────────────────────────────────────────

def _names(v: Any, what: str) -> List[str]:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > 200 or not all(isinstance(x, str) and _TOOL_RE.match(x) for x in v):
        raise RunError(400, f"{what} must be a list of tool names")
    return list(dict.fromkeys(v))


def _messages(body: Dict[str, Any]) -> List[Dict[str, str]]:
    msgs = body.get("messages")
    if msgs is None and isinstance(body.get("prompt"), str) and body["prompt"].strip():
        msgs = [{"role": "user", "content": body["prompt"]}]
    if (not isinstance(msgs, list) or not msgs or len(msgs) > 100
            or not all(isinstance(m, dict) and m.get("role") in ("user", "assistant")
                       and isinstance(m.get("content"), str) for m in msgs)
            or msgs[-1]["role"] != "user"):
        raise RunError(400, "messages must be a list of {role: user|assistant, content} ending with a user "
                            "message (or give prompt)")
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


def _folder(entry: Dict[str, Any], v: Any) -> str:
    name = v if isinstance(v, str) and v.strip() else (entry.get("name") or entry["id"])
    return _FOLDER_RE.sub("", name).strip()[:60] or entry["id"]


def parse(entry: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    if len(json.dumps(body, default=str)) > MAX_BODY:
        raise RunError(413, "The run request is larger than 200 KB")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        raise RunError(400, "model is required: a spec from GET llm/models")
    system = body.get("system") or ""
    if not isinstance(system, str):
        raise RunError(400, "system must be a string")
    tools = body.get("tools") if isinstance(body.get("tools"), dict) else {}
    own = tools.get("mod", True)
    if own not in (True, False):
        own = _names(own, "tools.mod")
    core = tools.get("core")
    core = None if core is None else _names(core, "tools.core")
    deny = _names(tools.get("deny"), "tools.deny")
    try:
        max_rounds = max(1, min(int(body.get("max_rounds") or 20), 200))
        max_cost = float(body["max_cost_usd"]) if body.get("max_cost_usd") not in (None, "", 0) else None
    except (TypeError, ValueError):
        raise RunError(400, "max_rounds and max_cost_usd must be numbers")
    gate = body.get("gate") or "auto"
    if gate not in ("auto", "always", "never"):
        raise RunError(400, "gate must be auto, always or never")
    try:
        scope = usage_api.clean_path(body.get("scope"))
    except ValueError as e:
        raise RunError(400, str(e))
    cp = body.get("checkpoint")
    if cp is not None:
        from src.mods import manifest as mf
        if not isinstance(cp, str) or not mf.PATH_RE.match(cp) or ".." in cp.split("/"):
            raise RunError(400, "checkpoint must be a path on your own server, starting with /")
    meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    if len(json.dumps(meta, default=str)) > 4096:
        raise RunError(400, "metadata is larger than 4 KB")
    ws = body.get("workspace")
    if ws is not None and not isinstance(ws, dict):
        raise RunError(400, "workspace must be an object {session_id?, title?}")
    coding = body.get("coding_agent")
    if coding is not None and (not isinstance(coding, str) or not _TOOL_RE.match(coding)):
        raise RunError(400, "coding_agent must be an engine name")
    plan = body.get("approved_plan")
    if plan is not None and not isinstance(plan, str):
        raise RunError(400, "approved_plan must be a string")
    return {"model": body["model"].strip(), "local_only": bool(body.get("local_only")),
            "title": str(body.get("title") or "")[:120] or f"{entry.get('name') or entry['id']} run",
            "folder": _folder(entry, body.get("folder")), "workspace": ws, "system": system[:100_000],
            "messages": _messages(body), "own_tools": own, "core": core, "deny": deny,
            "other_mods": bool(tools.get("other_mods")), "coding_agent": coding, "max_rounds": max_rounds,
            "max_cost_usd": max_cost, "scope": scope, "approved_plan": plan, "gate": gate, "checkpoint": cp,
            "metadata": meta}


# ── Sessions ─────────────────────────────────────────────────────────────

def _sm():
    from src.ai_interaction import get_session_manager
    sm = get_session_manager()
    if sm is None:
        raise RunError(503, "Odysseus sessions are not ready yet")
    return sm


def _set_folder(sid: str, folder: str) -> None:
    from core import database as cdb
    db = cdb.SessionLocal()
    try:
        db.query(cdb.Session).filter(cdb.Session.id == sid).update({cdb.Session.folder: folder})
        db.commit()
    finally:
        db.close()


def _owned_session(sid: Any, owner: str) -> bool:
    if not isinstance(sid, str) or not sid:
        return False
    from core import database as cdb
    db = cdb.SessionLocal()
    try:
        row = db.query(cdb.Session.id, cdb.Session.owner).filter(cdb.Session.id == sid).first()
        return bool(row) and (row.owner or "") == (owner or "")
    finally:
        db.close()


def _make_sessions(run: ModRun, req: Dict[str, Any]) -> None:
    from core.models import ChatMessage
    sm = _sm()
    owner = run.owner or None
    ws = req.get("workspace")
    if ws is not None:
        if _owned_session(ws.get("session_id"), run.owner):
            run.workspace_id = ws["session_id"]
        else:
            run.workspace_id = str(uuid.uuid4())
            sm.create_session(run.workspace_id, str(ws.get("title") or req["title"])[:120], run.url, run.model_id,
                              owner=owner)
            _set_folder(run.workspace_id, req["folder"])
    run.session_id = str(uuid.uuid4())
    thread = sm.create_session(run.session_id, req["title"], run.url, run.model_id, owner=owner,
                               parent_session_id=run.workspace_id)
    if run.headers:
        thread.headers = dict(run.headers)
    if not run.workspace_id:
        _set_folder(run.session_id, req["folder"])
    try:
        from core.database import set_session_mode
        set_session_mode(run.session_id, "agent")
    except Exception:
        logger.debug("run chat mode not set", exc_info=True)
    if req.get("coding_agent"):
        try:
            from src import chat_prefs
            for eng in chat_prefs.ENGINE_KEYS:
                chat_prefs.set_pref(run.session_id, eng, eng == req["coding_agent"])
        except Exception:
            logger.debug("coding agent prefs not set", exc_info=True)
    for m in req["messages"]:
        sm.add_message(run.session_id, ChatMessage(m["role"], m["content"], metadata={
            "source": f"mod:{run.mod_id}", "mod_run_id": run.id}))
    try:
        sm.save_sessions()
    except Exception:
        pass


# ── Keeping runs across a restart ────────────────────────────────────────

def _store_path() -> str:
    from src.mods import paths
    return os.path.join(paths.mods_root(), "runs.json")


def _record(r: ModRun) -> Dict[str, Any]:
    return dict(r.view(full=False), mod_id=r.mod_id, owner=r.owner, model_id=r.model_id, restarted=r.restarted,
                notified=r.notified)


def persist() -> None:
    """Write every kept run to disk (a small file; runs change a few times each)."""
    rows = sorted(_RUNS.values(), key=lambda r: r.created)[-MAX_KEPT:]
    path = _store_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"runs": [_record(r) for r in rows]}, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("[mods] could not save agent runs: %s", e)


def _from_record(d: Dict[str, Any]) -> Optional[ModRun]:
    if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not isinstance(d.get("mod_id"), str):
        return None
    r = ModRun(d["mod_id"], str(d.get("owner") or ""))
    r.id = d["id"]
    r.status = str(d.get("status") or "interrupted")
    r.error, r.stop_reason = d.get("error"), d.get("stop_reason")
    r.session_id, r.workspace_id = str(d.get("session_id") or ""), d.get("workspace_id")
    r.spec, r.model_id, r.local = str(d.get("model") or ""), str(d.get("model_id") or ""), bool(d.get("local"))
    r.scope = str(d.get("scope") or "")
    r.round = int(d.get("rounds") or 1)
    r.usage = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    r.metadata = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
    r.created = float(d.get("created") or time.time())
    r.started, r.finished = d.get("started"), d.get("finished")
    r.tool_calls_before = int(d.get("tool_calls") or 0)
    r.restarted, r.notified = bool(d.get("restarted")), bool(d.get("notified"))
    return r


def load_saved() -> List[ModRun]:
    try:
        with open(_store_path(), encoding="utf-8") as f:
            rows = json.load(f).get("runs") or []
    except (OSError, ValueError):
        return []
    return [r for r in (_from_record(d) for d in rows) if r]


def recover() -> List[ModRun]:
    """At core start: load the saved runs, mark those that were still queued or
    running as interrupted, and tell each owning mod (`agent.run.finished`, with
    `restarted: true`). Returns the runs it told about. Safe to call once per start;
    a run is told about once (`notified`)."""
    told: List[ModRun] = []
    now = time.time()
    for r in load_saved():
        if r.id in _RUNS:
            continue
        if r.status in LIVE:
            r.status, r.error, r.finished, r.restarted = "interrupted", "Odysseus restarted", now, True
            r.notified = False
        elif r.status == "interrupted" and r.error == "Odysseus restarted":
            r.restarted = True
        _RUNS[r.id] = r
        if r.restarted and not r.notified:
            told.append(r)
    _prune()
    for r in told:
        r.notified = True
        try:
            from src.mods import events
            events.send_to(r.mod_id, "agent.run.finished", r.view())
        except Exception:
            logger.debug("restart notice not delivered", exc_info=True)
    if told or _RUNS:
        persist()
    if told:
        logger.info("[mods] %d agent run(s) interrupted by the restart; their mods were told", len(told))
    return told


# ── Starting, the loop, finishing ────────────────────────────────────────

def _live(mod_id: Optional[str] = None) -> List[ModRun]:
    return [r for r in _RUNS.values() if r.status in LIVE and (mod_id is None or r.mod_id == mod_id)]


def _prune() -> None:
    now = time.time()
    done = sorted((r for r in _RUNS.values() if r.status not in LIVE), key=lambda r: r.finished or 0)
    for r in done:
        if now - (r.finished or now) > KEEP_FINISHED_S or len(_RUNS) > MAX_KEPT:
            _RUNS.pop(r.id, None)


def _should_gate(run: ModRun, mode: str) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    return run.local


def gate_max_wait() -> float:
    try:
        return max(0.0, float(os.getenv("ODYSSEUS_MOD_GATE_MAX_WAIT_S", "60")))
    except ValueError:
        return 60.0


async def _wait_quiet(label: str) -> None:
    from src.interactive_gate import wait_for_interactive_quiet
    try:
        await asyncio.wait_for(wait_for_interactive_quiet(label), timeout=gate_max_wait() or None)
    except asyncio.TimeoutError:
        logger.info("[mods] %s: Odysseus stayed busy; going ahead", label)


async def start(entry: Dict[str, Any], owner: str, body: Dict[str, Any]) -> ModRun:
    req = parse(entry, body)
    mod_id = entry["id"]
    if len(_live(mod_id)) >= MAX_LIVE_PER_MOD:
        raise RunError(429, f"At most {MAX_LIVE_PER_MOD} runs of one mod at once")
    if len(_live()) >= MAX_LIVE_TOTAL:
        raise RunError(429, f"At most {MAX_LIVE_TOTAL} mod runs at once")
    why = usage_api.stop_reason(mod_id, req["scope"])
    if why:
        raise RunError(402, f"Not started: {why}")
    run = ModRun(mod_id, owner)
    run.req, run.scope, run.metadata = req, req["scope"], req["metadata"]
    try:
        url, model_id, headers = await asyncio.to_thread(mod_llm.resolve, owner, req["model"], "utility",
                                                         req["local_only"])
    except mod_llm.ModelError as e:
        raise RunError(e.status, e.message)
    from src.endpoint_resolver import invalid_port_error
    bad = invalid_port_error(url)
    if bad:
        raise RunError(400, f"Model {req['model']} is not reachable: {bad}")
    try:
        from src.model_context import get_context_length
        ctx_len = int(await asyncio.to_thread(get_context_length, url, model_id) or 0)
    except Exception:
        ctx_len = 0
    run.spec, run.url, run.model_id, run.headers = req["model"], url, model_id, headers
    run.local, run.context_length = mod_llm.is_local_url(url), ctx_len
    run.gate = _should_gate(run, req["gate"])
    _make_sessions(run, req)
    _RUNS[run.id] = run
    _prune()
    persist()
    task = asyncio.get_running_loop().create_task(_execute(run))
    run.task = task
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    from src.mods import runtime
    runtime.log_line(mod_id, f"[odysseus] agent run {run.id[:8]} on {run.spec}")
    return run


def _collect(chunk: str, run: ModRun) -> None:
    if not isinstance(chunk, str):
        return
    is_error = chunk.startswith("event: error")
    for line in chunk.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[6:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if is_error or (d.get("error") and not d.get("type")):
            run.loop_error = str(d.get("error") or d.get("message") or "Model error")[:1000]
        elif isinstance(d.get("delta"), str):
            run.text += d["delta"]
            if d.get("stall"):
                run.model_error = re.sub(r"[_*]", "", d["delta"]).strip() or "The model did not respond"
        elif d.get("type") == "agent_step":
            run.round = d.get("round", run.round)
            run.text += "\n\n"
        elif d.get("type") == "tool_output":
            run.tools.append({"round": run.round, "tool": d.get("tool"), "command": d.get("command"),
                              "output": d.get("output"), "exit_code": d.get("exit_code")})


def _run_cost(run: ModRun) -> Dict[str, Any]:
    from src import budgets
    from src.usage import utcnow
    return budgets.spent("mod_scope", usage_api.full_scope(run.mod_id, run.run_scope), None,
                         start=utcnow() - timedelta(days=2), end=utcnow() + timedelta(minutes=5))


async def _checkpoint(run: ModRun) -> Optional[str]:
    path = run.req.get("checkpoint")
    if not path:
        return None
    from src.mods import runtime
    port = runtime.state(run.mod_id).port
    if not port:
        return None
    headers = signing.sign_identity(registry.token_of(run.mod_id), run.mod_id, signing.CORE_USER)
    headers["Content-Type"] = "application/json"
    headers["X-Odysseus-Run"] = run.id
    body = {"run_id": run.id, "round": run.round, "cost_usd": run.usage.get("cost_usd", 0.0),
            "metadata": run.metadata}
    try:
        r = await runtime._client().post(f"http://127.0.0.1:{port}{path}", content=json.dumps(body),
                                         headers=headers, timeout=CHECKPOINT_TIMEOUT_S)
        d = r.json() if r.status_code < 400 else {}
    except Exception as e:
        runtime.log_line(run.mod_id, f"[odysseus] run checkpoint {path} failed: {type(e).__name__}")
        return None
    stop = d.get("stop") if isinstance(d, dict) else None
    return str(stop)[:300] if stop else None


async def _between_rounds(run: ModRun) -> Optional[str]:
    if run.stop_requested:
        return "stopped by the mod"
    try:
        s = await asyncio.to_thread(_run_cost, run)
        run.usage = {"cost_usd": round(s["cost_usd"], 6), "input_tokens": s["input_tokens"],
                     "output_tokens": s["output_tokens"]}
        mc = run.req.get("max_cost_usd")
        if mc and s["cost_usd"] >= mc:
            return f"the run's cost limit of ${mc:.2f} was reached"
    except Exception as e:
        logger.debug("mod run cost check failed: %s", e)
    try:
        why = await asyncio.to_thread(usage_api.stop_reason, run.mod_id, run.scope)
        if why:
            return why
    except Exception as e:
        logger.debug("mod run budget check failed: %s", e)
    why = await _checkpoint(run)
    if why:
        return why
    if run.gate:
        from src.interactive_gate import has_foreground_activity
        if has_foreground_activity():
            await _wait_quiet(f"mod run {run.id[:8]}")
    return None


async def _stream(run: ModRun):
    from src.agent_loop import stream_agent_loop
    req = run.req
    messages = ([{"role": "system", "content": req["system"]}] if req["system"] else []) + list(req["messages"])
    disabled = set(req["deny"]) | {"ask_user"}
    relevant = None
    if req["core"] is not None:
        relevant = set(req["core"]) - disabled
    gen = stream_agent_loop(run.url, run.model_id, messages, headers=run.headers, max_rounds=req["max_rounds"],
                            session_id=run.session_id, disabled_tools=disabled, owner=run.owner or None,
                            relevant_tools=relevant, approved_plan=req.get("approved_plan"),
                            context_length=run.context_length, auto_input_budget=True)
    try:
        async for chunk in gen:
            _collect(chunk, run)
            yield chunk
            if '"agent_step"' in chunk:
                why = await _between_rounds(run)
                if why:
                    run.stop_reason = why
                    note = f"\n\n_Stopped: {why}._"
                    run.text += note
                    yield f'data: {json.dumps({"delta": note})}\n\n'
                    yield "data: [DONE]\n\n"
                    break
    finally:
        try:
            await gen.aclose()
        except Exception:
            pass
        if run.text.strip() or run.tools:
            try:
                from core.models import ChatMessage
                sm = _sm()
                sm.add_message(run.session_id, ChatMessage("assistant", run.text.strip(), metadata={
                    "tool_events": run.tools, "model": run.model_id, "source": f"mod:{run.mod_id}",
                    "mod_run_id": run.id}))
                sm.save_sessions()
            except Exception as e:
                logger.warning("[mods] could not save run %s reply: %s", run.id[:8], e)


async def _execute(run: ModRun) -> None:
    from src import agent_runs
    from src.usage import usage_scope
    try:
        if run.gate:
            await _wait_quiet(f"mod run {run.id[:8]}")
        if run.stop_requested:
            run.status, run.error = "cancelled", "Stopped before it started"
            return
        run.status, run.started = "running", time.time()
        persist()
        with usage_scope(source="mod", owner=run.owner or None, session_id=run.session_id,
                         source_scope=usage_api.full_scope(run.mod_id, run.run_scope)), \
                run_context(mod_id=run.mod_id, run_id=run.id, tools=run.req["own_tools"],
                            other_mods=run.req["other_mods"]):
            ar = agent_runs.start(run.session_id, _stream(run), background=True)
        await asyncio.wait({ar.task})
        if run.stop_reason:
            run.status, run.error = "cancelled", run.stop_reason
        elif run.stop_requested or ar.status == "stopped":
            stopping = agent_runs.restarting
            run.status = "interrupted" if stopping else "cancelled"
            run.error = "Odysseus restarted" if stopping else "Stopped"
        elif ar.status == "error":
            run.status, run.error = "failed", run.loop_error or "The agent run failed"
        elif run.loop_error and not run.text.strip() and not run.tools:
            run.status, run.error = "failed", run.loop_error
        elif run.model_error and not run.tools:
            run.status, run.error = "failed", run.model_error[:1000]
        else:
            run.status = "succeeded"
    except asyncio.CancelledError:
        agent_runs.stop(run.session_id)
        run.status = "interrupted" if agent_runs.restarting else "cancelled"
        run.error = "Odysseus restarted" if agent_runs.restarting else "Stopped"
    except Exception as e:
        logger.warning("[mods] run %s failed: %s", run.id[:8], e, exc_info=True)
        run.status, run.error = "failed", str(e)[:1000]
    finally:
        run.finished = time.time()
        try:
            s = await asyncio.to_thread(_run_cost, run)
            run.usage = {"cost_usd": round(s["cost_usd"], 6), "input_tokens": s["input_tokens"],
                         "output_tokens": s["output_tokens"], "unpriced_calls": s["unpriced_calls"]}
        except Exception:
            pass
        run.restarted = bool(agent_runs.restarting and run.status == "interrupted")
        persist()
        try:
            from src.mods import events
            events.send_to(run.mod_id, "agent.run.finished", run.view())
        except Exception:
            logger.debug("agent.run.finished not delivered", exc_info=True)


def stop(run: ModRun) -> bool:
    if run.status not in LIVE:
        return False
    run.stop_requested = True
    from src import agent_runs
    if run.session_id and agent_runs.stop(run.session_id):
        return True
    if run.task and not run.task.done() and run.status == "queued":
        run.task.cancel()
    return True


# ── Routes ───────────────────────────────────────────────────────────────

def setup(router: APIRouter, mod_for, owner_for) -> None:
    def _get(e: Dict[str, Any], run_id: str) -> ModRun:
        r = _RUNS.get(run_id)
        if r is None or r.mod_id != e["id"]:
            raise HTTPException(404, "No such run (finished runs are forgotten after six hours)")
        return r

    @router.post("/api/mods/v1/agent/runs")
    async def v1_run_start(request: Request):
        e = mod_for(request, "agent.run")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Expected a JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        try:
            run = await start(e, owner_for(e, body), body)
        except RunError as ex:
            raise HTTPException(ex.status, ex.message)
        return {"id": run.id, "session_id": run.session_id, "workspace_id": run.workspace_id,
                "status": run.status, "model": run.spec, "local": run.local}

    @router.get("/api/mods/v1/agent/runs")
    async def v1_runs(request: Request):
        e = mod_for(request, "agent.run")
        rows = sorted((r for r in _RUNS.values() if r.mod_id == e["id"]), key=lambda r: r.created, reverse=True)
        return {"runs": [r.view(full=False) for r in rows]}

    @router.get("/api/mods/v1/agent/runs/{run_id}")
    async def v1_run(request: Request, run_id: str):
        e = mod_for(request, "agent.run")
        return _get(e, run_id).view()

    @router.delete("/api/mods/v1/agent/runs/{run_id}")
    async def v1_run_stop(request: Request, run_id: str):
        e = mod_for(request, "agent.run")
        r = _get(e, run_id)
        return {"ok": stop(r), "status": r.status}

    @router.get("/api/mods/v1/agent/runs/{run_id}/events")
    async def v1_run_events(request: Request, run_id: str):
        e = mod_for(request, "agent.run")
        r = _get(e, run_id)

        async def gen():
            from src import agent_runs
            while r.status == "queued":
                yield ": waiting for Odysseus to be quiet\n\n"
                await asyncio.sleep(2)
            async for ev in agent_runs.subscribe(r.session_id):
                yield ev
            for _ in range(50):          # the finish bookkeeping right after the loop ends
                if r.status not in LIVE:
                    break
                await asyncio.sleep(0.1)
            yield f"event: run\ndata: {json.dumps(r.view(full=False))}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
