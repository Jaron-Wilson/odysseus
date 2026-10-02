"""The free SIP line's settings (Settings > Devices > Phone calls).

Asked for: "im wanting free and possibly localhosted". A softphone on the
tailnet calls Odysseus directly (src/telephony/sip_line.py); no number, no
provider, nothing public. Logged in:

    GET  /api/telephony/sip              settings and status (never the password)
    PUT  /api/telephony/sip              save them (starts or stops the listener)
    POST /api/telephony/sip/test         listener, account, engines, model, softphone
    POST /api/telephony/sip/call-me      ring your registered softphone
    POST /api/telephony/sip/provision    a one-time Linphone setup QR code

The one path a phone fetches without logging in:

    GET  /api/telephony/sip/provision/{token}.xml

is the Linphone account file behind that QR code. The token is the
credential: random, good for PROVISION_TTL, used once; anything else is a
404. It is exempt from login in app.py, and FunnelGuardMiddleware keeps it
off Funnel like everything but the Twilio webhooks.
"""

import asyncio
import logging
import re
import socket
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from src.auth_helpers import require_user
from src.telephony import agent, call as call_mod, config, sip, sip_line

logger = logging.getLogger(__name__)

LINE = sip_line.LINE
_PW_BAD = re.compile(r'["\\\x00-\x1f\x7f]')


def _is_admin(request: Request, user: Optional[str]) -> bool:
    from routes.telephony_routes import _is_admin as is_admin
    return is_admin(request.app, user)


def _bound() -> List[str]:
    srv = LINE.server
    if not srv:
        return []
    out = []
    for kind, ip, _ in srv.listeners:
        if ip not in out:
            out.append(ip)
    return out


def view(user: Optional[str], cfg: Dict) -> Dict:
    s = sip_line.sip_config(cfg)
    info = LINE.info() if not sip_line.test_mode() else {"dns_name": "", "devices": []}
    addrs = _bound()
    port = sip_line.sip_port()
    regs = []
    srv = LINE.server
    if srv and s.get("username"):
        now = time.time()
        regs = [{"ip": r.link.peer[0], "transport": r.link.kind, "expires_in": int(r.expires_at - now)}
                for r in srv.registered(str(s["username"]))]
    from routes.telephony_routes import ACTIVE
    v4 = [a for a in addrs if ":" not in a]
    return {
        "enabled": bool(s.get("enabled")),
        "username": str(s.get("username") or ""),
        "has_password": bool(s.get("password")),
        "devices": [str(d) for d in s.get("devices") or []],
        "require_pin": bool(s.get("require_pin")),
        "has_pin": bool(cfg.get("pin_hash")),
        "running": LINE.running,
        "test_mode": sip_line.test_mode(),
        "error": LINE.error if s.get("enabled") else "",
        "addresses": addrs,
        "port": port,
        "rtp_ports": list(sip_line.rtp_ports()),
        "server_host": v4[0] if v4 else (addrs[0] if addrs else ""),
        "dial": sip_line.dial_addresses(addrs, port, info.get("dns_name", "")) if addrs else [],
        "dns_name": info.get("dns_name", ""),
        "tailnet_devices": info.get("devices", []),
        "registered": regs,
        "active_calls": sum(1 for c in ACTIVE.values() if c["owner"] == user and c.get("engine") == "sip"),
        "engines": call_mod.engines_ready(),
        "greeting": str(cfg.get("greeting") or ""),
        "default_greeting": config.DEFAULT_GREETING,
    }


