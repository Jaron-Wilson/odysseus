"""Chats with a standing job.

Asked for on 2026-09-29: "what if I see an app does not have an MCP and then I
can say generate with AI and task it off to an OpenCode or a Claude Code
session? generates a chat called like MCP MAKER - DEVICE IT'S ON", and "maybe
a designated Odysseus chat that is set on just modifying the website".

- MCP Maker: from an app in Settings > Devices, a chat named
  "MCP Maker · <app> · <device>" that hands the job to a coding agent: build
  an MCP server for that app in a copy of the Odysseus repo, wire it into
  Install/Update, open a PR. Its first turn starts at once.
- Odysseus development: one chat for changes to Odysseus itself. Its rules
  are pinned in "Needs to know", which every turn reads (even after pruning).
Both are agent-mode chats; the rules also go in as pinned notes, so they hold
however long the chat gets.
"""
import json
import logging
import os
import re
import subprocess
import uuid
from typing import Dict

from fastapi import APIRouter, HTTPException, Request

from core.models import ChatMessage

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _workspaces() -> str:
    from src.agent_tools.claude_code_tool import WORKSPACES_DIR
    return WORKSPACES_DIR


def _repo() -> str:
    """The GitHub repository PRs go to: this install's origin (the fork)."""
    try:
        url = subprocess.run(["git", "-C", _HERE, "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception:
        url = ""
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    return m.group(1) if m else "the fork (origin)"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:32] or "app"


def dev_rules(repo: str, ws: str) -> list:
    return [
        "This chat is for changing Odysseus itself (the website and app). Work only in "
        f"{ws}/odysseus, a clone of this install (clone it from https://github.com/{repo} if it is "
        "missing). Never edit the live install.",
        f"Each change: branch from origin/dev, commit, push, then open a PR against dev on {repo} with "
        f"gh (gh pr create -R {repo} -B dev). The user merges and deploys. Never merge, never push to "
        "dev or main, never force-push.",
        "Use a coding agent (claude_code; OpenCode unless the user names Claude) for the code, run "
        "the tests before opening the PR, and say in the PR what was checked.",
    ]


def mcp_maker_rules(app: str, device: str, os_name: str, slug: str, repo: str, ws: str) -> list:
    conv = "desktop_mcp_server.py" if os_name == "windows" else "linux_desktop_mcp_server.py"
    return [
        f"Task: an MCP server so Odysseus can control {app} on {device} ({os_name or 'unknown OS'}). "
        f"Work in {ws}/odysseus on a branch mcp-{slug} from origin/dev, and follow tools/mcp/{conv}.",
        f"Deliver: tools/mcp/{slug}_mcp_server.py with tests, wired into Install/Update (tools/enroll "
        f"scripts, SERVER_FILES and SERVER_KINDS in routes/enroll_routes.py), in a PR against dev on "
        f"{repo}. Never merge; never touch the live install.",
    ]


def mcp_maker_prompt(app: str, device: str, os_name: str, slug: str, repo: str, ws: str) -> str:
    conv = "desktop_mcp_server.py" if os_name == "windows" else "linux_desktop_mcp_server.py"
    return (
        f"[MCP maker · {app} on {device}]\n\n"
        f"The user wants Odysseus to be able to control **{app}** on **{device}** ({os_name or 'unknown OS'}); "
        "it has no MCP server yet. Build one with a coding agent (claude_code; OpenCode unless the user "
        "asks for Claude). Plan first, and show the plan for approval.\n\n"
        f"1. Work in {ws}/odysseus (clone https://github.com/{repo} there if missing), on a new branch "
        f"mcp-{slug} from origin/dev. Read tools/mcp/{conv} for the conventions: FastMCP 1.x over SSE, "
        "bound to the tailnet address only, one tool per clear action with a docstring saying when to "
        "use it, dict results, nothing secret in the output.\n"
        f"2. Find how {app} can be driven on {os_name or 'that machine'}: a command line, a local HTTP "
        "API, a scripting interface (COM on Windows, D-Bus on Linux), or its files. Prefer an official "
        "interface; screen automation only as a last resort. Say what you found.\n"
        f"3. Write tools/mcp/{slug}_mcp_server.py with tests, and wire it into Install/Update so the "
        f"user can put it on {device} from Settings > Devices: the install scripts in tools/enroll, and "
        "SERVER_FILES and SERVER_KINDS in routes/enroll_routes.py (use the next free port).\n"
        f"4. Commit, push the branch and open a PR against dev: gh pr create -R {repo} -B dev. Never "
        "merge, never touch the live install.\n\n"
        "Report with the PR link and a list of the tools and what each does."
    )


def setup_special_chat_routes(session_manager, get_mcp_manager=None) -> APIRouter:
    router = APIRouter(tags=["special_chats"])

    def _store_path() -> str:
        from src.constants import DATA_DIR
        return os.path.join(DATA_DIR, "special_chats.json")

    def _load() -> Dict:
        try:
            with open(_store_path(), encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, ValueError):
            return {}

    def _save(d: Dict) -> None:
        with open(_store_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)

    def _user(request: Request) -> str:
        from core.middleware import require_admin
        require_admin(request)                         # both hand work to coding agents
        try:
            from src.auth_helpers import effective_user
            return effective_user(request) or ""
        except Exception:
            return ""

    def _new_chat(name: str, body: Dict, owner: str):
        endpoint = str(body.get("endpoint_url") or "").strip()
        model = str(body.get("model") or "").strip()
        if not endpoint or not model:
            raise HTTPException(400, "Pick a model first (the chat uses the one you are using now)")
        sid = str(uuid.uuid4())
        sess = session_manager.create_session(sid, name, endpoint, model, owner=owner or None)
        try:                                           # an agent-mode chat
            from core.database import SessionLocal, Session as DbSession
            db = SessionLocal()
            try:
                row = db.query(DbSession).filter(DbSession.id == sid).first()
                if row is not None:
                    row.mode = "agent"
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("[special-chats] could not set agent mode on %s: %s", sid[:8], e)
        return sess

    def _pin(sid: str, rules: list, owner: str) -> None:
        from src import chat_memory
        for r in rules:
            try:
                chat_memory.add(sid, r, by="you", owner=owner)
            except ValueError as e:
                logger.warning("[special-chats] could not pin a rule in %s: %s", sid[:8], e)

    @router.post("/api/special-chats/mcp-maker")
    async def mcp_maker(request: Request):
        owner = _user(request)
        body = await request.json()
        app = " ".join(str(body.get("app") or "").split())[:60]
        if not app:
            raise HTTPException(400, "Which app?")
        from src import device_routing, machines
        if get_mcp_manager:
            mgr = get_mcp_manager()
        else:
            from src.tool_utils import get_mcp_manager as _g
            mgr = _g()
        dev = next((d for d in device_routing.all_devices(mgr) if d.get("server_id") == body.get("server_id")), None)
        if not dev:
            raise HTTPException(404, "No such computer")
        device = dev.get("name") or dev.get("server_id")
        peer = machines.find_peer(machines.peers(), dev.get("host") or "")
        os_name = (peer or {}).get("os") or ""
        slug, repo, ws = _slug(app), _repo(), _workspaces()
        sess = _new_chat(f"MCP Maker · {app} · {device}", body, owner)
        _pin(sess.id, mcp_maker_rules(app, device, os_name, slug, repo, ws), owner)
        from src.screen_control_resume import start_turn
        started = start_turn(sess.id, mcp_maker_prompt(app, device, os_name, slug, repo, ws),
                             note_source="mcp_maker", reply_source="mcp_maker_run")
        logger.info("[special-chats] MCP maker for %s on %s: chat %s (started=%s)", app, device, sess.id[:8], started)
        return {"id": sess.id, "name": sess.name, "started": started}

    @router.post("/api/special-chats/odysseus-dev")
    async def odysseus_dev(request: Request):
        owner = _user(request)
        body = await request.json()
        d = _load()
        sid = (d.get("odysseus_dev") or {}).get(owner or "_")
        if sid:
            try:
                sess = session_manager.get_session(sid)
                if sess and not getattr(sess, "archived", False):
                    return {"id": sid, "name": sess.name, "created": False}
            except KeyError:
                pass
        sess = _new_chat("Odysseus development", body, owner)
        repo, ws = _repo(), _workspaces()
        _pin(sess.id, dev_rules(repo, ws), owner)
        sess.add_message(ChatMessage("assistant", (
            "**Odysseus development.** This chat changes Odysseus itself. Ask for a feature or a fix and "
            f"I plan it, have a coding agent build it in {ws}/odysseus on a branch from dev, run the tests, "
            f"and open a PR against dev on {repo} for you to merge and deploy. I never edit the live install "
            "or merge anything. The rules are pinned in Needs to know."),
            metadata={"source": "odysseus_dev_intro"}))
        d.setdefault("odysseus_dev", {})[owner or "_"] = sess.id
        _save(d)
        return {"id": sess.id, "name": sess.name, "created": True}

    return router
