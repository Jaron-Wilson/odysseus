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

import asyncio
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
# The only files an install script may fetch, and where each lives.
SERVER_FILES = {"linux_desktop_mcp_server.py", "mcp_transport_security.py",
                "desktop_mcp_server.py", "resolve_mcp_server.py", "music_overlay.py"}
_FILE_DIRS = {"music_overlay.py": os.path.join(_HERE, "tools", "music_overlay")}

# What a fully set-up computer's desktop MCP offers, by OS, as features the
# user knows by name. A linked computer missing any is offered Install/Update.
# Asked for: "lets say I get my device linked, ask to install all MCPs
# available ... cause I know my laptop does not have music on it".
FEATURES = {
    "windows": {"Music overlay (Pop out)": "start_music_overlay",
                "Hear the phone here (Bluetooth)": "bluetooth_audio_receive",
                "Pair the phone from the site": "bluetooth_pair",
                "Stream this computer's sound": "audio_stream_start",
                "Play another computer's sound": "play_stream"},
    "linux": {"Music overlay (Pop out)": "start_music_overlay",
              "Play another computer's sound": "play_stream"},
}
MCP_FILE = {"windows": "desktop_mcp_server.py", "linux": "linux_desktop_mcp_server.py"}
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


def _page(title: str, body: str, *, page: str, code: str, device: str = "") -> HTMLResponse:
    """A standalone page for a device that is not logged in.

    No inline script: Odysseus's Content-Security-Policy only runs scripts
    from its own origin (or with a per-request nonce), so an inline <script>
    is silently blocked -- which is how the first version's buttons did
    nothing at all. The behaviour lives in /static/js/enroll-page.js, and the
    page hands it what it needs through data- attributes.
    """
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{html.escape(title)} · Odysseus</title>
<link rel="stylesheet" href="/static/css/enroll-page.css">
<script src="/static/js/enroll-page.js" defer></script>
</head><body data-page="{html.escape(page)}" data-code="{html.escape(code)}" data-device="{html.escape(device)}">
<header><div class="brand">Odysseus</div><h1>{html.escape(title)}</h1></header>
<main>{body}</main></body></html>""")


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
            rec = enrollment.create(kind, device=device, owner=_owner(request))
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

    # ── Keeping linked computers' tools complete and current ──────────
    # Asked for: "when I get my device linked, ask to install all MCPs
    # available on that server or PC that my PC has ... my laptop does not
    # have music on it, so that popup would be nice to have".

    @router.get("/api/devices/tools")
    async def tools_status(request: Request):
        """For each linked computer: which features its desktop MCP lacks and
        whether its files are the same as this server's."""
        require_admin(request)
        return {"machines": [{k: v for k, v in m.items() if k != "_peer"} for m in await machine_tools(_mgr())]}

    @router.post("/api/devices/tools/{server_id}/update")
    async def update_tools(server_id: str, request: Request):
        """Install or update everything on a linked computer: Odysseus runs its
        install command there over the SSH access the first install set up,
        with a fresh one-time code. It runs on its own (the first install can
        take minutes, past the 45s request limit, which cut it off: "Could not
        install: Request exceeded 45s timeout", 2026-09-29); the page asks
        GET .../update how it went."""
        require_admin(request)
        job = _INSTALLS.get(server_id)
        if job and job.get("running"):
            return _install_view(job)
        rows = {m["server_id"]: m for m in await machine_tools(_mgr())}
        m = rows.get(server_id)
        if not m:
            raise HTTPException(404, "No such computer")
        if not m.get("online"):
            raise HTTPException(409, f"{m['name']} is offline")
        rec = enrollment.create("computer", owner=_owner(request))
        job = _INSTALLS[server_id] = {"running": True, "machine": m["name"], "ok": None, "error": ""}
        job["task"] = asyncio.create_task(_install(job, m, base_url(request), rec["code"]))
        return _install_view(job)

    @router.get("/api/devices/tools/{server_id}/update")
    async def update_status(server_id: str, request: Request):
        require_admin(request)
        job = _INSTALLS.get(server_id)
        if not job:
            raise HTTPException(404, "No install has been started for that computer")
        return _install_view(job)

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
        with open(os.path.join(_FILE_DIRS.get(name, _MCP_DIR), name), encoding="utf-8") as f:
            return PlainTextResponse(f.read(), media_type="text/x-python")

    @router.post("/enroll/{code}/register")
    async def register(code: str, request: Request):
        _code_or_404(code, "computer")
        body = await _json(request)
        rec = _code_or_404(code, "computer")
        result = await register_machine(body, _mgr())
        if not enrollment.use(code, result):
            raise HTTPException(404, "Not found")
        logger.info("[enroll] added %s: %s", result.get("machine"), result.get("servers"))
        # The music overlay's token (its messages and the phone's song need
        # one): made here so nobody has to paste one. Not stored with the code.
        try:
            token = mint_overlay_token(rec.get("owner") or "", result.get("machine") or "computer")
            _invalidate_token_cache(request)
            return dict(result, overlay_token=token)
        except Exception as e:
            logger.warning("[enroll] no overlay token for %s: %s", result.get("machine"), e)
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
<section class="card">
  <div class="step">1</div>
  <h2>Notifications</h2>
  <p>So "notify my phone" reaches this phone. Filed under <b>{html.escape(name)}</b>.</p>
  <button class="primary" data-action="subscribe">Turn on notifications</button>
  <p class="result" data-result="subscribe"></p>
