"""An unauthenticated /v1/* request gets a 401 with an OpenAI-style JSON error,
never a 302 to the login page.

2026-11-01: opencode (the coding tool's engine) was pointed at the gateway with
no token. `/v1/models` answered 302 -> /login, opencode followed it, got no
completion and retried 4,878 empty steps in 27 minutes. A browser page still
gets the login redirect; only the program-facing /v1/ paths change.

The whole app (app.py) is imported in a child process, like
tests/test_mods_cors_app.py: the suite stubs modules app.py needs, and the child
gets its own data dir and no route to a live core.
"""
import json
import os
import subprocess
import sys

from core.middleware import is_openai_api_path, openai_auth_error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r'''
import json
import app as appmod
from starlette.testclient import TestClient

_cfg = [True]
type(appmod.auth_manager).is_configured = property(lambda self: _cfg[0])
c = TestClient(appmod.app, follow_redirects=False)
out = {}

def hit(name, path, **kw):
    r = c.get(path, **kw)
    try:
        body = r.json()
    except Exception:
        body = None
    out[name] = {"status": r.status_code, "location": r.headers.get("location"), "body": body,
                 "www": r.headers.get("www-authenticate")}

hit("models_none", "/v1/models")
hit("models_empty_bearer", "/v1/models", headers={"Authorization": "Bearer "})
hit("models_bad_token", "/v1/models", headers={"Authorization": "Bearer ody_" + "x" * 43})
hit("models_short_token", "/v1/models", headers={"Authorization": "Bearer ody_x"})
r = c.post("/v1/chat/completions", json={"model": "m", "messages": []})
out["chat_none"] = {"status": r.status_code, "location": r.headers.get("location"), "body": r.json()}
hit("page", "/")                       # a browser page keeps its redirect
hit("api", "/api/chats")               # /api/ keeps its own JSON 401

_cfg[0] = False
hit("models_unconfigured", "/v1/models")
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
        "LOCALHOST_BYPASS": "false",
        "PYTHONPATH": ROOT,
    })
    (tmp_path / "data").mkdir()
    p = subprocess.run([sys.executable, "-c", CHILD], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=180)
    line = next((x for x in p.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, f"child failed ({p.returncode}):\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}"
    return json.loads(line[len("RESULT "):])


def _is_openai_401(r):
    assert r["status"] == 401, r
    assert not r.get("location"), r
    err = r["body"]["error"]
    assert isinstance(err["message"], str) and err["message"]
    assert err["type"] == "authentication_error"
    assert err["code"]


def test_v1_without_credentials_is_a_json_401_not_a_redirect(tmp_path):
    out = _run(tmp_path)
    for name in ("models_none", "models_empty_bearer", "models_bad_token", "models_short_token",
                 "chat_none", "models_unconfigured"):
        _is_openai_401(out[name])
    assert out["models_none"]["www"] == "Bearer"
    # The rest of the app is unchanged.
    assert out["page"]["status"] == 302 and out["page"]["location"] == "/login"
    assert out["api"]["status"] == 401 and out["api"]["body"] == {"error": "Not authenticated"}


def test_openai_path_predicate():
    assert is_openai_api_path("/v1/models")
    assert is_openai_api_path("/v1/chat/completions")
    assert is_openai_api_path("/v1")
    assert not is_openai_api_path("/v10/x")
    assert not is_openai_api_path("/api/v1/models")
    assert not is_openai_api_path("/api/mods/v1/stats")


def test_openai_auth_error_shape():
    r = openai_auth_error("nope", "missing_api_key")
    assert r.status_code == 401
    assert json.loads(r.body) == {"error": {"message": "nope", "type": "authentication_error",
                                            "code": "missing_api_key"}}
