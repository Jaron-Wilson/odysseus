"""Game streaming tools for chats (src/games.py, docs/games.md): the PC's
Sunshine apps, what is running, a link that starts a stream, and Stop.

Admin only (src/tool_security.py NON_ADMIN_BLOCKED_TOOLS): Sunshine's
account controls the PC.

Launching: Sunshine starts an app only when a Moonlight client asks for a
stream (GameStream's /launch, which needs a paired client's certificate and
then the stream itself). Its admin API has no "start this app" call. So
game_launch does not start anything on its own: it returns a link to the
Games page that opens Moonlight Web's stream page for that game, which
starts it the moment the user opens the link in their browser.

Registration follows src/agent_tools/company_chat_tools.py: TOOL_TAGS in
src/agent_tools/__init__.py, schemas in src/tool_schemas.py, dispatch in
src/tool_execution.py, retrieval text in src/tool_index.py, plan-mode lists
in src/tool_security.py, availability in src/agent_loop.py (hidden until
Sunshine has credentials, offered when a message is about games).

Cheap to import: src/agent_tools/__init__.py reads the name sets early.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

READ_TOOLS = frozenset({"games_list", "game_status"})
WRITE_TOOLS = frozenset({"game_launch", "game_stop", "game_sync_steam"})
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS

SYNC_WAIT_S = 240

SCHEMAS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "games_list",
        "description": ("The games and apps the user can stream from their PC (Sunshine), and which one "
                        "is running. Read-only."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Optional words to filter the names by."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "game_status",
        "description": ("Game streaming status: is the PC online, does Sunshine answer, which game is "
                        "running, is Moonlight Web (the browser player) up. Read-only."),
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "game_launch",
        "description": ("Get the link that starts streaming a game from the PC in the browser. Sunshine "
                        "only starts a game when a player opens the stream, so give the user this link; "
                        "it starts the game when they open it."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "The game, as named in games_list (close is fine)."}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "game_stop",
        "description": "Close the game or app running on the PC's Sunshine (ends the stream). Only when the user asks.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "game_sync_steam",
        "description": ("Add the Steam games installed on the PC that are missing from Sunshine, with "
                        "their covers; reports what was added and what is no longer installed."),
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

TOOL_DESCRIPTIONS: Dict[str, str] = {
    "games_list": "List the PC games the user can stream (Sunshine apps: Steam games, Desktop, Big Picture) and which one is running. 'What games do I have', 'list my games', 'what can I play', 'is anything running on the PC'.",
    "game_status": "Game streaming status: whether the gaming PC is online, Sunshine is reachable, which game is running, and whether Moonlight Web (the browser game player at /play/) is up. 'Is my PC ready to play', 'is the game still running', 'is game streaming working'.",
    "game_launch": "Start streaming a PC game in the browser: returns the link that opens Moonlight Web on that game, which launches it on the PC. 'Launch Elden Ring', 'play Hades', 'start a game', 'stream Rocket League'.",
    "game_stop": "Stop / close / quit the game running on the gaming PC through Sunshine. 'Stop the game', 'close the game on my PC', 'quit what is streaming'.",
    "game_sync_steam": "Sync the PC's installed Steam games into Sunshine (the game streaming host) with covers: 'sync my steam games', 'add my new games', 'update the games list'.",
}

TOOL_SECTIONS: Dict[str, str] = {
    "games_list": "- ```games_list```: The PC games the user can stream and which is running: {} or {\"query\": \"souls\"}. Read-only.",
    "game_status": "- ```game_status```: PC online, Sunshine reachable, running game, Moonlight Web up: {}. Read-only.",
    "game_launch": "- ```game_launch```: The link that starts streaming a game: {\"name\": \"Hades\"}. Give the user the link; it starts when they open it.",
    "game_stop": "- ```game_stop```: Close the running game on the PC: {}.",
    "game_sync_steam": "- ```game_sync_steam```: Add the PC's installed Steam games to Sunshine with covers: {}.",
}

_TOPIC_RE = re.compile(
    r"\b(games?|gaming|steam|sunshine|moonlight|game ?stream(?:ing)?|big picture|my pc|gaming pc)\b", re.I)


def hidden_tools(owner: Optional[str] = None) -> set:
    """All of them until Sunshine has credentials (settings or the env file)."""
    from src import games
    return set() if games.is_configured() else set(ALL_TOOLS)


def relevant_tools(owner: Optional[str], text: str) -> set:
    if not text or not _TOPIC_RE.search(text):
        return set()
    return set(ALL_TOOLS) - hidden_tools(owner)


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {"name": raw} if raw else {}


def _running_line(st: Dict[str, Any]) -> str:
    r = st.get("running")
    if not r:
        return "Nothing is running."
    return f"Running: {r['name']}." if r.get("name") else f"An app is running (Sunshine app id {r.get('app_id')})."


async def _list(args) -> str:
    from src import games
    apps = await games.list_apps()
    st = await games.status()
    q = (args.get("query") or "").strip().lower()
    rows = [a for a in apps if not q or q in a["name"].lower()]
    if not rows:
        return f"No apps{' matching ' + repr(q) if q else ''} in Sunshine ({len(apps)} in all)."
    lines = [f"{len(rows)} of {len(apps)} apps in Sunshine. {_running_line(st)}"]
    run = (st.get("running") or {}).get("name")
    for a in rows:
        mark = " (running)" if run and a["name"] == run else ""
        lines.append(f"- {a['name']}{mark}" + (f" [Steam {a['steam_appid']}]" if a.get("steam_appid") else ""))
    lines.append("Games page: /#games")
    return "\n".join(lines)


async def _status(args) -> str:
    from src import games
    h = await games.health()
    pc = h.get("pc") or {}
    sun = h.get("sunshine") or {}
    ml = h.get("moonlight") or {}
    lines = [
        f"PC: {pc.get('name') or 'unknown'} - " + ("online" if pc.get("online") else
                                                   "offline" if pc.get("online") is False else "not on the tailnet list"),
        "Sunshine: " + (f"answering at {sun.get('url')} ({h.get('apps', 0)} apps)" if sun.get("ok")
                        else f"not reachable: {sun.get('error') or 'unknown'}"),
        _running_line(h),
        "Moonlight Web: " + ("up" if ml.get("ok") else f"down ({ml.get('detail')})" if ml.get("ok") is False
                             else "not checked"),
    ]
    return "\n".join(lines)


async def _launch(args) -> str:
    from src import games
    name = (args.get("name") or args.get("game") or "").strip()
    if not name:
        return "Say which game: game_launch {\"name\": \"...\"}"
    apps = await games.list_apps()
    app = games.find_app(apps, name)
    if app is None:
        some = ", ".join(a["name"] for a in apps[:12])
        raise games.GamesError(f"No app called {name!r} in Sunshine. Some there are: {some}")
    st = await games.status()
    link = games.launch_link(app["name"])
    run = st.get("running")
    note = ""
    if run and run.get("name") and run["name"] != app["name"]:
        note = f"\n{run['name']} is running now; opening the link asks Moonlight to switch, or use game_stop first."
    label = app["name"].replace("[", "(").replace("]", ")")       # keep the Markdown link whole
    return (f"[Play {label}]({link}) opens the Games page and starts the stream in Moonlight Web, "
            f"which launches {app['name']} on the PC. Nothing starts until the user opens it: Sunshine "
            f"only launches a game for a Moonlight client that is streaming.{note}\n"
            f"Moonlight Web itself: {st.get('moonlight_url') or '/play/'}")


async def _stop(args) -> str:
    from src import games
    st = await games.status(force=True)
    was = (st.get("running") or {}).get("name")
    await games.stop_app()
    return f"Closed {was} on the PC." if was else "Asked Sunshine to close the running app (it said nothing was named as running)."


async def _sync(args) -> str:
    from src import games
    state = games.start_sync()
    waited = 0.0
    while games.sync_state().get("running") and waited < SYNC_WAIT_S:
        await asyncio.sleep(1.0)
        waited += 1.0
    s = games.sync_state()
    if s.get("running"):
        return f"The Steam sync is still running ({s.get('step') or 'working'}); the Games page shows the result when it ends."
    if s.get("error"):
        raise games.GamesError(s["error"])
    return games.summarize_sync(s.get("result") or state.get("result") or {"added": [], "already": []})


_RUNNERS = {"games_list": _list, "game_status": _status, "game_launch": _launch,
            "game_stop": _stop, "game_sync_steam": _sync}


async def run_tool(name: str, content: str, owner: Optional[str] = None) -> Dict[str, Any]:
    from src import games
    if name not in ALL_TOOLS:
        return {"error": f"Unknown game tool {name}", "exit_code": 1}
    try:
        return {"output": await _RUNNERS[name](_args(content)), "exit_code": 0}
    except games.GamesError as e:
        return {"error": str(e), "exit_code": 1}
