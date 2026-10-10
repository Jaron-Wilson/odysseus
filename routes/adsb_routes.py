"""The ADS-B receiver page (static/js/adsbPanel.js, src/adsb.py).

  GET /api/adsb/status   the receiver's decoding stats and current aircraft
  GET /api/adsb/config   the receiver address
  PUT /api/adsb/config   set it ({"url": "https://..."}; "" clears it)

Admin only: the receiver is a device on the host's private network, and its
address goes into the page's CSP.
"""
from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import adsb


def setup_adsb_routes() -> APIRouter:
    router = APIRouter(tags=["adsb"])

    @router.get("/api/adsb/status")
    async def status(request: Request):
        require_admin(request)
        return await adsb.fetch_status()

    @router.get("/api/adsb/config")
    async def get_config(request: Request):
        require_admin(request)
        return adsb.load_config()

    @router.put("/api/adsb/config")
    async def put_config(request: Request):
        require_admin(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Send JSON like {\"url\": \"https://...\"}")
        if not isinstance(body, dict) or not isinstance(body.get("url", ""), str):
            raise HTTPException(400, "Send JSON like {\"url\": \"https://...\"}")
        try:
            return adsb.save_config(body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    return router
