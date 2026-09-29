"""Designed HTML emails are shown as designed; their pictures come through a
proxy that only reaches public addresses; the Inbound header is not cut off;
and a failed MCP connection says what went wrong.

Seen 2026-09-29: "in the emails i dont see any rendering", "html is not
rendering", "when ... clicking one of the inbound it cuts off the header",
and Devices showing "unhandled errors in a TaskGroup (1 sub-exception)".
"""
import asyncio
import os

import httpx
import pytest
from fastapi import HTTPException

from src.mcp_manager import _format_mcp_connection_error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_designed_emails_get_a_sandboxed_frame():
    utils = _read("static", "js", "emailLibrary", "utils.js")
    assert "export function _emailFrameDoc(" in utils and "export function _isDesignedHtml(" in utils
    assert "'script, iframe, object, embed, form, base, meta, link," in utils
    assert "name.startsWith('on')" in utils
    assert "/api/email/image?u=${encodeURIComponent(u)}" in utils
    assert ".replace(/&/g, '&amp;')" in utils                       # srcdoc is decoded once
    js = _read("static", "js", "emailLibrary.js")
    frame = js[js.index("function _renderEmailBody(data) {"):js.index("function _renderEmailBodyText(data) {")]
    # No allow-scripts: nothing in an email runs.
    assert 'sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"' in frame
    assert "allow-scripts" not in frame
    assert 'data-email-view="text"' in frame                        # the text view is a click away


def test_the_sidebar_and_notifications_open_the_email_window():
    inbound = _read("static", "js", "inboundMail.js")
    assert "m.openEmailLibrary({ inbound: true, inboundKey: key || null })" in inbound
    js = _read("static", "js", "emailLibrary.js")
    assert "_inbound.on = !!opts.inbound;" in js
    assert "if (_inbound.openKey === m.key)" in js


def test_the_inbound_header_is_not_pulled_up():
    css = _read("static", "style.css")
    i = css.index(".doclib-card.email-card-expanded.email-inbound-card.memory-item > div:first-child")
    assert "margin-top: 0 !important" in css[i:i + 200]


def _route(name):
    from routes.email_routes import setup_email_routes
    import routes.email_routes as er
    er._start_poller = lambda: None
    router = setup_email_routes()
    return next(r.endpoint for r in router.routes if getattr(r, "path", "") == name)


@pytest.mark.parametrize("url", ["http://127.0.0.1:7000/", "http://100.102.86.125:8931/sse",
                                 "http://localhost/x.png", "file:///etc/passwd", "http://192.168.1.1/a.png"])
def test_the_image_proxy_refuses_this_servers_networks(url):
    image = _route("/api/email/image")
    with pytest.raises(HTTPException) as e:
        asyncio.run(image(u=url, owner="jaron"))
    assert e.value.status_code == 400


def test_mcp_errors_name_the_cause():
    eg = BaseExceptionGroup("unhandled errors in a TaskGroup", [httpx.ConnectTimeout("Connection timed out")])
    msg = _format_mcp_connection_error("windows-desktop", "", [], eg)
    assert "TaskGroup" not in msg and "nothing answered on its port" in msg
    eg = ExceptionGroup("g", [ExceptionGroup("h", [httpx.ConnectError("[Errno 111] Connection refused")])])
    assert "no MCP server is listening" in _format_mcp_connection_error("x", "", [], eg)
    assert _format_mcp_connection_error("x", "", [], RuntimeError("boom")) == "boom"      # plain: as it is
    assert _format_mcp_connection_error("x", "", [], ExceptionGroup("g", [OSError("no route")])) == "OSError: no route"
