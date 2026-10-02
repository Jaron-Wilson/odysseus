"""Helpers for keeping sensitive data out of logs.

Endpoint URLs configured by admins can embed credentials in the userinfo
(``https://user:pass@host``) or query string (``?api_key=...``). Logging them
raw leaks those secrets, so route/diagnostic logs run URLs through
``redact_url`` first. Reconstructing the URL without userinfo/query/fragment
also doubles as a sanitizer barrier for CodeQL's clear-text-logging query.

``scrub`` is the broader counterpart: it redacts credential-shaped substrings
(Authorization/API-key headers, bearer tokens, query-string secrets) out of an
arbitrary log line, for call sites that log free-text error messages rather
than a single known URL value — e.g. an exception's ``str()``, which can embed
the request URL (and any query-string secret) a lower-level HTTP client
attached to its message.
"""

import re
from urllib.parse import urlparse, urlunparse


def redact_url(url: str) -> str:
    """Return a URL safe for logs by removing userinfo and query/fragment.

    Keeps scheme, host, port and path so logs stay useful for debugging.
    """
    try:
        parsed = urlparse(url or "")
        host = parsed.hostname or ""
        if ":" in host:  # IPv6 literal — re-bracket so host:port stays unambiguous
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
    except Exception:
        return "<endpoint>"


# Each pattern is a single bounded run (no nested quantifiers), so none of
# these can backtrack catastrophically regardless of input length.
_AUTH_HEADER_RE = re.compile(r'(?i)(authorization\s*:\s*)[^,;\n\r"\']+')
_BEARER_RE = re.compile(r'(?i)(bearer\s+)[A-Za-z0-9\-_.=]+')
_API_KEY_HEADER_RE = re.compile(r'(?i)((?:x-api-key|x-auth-token|api-key)\s*[:=]\s*)\S+')
# Query-string secrets: ?api_key=..., &token=..., etc. Value runs to the next
# '&', '#', whitespace, or quote/bracket so it doesn't eat trailing log text.
_QUERY_SECRET_RE = re.compile(
    r'(?i)([?&](?:api[_-]?key|token|secret|password|passwd|access_token|auth)=)'
    r'[^&#\s"\'\)\]]+'
)


def scrub(text: str) -> str:
    """Redact credential-shaped substrings from an arbitrary log line.

    Unlike ``redact_url``, this does not require the caller to know the value
    is a URL — it is meant for free-text (an exception's ``str()``, a raw
    response snippet, etc.) that might embed an ``Authorization:`` header, a
    bearer token, or a query-string secret. Safe to call on any string,
    including one with no secrets, in which case it is returned unchanged.
    """
    if not text:
        return text
    text = _AUTH_HEADER_RE.sub(r'\1[redacted]', text)
    text = _BEARER_RE.sub(r'\1[redacted]', text)
    text = _API_KEY_HEADER_RE.sub(r'\1[redacted]', text)
    text = _QUERY_SECRET_RE.sub(r'\1[redacted]', text)
    return text
