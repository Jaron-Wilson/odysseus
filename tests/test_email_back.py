"""An open email has a Back button to the list.

Asked for on 2026-09-29: "on email page i want a go back button, so when i
click an email read it i want to go back."
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_open_email_has_back():
    js = open(os.path.join(ROOT, "static", "js", "emailLibrary.js"), encoding="utf-8").read()
    list_reader = js[js.index("async function _toggleCardPreview("):js.index("async function _openEmailAsTab(")]
    i = list_reader.index('class="memory-toolbar-btn reader-icon-btn email-reader-back" data-act="close"')
    assert i < list_reader.index('data-act="reply"')                     # first, before Reply
    assert "reader.querySelector('[data-act=\"close\"]')?.addEventListener('click'" in list_reader
    assert '<span class="reader-btn-label">Back</span>' in list_reader
