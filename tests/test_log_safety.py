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
