from core.log_safety import redact_url, scrub


def test_strips_userinfo():
    assert redact_url("https://user:pass@host.example/v1/models") == "https://host.example/v1/models"


def test_strips_query_and_fragment():
    assert redact_url("https://host.example/v1?api_key=secret#frag") == "https://host.example/v1"


def test_keeps_port_and_path():
    assert redact_url("http://host.example:8080/api/tags") == "http://host.example:8080/api/tags"


def test_ipv6_host_keeps_brackets():
    assert redact_url("https://user:pass@[2001:db8::1]:8443/v1") == "https://[2001:db8::1]:8443/v1"
    assert redact_url("https://[2001:db8::1]/v1") == "https://[2001:db8::1]/v1"


def test_no_credentials_passthrough():
    assert redact_url("https://host.example/v1/models") == "https://host.example/v1/models"


def test_empty_and_none():
    assert redact_url("") == ""
    assert redact_url(None) == ""


def test_garbage_does_not_raise():
    # urlparse is lenient; just assert no credential-looking userinfo survives.
    assert "@" not in redact_url("::::not a url::::")


# ---------------------------------------------------------------------------
# scrub — free-text log-line redaction
# ---------------------------------------------------------------------------

def test_scrub_redacts_authorization_header():
    line = "request failed: Authorization: Bearer sk-abc123xyz, status 401"
    out = scrub(line)
    assert "sk-abc123xyz" not in out
    assert "Authorization: [redacted]" in out


def test_scrub_redacts_bearer_token_without_header_prefix():
    line = "connect to wss://relay.example/stream?token=abc token=Bearer eyJhbGciOi.xyz.sig failed"
    out = scrub(line)
    assert "eyJhbGciOi.xyz.sig" not in out


def test_scrub_redacts_api_key_header():
    line = "GET http://host/v1 X-Api-Key: topsecret123 -> 403"
    out = scrub(line)
    assert "topsecret123" not in out
    assert "[redacted]" in out


def test_scrub_redacts_query_string_secrets():
    line = "HTTPStatusError: http://tts.example/v1/speak?api_key=sk-livekey&voice=en"
    out = scrub(line)
    assert "sk-livekey" not in out
    assert "voice=en" in out  # non-secret params survive


def test_scrub_redacts_access_token_and_password_params():
    assert "hunter2" not in scrub("http://h/x?password=hunter2")
    assert "tok-123" not in scrub("http://h/x?access_token=tok-123&next=/home")


def test_scrub_is_noop_on_clean_text():
    line = "call ended after 3 turn(s)"
    assert scrub(line) == line


def test_scrub_handles_empty_and_none():
    assert scrub("") == ""
    assert scrub(None) is None


import logging

from core.log_safety import RedactingFilter, redact_secrets, register_redaction

FAKE = "trv_mcp_FAKEFAKEFAKEFAKEFAKEFAKE123456"


def _rec(msg, args=()):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def _out(msg, args=()):
    r = _rec(msg, args)
    assert RedactingFilter().filter(r) is True
    return r.getMessage()


def test_uvicorn_access_style_args():
    out = _out('%s - "%s %s HTTP/%s" %d',
               ("127.0.0.1:1", "POST", f"/mods/trove/public/mcp/{FAKE}", "1.1", 200))
    assert FAKE not in out
    assert "/mods/trove/public/mcp/<redacted>" in out and out.endswith("200")


def test_args_structure_preserved_for_uvicorn():
    r = _rec('%s - "%s %s HTTP/%s" %d',
             ("1.2.3.4:5", "GET", f"/public/mcp/{FAKE}", "1.1", 200))
    RedactingFilter().filter(r)
    assert len(r.args) == 5 and r.args[2] == "/public/mcp/<redacted>" and r.args[4] == 200


def test_secret_split_across_msg_and_arg():
    out = _out("POST /public/mcp/%s done", (FAKE,))
    assert FAKE not in out and "/public/mcp/<redacted>" in out


def test_httpx_style_messages():
    out = _out(f"HTTP Request: POST https://h.example/mods/trove/public/mcp/{FAKE} \"HTTP/1.1 200 OK\"")
    assert out == 'HTTP Request: POST https://h.example/mods/trove/public/mcp/<redacted> "HTTP/1.1 200 OK"'
    out = _out(f"HTTP Request: POST http://127.0.0.1:9/public/mcp/{FAKE}")
    assert FAKE not in out and "/public/mcp/<redacted>" in out


def test_generic_mcp_token_and_link():
    assert redact_secrets(f"/x/mcp/{FAKE}") == "/x/mcp/<redacted>"
    assert redact_secrets("/public/link/abc123") == "/public/link/<redacted>"
    assert redact_secrets("/x/mcp/tools") == "/x/mcp/tools"


def test_query_params():
    out = redact_secrets("GET /a?Token=abc&x=1&API_KEY=def&sig=g&signature=h&password=p HTTP/1.1")
    assert out == ("GET /a?Token=<redacted>&x=1&API_KEY=<redacted>&sig=<redacted>"
                   "&signature=<redacted>&password=<redacted> HTTP/1.1")
    assert redact_secrets("/a?monkey=1&keyboard=2") == "/a?monkey=1&keyboard=2"


def test_bearer_basic():
    assert redact_secrets("Authorization: Bearer abcdefghijkl").endswith("Bearer <redacted>")
    assert redact_secrets("got Basic dXNlcjpwYXNz") == "got Basic <redacted>"


def test_non_secret_unchanged_and_idempotent():
    line = "GET /api/sessions?limit=5 HTTP/1.1 200"
    assert _out(line) == line
    assert _out("%d items for %s", (3, "bob")) == "3 items for bob"
    once = redact_secrets(f"/public/mcp/{FAKE}?token=zzz")
    assert redact_secrets(once) == once


def test_bad_args_never_raise():
    assert RedactingFilter().filter(_rec("%d items", ("notanint",))) is True
    assert RedactingFilter().filter(_rec("plain", None)) is True


def test_exception_text_redacted():
    import sys
    try:
        raise RuntimeError(f"fail https://h/public/mcp/{FAKE}")
    except RuntimeError:
        r = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    RedactingFilter().filter(r)
    assert FAKE not in logging.Formatter().format(r)


def test_register_redaction():
    register_redaction(r"(hook_)[a-z0-9]{6,}")
    assert redact_secrets("at hook_abcdef123") == "at hook_<redacted>"
    register_redaction("/public/hook/", path_prefix=True)
    assert redact_secrets("/public/hook/s3cr3t/x") == "/public/hook/<redacted>/x"


def test_scrub_still_uses_legacy_marker():
    assert scrub("?api_key=abc") == "?api_key=[redacted]"
