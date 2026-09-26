"""Adding a device with one command or one QR scan.

Admin side (logged in):   POST /api/devices/enroll          make a code
                          GET  /api/devices/enroll/{code}   has it been used yet?
Device side (the code is the credential; see src/enrollment.py):
    GET  /enroll/{code}/install.sh | install.ps1   the install script
    GET  /enroll/{code}/file/{name}                 server files it downloads
    POST /enroll/{code}/register                    "I'm installed, here's what I am"
    GET  /enroll/{code}/phone                       pairing page for a phone
    GET  /enroll/{code}/push-key, POST .../push     notifications for that phone
    GET  /enroll/{code}/check, POST .../check/*     "is this device still connected?"

The device-side paths are exempt from login in app.py, so everything here
checks the code first and answers 404 for a bad one, the same as for a path
that does not exist.
"""

import html
import ipaddress
import json
import logging
import os
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from core.middleware import require_admin
from src import devices, enrollment, machines, webpush

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MCP_DIR = os.path.join(_HERE, "tools", "mcp")
_ENROLL_DIR = os.path.join(_HERE, "tools", "enroll")
# The only files an install script may fetch.
SERVER_FILES = {"linux_desktop_mcp_server.py", "mcp_transport_security.py",
                "desktop_mcp_server.py", "resolve_mcp_server.py"}
SERVER_KINDS = {"desktop": "desktop", "resolve": "resolve"}
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


def base_url(request: Request) -> str:
    """How devices reach Odysseus: the host the admin is using right now
    (Tailscale Serve's https name), not the loopback it binds."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def server_pubkey() -> str:
    """The key Ping uses, for the install script to authorize."""
    for name in ("odysseus_mcp.pub", "id_ed25519.pub"):
        p = os.path.expanduser(f"~/.ssh/{name}")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                parts = f.read().split()
            if len(parts) >= 2:
                return f"{parts[0]} {parts[1]} odysseus"
    return ""


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return img.to_string(encoding="unicode")


def _code_or_404(code: str, kind: str) -> dict:
    rec = enrollment.valid(code, kind)
    if not rec:
        raise HTTPException(404, "Not found")
    return rec


def _render_script(name: str, base: str, code: str) -> str:
    with open(os.path.join(_ENROLL_DIR, name), encoding="utf-8") as f:
        text = f.read()
    return (text.replace("__ODYSSEUS_BASE__", base)
                .replace("__ENROLL_CODE__", code)
                .replace("__ODYSSEUS_PUBKEY__", server_pubkey()))


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Odysseus</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#151311;color:#ece6df;margin:0;padding:24px;max-width:560px}}
 h1{{font-size:20px;color:#e0654f}} .card{{background:#1f1c19;border-radius:12px;padding:16px;margin:14px 0}}
 button{{background:#e0654f;color:#fff;border:0;border-radius:8px;padding:10px 14px;font-size:15px;margin:4px 0}}
 button.secondary{{background:#3a342f}} code{{background:#2a2622;padding:2px 6px;border-radius:5px;word-break:break-all}}
 .ok{{color:#3fb950}} .bad{{color:#f85149}} .muted{{opacity:.7;font-size:13px}}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>""")


