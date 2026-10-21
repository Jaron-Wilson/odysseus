"""Hello Mod: the smallest complete Odysseus mod, standard library only.

It shows every part of the mod contract (docs/mods.md):

  GET  /health            the health check the core polls
  GET  /                  the Workspace tab: wave, see the events it received
  GET  /cards/status      the Home card
  GET  /settings          its settings page (a greeting word, kept by the core)
  POST /api/wave          emits the hello.waved event and sends a notification
  GET  /api/state         JSON for the page
  GET  /mod/tools         its one tool, hello_greet
  POST /mod/tools/<name>  runs a tool call from the agent
  POST /mod/events        events from other mods (it subscribes to */hello.*)

Under the core (ODYSSEUS_URL set) it trusts only requests with a valid
signed identity; on its own it asks for a password (HELLO_PASSWORD, or one
printed at start) and "notifies" by printing.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

SKEW_S = 300


class Core:
    """The bits of the Odysseus mod API this mod uses."""

    def __init__(self, url: str, token: str, mod_id: str):
        self.url = url.rstrip("/")
        self.token = token
        self.mod_id = mod_id

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}/api/mods/v1/{path.lstrip('/')}", data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail")
            except Exception:
                detail = None
            return {"error": detail or f"HTTP {e.code}"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def verify(self, headers) -> Optional[str]:
        user, mod, ts, sig = (headers.get("X-Odysseus-User"), headers.get("X-Odysseus-Mod"),
                              headers.get("X-Odysseus-Ts"), headers.get("X-Odysseus-Sig"))
        if not (user and mod and ts and sig) or mod != self.mod_id:
            return None
        try:
            if abs(time.time() - int(ts)) > SKEW_S:
                return None
        except ValueError:
            return None
        want = hmac.new(self.token.encode(), f"v1\n{mod}\n{user}\n{ts}".encode(), hashlib.sha256).hexdigest()
        return user if hmac.compare_digest(want, sig) else None


class State:
    def __init__(self, data_dir: str):
        self.lock = threading.Lock()
        self.path = os.path.join(data_dir, "state.json")
        self.waves = 0
        self.greeted = 0
        self.events: list = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.waves, self.greeted, self.events = d.get("waves", 0), d.get("greeted", 0), d.get("events", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"waves": self.waves, "greeted": self.greeted, "events": self.events[-20:]}, f)
        os.replace(tmp, self.path)


TOOLS = [{
    "type": "function",
    "function": {
        "name": "hello_greet",
        "description": "Greet someone by name from the Hello Mod, and count the greeting.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Who to greet"}},
                       "required": ["name"]},
    },
    "x-odysseus": {"keywords": ["say hello"], "read_only": True},
}]

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hello Mod</title><style>
body{font:14px system-ui,sans-serif;margin:0;padding:20px;background:#111;color:#ddd}
h1{font-size:18px;margin:0 0 12px}button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #444;
background:#222;color:#eee;cursor:pointer}button:hover{background:#2c2c2c}.dim{opacity:.6}
li{margin:4px 0}code{background:#222;padding:1px 4px;border-radius:4px}
</style></head><body>
<h1>Hello from Hello Mod</h1>
<p>Signed in as <code>__USER__</code>. Waves so far: <strong id="waves">__WAVES__</strong>,
greetings by the agent: <strong id="greeted">__GREETED__</strong>.</p>
<p><button id="wave">Wave</button> <span id="msg" class="dim"></span></p>
<h2 style="font-size:15px">Events received</h2><ul id="events"><li class="dim">None yet.</li></ul>
<script>
const $ = (id) => document.getElementById(id);
async function refresh() {
  const r = await fetch('api/state'); if (!r.ok) return;
  const s = await r.json();
  $('waves').textContent = s.waves; $('greeted').textContent = s.greeted;
  $('events').innerHTML = s.events.length ? s.events.slice().reverse().map(e =>
    `<li><code>${e.source}/${e.type}</code> <span class="dim">${new Date(e.ts * 1000).toLocaleTimeString()}</span></li>`).join('')
    : '<li class="dim">None yet.</li>';
}
$('wave').onclick = async () => {
  $('msg').textContent = 'Waving...';
  const r = await fetch('api/wave', {method: 'POST'});
  const d = await r.json().catch(() => ({}));
  $('msg').textContent = r.ok ? 'Waved. Check your notifications.' : (d.error || 'Failed');
  refresh();
};
refresh(); setInterval(refresh, 5000);
</script></body></html>"""

