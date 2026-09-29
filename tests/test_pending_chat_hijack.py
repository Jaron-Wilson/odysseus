"""A New chat that was never sent to must not take the next message of the chat on screen.

Seen 2026-09-29: "when i replied with my music chat bar it created a new
chat instead" and "i approved ... it cloned the chat". A pending New chat
(a model picked with no chat open) was left set when an existing chat was
opened, so the next message sent in that chat (a queued overlay reply, a
Stop-and-resend of a plan approval) created a new chat on the pending
chat's model and went there instead, without any of the chat's context.
Reproduced in a browser on dev and checked fixed on this branch.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def test_opening_a_chat_drops_a_pending_new_chat():
    js = _read("static", "js", "sessions.js")
    start = js.index("export async function selectSession(")
    body = js[start:js.index("\n}\n", start)]
    assert "_pendingChat = null;" in body


def test_a_pending_chat_is_only_made_real_when_no_chat_is_open():
    js = _read("static", "js", "chat.js")
    i = js.index("sessionModule.materializePendingSession();")
    guard = js[js.rfind("if (", 0, i):i]
    assert "!sessionModule.getCurrentSessionId()" in guard


def test_send_now_reads_the_queue_from_the_server():
    js = _read("static", "js", "chat.js")
    start = js.index("async function sendQueuedNow()")
    body = js[start:js.index("\n  }\n", start)]
    assert "_queueCall(sid, '', 'GET')" in body
    assert body.index("_queueCall(sid, '', 'GET')") < body.index("_queueCall(sid, '', 'DELETE')")
