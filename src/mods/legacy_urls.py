"""Old webhook addresses that keep working after a feature became a mod.

Texting the agent from the phone moved to the Phone & Messages mod. Its
SMS Forwarder app was set up with `https://<server>/api/sms/inbound/<secret>`,
which the core used to answer; the mod answers `/mods/phone/public/sms/inbound/
<secret>`. So the phone does not have to be touched, the core still accepts the
old address and hands the request to the mod, in-process (not a redirect: the
forwarder app may not follow a redirect of a POST). The secret is the same
one: the mod keeps the hash the core stored, and checks the path against it
exactly as before (and 404s the same way for a wrong secret or sender).

This is a plain forward through the mod proxy (src/mods/proxy.py), so the
mod must be installed, enabled and granted `public.http`; otherwise the
answer is the proxy's own 404 page. The reply URL (where the server sends a
text out, SMS Gate for Android on the phone) is an outgoing address the mod
imports with the rest of the texting settings (src/mods/legacy.py); the phone
side has nothing to change for it.

Not forwarded: Twilio's webhooks. Twilio signs the URL it called, so a
forward under another path cannot verify; the new address is shown in the
mod's phone settings.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# (old path prefix, mod id, path inside the mod). The secret is the rest.
FORWARDS = (("/api/sms/inbound/", "phone", "public/sms/inbound/"),)
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")

_IN_PATH = re.compile(r"((?:/api/sms/inbound/|/mods/[a-z][a-z0-9-]*/public/sms/inbound/))[^/?\s\"]+")


class _RedactSecrets(logging.Filter):
    """uvicorn logs every request path; these carry the forward secret."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(_IN_PATH.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                                    for a in record.args)
            if isinstance(record.msg, str) and "/sms/inbound/" in record.msg:
                record.msg = _IN_PATH.sub(r"\1<redacted>", record.msg)
        except Exception:
            pass
        return True


def install_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactSecrets) for f in access.filters):
        access.addFilter(_RedactSecrets())


def setup_legacy_url_routes() -> APIRouter:
    router = APIRouter(tags=["mods"])
    install_log_redaction()

    @router.post("/api/sms/inbound/{secret}")
    async def old_sms_inbound(secret: str, request: Request):
        if not SECRET_RE.match(secret or ""):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        from src.mods import proxy
        _old, mod_id, new = FORWARDS[0]
        return await proxy.handle_http(request, mod_id, new + secret)

    return router