</section>
<section class="card">
  <div class="step">2</div>
  <h2>Modes listener</h2>
  <p>So Odysseus can open links and apps here without a tap. In the Modes app, open
     <b>Remote control</b> and paste this token.</p>
  <code class="token">{html.escape(token)}</code>
  <button data-action="copy" data-text="{html.escape(token)}">Copy token</button>
</section>
<p class="foot">This page works for 20 minutes. Keep Tailscale on so Odysseus can reach this phone.</p>
""", page="phone", code=code, device=name)

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
        return _page(f"Is {name} connected?", """
<section class="card">
  <div class="row ok"><span class="icon">✓</span><div><b>This device can reach Odysseus</b>
    <p>You are reading this page over the tailnet.</p></div></div>
</section>
<section class="card">
  <h2>Can Odysseus reach it?</h2>
  <div data-slot="device" class="muted">Checking…</div>
  <div data-slot="checks"></div>
</section>
<section class="card">
  <h2>Try it</h2>
  <button class="primary" data-action="test">Send a test notification</button>
  <button data-action="subscribe" hidden>Turn on notifications on this device</button>
  <p class="result" data-result="test"></p>
</section>
<p class="foot">This page works for 10 minutes; reload it to check again.</p>
""", page="check", code=code, device=name)

    @router.get("/enroll/{code}/check/status")
    async def check_status(code: str):
        rec = _code_or_404(code, "check")
        return {"device": rec["device"], "info": _device_info(rec["device"]),
                "checks": await device_checks(rec["device"], _mgr())}

    @router.post("/enroll/{code}/check/push")
    async def check_push(code: str):
        rec = _code_or_404(code, "check")
        r = await webpush.send("Odysseus", f"Check: {rec['device']} is connected.", device=rec["device"], url="/")
        return {"sent": r.get("sent", 0), "detail": r.get("detail", ""), "errors": r.get("errors", [])}

    @router.get("/enroll/{code}/check/push-key")
    async def check_push_key(code: str):
        _code_or_404(code, "check")
        return {"public_key": webpush.public_key()}

    @router.post("/enroll/{code}/check/subscribe")
    async def check_subscribe(code: str, request: Request):
        """Fix it from the check page: subscribe this browser under the
        device's own name. Not single-use, like the rest of a check code."""
        rec = _code_or_404(code, "check")
        body = await _json(request)
        try:
            webpush.save_subscription(body.get("subscription") or {}, device=rec["device"],
                                      owner=rec.get("owner") or "")
        except ValueError as e:
            raise HTTPException(400, str(e))
        r = await webpush.send("Odysseus", f"Notifications are on for {rec['device']}.",
                               device=rec["device"], url="/")
        return {"ok": True, "sent": r.get("sent", 0)}

    return router


# ── Logic, kept outside the router so it can be tested directly ────────────

_DNS_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.ts\.net$")


def _sha_file(path: str) -> str:
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return ""


async def machine_tools(mgr) -> list:
    """Linked computers' desktop MCPs, what they are missing and whether their
    files are current."""
    import json as _json
    from routes.device_routes import _configured_servers
    peers = await asyncio.to_thread(machines.peers)
    out = []
    for srv in _configured_servers():
        if not srv.get("enabled") or not srv.get("url"):
            continue
        peer = machines.find_peer(peers, machines.url_host(srv["url"]))
        os_name = (peer or {}).get("os") or ""
        if os_name not in FEATURES or not str(srv.get("name", "")).endswith("-desktop"):
            continue
        tools = {t.get("name") for t in (getattr(mgr, "_tools", {}) or {}).get(srv["id"], [])}
        connected = (mgr.get_server_status(srv["id"]).get("status") == "connected") if mgr else False
        missing = [label for label, tool in FEATURES[os_name].items() if tool not in tools] if connected else []
        outdated = []
        if connected and "tools_version" in tools:
            try:
                res = await asyncio.wait_for(mgr.call_tool(f"mcp__{srv['id']}__tools_version", {}), 10)
                files = (_json.loads(res.get("stdout") or "{}") or {}).get("files") or {}
            except Exception:
                files = {}
            for name in (MCP_FILE[os_name], "music_overlay.py"):
                here = _sha_file(os.path.join(_FILE_DIRS.get(name, _MCP_DIR), name))
                if here and files.get(name) != here:
                    outdated.append(name)
        elif connected:
            outdated.append(MCP_FILE[os_name])                 # too old to say: update it
        out.append({"server_id": srv["id"], "name": srv["name"], "host": (peer or {}).get("host"),
                    "os": os_name, "online": bool((peer or {}).get("online")), "connected": connected,
                    "missing": missing, "outdated": outdated,
                    "up_to_date": connected and not missing and not outdated,
                    "_peer": peer})
    return out


