"""Notifications from mods, fanned out through the channels Odysseus already has.

    push     Web Push to the user's browsers and phones (src/webpush.py)
    app      the in-app notification a scheduled task also uses (a toast,
             or a browser notification when allowed)
    discord  a Discord webhook integration (Settings > Integrations)
    ntfy     an ntfy integration
    sms      a text through the SMS gateway's reply URL (docs/sms-gateway.md),
             only at or above `sms_min_level`, since texts interrupt

Which are on is a core setting (Settings > Mods > Notifications), and each
mod can be muted. Mods never choose the channel: they say how important a
message is (`level`) and the user decides where it goes.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from src.mods import registry

logger = logging.getLogger(__name__)

LEVELS = ("info", "success", "warning", "error", "critical")
DEFAULTS: Dict[str, Any] = {"push": True, "app": True, "discord": False, "ntfy": False, "sms": False,
                            "sms_min_level": "error", "ntfy_topic": "odysseus-mods"}
_TAG_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def channel_settings() -> Dict[str, Any]:
    s = dict(DEFAULTS)
    s.update((registry.get_settings().get("notify") or {}))
    return s


def save_channel_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    cur = channel_settings()
    for k in ("push", "app", "discord", "ntfy", "sms"):
        if k in values:
            cur[k] = bool(values[k])
    if values.get("sms_min_level") in LEVELS:
        cur["sms_min_level"] = values["sms_min_level"]
    if isinstance(values.get("ntfy_topic"), str) and re.match(r"^[A-Za-z0-9_-]{1,64}$", values["ntfy_topic"]):
        cur["ntfy_topic"] = values["ntfy_topic"]
    allset = registry.get_settings()
    allset["notify"] = cur
    registry.save_settings(allset)
    return cur


def clean(body: Dict[str, Any]) -> Dict[str, Any]:
    """Check and trim a notify request. Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    level = body.get("level") or "info"
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    title = str(body.get("title") or "").strip()[:120]
    text = str(body.get("body") or "").strip()[:2000]
    if not title and not text:
        raise ValueError("give a title or a body")
    url = str(body.get("url") or "").strip()
    if url and (not url.startswith("/") or url.startswith("//")):
        raise ValueError("url must be a path in Odysseus, like /mods/<id>/jobs/1")
    tag = str(body.get("tag") or "").strip()
    if tag and not _TAG_RE.match(tag):
        raise ValueError("tag may use letters, digits and . _ : -")
    image = str(body.get("image_url") or "").strip()
    if image and not re.match(r"^(?:https://|/)", image):
        raise ValueError("image_url must be https:// or a path in Odysseus")
    return {"level": level, "title": title, "body": text, "url": url, "tag": tag, "image_url": image}


async def _push(owner: str, title: str, body: str, url: str, tag: str) -> int:
    from src import webpush
    from src.auth_helpers import _auth_disabled
    sent = 0
    for s in webpush.load_subscriptions():
        if not s.get("endpoint"):
            continue
        if not _auth_disabled() and owner and (s.get("owner") or "") != owner:
            continue
        r = await webpush.send(title, body, endpoint=s["endpoint"], url=url or "/", tag=tag)
        sent += int(r.get("sent") or 0)
    return sent


def _app(owner: str, title: str, body: str) -> bool:
    from src.event_bus import get_task_scheduler
    ts = get_task_scheduler()
    if not ts:
        return False
    ts.add_notification(title, "success", owner=owner or None, body=body or title)
    return True


