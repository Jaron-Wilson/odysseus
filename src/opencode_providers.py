"""Odysseus's model servers, offered to OpenCode.

Asked for on 2026-09-29: "when approving plans it wont allow me to pick a
different server for the open code." OpenCode only knew the servers in its
own config (~/.config/opencode/opencode.json), so a server added to Odysseus,
like totoro, could not be picked for a run.

Each enabled Odysseus LLM endpoint whose address OpenCode does not already
have becomes a provider named "ody-<endpoint>", with the endpoint's models.
It is handed to each OpenCode run through OPENCODE_CONFIG_CONTENT, which
OpenCode merges over its own config, so opencode.json is never edited and an
API key never lands in a file.
"""
import json
import logging
import os
import re
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

PREFIX = "ody-"


def _own_config() -> Dict:
    try:
        with open(os.path.expanduser("~/.config/opencode/opencode.json"), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _norm(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:24] or "server"


def _endpoints() -> List[Dict]:
    """Enabled LLM endpoints: name, base_url, models, api key (decrypted)."""
    try:
        from core.database import SessionLocal, ModelEndpoint
    except Exception:
        return []
    out = []
    db = SessionLocal()
    try:
        for ep in db.query(ModelEndpoint).all():
            if not ep.is_enabled or (ep.model_type or "llm") != "llm" or not ep.base_url:
                continue
            try:
                models = json.loads(ep.cached_models or "[]") or []
            except ValueError:
                models = []
            try:
                hidden = set(json.loads(ep.hidden_models or "[]") or [])
            except ValueError:
                hidden = set()
            models = [m if isinstance(m, str) else str((m or {}).get("id") or "") for m in models]
            models = [m for m in models if m and m not in hidden]
            if not models:
                continue
            key = ""
            if ep.api_key:
                try:
                    from src import secret_storage
                    key = secret_storage.decrypt(ep.api_key)
                except Exception:
                    key = ""
            out.append({"id": ep.id, "name": ep.name or ep.id, "base_url": ep.base_url,
                        "models": models, "api_key": key})
    except Exception as e:
        logger.debug("[opencode] could not read endpoints: %s", e)
    finally:
        db.close()
    return out


def providers() -> Dict[str, Dict]:
    """OpenCode provider entries for the Odysseus servers OpenCode lacks."""
    own = _own_config().get("provider") or {}
    have = {_norm(((p or {}).get("options") or {}).get("baseURL", "")) for p in own.values()}
    out: Dict[str, Dict] = {}
    for ep in _endpoints():
        if _norm(ep["base_url"]) in have:
            continue                           # OpenCode already has this server
        pid = PREFIX + _slug(ep["name"])
        while pid in out or pid in own:
            pid += "-" + ep["id"][:4]
        options = {"baseURL": ep["base_url"].rstrip("/")}
        if ep["api_key"]:
            options["apiKey"] = ep["api_key"]
        out[pid] = {"npm": "@ai-sdk/openai-compatible", "name": f"{ep['name']} (Odysseus)",
                    "options": options, "models": {m: {"name": m} for m in ep["models"]}}
        have.add(_norm(ep["base_url"]))
    return out


def config_content() -> str:
    """For OPENCODE_CONFIG_CONTENT; "" when there is nothing to add."""
    p = providers()
    return json.dumps({"provider": p}) if p else ""


def models() -> Tuple[List[Tuple[str, str]], str]:
    """Every model an OpenCode run can use, as (provider/model, label), and
    OpenCode's own default."""
    cfg = _own_config()
    out = []
    for pid, prov in (cfg.get("provider") or {}).items():
        where = (prov or {}).get("name") or pid
        for mid, m in ((prov or {}).get("models") or {}).items():
            out.append((f"{pid}/{mid}", f"{(m or {}).get('name') or mid} · {where}"))
    for pid, prov in providers().items():
        for mid in prov["models"]:
            out.append((f"{pid}/{mid}", f"{mid} · {prov['name']}"))
    return out, str(cfg.get("model") or "")


# "when approving an opencode and selecting a model i would like to see if a
# model is already being used: ie: chat session, in a opencode already etc."
# What is using each model right now, so the Approve dialog can say so.

def _base(url: str) -> str:
    u = _norm(url)
    for tail in ("/chat/completions", "/completions", "/v1"):
        if u.endswith(tail):
            u = u[: -len(tail)].rstrip("/")
    return u


def _provider_bases() -> Dict[str, str]:
    """OpenCode provider id -> its server address."""
    out = {pid: _base(((p or {}).get("options") or {}).get("baseURL", ""))
           for pid, p in (_own_config().get("provider") or {}).items()}
    out.update({pid: _base(p["options"]["baseURL"]) for pid, p in providers().items()})
    return {k: v for k, v in out.items() if v}


def _replying_chats() -> List[Dict]:
    """Chats with a reply being written now: name, server, model."""
    from src import agent_runs
    ids = agent_runs.active_sessions()
    if not ids:
        return []
    try:
        from core.database import SessionLocal, Session as DbSession
    except Exception:
        return []
    db = SessionLocal()
    try:
        rows = db.query(DbSession).filter(DbSession.id.in_(ids)).all()
        return [{"id": s.id, "name": s.name or "a chat", "base": _base(s.endpoint_url or ""),
                 "model": s.model or ""} for s in rows]
    except Exception as e:
        logger.debug("[opencode] could not read replying chats: %s", e)
        return []
    finally:
        db.close()


def in_use(model_ids: List[str]) -> Dict[str, Dict[str, List[str]]]:
    """For each OpenCode model id: "busy", what runs on that very model now,
    and "server_busy", what runs on the same server with another model (a
    shared GPU is slower for both). Only ids with something are returned."""
    from src import claude_code_jobs as jobs
    bases = _provider_bases()
    default = str(_own_config().get("model") or "")
    running = [j for j in jobs.list_jobs("") if j.status == "running"]
    chats = _replying_chats()
    out: Dict[str, Dict[str, List[str]]] = {}
    for mid in model_ids:
        pid, _, name = mid.partition("/")
        base = bases.get(pid, "")
        busy, near = [], []
        for j in running:
            jm = j.model or (default if j.engine == "opencode" else "")
            if j.engine != "opencode" or not jm:
                continue
            what = f"an OpenCode {j.action or 'run'} in {jobs._chat_name(j.chat_session_id) or 'a chat'}"
            jpid, _, jname = jm.partition("/")
            if jm == mid:
                busy.append(what)
            elif base and bases.get(jpid) == base:
                near.append(f"{what} ({jname})")
        for c in chats:
            if not base or c["base"] != base:
                continue
            what = f"a reply in chat “{c['name'][:40]}”"
            (busy if c["model"] == name else near).append(what if c["model"] == name else f"{what} ({c['model']})")
        if busy or near:
            out[mid] = {"busy": busy, "server_busy": near}
    return out