# Installs running or finished, by server id: {running, machine, ok, error, output}.
_INSTALLS: dict = {}


def _install_view(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "task"}


async def _install(job: dict, m: dict, base: str, code: str) -> None:
    try:
        r = await run_installer(m, base, code, job)
    except Exception as e:
        r = {"ok": False, "error": str(e) or type(e).__name__}
    job.update(running=False, ok=bool(r.get("ok")),
               error="" if r.get("ok") else (r.get("error") or "the install did not finish"),
               output=r.get("output", ""))
    logger.info("[enroll] update of %s: %s", m["name"], "ok" if job["ok"] else job["error"])


async def run_installer(m: dict, base: str, code: str, job: dict = None, timeout: float = 900) -> dict:
    """Run this computer's install command there, over SSH. Its output is kept
    as it comes (job["output"], and job["step"]: the last "==>" line), so a
    stall shows where it is; at the timeout ssh is killed, which ends the
    install there rather than leaving it running."""
    import getpass
    peer = m.get("_peer") or {}
    host = peer.get("dns") or (peer.get("ips") or [peer.get("host")])[0]
    user = machines.load_prefs().get(peer.get("host") or "", {}).get("ssh_user") or getpass.getuser()
    if m["os"] == "windows":
        remote = ["powershell", "-NoProfile", "-Command",
                  f"iwr -useb {base}/enroll/{code}/install.ps1 | iex"]
    else:
        remote = ["bash", "-lc", f"curl -fsSL {base}/enroll/{code}/install.sh | bash"]
    job = job if job is not None else {}
    last = ""
    for key in machines._ssh_keys():
        r = await _run_streamed(machines._ssh_argv(user, host, key, remote, m["os"]), job, timeout)
        if r["rc"] == 0:
            return {"ok": True, "machine": m["name"], "output": r["out"][-3000:]}
        last = r["err"] or r["out"]
        if "Permission denied" not in last:
            break
    return {"ok": False, "error": last[-800:] or "SSH failed", "machine": m["name"],
            "output": job.get("output", "")}


async def _run_streamed(argv: list, job: dict, timeout: float) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines: list = []

    async def read():
        async for raw in proc.stdout:
            line = re.sub(r"\x1b\[[0-9;]*m", "", raw.decode(errors="replace")).rstrip()
            lines.append(line)
            del lines[:-200]
            job["output"] = "\n".join(lines)[-3000:]
            if "==> " in line:
                job["step"] = line.split("==> ", 1)[1].strip()
        await proc.wait()

    try:
        await asyncio.wait_for(read(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        step = job.get("step")
        return {"rc": -1, "out": "\n".join(lines),
                "err": f"timed out after {timeout:.0f}s" + (f", at: {step}" if step else "")}
    except asyncio.CancelledError:
        proc.kill()
        raise
    out = "\n".join(lines)
    return {"rc": proc.returncode, "out": out, "err": "" if proc.returncode == 0 else out}


def _owner(request: Request) -> str:
    try:
        from src.auth_helpers import effective_user
        return effective_user(request) or ""
    except Exception:
        return ""


def _invalidate_token_cache(request: Request) -> None:
    inv = getattr(request.app.state, "invalidate_token_cache", None)
    if inv:
        try:
            inv()
        except Exception:
            pass


def mint_overlay_token(owner: str, machine: str) -> str:
    """An API token that can only use the overlay's routes, one per machine:
    re-running the install replaces that machine's previous one."""
    import secrets
    import bcrypt
    from core.database import ApiToken, SessionLocal
    name = f"overlay \u00b7 {machine}"[:60]
    raw = "ody_" + secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        for old in db.query(ApiToken).filter(ApiToken.name == name).all():
            old.is_active = False
        db.add(ApiToken(id=str(uuid.uuid4())[:8], owner=owner or None, name=name,
                        token_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
                        token_prefix=raw[:8], scopes="overlay", is_active=True))
        db.commit()
    finally:
        db.close()
    return raw


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


def _device_info(name: str) -> dict:
    d = devices.get(name)
    all_peers = machines.peers()
    peer = None
    if d and d.get("endpoint"):
        peer = machines.find_peer(all_peers, machines.url_host(d["endpoint"]))
    if peer is None:
        peer = machines.find_peer(all_peers, name)
    if peer is None:
        return {"name": name}
    return {"name": peer["name"], "os": peer["os"], "ip": (peer["ips"] or [""])[0],
            "online": peer["online"], "last_seen": peer["last_seen"]}


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
