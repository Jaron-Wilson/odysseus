"""List, register and remove the user's devices."""

import json
from typing import Dict

from src import devices as registry


class ManageDevicesTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        raw = (content or "").strip()
        args: Dict = {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    args = parsed
            except json.JSONDecodeError:
                pass
        if not args:
            # Bare text is the action, which is how a small model will send it.
            args = {"action": raw or "list"}

        action = (args.get("action") or "list").strip().lower()

        if action == "list":
            devs = registry.list_devices()
            if not devs:
                return {
                    "output": (
                        "No devices registered yet. Register one with "
                        '{"action":"register","name":"pixel-8a","kind":"phone",'
                        '"commands":["notify","open_app"]} — the name is what the user '
                        "will call it, and the commands are what its automation actually "
                        "honours."
                    ),
                    "devices": [],
                    "exit_code": 0,
                }
            lines = [
                f"- **{d['name']}** ({d.get('kind', '?')}) — topic `{d.get('topic')}`, "
                f"handles: {', '.join(d.get('commands') or ['notify'])}"
                for d in devs
            ]
            return {"output": "\n".join(lines), "devices": devs, "exit_code": 0}

        if action == "capabilities":
            lines = [f"- `{k}` — {v}" for k, v in registry.KNOWN_COMMANDS.items()]
            return {
                "output": "Commands a device automation can be set up to honour:\n"
                          + "\n".join(lines)
                          + "\n\nInstalling apps and signing in are absent on purpose: a phone "
                            "will not do either from a push message.",
                "exit_code": 0,
            }

        if action == "register":
            try:
                rec = registry.register(
                    args.get("name") or "",
                    topic=args.get("topic") or "",
                    kind=args.get("kind") or "phone",
                    commands=args.get("commands") or ["notify"],
                )
            except ValueError as e:
                return {"error": str(e), "exit_code": 1}
            return {
                "output": (
                    f"Registered **{rec['name']}** ({rec['kind']}) on topic `{rec['topic']}` "
                    f"handling: {', '.join(rec['commands'])}.\n"
                    f"Subscribe that device to the `{rec['topic']}` topic on the ntfy server "
                    "for it to receive anything."
                ),
                "device": rec,
                "exit_code": 0,
            }

        if action in ("remove", "delete"):
            name = args.get("name") or ""
            if registry.remove(name):
                return {"output": f"Removed device {name}.", "exit_code": 0}
            return {"error": f"no device named {name}", "exit_code": 1}

        return {
            "error": "action must be 'list', 'register', 'remove' or 'capabilities'",
            "exit_code": 1,
        }