def setup_enroll_routes(get_mcp_manager=None) -> APIRouter:
    router = APIRouter(tags=["enroll"])

    def _mgr():
        if get_mcp_manager:
            return get_mcp_manager()
        from src.tool_utils import get_mcp_manager as g
        return g()

    async def _json(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    # ── Admin ──────────────────────────────────────────────────────────

    @router.post("/api/devices/enroll")
    async def make_code(request: Request):
        require_admin(request)
        body = await _json(request)
        kind = (body.get("kind") or "computer").strip()
        device = (body.get("device") or "").strip()
        if kind in ("phone", "check") and not device:
            raise HTTPException(400, "device is required for a phone or check code")
        try:
            rec = enrollment.create(kind, device=device)
        except ValueError as e:
            raise HTTPException(400, str(e))
        base = base_url(request)
        out = {"code": rec["code"], "kind": kind, "device": device, "expires": rec["expires"],
               "base": base}
        if kind == "computer":
            out["commands"] = {
                "windows": f"iwr -useb {base}/enroll/{rec['code']}/install.ps1 | iex",
                "unix": f"curl -fsSL {base}/enroll/{rec['code']}/install.sh | bash",
            }
            out["pubkey"] = bool(server_pubkey())
        else:
            url = f"{base}/enroll/{rec['code']}/{'phone' if kind == 'phone' else 'check'}"
            out["url"] = url
            out["qr_svg"] = qr_svg(url)
        return out

    @router.get("/api/devices/enroll/{code}")
    async def code_status(code: str, request: Request):
        require_admin(request)
        rec = enrollment.status(code)
        if not rec:
            raise HTTPException(404, "no such code")
        return {k: rec.get(k) for k in ("kind", "device", "expires", "used", "result")}

    # ── Computer: script, files, register ──────────────────────────────

    @router.get("/enroll/{code}/install.sh")
    async def install_sh(code: str, request: Request):
        _code_or_404(code, "computer")
        return PlainTextResponse(_render_script("install.sh", base_url(request), code),
                                 media_type="text/x-shellscript")

    @router.get("/enroll/{code}/install.ps1")
    async def install_ps1(code: str, request: Request):
        _code_or_404(code, "computer")
        return PlainTextResponse(_render_script("install.ps1", base_url(request), code))

    @router.get("/enroll/{code}/file/{name}")
    async def server_file(code: str, name: str):
        _code_or_404(code, "computer")
        if name not in SERVER_FILES:
            raise HTTPException(404, "Not found")
        with open(os.path.join(_MCP_DIR, name), encoding="utf-8") as f:
            return PlainTextResponse(f.read(), media_type="text/x-python")

    @router.post("/enroll/{code}/register")
    async def register(code: str, request: Request):
        _code_or_404(code, "computer")
        body = await _json(request)
        result = await register_machine(body, _mgr())
        if not enrollment.use(code, result):
            raise HTTPException(404, "Not found")
        logger.info("[enroll] added %s: %s", result.get("machine"), result.get("servers"))
        return result

    # ── Phone: pairing page ────────────────────────────────────────────

    @router.get("/enroll/{code}/phone")
    async def phone_page(code: str, request: Request):
        rec = _code_or_404(code, "phone")
        name = rec["device"]
        d = devices.get(name)
        if d is None:
            d = devices.register(name, kind="phone", commands=["notify", "open_url", "open_app", "list_apps"])
        token = d.get("token") or ""
        return _page(f"Pair {name}", f"""
<div class="card"><b>1. Notifications</b>
 <p class="muted">Turns on notifications on this phone, filed under <b>{html.escape(name)}</b>, so the agent's
 "notify my phone" reaches it.</p>
 <button id="push">Turn on notifications</button> <span id="push-msg" class="muted"></span></div>
<div class="card"><b>2. Modes listener</b>
 <p class="muted">For opening links and apps without a tap: in the Modes app, open Remote control and paste this token.</p>
 <code id="tok">{html.escape(token)}</code><br><button class="secondary" id="copy">Copy token</button></div>
<p class="muted">This page works for 20 minutes. Tailscale must be on for Odysseus to reach this phone.</p>
<script>
const code = {json.dumps(code)};
document.getElementById('copy').onclick = async () => {{
  await navigator.clipboard.writeText(document.getElementById('tok').textContent);
  document.getElementById('copy').textContent = 'Copied';
}};
function b64(s) {{ const p='='.repeat((4-s.length%4)%4); const r=atob((s+p).replace(/-/g,'+').replace(/_/g,'/'));
  return Uint8Array.from([...r].map(ch => ch.charCodeAt(0))); }}
document.getElementById('push').onclick = async () => {{
  const msg = document.getElementById('push-msg');
  try {{
    if (!('serviceWorker' in navigator) || !window.isSecureContext) throw new Error('this browser cannot do push here');
    if (await Notification.requestPermission() !== 'granted') throw new Error('permission was not given');
    const reg = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    const {{ public_key }} = await (await fetch(`/enroll/${{code}}/push-key`)).json();
    const sub = (await reg.pushManager.getSubscription()) ||
      await reg.pushManager.subscribe({{ userVisibleOnly: true, applicationServerKey: b64(public_key) }});
    const r = await fetch(`/enroll/${{code}}/push`, {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ subscription: sub.toJSON() }}) }});
    if (!r.ok) throw new Error('Odysseus did not accept it (the code may have expired)');
    msg.textContent = 'On. A test notification is on its way.'; msg.className = 'ok';
  }} catch (e) {{ msg.textContent = e.message; msg.className = 'bad'; }}
}};
</script>""")

    @router.get("/enroll/{code}/push-key")
    async def push_key(code: str):
        _code_or_404(code, "phone")
        return {"public_key": webpush.public_key()}

    @router.post("/enroll/{code}/push")
    async def phone_push(code: str, request: Request):
        rec = _code_or_404(code, "phone")
        body = await _json(request)
        try:
            webpush.save_subscription(body.get("subscription") or {}, device=rec["device"],
                                      owner=rec.get("owner") or "")
        except ValueError as e:
            raise HTTPException(400, str(e))
        enrollment.use(code, {"device": rec["device"], "push": True})
        sent = await webpush.send("Odysseus", f"{rec['device']} is paired. Notifications work.",
                                  device=rec["device"], url="/")
        return {"ok": True, "sent": sent.get("sent", 0)}

    # ── Check: is this device still connected? ─────────────────────────

    @router.get("/enroll/{code}/check")
    async def check_page(code: str):
        rec = _code_or_404(code, "check")
        name = rec["device"]
        return _page(f"Check {name}", f"""
<div class="card"><span class="ok">✓</span> This device reached Odysseus over the tailnet: you are reading this page.</div>
<div class="card" id="res">Checking whether Odysseus can reach <b>{html.escape(name)}</b>…</div>
<button id="test">Send a test notification here</button> <span id="msg" class="muted"></span>
<script>
const code = {json.dumps(code)};
(async () => {{
  const r = await (await fetch(`/enroll/${{code}}/check/status`)).json();
  const row = (ok, text) => `<div class="${{ok ? 'ok' : 'bad'}}">${{ok ? '✓' : '✗'}} ${{text}}</div>`;
  document.getElementById('res').innerHTML = (r.checks || []).map(x => row(x.ok, x.text)).join('')
    || 'Nothing to check.';
}})();
document.getElementById('test').onclick = async () => {{
  const r = await (await fetch(`/enroll/${{code}}/check/push`, {{ method: 'POST' }})).json();
  const m = document.getElementById('msg');
  m.textContent = r.sent ? 'Sent. It should arrive in a few seconds.' : (r.detail || 'Nothing was delivered.');
  m.className = r.sent ? 'ok' : 'bad';
}};
</script>""")

    @router.get("/enroll/{code}/check/status")
    async def check_status(code: str):
        rec = _code_or_404(code, "check")
        return {"device": rec["device"], "checks": await device_checks(rec["device"], _mgr())}

    @router.post("/enroll/{code}/check/push")
    async def check_push(code: str):
        rec = _code_or_404(code, "check")
        r = await webpush.send("Odysseus", f"Check: {rec['device']} is connected.", device=rec["device"], url="/")
        return {"sent": r.get("sent", 0), "detail": r.get("detail", "")}

    return router


# ── Logic, kept outside the router so it can be tested directly ────────────

_DNS_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.ts\.net$")


def _validated(body: dict) -> dict:
    ip = str(body.get("ip") or "").strip()
    dns = str(body.get("dns") or "").strip().lower().rstrip(".")
    try:
        if ipaddress.ip_address(ip) not in _TAILNET:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "ip must be this machine's Tailscale address (100.64.0.0/10)")
    if not _DNS_RE.match(dns):
        raise HTTPException(400, "dns must be this machine's MagicDNS name (*.ts.net)")
    servers = []
    for s in body.get("servers") or []:
        kind = SERVER_KINDS.get(str((s or {}).get("kind") or ""))
        try:
            port = int((s or {}).get("port"))
        except (TypeError, ValueError):
            continue
        if kind and 1024 <= port <= 65535:
            servers.append({"kind": kind, "port": port})
    user = re.sub(r"[^A-Za-z0-9._-]", "", str(body.get("user") or ""))[:32]
    return {"ip": ip, "dns": dns, "host": dns.split(".")[0], "os": str(body.get("os") or "")[:16],
            "user": user, "gpu": str(body.get("gpu") or "").strip()[:80],
            "ssh": bool(body.get("ssh")), "servers": servers}


