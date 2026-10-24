"""API endpoints for the background hosting-service tracker — /api/services/*.

Provides endpoints to list running/persisted services, view redacted logs,
and safely stop services with confirmation and process-identity verification.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src import hosting_tracker
from src.auth_helpers import _auth_disabled, get_current_user

logger = logging.getLogger(__name__)

_SERVICE_ID_RE = re.compile(r"^srv_[0-9a-f]{8}$")


class LaunchServiceRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    name: Optional[str] = None
    session_id: Optional[str] = None


class RegisterServiceRequest(BaseModel):
    pid: int
    name: Optional[str] = None
    command: Optional[str] = None
    cwd: Optional[str] = None
    session_id: Optional[str] = None
    port: Optional[int] = None
    log_path: Optional[str] = None


class StopServiceRequest(BaseModel):
    confirm: bool = False


def setup_hosting_routes() -> APIRouter:
    router = APIRouter(prefix="/api/services", tags=["hosting_services"])

    def _require_user(request: Request) -> str:
        user = get_current_user(request)
        if not user:
            if _auth_disabled():
                return ""
            raise HTTPException(401, "Not authenticated")
        return user

    def _validate_id(service_id: str) -> None:
        if not _SERVICE_ID_RE.match(service_id):
            raise HTTPException(400, "Invalid service ID format")

    @router.get("")
    async def list_services(request: Request):
        """List running and persisted services visible to the current user."""
        user = _require_user(request)
        services = hosting_tracker.list_services(user)
        return {"services": services, "count": len(services)}

    @router.post("/launch")
    async def launch_service(request: Request, body: LaunchServiceRequest):
        """Launch a long-lived service in the background."""
        user = _require_user(request)
        cmd = (body.command or "").strip()
        if not cmd:
            raise HTTPException(400, "Command cannot be empty")
        try:
            svc = hosting_tracker.launch_service(
                cmd,
                cwd=body.cwd,
                owner=user,
                name=body.name or "",
                session_id=body.session_id or "",
            )
            return {"ok": True, "service": svc}
        except Exception as e:
            logger.error("Failed to launch service: %s", e)
            raise HTTPException(500, f"Failed to launch service: {e}")

    @router.post("/register")
    async def register_service(request: Request, body: RegisterServiceRequest):
        """Register an existing Odysseus-managed background process as a hosting service."""
        user = _require_user(request)
        try:
            svc = hosting_tracker.register_service(
                body.pid,
                name=body.name or "",
                command=body.command or "",
                cwd=body.cwd,
                owner=user,
                session_id=body.session_id or "",
                port=body.port,
                log_path=body.log_path,
            )
            return {"ok": True, "service": svc}
        except ValueError as ve:
            raise HTTPException(400, str(ve))
        except Exception as e:
            logger.error("Failed to register service: %s", e)
            raise HTTPException(500, f"Failed to register service: {e}")

    @router.get("/{service_id}")
    async def get_service(request: Request, service_id: str):
        """Get details and status for a service."""
        user = _require_user(request)
        _validate_id(service_id)
        svc = hosting_tracker.get_service(service_id, user)
        if not svc:
            raise HTTPException(404, "No such service")
        return {"service": svc}

    @router.get("/{service_id}/logs")
    async def get_service_logs(request: Request, service_id: str, lines: int = 500):
        """Get secret-redacted log output for a service."""
        user = _require_user(request)
        _validate_id(service_id)
        svc = hosting_tracker.get_service(service_id, user)
        if not svc:
            raise HTTPException(404, "No such service")
        logs = hosting_tracker.get_logs(service_id, user=user, max_lines=max(1, min(lines, 2000)))
        return {"id": service_id, "logs": logs}

    @router.post("/{service_id}/stop")
    async def stop_service(request: Request, service_id: str, body: StopServiceRequest):
        """Safely stop a service with explicit confirmation and PID-reuse safety."""
        user = _require_user(request)
        _validate_id(service_id)
        if not body.confirm:
            raise HTTPException(400, "Confirmation required to stop service (confirm=true)")

        try:
            svc = hosting_tracker.stop_service(service_id, user=user, confirm=True)
            return {"ok": True, "service": svc}
        except KeyError:
            raise HTTPException(404, "No such service")
        except RuntimeError as re_err:
            raise HTTPException(409, str(re_err))
        except Exception as e:
            logger.error("Failed to stop service %s: %s", service_id, e)
            raise HTTPException(500, f"Failed to stop service: {e}")

    return router
