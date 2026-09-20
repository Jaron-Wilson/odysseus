"""Push a notification, or a command, to one of the user's devices.

Delivery is Web Push: the browser is the client, so a device that has opened
Odysseus and enabled notifications can be reached with nothing else installed,
and tapping a notification returns to the page that sent it.

A plain notification is what the user reads. A command additionally carries a
`command` field that an automation on the device can act on — launching an
app, starting a timer — rather than just displaying text. The registry records
what each device actually honours, and a command it never claimed is refused
here rather than sent into the void for nobody to act on.

Nothing here can make a device do what it has not already agreed to. Android
will not install software or type credentials because a notification asked, and
this deliberately does not pretend otherwise.
"""

import json
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class NotifyDeviceTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        raw = (content or "").strip()
        parsed_json = False
        args: Dict = {}
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
                parsed_json = isinstance(args, dict)
            except (json.JSONDecodeError, TypeError):
                parsed_json = False
        # Only treat the body as bare text when it was never JSON. Falling back
        # on an empty object instead sends the literal "{}" as the message.
        if not parsed_json:
            args = {"message": raw}

        message = (args.get("message") or "").strip()
        if not message:
            return {"error": "message is required", "exit_code": 1}

        # Resolve a named device so "my phone" means a particular phone, and so
        # a command can be checked against what that device honours.
        want = (args.get("device") or "").strip()
        device = None
        if want:
            try:
                from src import devices as device_registry
                device = device_registry.resolve(want)
            except Exception as e:
                logger.debug("device lookup failed: %s", e)

        cmd = (args.get("command") or "").strip()
        if cmd and device:
            try:
                from src import devices as device_registry
                if not device_registry.supports(device, cmd):
                    return {
                        "error": (
                            f"{device['name']} does not support `{cmd}`. It handles: "
                            f"{', '.join(device.get('commands') or ['notify'])}. "
                            "Send a plain notification instead, or add the capability to "
                            "that device's automation first."
                        ),
                        "exit_code": 1,
                    }
            except Exception:
                pass

        try:
            from src import webpush
        except Exception as e:
            return {"error": f"web push unavailable: {e}", "exit_code": 1}

        if not webpush.load_subscriptions():
            return {
                "error": (
                    "No device has enabled notifications yet. Open Odysseus on the device "
                    "and turn them on under Settings → How you're reminded."
                ),
                "exit_code": 1,
            }

        body = message
        if cmd:
            # Carried in the payload for an on-device automation to match on.
            body = f"{message}"
        payload_url = args.get("click") or "/"

        res = await webpush.send(
            (args.get("title") or "Odysseus")[:200],
            body,
            device=(device.get("name") if device else want),
            url=payload_url,
            tag=(args.get("tag") or "odysseus"),
            command=cmd,
            command_arg=str(args.get("command_arg") or ""),
        )

        if not res.get("sent"):
            return {
                "error": (
                    f"Nothing was delivered. {'; '.join(res.get('errors') or []) or res.get('detail', '')}"
                ),
                "exit_code": 1,
            }

        target = device["name"] if device else (want or "all devices")
        return {
            "output": (
                f"Sent to {target} ({res['sent']} subscription(s)): {message[:200]}"
                + (f"\nCommand `{cmd}` included; the device acts on it only if its "
                   "automation listens for that." if cmd else "")
            ),
            "sent": res["sent"],
            "failed": res.get("failed", 0),
            "exit_code": 0,
        }
