"""Inbound mail is one of the Email window's entries, and an HTML email is
shown as HTML.

Asked for on 2026-09-29: "i see all default, then my gmail, but the inbounds
are not there, i would like that to be shown, and emails that generate using
html lets generate those too."
"""
import os
from email.message import EmailMessage

from src import mail_listener

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_an_html_email_keeps_its_html():
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "Form <form@clevernode.org>", "submissions@clevernode.org", "New submission"
    msg.set_content("Plan: Pro")
    msg.add_alternative("<table><tr><td><b>Plan</b></td><td>Pro</td></tr></table>", subtype="html")
    m = mail_listener.parse(msg.as_bytes())
    assert m["body"].strip() == "Plan: Pro"
    assert "<table>" in m["body_html"]
    plain = EmailMessage()
    plain["From"], plain["To"], plain["Subject"] = "a@b.c", "d@e.f", "Hi"
    plain.set_content("Just text")
    assert mail_listener.parse(plain.as_bytes())["body_html"] == ""


def test_read_message_sends_it():
    src = _read("src", "mail_listener.py")
    i = src.index("def read_message(")
    assert '"body_html": html if len(html) <= 500000 else ""' in src[i:i + 800]


def test_the_email_window_has_inbound():
    js = _read("static", "js", "emailLibrary.js")
    assert "import * as inboundMail from './inboundMail.js';" in js
    assert 'data-inbound="1"' in js and "_loadInbound(grid, seq)" in js
    # Its HTML goes through the same sanitizer as every other email.
    assert "_safeRenderEmailBody({ body: e.body || '', body_html: e.body_html || '' })" in js
    reader = js[js.index("async function _openInbound("):]
    assert reader.index('data-act="close"') < reader.index('data-act="delete"')   # Back first
    inbound = _read("static", "js", "inboundMail.js")
    for fn in ("export async function read(", "export async function list(",
               "export async function remove(", "export async function ask("):
        assert fn in inbound
