"""Plans: Markdown plans the user writes and keeps (src/plans.py, static/js/plans.js).

  GET    /api/plans?q=            the owner's plans, newest first; q searches titles and text
  POST   /api/plans               create {"title": "...", "content"?: "..."}
  GET    /api/plans/{name}        one plan with its text
  PUT    /api/plans/{name}        save {"content": "...", "base_version"?: "..."}
  POST   /api/plans/{name}/rename {"title": "..."}
  DELETE /api/plans/{name}        moves it to the owner's .trash folder

Per user: each account has its own folder. No model is ever called.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src import plans
from src.auth_helpers import require_user


class PlanCreate(BaseModel):
    title: str = ""
    content: Optional[str] = None


class PlanSave(BaseModel):
    content: str
    base_version: Optional[str] = None


class PlanRename(BaseModel):
    title: str


def _run(fn, *args):
    try:
        return fn(*args)
    except plans.PlanError as e:
        raise HTTPException(e.status, e.message)


def setup_plans_routes() -> APIRouter:
    router = APIRouter(tags=["plans"])

    @router.get("/api/plans")
    async def list_plans(request: Request, q: str = ""):
        owner = require_user(request)
        return {"plans": _run(plans.list_plans, owner, q[:200])}

    @router.post("/api/plans")
    async def create_plan(request: Request, body: PlanCreate):
        owner = require_user(request)
        return _run(plans.create_plan, owner, body.title, body.content)

    @router.get("/api/plans/{name}")
    async def get_plan(request: Request, name: str):
        owner = require_user(request)
        return _run(plans.get_plan, owner, name)

    @router.put("/api/plans/{name}")
    async def save_plan(request: Request, name: str, body: PlanSave):
        owner = require_user(request)
        return _run(plans.save_plan, owner, name, body.content, body.base_version)

    @router.post("/api/plans/{name}/rename")
    async def rename_plan(request: Request, name: str, body: PlanRename):
        owner = require_user(request)
        return _run(plans.rename_plan, owner, name, body.title)

    @router.delete("/api/plans/{name}")
    async def delete_plan(request: Request, name: str):
        owner = require_user(request)
        return _run(plans.delete_plan, owner, name)

    return router
