"""The Paperclip page (src/paperclip.py), read-only.

  GET /api/paperclip/status                         health + who-am-i
  GET /api/paperclip/config                         {url, company_id, has_token}
  PUT /api/paperclip/config                         {url, token?, clear_token?, company_id?}
  GET /api/paperclip/companies
  GET /api/paperclip/companies/{cid}/dashboard
  GET /api/paperclip/companies/{cid}/agents
  GET /api/paperclip/companies/{cid}/issues         ?status=&q=
  GET /api/paperclip/companies/{cid}/runs           ?agent_id=
  GET /api/paperclip/companies/{cid}/live-runs
  GET /api/paperclip/companies/{cid}/activity

Admin only: the settings hold a Paperclip board token. Upstream failures
come back as {"ok": false, "error": "<code>"} with HTTP 200 (like the
ADS-B page); a malformed id or filter is a 400.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import paperclip

_BAD_INPUT = {"invalid_company_id", "invalid_status", "invalid_agent_id"}
_PUT_HELP = "Send JSON like {\"url\": \"https://...\", \"token\": \"...\", \"company_id\": \"<uuid>\"}"


def _out(result):
    if isinstance(result, dict) and result.get("error") in _BAD_INPUT:
        raise HTTPException(400, result["error"])
    return result


def setup_paperclip_routes() -> APIRouter:
    router = APIRouter(tags=["paperclip"])

    @router.get("/api/paperclip/status")
    async def status(request: Request):
        require_admin(request)
        return await paperclip.fetch_status()

    @router.get("/api/paperclip/config")
    async def get_config(request: Request):
        require_admin(request)
        return paperclip.public_config()

    @router.put("/api/paperclip/config")
    async def put_config(request: Request):
        require_admin(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, _PUT_HELP)
        if not isinstance(body, dict):
            raise HTTPException(400, _PUT_HELP)
        url = body.get("url", "")
        token = body.get("token")
        clear_token = body.get("clear_token", False)
        company_id = body.get("company_id")
        if (not isinstance(url, str) or not isinstance(token, (str, type(None)))
                or not isinstance(clear_token, bool) or not isinstance(company_id, (str, type(None)))):
            raise HTTPException(400, _PUT_HELP)
        try:
            return paperclip.save_config(url, token=token, clear_token=clear_token, company_id=company_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/api/paperclip/companies")
    async def companies(request: Request):
        require_admin(request)
        return await paperclip.list_companies()

    @router.get("/api/paperclip/companies/{cid}/dashboard")
    async def dashboard(cid: str, request: Request):
        require_admin(request)
        return _out(await paperclip.dashboard(cid))

    @router.get("/api/paperclip/companies/{cid}/agents")
    async def agents(cid: str, request: Request):
        require_admin(request)
        return _out(await paperclip.agents(cid))

    @router.get("/api/paperclip/companies/{cid}/issues")
    async def issues(cid: str, request: Request, status: Optional[str] = None, q: Optional[str] = None):
        require_admin(request)
        return _out(await paperclip.issues(cid, status=status, q=q))

    @router.get("/api/paperclip/companies/{cid}/runs")
    async def runs(cid: str, request: Request, agent_id: Optional[str] = None):
        require_admin(request)
        return _out(await paperclip.runs(cid, agent_id=agent_id))

    @router.get("/api/paperclip/companies/{cid}/live-runs")
    async def live_runs(cid: str, request: Request):
        require_admin(request)
        return _out(await paperclip.live_runs(cid))

    @router.get("/api/paperclip/companies/{cid}/activity")
    async def activity(cid: str, request: Request):
        require_admin(request)
        return _out(await paperclip.activity(cid))

    return router
