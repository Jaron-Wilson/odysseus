"""Push a notification, or a command, to one of the user's devices via ntfy.

Two things share one pipe. A plain notification is what the user reads on their
phone. A command carries a `cmd` header that an automation app on the device
(Tasker, Automate, or a purpose-built companion) matches on to *do* something —
launch an app, start a timer — rather than just display text.

The split matters because the device decides what it will honour. Nothing here
can make a phone do anything it has not already agreed to: Android will not
install software or type credentials on a push message's say-so, and this tool
deliberately does not pretend otherwise. What it can reliably do is deliver an
instruction the device already knows how to carry out.
"""

import json
import logging
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TOPIC = "Reminders"
REQUEST_TIMEOUT = 15.0

# ntfy priorities: 1 min .. 5 max. Anything outside that is rejected upstream.
_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}


def _ntfy_base() -> Optional[str]:
    """Base URL of the configured ntfy integration, if one is enabled."""
    try:
        from src.integrations import load_integrations
        for i in load_integrations():
            if (i.get("type") or i.get("service")) == "ntfy" and i.get("enabled", True):
                base = (i.get("base_url") or "").strip().rstrip("/")
                if base:
                    return base
    except Exception as e:
        logger.debug("integration lookup failed: %s", e)
    return None


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

        # Web Push first: the browser is already the client, so it needs no
        # extra app installed, and a tapped notification lands back in Odysseus.
        # ntfy stays as the fallback for devices that never subscribed.
        try:
            from src import webpush
            if webpush.load_subscriptions():
                res = await webpush.send(
                    (args.get("title") or "Odysseus")[:200],
                    message,
                    device=(args.get("device") or "").strip(),
                    url=args.get("click") or "/",
                    tag=(args.get("tag") or "odysseus"),
                )
                if res.get("sent"):
                    return {
                        "output": f"Pushed to {res['sent']} device(s): {message[:200]}",
                        "channel": "webpush",
                        "sent": res["sent"],
                        "exit_code": 0,
                    }
                # Fall through to ntfy rather than reporting success on zero.
                logger.info("[notify] web push sent nothing (%s); trying ntfy", res)
        except Exception as e:
            logger.debug("web push unavailable: %s", e)

        base = _ntfy_base()
        if not base:
            return {
                "error": (
                    "Nowhere to send this: no device has subscribed to browser "
                    "notifications, and no ntfy integration is configured. Open Odysseus "
                    "on the device and enable notifications there."
                ),
                "exit_code": 1,
            }

        # One topic per device is what makes "send it to my phone" mean a
        # particular phone rather than every subscriber at once. A registered
        # device is looked up by name; anything else is taken as a raw topic so
        # an unregistered device still works.
        want = (args.get("device") or args.get("topic") or "").strip()
        device = None
        if want:
            try:
                from src import devices as device_registry
                device = device_registry.resolve(want)
            except Exception as e:
                logger.debug("device lookup failed: %s", e)
        topic = (device.get("topic") if device else want) or DEFAULT_TOPIC
        headers = {
            "Title": (args.get("title") or "Odysseus")[:200],
            "Priority": str(_PRIORITY.get(str(args.get("priority", "default")).lower(), 3)),
        }
        if args.get("tags"):
            headers["Tags"] = str(args["tags"])[:200]
        # A click target turns the notification into something actionable on the
        # phone rather than a dead end.
        if args.get("click"):
            headers["Click"] = str(args["click"])[:500]

        # Commands ride the same message with a header the device filters on.
        cmd = (args.get("command") or "").strip()
        if cmd and device:
            # Refuse rather than fire a command into the void. A device that
            # never claimed the capability will silently ignore it, and the
            # model would report success it has no grounds for.
            try:
                from src import devices as device_registry
                if not device_registry.supports(device, cmd):
                    return {
                        "error": (
                            f"{device['name']} does not support `{cmd}`. It handles: "
                            f"{', '.join(device.get('commands') or ['notify'])}. "
                            "Send a plain notification instead, or add the capability on the "
                            "device's automation first."
                        ),
                        "exit_code": 1,
                    }
            except Exception:
                pass
        if cmd:
            headers["X-Odysseus-Cmd"] = cmd[:200]
            if args.get("command_arg"):
                headers["X-Odysseus-Arg"] = str(args["command_arg"])[:300]

        url = f"{base}/{topic}"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                r = await client.post(url, content=message.encode("utf-8"), headers=headers)
            if r.status_code >= 400:
                return {
                    "error": f"ntfy returned HTTP {r.status_code}: {r.text[:200]}",
                    "exit_code": 1,
                }
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as e:
            return {
                "error": (
                    f"could not reach ntfy at {url}: {e}. The device subscribes to this "
                    "server directly, so it has to be reachable from the phone too."
                ),
                "exit_code": 1,
            }

        sent = f"Sent to `{topic}`" + (f" with command `{cmd}`" if cmd else "")
        return {
            "output": (
                f"{sent}: {message[:200]}\n"
                + ("The device will only act on this if an automation there is listening "
                   "for that command." if cmd else "")
            ),
            "topic": topic,
            "message_id": body.get("id"),
            "exit_code": 0,
        }
