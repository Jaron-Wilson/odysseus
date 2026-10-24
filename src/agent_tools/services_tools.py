"""Agent tool to inspect and control long-lived background hosting services.

Provides the `manage_services` tool for agents to list, inspect, and safely
stop hosted services (web servers, APIs, background daemons) launched by Odysseus.
Services are scoped to their owner/chat with verified process identity.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

_LIST_ACTIONS = {"list", "ls", "services"}
_STATUS_ACTIONS = {"status", "info", "get"}
_LOGS_ACTIONS = {"logs", "log", "tail", "output"}
_STOP_ACTIONS = {"stop", "kill", "cancel", "terminate"}
_LAUNCH_ACTIONS = {"launch", "start", "run"}


def _row(s: Dict[str, Any]) -> str:
    port_str = f":{s.get('port')}" if s.get('port') else ""
    lh = f" [{s.get('bind_label')}]" if s.get('port') else ""
    cmd = (s.get("command") or "").strip().splitlines()[0][:60]
    return f"[{s.get('id')}] {s.get('status')} {s.get('readiness', '')}{port_str}{lh} | {s.get('name')} | {cmd}"


class ManageServicesTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import hosting_tracker

        owner = ctx.get("owner") or ""
        session_id = ctx.get("session_id") or ""
        workspace = ctx.get("workspace") or ""

        raw = (content or "").strip()
        try:
            args = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}

        action = str(args.get("action", "list")).strip().lower()
        service_id = str(args.get("service_id") or args.get("id") or "").strip()

        if action in _LIST_ACTIONS:
            services: List[Dict[str, Any]] = hosting_tracker.list_services(owner)
            if not services:
                return {"output": "No background hosting services currently running.", "exit_code": 0}
            lines = "\n".join(_row(s) for s in services)
            return {"output": f"{len(services)} hosting service(s):\n{lines}", "exit_code": 0}

        if action in _LAUNCH_ACTIONS:
            cmd = str(args.get("command") or "").strip()
            if not cmd:
                return {"error": "manage_services launch: requires a 'command' to run.", "exit_code": 1}
            name = str(args.get("name") or "").strip()
            cwd = str(args.get("cwd") or workspace or "").strip() or None
            try:
                svc = hosting_tracker.launch_service(
                    cmd, cwd=cwd, owner=owner, name=name, session_id=session_id
                )
                url_msg = f" Reachable at {svc['url']} ({svc['bind_label']})." if svc.get('url') else ""
                return {
                    "output": f"Launched service `{svc['id']}` ({svc['name']}). Status: {svc['status']}.{url_msg}",
                    "service_id": svc["id"],
                    "service": svc,
                    "exit_code": 0,
                }
            except Exception as e:
                return {"error": f"Failed to launch service: {e}", "exit_code": 1}

        if action in _STATUS_ACTIONS or action in _LOGS_ACTIONS or action in _STOP_ACTIONS:
            if not service_id:
                return {"error": f"manage_services: action '{action}' requires a 'service_id'.", "exit_code": 1}

            svc = hosting_tracker.get_service(service_id, user=owner)
            if not svc:
                return {"error": f"manage_services: service '{service_id}' not found.", "exit_code": 1}

            if action in _STOP_ACTIONS:
                if svc.get("status") in ("stopped", "failed", "dead"):
                    return {"output": f"Service `{service_id}` is already {svc.get('status')}; nothing to stop.", "exit_code": 0}
                try:
                    stopped = hosting_tracker.stop_service(service_id, user=owner, confirm=True)
                    return {
                        "output": f"Stopped hosting service `{service_id}` ({stopped.get('name')}).",
                        "service": stopped,
                        "exit_code": 0,
                    }
                except Exception as e:
                    return {"error": f"Failed to stop service: {e}", "exit_code": 1}

            if action in _LOGS_ACTIONS:
                logs = hosting_tracker.get_logs(service_id, user=owner, max_lines=200)
                return {
                    "output": f"Logs for `{service_id}` ({svc.get('name')}):\n\n{logs}",
                    "exit_code": 0,
                }

            # Status action
            url_line = f"URL: {svc.get('url')} ({svc.get('bind_label')})\n" if svc.get('url') else ""
            return {
                "output": (
                    f"Service `{service_id}`:\n"
                    f"Name: {svc.get('name')}\n"
                    f"Status: {svc.get('status')} ({svc.get('readiness_detail')})\n"
                    f"{url_line}"
                    f"Command: {svc.get('command')}\n"
                    f"Started: {svc.get('elapsed_s')}s ago\n"
                    f"PID: {svc.get('pid') or 'none'}"
                ),
                "service": svc,
                "exit_code": 0,
            }

        return {"error": f"manage_services: unknown action '{action}'. Use list, launch, status, logs, or stop.", "exit_code": 1}