CARD = """<!doctype html><html><head><meta charset="utf-8"><style>
body{font:13px system-ui,sans-serif;margin:0;padding:10px 12px;color:#ddd;background:transparent}
.n{font-size:22px;font-weight:600}.dim{opacity:.6}</style></head><body>
<div class="n">__WAVES__</div><div class="dim">waves, __GREETED__ greetings, __EVENTS__ events received</div>
</body></html>"""

SETTINGS_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{font:14px system-ui,sans-serif;margin:0;padding:20px;background:#111;color:#ddd}
input,button{font:inherit;padding:5px 10px;border-radius:6px;border:1px solid #444;background:#222;color:#eee}
</style></head><body><h1 style="font-size:17px">Hello Mod settings</h1>
<form method="post" action="settings"><label>Greeting word <input name="greeting" value="__GREETING__"></label>
<button>Save</button></form></body></html>"""


def make_handler(core: Optional[Core], state: State, password: Optional[str]):
    sessions: set = set()
    local_settings: Dict[str, Any] = {"greeting": "Hello"}

    def settings() -> Dict[str, Any]:
        if core:
            s = core.call("GET", "settings").get("settings") or {}
            return {"greeting": "Hello", **s}
        return local_settings

    class Handler(BaseHTTPRequestHandler):
        server_version = "hello-mod/0.1"

        def log_message(self, fmt, *args):  # one line per request on stdout
            sys.stdout.write("%s %s\n" % (self.command, self.path))

        # ── helpers ──
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode())

        def _html(self, text: str) -> None:
            self._send(200, text.encode(), "text/html; charset=utf-8")

        def _body(self) -> Dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type") or ""
            if "application/x-www-form-urlencoded" in ctype:
                from urllib.parse import parse_qs
                return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            try:
                v = json.loads(raw or b"{}")
                return v if isinstance(v, dict) else {}
            except json.JSONDecodeError:
                return {}

        def _user(self) -> Optional[str]:
            """Who is asking: the core's signed identity, or (standalone) a session."""
            if core:
                return core.verify(self.headers)
            c = SimpleCookie(self.headers.get("Cookie") or "")
            sid = c.get("hello_session")
            return "you" if sid and sid.value in sessions else None

        def _route(self) -> Tuple[str, str]:
            path = self.path.split("?", 1)[0]
            return self.command, path

        # ── standalone login ──
        def _login_page(self, error: str = "") -> None:
            self._html("<!doctype html><meta charset=utf-8><body style='font:14px system-ui;padding:24px'>"
                       "<h1 style='font-size:17px'>Hello Mod</h1><form method=post action=login>"
                       "<input type=password name=password placeholder=Password autofocus> <button>Sign in</button>"
                       f"</form><p style='color:#c33'>{html.escape(error)}</p>")

        def do_GET(self):
            method, path = self._route()
            if path == "/health":
                return self._json(200, {"ok": True})
            user = self._user()
            if path == "/mod/tools":
                if core and not user:
                    return self._json(401, {"error": "unsigned"})
                return self._json(200, {"tools": TOOLS})
            if not user:
                if core:
                    return self._json(401, {"error": "Requests must come through Odysseus"})
                return self._login_page()
            if path == "/":
                return self._html(PAGE.replace("__USER__", html.escape(user)).replace("__WAVES__", str(state.waves))
                                  .replace("__GREETED__", str(state.greeted)))
            if path == "/cards/status":
                return self._html(CARD.replace("__WAVES__", str(state.waves)).replace("__GREETED__", str(state.greeted))
                                  .replace("__EVENTS__", str(len(state.events))))
            if path == "/settings":
                return self._html(SETTINGS_PAGE.replace("__GREETING__", html.escape(str(settings()["greeting"]))))
            if path == "/api/state":
                with state.lock:
                    return self._json(200, {"waves": state.waves, "greeted": state.greeted, "events": state.events[-20:]})
            return self._json(404, {"error": "not found"})

        do_HEAD = do_GET

        def do_POST(self):
            method, path = self._route()
            if not core and path == "/login":
                body = self._body()
                if password and hmac.compare_digest(str(body.get("password") or ""), password):
                    sid = secrets.token_urlsafe(24)
                    sessions.add(sid)
                    self.send_response(303)
                    self.send_header("Set-Cookie", f"hello_session={sid}; HttpOnly; SameSite=Lax; Path=/")
                    self.send_header("Location", "./")
                    self.end_headers()
                    return
                return self._login_page("Wrong password")
            user = self._user()
            if not user:
                return self._json(401, {"error": "unsigned"})
            if path == "/mod/events":
                ev = self._body()
                with state.lock:
                    state.events.append({k: ev.get(k) for k in ("id", "source", "type", "ts")})
                    state.events = state.events[-20:]
                    state.save()
                return self._json(200, {"ok": True})
            if path.startswith("/mod/tools/"):
                name = path.rsplit("/", 1)[-1]
                if name != "hello_greet":
                    return self._json(404, {"error": f"no tool {name}"})
                who = str(self._body().get("name") or "there")[:60]
                with state.lock:
                    state.greeted += 1
                    state.save()
                    n = state.greeted
                word = settings().get("greeting") or "Hello"
                return self._json(200, {"output": f"{word}, {who}! (greeting number {n} from Hello Mod)",
                                        "data": {"count": n}})
            if path == "/api/wave":
                with state.lock:
                    state.waves += 1
                    state.save()
                    n = state.waves
                if core:
                    ev = core.call("POST", "events", {"type": "hello.waved", "data": {"waves": n, "by": user}})
                    note = core.call("POST", "notify", {"level": "info", "title": "Someone waved",
                                                        "body": f"{user} waved. That is wave number {n}.",
                                                        "url": f"/mods/{core.mod_id}/", "tag": "wave",
                                                        "user": user})
                    if ev.get("error") or note.get("error"):
                        return self._json(502, {"error": ev.get("error") or note.get("error")})
                    return self._json(200, {"ok": True, "waves": n, "event": ev, "notify": note})
                print(f"[notify] Someone waved. That is wave number {n}.", flush=True)
                return self._json(200, {"ok": True, "waves": n})
            if path == "/settings":
                word = str(self._body().get("greeting") or "Hello")[:40]
                if core:
                    core.call("PUT", "settings", {"settings": {"greeting": word}})
                else:
                    local_settings["greeting"] = word
                self.send_response(303)
                self.send_header("Location", "settings")
                self.end_headers()
                return
            return self._json(404, {"error": "not found"})

    return Handler


def build_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    url = os.environ.get("ODYSSEUS_URL")
    core = Core(url, os.environ["ODYSSEUS_MOD_TOKEN"], os.environ.get("ODYSSEUS_MOD_ID", "hello-mod")) if url else None
    data_dir = os.environ.get("MOD_DATA_DIR") or os.path.join(os.getcwd(), "data")
    password = None
    if not core:
        password = os.environ.get("HELLO_PASSWORD") or secrets.token_urlsafe(9)
        if not os.environ.get("HELLO_PASSWORD"):
            print(f"Standalone mode. Password: {password}", flush=True)
    srv = ThreadingHTTPServer((host, port), make_handler(core, State(data_dir), password))
    srv.daemon_threads = True
    return srv


def main() -> None:
    host = os.environ.get("MOD_HOST", "127.0.0.1")
    port = int(os.environ.get("MOD_PORT") or 8765)
    srv = build_server(host, port)
    print(f"Hello Mod on http://{host}:{srv.server_address[1]}/ "
          f"({'under Odysseus' if os.environ.get('ODYSSEUS_URL') else 'standalone'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
