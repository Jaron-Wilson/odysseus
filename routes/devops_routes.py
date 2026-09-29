"""The DevOps page (static/js/devopsPage.js): src/devops_stats.py."""
import asyncio

from fastapi import APIRouter, Request

from core.middleware import require_admin


def setup_devops_routes() -> APIRouter:
    router = APIRouter(tags=["devops"])

    @router.get("/api/devops")
    async def devops(request: Request, hours: float = 24):
        require_admin(request)             # server-wide: every chat and run
        from src import devops_stats
        return await asyncio.to_thread(devops_stats.snapshot, hours)

    return router
