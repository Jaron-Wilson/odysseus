"""A video linked in a reply plays without a refresh, however the reply got
on screen.

Seen 2026-09-29: "video linked in chat then i refreshed and it showed the
video as a view i can see": a reply picked up again mid-run (not the live
stream's own end) left its media link bare until the page was reloaded.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_finished_replies_are_watched_for_media_links():
    js = open(os.path.join(ROOT, "static", "js", "markdown.js"), encoding="utf-8").read()
    watch = js[js.index("// Players for media links however a reply got on screen."):]
    assert "document.getElementById('chat-history')" in watch
    assert "root.querySelectorAll('.msg-ai:not(.streaming)')" in watch      # not while it streams
    assert "attributeFilter: ['class']" in watch                            # catches the end of streaming
    assert "if (!timer) timer = setTimeout(run, 300);" in watch             # batched
