"""Static guard for queued messages in the chat composer.

Pressing Enter mid-reply used to stop the reply. With text in the box it now
queues the message, and each finished reply sends the next one. Checked in a
real browser when it was built; this keeps the wiring from being undone.
"""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(HERE, *parts), encoding="utf-8") as f:
        return f.read()


def test_enter_mid_reply_with_text_queues_before_the_stop_path():
    js = _read("static", "js", "chat.js")
    submit = js[js.index("export async function handleChatSubmit"):]
    queue_at = submit.index("queueMessage(_typed)")
    stop_at = submit.index("abortCurrentRequest(true)")
    assert queue_at < stop_at, "text typed mid-reply must be queued before Stop runs"
    assert "clearQueue(sessionModule.getCurrentSessionId());   // an explicit Stop stops everything" in submit


def test_a_finished_reply_sends_the_next_queued_message():
    js = _read("static", "js", "chat.js")
    idle = js[js.index("} else if (state === 'idle') {"):]
    assert "setTimeout(drainQueue, 700)" in idle[:800]
    assert re.search(r"function drainQueue\(\)\s*\{\s*if \(isStreaming\) return;", js)


def test_queue_panel_and_button_are_wired():
    assert '<div id="chat-queue" class="chat-queue" hidden></div>' in _read("static", "index.html")
    app = _read("static", "app.js")
    assert "Queue: sends when this reply finishes" in app
    assert ".chat-queue {" in _read("static", "style.css")
