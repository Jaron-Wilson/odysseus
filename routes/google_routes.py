"""Settings > Google: the user's Google connection and its products.

    GET  /api/google/status                       account, granted scopes, each product
    GET  /api/google/connect?products=docs,sheets page navigation to Google's consent
    POST /api/google/products/{product}/disconnect stop using one product
    POST /api/google/disconnect                   revoke at Google and forget it
    PUT  /api/google/client                       the server's OAuth client (admin)

The connection itself: src/google_workspace/connection.py. Everything is
the signed-in user's own; nothing secret is ever returned.
"""

import logging
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from src.auth_helpers import require_user
from src.google_workspace import connection
from src.google_workspace import products as P

logger = logging.getLogger(__name__)


def _is_admin(request: Request, owner: Optional[str]) -> bool:
    from routes.telephony_routes import _is_admin as tel_is_admin
    return tel_is_admin(request.app, owner)


def view(request: Request, user: Optional[str]) -> Dict:
    from src import google_oauth
    out = connection.status(user)
    out["redirect_uri"] = google_oauth.redirect_uri(request)
    out["can_edit_client"] = _is_admin(request, user)
    if out["can_edit_client"]:
        out["client_id"] = google_oauth.client_config()["client_id"]
    return out


def setup_google_routes() -> APIRouter:
    router = APIRouter(prefix="/api/google", tags=["google"])

    @router.get("/status")
    async def status(request: Request):
        user = require_user(request) or None
        return view(request, user)

    @router.get("/connect")
    async def connect(request: Request, products: str = ""):
        """A page navigation, not a fetch: it ends on Google's consent
        screen, which comes back to /api/auth/google/callback."""
        user = require_user(request) or None
        try:
            return RedirectResponse(connection.connect_url(request, user, connection.clean_products(products)),
                                    status_code=303)
        except connection.GoogleError as e:
            raise HTTPException(400, str(e))

    @router.post("/products/{product}/disconnect")
    async def disconnect_product(request: Request, product: str):
        user = require_user(request) or None
        if product not in P.PRODUCTS:
            raise HTTPException(404, "No such Google product.")
        await connection.disconnect(user, product)
        return view(request, user)

    @router.post("/disconnect")
    async def disconnect(request: Request):
        user = require_user(request) or None
        await connection.disconnect(user)
        return view(request, user)

    @router.put("/client")
    async def client(request: Request):
        """The server's Google OAuth client (the one Sign in with Google uses
        too). Admin only; the secret is saved, never sent back."""
        user = require_user(request) or None
        if not _is_admin(request, user):
            raise HTTPException(403, "Only an admin can set the server's Google OAuth client.")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object.")
        cid = str(body.get("client_id") or "").strip()
        secret = str(body.get("client_secret") or "").strip()
        if not cid.endswith(".apps.googleusercontent.com"):
            raise HTTPException(400, "A Google OAuth client ID ends in .apps.googleusercontent.com.")
        from src import google_oauth
        if not secret and not google_oauth.client_config()["client_secret"]:
            raise HTTPException(400, "Paste the client secret too.")
        google_oauth.save_client(cid, secret)
        logger.info("[google] OAuth client saved by %s", user or "-")
        return view(request, user)

    return router
