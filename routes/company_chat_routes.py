"""Company mentions in chats (src/company_chat.py, static/js/companyMentions.js).

  GET /api/company/mentions   what an @mention in the chat composer can point
                              at: the owner's companies, live agents and recent
                              issues. Feeds the autocomplete and the chips.
"""
from fastapi import APIRouter, Request

import core.database as cdb
from src import company_chat
from src.auth_helpers import require_user


def setup_company_chat_routes() -> APIRouter:
    r = APIRouter(prefix="/api/company", tags=["company"])

    @r.get("/mentions")
    def mentions(request: Request):
        user = require_user(request)
        db = cdb.SessionLocal()
        try:
            return company_chat.directory(db, user)
        finally:
            db.close()

    return r
