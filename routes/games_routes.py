"""The Games page and its settings (static/js/gamesPanel.js,
static/js/gamesSettings.js, src/games.py).

  GET  /api/games/apps          Sunshine's apps (names, kind, cover URL; no commands)
  GET  /api/games/status        PC online, Sunshine reachable, what is running
  GET  /api/games/cover/{key}   a cover image, from this server's covers folder or cache
  POST /api/games/stop          close the running app (Sunshine POST /api/apps/close)
  POST /api/games/sync          start the Steam sync; GET it for progress and the result
  GET  /api/games/health        Sunshine and Moonlight Web, for Settings > Devices
  GET  /api/games/config        the settings, without the password
  PUT  /api/games/config        change them
  POST /api/games/test          try Sunshine with the saved settings
  POST /api/games/forget-certificates   accept a new Sunshine certificate on next contact
  GET  /api/games/pairings      pairing requests waiting for a PIN
  POST /api/games/pair          {pairing_id, pin, name}

All admin only: the PC is a machine on the host's private network, and
Sunshine's account controls it.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from core.middleware import require_admin
from src import games


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Send JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Send a JSON object")
    return body


def setup_games_routes() -> APIRouter:
    router = APIRouter(tags=["games"])

    @router.get("/api/games/apps")
    async def apps(request: Request):
        require_admin(request)
        try:
            return {"apps": await games.list_apps()}
        except games.GamesError as e:
            return {"apps": [], "error": str(e)}

    @router.get("/api/games/status")
    async def status(request: Request):
        require_admin(request)
        return await games.status(force=request.query_params.get("refresh") == "1")

    @router.get("/api/games/cover/{key}")
    async def cover(key: str, request: Request):
        require_admin(request)
        path = await games.cover_path(key)
        if not path:
            raise HTTPException(404, "No cover")
        media = "image/png" if path.endswith(".png") else "image/jpeg"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=86400"})

    @router.post("/api/games/stop")
    async def stop(request: Request):
        require_admin(request)
        try:
            return await games.stop_app()
        except games.GamesError as e:
            raise HTTPException(502, str(e))

    @router.post("/api/games/sync")
    async def sync_start(request: Request):
        require_admin(request)
        if not games.is_configured():
            raise HTTPException(400, "Set up Sunshine in Settings > Devices > Game streaming first")
        return games.start_sync()

    @router.get("/api/games/sync")
    async def sync_get(request: Request):
        require_admin(request)
        return games.sync_state()

    @router.get("/api/games/health")
    async def health(request: Request):
        require_admin(request)
        return await games.health()

    @router.get("/api/games/config")
    async def get_config(request: Request):
        require_admin(request)
        return games.public_config()

    @router.put("/api/games/config")
    async def put_config(request: Request):
        require_admin(request)
        try:
            return games.save_config(await _body(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.post("/api/games/test")
    async def test(request: Request):
        require_admin(request)
        return await games.test_connection()

    @router.post("/api/games/forget-certificates")
    async def forget(request: Request):
        require_admin(request)
        return games.forget_certificates()

    @router.get("/api/games/pairings")
    async def pairings(request: Request):
        require_admin(request)
        try:
            return {"pairings": await games.pending_pairings(), "clients": await games.clients()}
        except games.GamesError as e:
            return {"pairings": [], "clients": [], "error": str(e)}

    @router.post("/api/games/pair")
    async def pair(request: Request):
        require_admin(request)
        body = await _body(request)
        try:
            return await games.submit_pin(str(body.get("pairing_id") or ""), str(body.get("pin") or ""),
                                          str(body.get("name") or ""))
        except games.GamesError as e:
            raise HTTPException(400, str(e))

    return router