async def _post_integration(preset: str, payload: bytes, url_suffix: str = "",
                            content_type: str = "application/json", extra: Optional[Dict[str, str]] = None) -> str:
    import httpx
    from src.integrations import load_integrations
    from src.url_safety import resolve_and_check, PinnedAsyncTransport
    intg = next((i for i in load_integrations()
                 if i.get("preset") == preset and i.get("enabled", True) and i.get("base_url")), None)
    if not intg:
        return f"no enabled {preset} integration"
    url = intg["base_url"].rstrip("/") + url_suffix
    ok, reason, ips = resolve_and_check(url, block_private=False)
    if not ok:
        return f"rejected: {reason}"
    hdrs = {"Content-Type": content_type, **(extra or {})}
    if intg.get("api_key") and (intg.get("auth_type") or "none").lower() == "bearer":
        hdrs["Authorization"] = f"Bearer {intg['api_key']}"
    async with httpx.AsyncClient(timeout=10.0, transport=PinnedAsyncTransport(ips)) as c:
        r = await c.post(url, content=payload, headers=hdrs)
    return "sent" if r.is_success else f"HTTP {r.status_code}"


async def _sms(owner: str, text: str) -> str:
    """A text to the owner's first number: through the mod that provides
    messaging.sms when one is installed and enabled (Phone & Messages, with
    its own reply URL), else the core's built-in reply URL."""
    from routes import sms_routes
    from src.mods import capabilities
    prov = capabilities.provider("messaging.sms")
    if prov and prov["kind"] == "mod":
        own = capabilities._sms_own_numbers(owner or None)
        args = {"text": text[:600], **({"to": own[0]} if own else {})}   # no number: the mod's own first one
        try:
            r = await capabilities._call_mod(prov["id"], "messaging.sms", "send", args, owner or None, "core")
            return str(r.get("via") or "sent")
        except capabilities.CapError as e:
            return f"the {prov['id']} mod did not send it: {e.message}"[:200]
    cfg = sms_routes.get_config(owner or None)
    numbers = cfg.get("numbers") or []
    if not numbers or not str(cfg.get("reply_url") or "").strip():
        return "the SMS gateway has no reply URL or number"
    return await sms_routes.deliver(owner or None, cfg, numbers[0], text[:600])


async def send(mod_id: str, owner: str, msg: Dict[str, Any]) -> Dict[str, Any]:
    """Deliver a cleaned notify request. Returns what each channel did."""
    import json
    entry = registry.get(mod_id) or {}
    name = entry.get("name") or mod_id
    if entry.get("notify_muted"):
        return {"muted": True}
    s = channel_settings()
    title = msg["title"] or name
    if not title.lower().startswith(name.lower()):
        title = f"{name}: {title}"
    tag = f"odysseus-mod-{mod_id}" + (f"-{msg['tag']}" if msg["tag"] else "")
    out: Dict[str, Any] = {}

    async def attempt(channel, coro):
        try:
            out[channel] = await coro
        except Exception as e:
            logger.info("[mods] %s notify via %s failed: %s", mod_id, channel, e)
            out[channel] = f"failed: {type(e).__name__}"

    if s["push"]:
        await attempt("push", _push(owner, title, msg["body"], msg["url"], tag))
    if s["app"]:
        try:
            out["app"] = _app(owner, title, msg["body"])
        except Exception as e:
            out["app"] = f"failed: {type(e).__name__}"
    if s["discord"]:
        embed: Dict[str, Any] = {"title": title[:256], "description": msg["body"][:4000],
                                 "color": {"error": 0xE5534B, "critical": 0xE5534B, "warning": 0xD29922,
                                           "success": 0x3FB950}.get(msg["level"], 0x5865F2)}
        if msg["image_url"].startswith("https://"):
            embed["image"] = {"url": msg["image_url"]}
        await attempt("discord", _post_integration("discord_webhook", json.dumps({"embeds": [embed]}).encode()))
    if s["ntfy"]:
        prio = {"critical": "5", "error": "4", "warning": "4"}.get(msg["level"], "3")
        await attempt("ntfy", _post_integration("ntfy", (msg["body"] or title).encode(), "/" + s["ntfy_topic"],
                                                "text/plain", {"Title": title.encode("ascii", "replace").decode(),
                                                               "Priority": prio}))
    if s["sms"] and LEVELS.index(msg["level"]) >= LEVELS.index(s["sms_min_level"]):
        await attempt("sms", _sms(owner, f"{title}: {msg['body']}" if msg["body"] else title))
    return out
