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
    return redact_secrets(text)


# ---------------------------------------------------------------------------
# Log-wide redaction filter
# ---------------------------------------------------------------------------
# Secrets that live in URLs (MCP access tokens in /public/mcp/<token>, webhook
# links, ?token= query parameters) are printed by uvicorn's access log and by
# httpx/httpcore request logs. ``RedactingFilter`` scrubs every record, so no
# call site has to remember to do it.
import logging

REDACTED = "<redacted>"

# Path segments that are secrets by construction. Mods can add more with
# ``register_redaction``.
_SECRET_PATH_PREFIXES = [r"/public/mcp/", r"/public/link/"]
_QUERY_NAMES = (
    r"token|key|api[_-]?key|apikey|secret|password|passwd|access_token|auth|sig|signature"
)
_QUERY_RE = re.compile(
    r"(?i)([?&](?:" + _QUERY_NAMES + r")=)(?![\[<]redacted)[^&#\s\"'\)\]<>]+"
)
# Generic: a long opaque token directly after /mcp/ (e.g. trv_mcp_...).
_MCP_TOKEN_RE = re.compile(r"(/mcp/)[A-Za-z0-9_\-]{20,}")
_BEARER_BASIC_RE = re.compile(r"(?i)\b((?:bearer|basic)\s+)(?!<redacted>)[A-Za-z0-9\-_.~+/]{8,}=*")
_extra_patterns: list = []
_prefix_re = None


def _build_prefix_re():
    global _prefix_re
    alts = "|".join(_SECRET_PATH_PREFIXES)
    _prefix_re = re.compile(r"(" + alts + r")(?!<redacted>)[^\s/?#\"'<>]+")


_build_prefix_re()


def register_redaction(pattern, *, path_prefix: bool = False) -> None:
    """Register an extra secret pattern for log redaction (used by mods).

    ``pattern`` is a regex string or compiled regex. If it has a capture
    group 1, only the text after that group is replaced (group 1 stays
    visible as a prefix); otherwise the whole match is replaced. With
    ``path_prefix=True``, ``pattern`` is a literal URL path prefix such as
    ``/public/hook/`` and the segment after it is redacted. Never raises.
    """
    try:
        if path_prefix:
            lit = re.escape(str(pattern))
            if lit not in _SECRET_PATH_PREFIXES:
                _SECRET_PATH_PREFIXES.append(lit)
                _build_prefix_re()
            return
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        if rx not in _extra_patterns:
            _extra_patterns.append(rx)
    except Exception:
        pass


def _sub_extra(m):
    if m.re.groups >= 1 and m.group(1) is not None:
        return m.group(1) + REDACTED
    return REDACTED


def redact_secrets(text):
    """Return ``text`` with URL-embedded secrets replaced by ``<redacted>``.

    Idempotent, never raises, returns non-strings unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        text = _prefix_re.sub(lambda m: m.group(1) + REDACTED, text)
        text = _MCP_TOKEN_RE.sub(lambda m: m.group(1) + REDACTED, text)
        text = _QUERY_RE.sub(lambda m: m.group(1) + REDACTED, text)
        text = _BEARER_BASIC_RE.sub(lambda m: m.group(1) + REDACTED, text)
        for rx in _extra_patterns:
            text = rx.sub(_sub_extra, text)
    except Exception:
        pass
    return text


class RedactingFilter(logging.Filter):
    """Redact secrets from a record's rendered message, args and traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.msg
            args = record.args
            if not args:
                if isinstance(msg, str):
                    clean = redact_secrets(msg)
                    if clean != msg:
                        record.msg = clean
            else:
                # Keep args structure (uvicorn's access formatter unpacks
                # them); redact each string arg in place.
                if isinstance(args, tuple):
                    args = tuple(redact_secrets(a) for a in args)
                elif isinstance(args, dict):
                    args = {k: redact_secrets(v) for k, v in args.items()}
                record.args = args
                # A secret split across msg and args ("/public/mcp/%s") only
                # shows once rendered; collapse to plain text in that case.
                try:
                    rendered = str(msg) % args
                except Exception:
                    rendered = None
                if rendered is not None:
                    clean = redact_secrets(rendered)
                    if clean != rendered:
                        record.msg = clean
                        record.args = ()
            if record.exc_info and not record.exc_text:
                text = logging.Formatter().formatException(record.exc_info)
                record.exc_text = redact_secrets(text)
            elif record.exc_text:
                record.exc_text = redact_secrets(record.exc_text)
        except Exception:
            pass
        return True


_FILTER = RedactingFilter()
_NAMED_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore")


def install_log_redaction() -> None:
    """Attach the redacting filter to root + library loggers and their handlers.

    Idempotent; call at startup (and again after handlers change).
    """
    try:
        targets = [logging.getLogger()] + [logging.getLogger(n) for n in _NAMED_LOGGERS]
        for lg in targets:
            if _FILTER not in lg.filters:
                lg.addFilter(_FILTER)
            for h in lg.handlers:
                if _FILTER not in h.filters:
                    h.addFilter(_FILTER)
    except Exception:
        pass
