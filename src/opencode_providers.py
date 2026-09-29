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
