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
    # Stop no longer empties the queue: like Escape in Claude Code, the
    # queued messages are sent next.
    assert "clearQueue(sessionModule.getCurrentSessionId());" not in submit[:submit.index("abortCurrentRequest(true)")]


def test_clicking_the_stop_button_always_stops_even_with_text_in_the_box():
    # 2026-10-02: typing anything (including the word "stop") and clicking
    # the button that is showing the Stop icon only queued that text and let
    # the reply keep going — the button that says Stop did not stop. Enter
    # in the textarea mid-reply should still queue (someone still typing
    # while a reply comes in shouldn't get cut off by it), but an explicit
    # click on the button, which is the Stop icon right then, must always
    # reach the real stop path regardless of what is in the box.
    js = _read("static", "js", "chat.js")
    submit = js[js.index("export async function handleChatSubmit"):]
    guard_at = submit.index("const _clickedTheButton = e.submitter === submitBtn;")
    queue_if_at = submit.index("if (isStreaming && !_clickedTheButton) {")
    queue_at = submit.index("queueMessage(_typed)")
    assert guard_at < queue_if_at < queue_at, \
        "an explicit button click must bypass the queue-and-return path"


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


def test_claude_code_style_controls_are_wired():
    js = _read("static", "js", "chat.js")
    assert "async function sendQueuedNow()" in js and "async function takeBackQueued()" in js
    assert "e.key === 'Enter' && (e.ctrlKey || e.metaKey)" in js
    assert "e.key === 'ArrowUp' && hasQueue" in js
    assert js.count("markQueuedDelivered(json.items)") == 2      # live and resumed
    routes = _read("routes", "chat_routes.py")
    assert '"queued_delivered",' in routes
