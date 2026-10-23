"""A fake add-on that serves models (llm.provider) for the tests: an
OpenAI-compatible server at /mod/cap/llm.provider/v1/ that records each
request and the signed user, registered as the mod `chatgpt`. Nothing
leaves the machine."""
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.mods import paths, signing
from tests.mods_helpers import add_mod, mark_running, mods_dir  # noqa: F401

MOD_ID = "chatgpt"
MOD_BASE = "mod://chatgpt/v1"
MOD_URL = MOD_BASE + "/chat/completions"
MODEL = "gpt-5.6-terra"
META = {"label": "ChatGPT", "usage_provider": "chatgpt-plan", "billing": "subscription", "tool_cap": True}


class FakeModLLM:
    def __init__(self, token):
        self.token = token
        self.calls = []
        self.reply = "Working on it."
        self.status = 200
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _record(self, body):
                outer.calls.append({"path": self.path, "json": body,
                                    "user": signing.verify_identity(outer.token, self.headers),
                                    "auth": self.headers.get("Authorization")})

            def do_GET(self):
                self._record(None)
                data = json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                self._record(body)
                if outer.status != 200:
                    data = json.dumps({"error": {"type": "add_on", "message": "Sign in with ChatGPT on the ChatGPT tab first.",
                                                 "code": "not_signed_in"}}).encode()
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunk = {"choices": [{"index": 0, "delta": {"content": outer.reply}}]}
                    usage = {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
                    for c in (chunk, usage):
                        self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                data = json.dumps({"choices": [{"message": {"role": "assistant", "content": outer.reply}}],
                                   "usage": {"prompt_tokens": 12, "completion_tokens": 3}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def chat_calls(self):
        return [c for c in self.calls if c["path"].endswith("/chat/completions")]


def install_fake_mod(meta=None, enabled=True):
    entry, token = add_mod(mod_id=MOD_ID, enabled=enabled, access="users")
    with open(os.path.join(paths.src_dir(MOD_ID), "odysseus-mod.json"), "w") as f:
        json.dump({"provides": ["llm.provider"], "llm_provider": meta if meta is not None else META}, f)
    return entry, token


@pytest.fixture
def mod_llm(mods_dir, monkeypatch):
    from src.mods import providers
    providers._raw_cache.clear()
    _, token = install_fake_mod()
    fake = FakeModLLM(token)
    mark_running(MOD_ID, fake.port)
    yield fake
    fake.srv.shutdown()


def mod_endpoint(owner="alice", supports_tools=None):
    from core.database import SessionLocal, ModelEndpoint
    db = SessionLocal()
    try:
        ep = ModelEndpoint(id="mod" + uuid.uuid4().hex[:5], name="ChatGPT", base_url=MOD_BASE, owner=owner,
                           is_enabled=True, api_key=None, cached_models=json.dumps([MODEL]),
                           supports_tools=supports_tools)
        db.add(ep)
        db.commit()
        return ep.id
    finally:
        db.close()


def drop_endpoint(ep_id):
    from core.database import SessionLocal, ModelEndpoint
    db = SessionLocal()
    try:
        db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).delete()
        db.commit()
    finally:
        db.close()
