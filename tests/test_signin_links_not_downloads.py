"""Sign-in links open the provider's page instead of downloading.

chatRenderer.js fetches every same-origin /api/ link as a file so a chat is
not lost to a raw JSON page. Connect Google Calendar, Link a Google account
and an MCP server's Authorize are redirects to a consent page, so they only
ever said "download failed" (2026-10-01).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _skip_pattern() -> re.Pattern:
    js = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
    m = re.search(r"&& !/(.+?)/\.test\(u\.pathname\)", js)
    assert m, "the /api/ download intercept lost its sign-in exception"
    return re.compile(m.group(1).replace("\\/", "/"))


def test_sign_in_paths_are_left_to_the_browser():
    skip = _skip_pattern()
    for path in ("/api/meet/google/connect", "/api/auth/google/link",
                 "/api/mcp/oauth/authorize/abc123", "/api/auth/google/callback"):
        assert skip.search(path), path


def test_file_links_are_still_downloaded():
    skip = _skip_pattern()
    for path in ("/api/documents/42/download", "/api/files/report.pdf",
                 "/api/chat/export/abc", "/api/linked-devices"):
        assert not skip.search(path), path


def test_the_meeting_row_has_no_stray_brace():
    js = (ROOT / "static/js/devicesSettings.js").read_text(encoding="utf-8")
    assert "Leave</button>` : ''}}</div>" not in js


def test_a_refused_calendar_event_logs_googles_reason():
    src = (ROOT / "src/meet/google_calendar.py").read_text(encoding="utf-8")
    assert 'events.insert said %s: %s", r.status_code, why' in src
