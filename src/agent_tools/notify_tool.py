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

        # A command that needs the device to *do* something goes to its
        # listener when it has one. Seen live: "open Amazon on my Pixel" went
        # out as a push carrying command=open_url; the push was delivered, but
        # a browser notification cannot open anything by itself, so nothing
        # happened -- while the Modes listener could have opened the page.
        if cmd and device and device.get("endpoint"):
            try:
                from src import devices as device_registry
                if cmd in device_registry.ENDPOINT_COMMANDS and device_registry.supports(device, cmd):
                    arg = str(args.get("command_arg") or args.get("click") or "").strip()
                    params = ({"url": arg} if cmd == "open_url" else
                              {"package": arg} if cmd in ("open_app", "install_app") else
                              {"arg": arg, "message": message})
                    r = await device_registry.send_command(device, cmd, params)
                    if r.get("ok"):
                        return {"output": f"Done on {device['name']} through its listener: {cmd} {arg}".strip(),
                                "result": r.get("result"), "exit_code": 0}
                    logger.info("listener %s failed on %s: %s; falling back to push",
                                cmd, device.get("name"), r.get("error"))
            except Exception as e:
                logger.debug("listener route failed: %s", e)

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
            # Seen live: "open this link on my phone" went out as a push, found
            # no subscription under the phone's name, and the model then
            # invented a cause ("the WebSocket drops when the phone locks")
            # while the phone's listener sat there able to open the link
            # directly. So when there is a link and the device can open one,
            # open it that way, and otherwise say exactly why and what works.
            link = str(args.get("click") or "").strip()
            if device and link.startswith(("http://", "https://")):
                try:
                    from src import devices as device_registry
                    if device_registry.supports(device, "open_url") and device.get("endpoint"):
                        r = await device_registry.send_command(device, "open_url", {"url": link})
                        if r.get("ok"):
                            return {
                                "output": (f"No push subscription reaches {device['name']}, so the "
                                           f"link was opened on it directly through its listener: {link}"),
                                "sent": 0, "opened": link, "exit_code": 0,
                            }
                except Exception as e:
                    logger.debug("open_url fallback failed: %s", e)
            detail = "; ".join(res.get("errors") or []) or res.get("detail", "")
            if device and res.get("detail") == "no matching subscriptions":
                why = (f"No notification subscription is linked to {device['name']}. Its browser "
                       f"subscribed under its own name; link it to {device['name']} in "
                       f"Settings → Devices.")
            else:
                why = f"Nothing was delivered: {detail}"
            hint = ""
            if device and device.get("endpoint"):
                hint = (f" To open a link or an app on {device['name']} now, use manage_devices "
                        f"{{\"action\":\"control\",\"name\":\"{device['name']}\",\"command\":\"open_url\","
                        f"\"params\":{{\"url\":\"...\"}}}} -- it goes to the device's listener with "
                        f"its token and needs no tap.")
            return {
                "error": why + hint + " This is the actual reason; do not guess another.",
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
