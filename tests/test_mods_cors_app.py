"""CORS for sandboxed mod pages, through the whole app (app.py), not just the
proxy: the global CORSMiddleware used to answer every preflight itself and
reject `Origin: null` with a 400, so a mod page's JSON POST never reached the
proxy's own CORS handling (src/mods/proxy.py).

The app is imported in a child process: the test suite stubs modules app.py
needs (tests/conftest.py), and the child gets its own data dir, no .env and no
route to a live core or ChromaDB.
"""
import json
import os
import subprocess
import sys

from core.middleware import is_mod_frame_cors

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r'''
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _echo(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.dumps({"method": self.command, "got": self.rfile.read(n).decode(),
                           "user": self.headers.get("X-Odysseus-User"),
                           "custom": self.headers.get("X-Custom")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = do_PUT = _echo


up = ThreadingHTTPServer(("127.0.0.1", 0), H)
up.daemon_threads = True
threading.Thread(target=up.serve_forever, daemon=True).start()

import app as appmod
from starlette.testclient import TestClient
from src.mods import signing
from tests.mods_helpers import add_mod, mark_running

add_mod(access="users")
mark_running("test-mod", up.server_address[1])
tok = signing.frame_token("test-mod", "alice")
base = f"/mods/test-mod/~{tok}"
c = TestClient(appmod.app)        # no `with`: no lifespan, nothing else starts
out = {}

def pre(path, origin="null", headers="content-type, x-custom"):
    r = c.options(path, headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                 "Access-Control-Request-Headers": headers})
    return {"status": r.status_code, "headers": {k.lower(): v for k, v in r.headers.items()}}

out["preflight"] = pre(base + "/api/jobs")
r = c.post(base + "/api/jobs", json={"x": 1}, headers={"Origin": "null", "X-Custom": "yes"})
out["post"] = {"status": r.status_code, "body": r.json(), "headers": {k.lower(): v for k, v in r.headers.items()}}
r = c.delete(base + "/api/jobs/1", headers={"Origin": "null"})
out["delete"] = {"status": r.status_code, "body": r.json(), "acao": r.headers.get("access-control-allow-origin")}
out["bad_token"] = pre("/mods/test-mod/~bogus.token/api/jobs")
out["no_token"] = pre("/mods/test-mod/api/jobs")
out["frame_other_origin"] = pre(base + "/api/jobs", origin="https://evil.example")
out["api_null"] = pre("/api/mods")
out["api_allowed"] = pre("/api/mods", origin="http://localhost", headers="content-type")
print("RESULT " + json.dumps(out))
'''


def _run(tmp_path):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env.update({
        "ODYSSEUS_ENV_FILE": "none",
        "ODYSSEUS_DATA_DIR": str(tmp_path / "data"),
        "ODYSSEUS_MODS_DIR": str(tmp_path / "data" / "mods"),
        "DATABASE_URL": f"sqlite:///{tmp_path}/app.db",
        "ODYSSEUS_INTERNAL_BASE": "http://127.0.0.1:9",
        "CHROMADB_DISABLED": "1",
        "AUTH_ENABLED": "true",
        "ALLOWED_ORIGINS": "http://localhost",
        "PYTHONPATH": ROOT,
    })
    (tmp_path / "data").mkdir()
    p = subprocess.run([sys.executable, "-c", CHILD], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=180)
    line = next((x for x in p.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, f"child failed ({p.returncode}):\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}"
    return json.loads(line[len("RESULT "):])


def test_mod_frame_cors_through_the_whole_app(tmp_path):
    out = _run(tmp_path)

    # A framed page's preflight reaches the proxy, which allows Origin null.
    pre = out["preflight"]
    assert pre["status"] == 204, pre
    assert pre["headers"]["access-control-allow-origin"] == "null"
    assert "POST" in pre["headers"]["access-control-allow-methods"]
    assert pre["headers"]["access-control-allow-headers"] == "content-type, x-custom"
    assert "access-control-allow-credentials" not in pre["headers"]

    # The JSON POST and DELETE themselves get through with the user's identity.
    post = out["post"]
    assert post["status"] == 200 and post["body"]["got"] == '{"x":1}' and post["body"]["custom"] == "yes"
    assert post["body"]["user"] == "alice"
    assert post["headers"]["access-control-allow-origin"] == "null"
    assert "access-control-allow-credentials" not in post["headers"]
    assert out["delete"]["status"] == 200 and out["delete"]["body"]["method"] == "DELETE"
    assert out["delete"]["acao"] == "null"

    # Only a valid frame token gets that.
    assert out["bad_token"]["status"] == 401
    assert "access-control-allow-origin" not in out["bad_token"]["headers"]

    # Everything else keeps the global CORS rules.
    assert out["no_token"]["status"] == 400
    assert out["frame_other_origin"]["status"] == 400
    assert out["api_null"]["status"] == 400
    assert "access-control-allow-origin" not in out["api_null"]["headers"]
    assert out["api_allowed"]["status"] == 200
    assert out["api_allowed"]["headers"]["access-control-allow-origin"] == "http://localhost"


def _scope(path, origin=b"null", type_="http"):
    headers = [(b"origin", origin)] if origin is not None else []
    return {"type": type_, "path": path, "headers": headers}


def test_only_null_origin_frame_paths_bypass_the_global_cors():
    assert is_mod_frame_cors(_scope("/mods/print-hub/~abc.def/api/jobs"))
    assert is_mod_frame_cors(_scope("/mods/print-hub/~abc.def/"))
    assert not is_mod_frame_cors(_scope("/mods/print-hub/api/jobs"))         # no frame token
    assert not is_mod_frame_cors(_scope("/mods/print-hub/~abc", origin=b"https://evil.example"))
    assert not is_mod_frame_cors(_scope("/mods/print-hub/~abc", origin=None))
    assert not is_mod_frame_cors(_scope("/api/mods/~abc"))
    assert not is_mod_frame_cors(_scope("/x/mods/print-hub/~abc"))
    assert not is_mod_frame_cors(_scope("/mods//~abc"))
    assert not is_mod_frame_cors(_scope("/mods/print-hub/~abc", type_="websocket"))
    two = {"type": "http", "path": "/mods/a/~t/x", "headers": [(b"origin", b"null"), (b"origin", b"null")]}
    assert not is_mod_frame_cors(two)
