"""List, register and remove the user's devices."""

import json
from typing import Dict

from src import devices as registry


def _computers_block() -> str:
    """The user's machines, so the agent can pick one: which are online,
    which the user prefers for heavy work, and what runs on each."""
    try:
        from routes.device_routes import _build_overview
        from src import machines
        text = machines.summary_for_agent(_build_overview())
    except Exception:
        return ""
    if not text:
        return ""
    return ("\n\nComputers on the tailnet:\n" + text +
            "\n\nWork any computer can do (wrangler, builds, git) can go to whichever is "
            "online. For heavy or GPU work (a Resolve render) use the preferred one first, "
            "and fall back to another online computer with the same tool if it is off. "
            "Reach a Linux or Mac computer with bash `ssh <host>`, and Windows through its "
            "MCP tools.")


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
            computers = _computers_block()
            if not devs:
                return {
                    "output": (
                        "No devices registered yet. Register one with "
                        '{"action":"register","name":"pixel-8a","kind":"phone",'
                        '"commands":["notify","open_app"]} — the name is what the user '
                        "will call it, and the commands are what its automation actually "
                        "honours." + computers
                    ),
                    "devices": [],
                    "exit_code": 0,
                }
            lines = []
            for d in devs:
                cmds = d.get("commands") or ["notify"]
                # Say plainly which claimed commands cannot work yet, rather
                # than letting the model try one and read the failure as the
                # phone being off.
                needs_ep = sorted(set(cmds) & registry.ENDPOINT_COMMANDS)
                if d.get("endpoint"):
                    where = f"listener `{d['endpoint']}`"
                elif needs_ep:
                    where = (f"**no endpoint** — {', '.join(needs_ep)} cannot be delivered "
                             f"until one is set")
                else:
                    where = "notification only"
                lines.append(f"- **{d['name']}** ({d.get('kind', '?')}) — {where}, "
                             f"handles: {', '.join(cmds)}")
            # Never hand the model the token; it has no use for it and it would
            # end up in chat history.
            safe = [{k: v for k, v in d.items() if k != "token"} for d in devs]
            return {"output": "\n".join(lines) + computers, "devices": safe, "exit_code": 0}

        if action == "capabilities":
            lines = [f"- `{k}` — {v}" for k, v in registry.KNOWN_COMMANDS.items()]
            return {
                "output": "Commands a device automation can be set up to honour:\n"
                          + "\n".join(lines)
                          + "\n\nAll but `notify` need the device to have an endpoint: a "
                            "listener Odysseus can reach over the tailnet. `install_app` only "
                            "opens the store page — Android will not let anything install "
                            "silently, and signing in is always the user's to do.",
                "exit_code": 0,
            }

        if action == "register":
            try:
                rec = registry.register(
                    args.get("name") or "",
                    topic=args.get("topic") or "",
                    kind=args.get("kind") or "phone",
                    commands=args.get("commands") or ["notify"],
                    endpoint=args.get("endpoint") or "",
                )
            except ValueError as e:
                return {"error": str(e), "exit_code": 1}
            if rec.get("endpoint"):
                how = (f"It will be sent commands at `{rec['endpoint']}`. The Modes app's "
                       "remote-control listener must be running and configured with this "
                       "device's token — the user can copy it from Settings → Devices.")
            else:
                how = ("No endpoint set, so only notifications will reach it. To let it "
                       "launch apps, register again with "
                       '`"endpoint":"http://<phone>.ts.net:8778"` once the Modes app\'s '
                       "remote-control listener is on.")
            return {
                "output": (
                    f"Registered **{rec['name']}** ({rec['kind']}) handling: "
                    f"{', '.join(rec['commands'])}.\n{how}"
                ),
                "device": {k: v for k, v in rec.items() if k != "token"},
                "exit_code": 0,
            }

        if action in ("control", "send", "command"):
            dev = registry.resolve(args.get("name") or args.get("device") or "")
            if not dev:
                return {"error": f"no device matching {args.get('name')!r}. "
                                 "Use action 'list' to see them.", "exit_code": 1}
            command = (args.get("command") or "").strip()
            if not command:
                return {"error": "command is required, e.g. "
                                 '{"action":"control","name":"phone","command":"open_app",'
                                 '"params":{"package":"com.bambulab.bambuhandy"}}',
                        "exit_code": 1}
            params = args.get("params")
            if not isinstance(params, dict):
                params = {}
            res = await registry.send_command(dev, command, params)
            if not res.get("ok"):
                return {"error": res.get("error") or "command failed", "exit_code": 1}
            return {"output": f"Sent `{command}` to **{dev['name']}**: {res.get('result')}",
                    "result": res.get("result"), "exit_code": 0}

        if action in ("remove", "delete"):
            name = args.get("name") or ""
            if registry.remove(name):
                return {"output": f"Removed device {name}.", "exit_code": 0}
            return {"error": f"no device named {name}", "exit_code": 1}

        return {
            "error": "action must be 'list', 'register', 'control', 'remove' or 'capabilities'",
            "exit_code": 1,
        }