async def register_machine(body: dict, mgr) -> dict:
    """Add (or update) the MCP servers an install script reported, connect
    them, and record what was learned about the machine."""
    from core.database import McpServer, SessionLocal

    v = _validated(body)
    added = []
    db = SessionLocal()
    try:
        for s in v["servers"]:
            url = f"http://{v['ip']}:{s['port']}/sse"
            row = db.query(McpServer).filter(McpServer.url == url).first()
            if row is None:
                row = McpServer(id=str(uuid.uuid4())[:8], name=f"{v['host']}-{s['kind']}",
                                transport="sse", url=url, is_enabled=True,
                                args="[]", env="{}")
                db.add(row)
            else:
                row.is_enabled = True
            db.commit()
            added.append({"id": row.id, "name": row.name, "url": url})
    finally:
        db.close()

    machines.set_prefs(v["host"], gpu=bool(v["gpu"]) or None, ssh_user=v["user"] or None)
    out = []
    for a in added:
        ok = await mgr._reconnect_configured(a["id"]) if mgr else False
        out.append({"name": a["name"], "connected": bool(ok)})
    return {"ok": True, "machine": v["host"], "os": v["os"], "gpu": v["gpu"],
            "ssh": v["ssh"], "servers": out}


async def device_checks(name: str, mgr) -> list:
    """What Odysseus can reach of a device: tailnet, listener, push, MCP."""
    checks = []
    d = devices.get(name)
    peer = None
    all_peers = machines.peers(refresh=True)
    if d and d.get("endpoint"):
        peer = machines.find_peer(all_peers, machines.url_host(d["endpoint"]))
    if peer is None:
        peer = machines.find_peer(all_peers, name)
    if peer is not None:
        checks.append({"ok": bool(peer["online"]),
                       "text": f"Tailscale sees {peer['name']} as {'online' if peer['online'] else 'offline'}."})
    else:
        checks.append({"ok": False, "text": f"{name} is not on the tailnet by that name. Is Tailscale on?"})
    if d is not None:
        if d.get("endpoint"):
            r = await devices.send_command(d, "notify", {"title": "Odysseus", "message": "Listener check"}) \
                if devices.supports(d, "notify") else {"ok": False, "error": "notify not supported"}
            checks.append({"ok": bool(r.get("ok")), "text": "The Modes listener answered." if r.get("ok")
                           else f"The Modes listener did not answer: {r.get('error', '')[:140]}"})
        subs = [s for s in webpush.load_subscriptions()
                if (s.get("device") or "").lower() in devices.push_names(d)]
        checks.append({"ok": bool(subs), "text": f"{len(subs)} notification subscription(s) linked." if subs
                       else "No notification subscription is linked: pair this phone again."})
    if peer is not None and mgr:
        from routes.device_routes import _configured_servers
        for s in _configured_servers():
            if machines.find_peer([peer], machines.url_host(s.get("url", ""))):
                st = mgr.get_server_status(s["id"]).get("status") or "disconnected"
                checks.append({"ok": st == "connected", "text": f"MCP server {s['name']}: {st}."})
    return checks