async def _self_check(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Send OPTIONS to the listener from its own address, the way a phone
    would, and see that something answers."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not fut.done():
                fut.set_result(data)

    try:
        transport, _ = await loop.create_datagram_endpoint(P, local_addr=(ip, 0))
    except OSError:
        return False
    try:
        lport = transport.get_extra_info("sockname")[1]
        h = sip.host_for_uri(ip)
        m = sip.SipMessage(method="OPTIONS", uri=f"sip:odysseus@{h}:{port}")
        m.add("Via", f"SIP/2.0/UDP {h}:{lport};branch={sip.new_branch()};rport")
        m.add("Max-Forwards", "70")
        m.add("From", f"<sip:selftest@{h}>;tag={sip.new_tag()}")
        m.add("To", f"<sip:odysseus@{h}>")
        m.add("Call-ID", sip.new_call_id(ip))
        m.add("CSeq", "1 OPTIONS")
        transport.sendto(m.encode(), (ip, port))
        data = await asyncio.wait_for(fut, timeout)
        resp = sip.parse(data)
        return resp.status in (200, 403)
    except (asyncio.TimeoutError, sip.SipError, OSError):
        return False
    finally:
        transport.close()


def setup_sip_routes() -> APIRouter:
    router = APIRouter(tags=["telephony"])

    @router.get("/api/telephony/sip")
    async def read(request: Request):
        user = require_user(request) or None
        return view(user, config.get_config(user))

    @router.put("/api/telephony/sip")
    async def write(request: Request):
        user = require_user(request) or None
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object.")
        cfg = config.get_config(user)
        s = sip_line.sip_config(cfg)
        if "username" in body:
            name = str(body["username"] or "").strip()
            if name and not sip_line.USERNAME_RE.match(name):
                raise HTTPException(400, "The username is 3 to 32 letters, digits, dots, dashes or underscores.")
            if name and sip_line.username_taken(name, user):
                raise HTTPException(400, "Another user on this server has that SIP username.")
            s["username"] = name
        if body.get("password"):
            pw = str(body["password"])
            if len(pw) < sip_line.MIN_PASSWORD or len(pw) > 128 or _PW_BAD.search(pw):
                raise HTTPException(400, f"The password is {sip_line.MIN_PASSWORD} to 128 characters, "
                                         "without quotes or backslashes.")
            sip_line.set_password(s, pw)
        if "devices" in body:
            devs: List[str] = []
            for d in body.get("devices") or []:
                try:
                    ip = sip_line.valid_device(d)
                except ValueError as e:
                    raise HTTPException(400, str(e))
                if ip not in devs:
                    devs.append(ip)
            if len(devs) > sip_line.MAX_DEVICES:
                raise HTTPException(400, f"At most {sip_line.MAX_DEVICES} devices.")
            s["devices"] = devs
        if "require_pin" in body:
            s["require_pin"] = bool(body["require_pin"])
        if "enabled" in body:
            s["enabled"] = bool(body["enabled"])
        if s.get("enabled") and not (s.get("username") and s.get("password")):
            raise HTTPException(400, "Set a username and password before turning the SIP line on.")
        cfg[sip_line.SIP_KEY] = s
        config.save_config(user, cfg)
        logger.info("[sip] settings saved for %s (enabled=%s)", user or "-", bool(s.get("enabled")))
        await LINE.reconcile()
        return view(user, cfg)

    @router.post("/api/telephony/sip/test")
    async def test(request: Request):
        user = require_user(request) or None
        cfg = config.get_config(user)
        s = sip_line.sip_config(cfg)
        checks = []

        def add(name, ok, detail="", optional=False):
            checks.append({"name": name, "ok": bool(ok), "detail": detail, "optional": optional})

        add("Turned on", s.get("enabled"), "" if s.get("enabled") else "The line does not answer until you turn it on.")
        add("Account", s.get("username") and s.get("password"),
            f"Username {s.get('username')}." if s.get("username") and s.get("password")
            else "Set a username and a password.")
        addrs = _bound()
        port = sip_line.sip_port()
        if LINE.running and addrs:
            where = ", ".join(f"{sip.host_for_uri(a)}:{port}" for a in addrs)
            add("Listening", True, f"UDP and TCP on {where}" + (" (test mode, loopback only)" if sip_line.test_mode() else "") + ".")
            answers = await _self_check(addrs[0], port)
            add("Answers", answers, "It answered a SIP OPTIONS." if answers else "Nothing answered on the SIP port.")
        else:
            add("Listening", False, LINE.error or "Not listening (turn the line on and Save).")
        devs = s.get("devices") or []
        add("Devices", True, ("Only " + ", ".join(devs)) if devs else "Any device on your tailnet that has the password.")
        srv = LINE.server
        regs = srv.registered(str(s.get("username") or "")) if srv and s.get("username") else []
        add("Softphone registered", bool(regs),
            f"From {regs[0].link.peer[0]} over {regs[0].link.kind.upper()}." if regs
            else "Not yet. Needed only for Call me: open the app and check it says Registered.", optional=True)
        problems = call_mod.engines_ready()
        add("Speech engines", not problems, " ".join(problems) or "Ready.")
        try:
            model, ep = agent.resolve_model(user, cfg, _is_admin(request, user))
            add("Model", bool(model and ep), model or "No model picked and no default model.")
        except Exception:
            add("Model", False, "No model picked and no default model.")
        return {"ok": all(c["ok"] for c in checks if not c["optional"]), "checks": checks}

    @router.post("/api/telephony/sip/call-me")
    async def call_me(request: Request):
        user = require_user(request) or None
        try:
            body = await request.json()
        except Exception:
            body = {}
        greeting = str((body or {}).get("greeting") or "").strip()[: config.MAX_GREETING]
        try:
            return await LINE.call_me(user, greeting)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.post("/api/telephony/sip/provision")
    async def provision(request: Request):
        user = require_user(request) or None
        s = sip_line.sip_config(config.get_config(user))
        if not (s.get("username") and s.get("password")):
            raise HTTPException(400, "Save a username and password first.")
        if not LINE.running:
            raise HTTPException(400, "Turn the SIP line on and Save first, so the phone has something to register with.")
        from routes.enroll_routes import base_url, qr_svg
        base = base_url(request)
        token = LINE.provision_token(user)
        url = f"{base}/api/telephony/sip/provision/{token}.xml"
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(":")[0]
        local = host in ("localhost", "127.0.0.1", "[::1]") or host.startswith("192.168.") or host.startswith("10.")
        return {"url": url, "qr_svg": qr_svg(url), "expires_in": int(sip_line.PROVISION_TTL),
                "warning": ("This page is open on a local address the phone cannot reach. Open Settings through "
                            "your Tailscale name (https://<server>.<tailnet>.ts.net) and make the code again.")
                if local else ""}

    @router.get("/api/telephony/sip/provision/{token}.xml")
    async def provision_file(token: str):
        rec = LINE.take_provision(token)
        if not rec:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        s = sip_line.sip_config(config.get_config(rec["owner"]))
        addrs = [a for a in _bound() if ":" not in a] or _bound()
        pw = sip_line.password(s)
        if not (s.get("username") and pw and addrs):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        logger.info("[sip] Linphone fetched the setup for %s", s.get("username"))
        xml = sip_line.linphone_xml(str(s["username"]), pw, addrs[0], sip_line.sip_port())
        return Response(xml, media_type="application/xml", headers={"Cache-Control": "no-store"})

    return router
