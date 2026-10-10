"""The composer's Stop button: always there while a reply runs, never the mic.

Two reports from 2026-10-10:

1. "It's responding but not having a stop button." A reply this page did not
   start itself (re-attached after a reload or a chat switch, a queued
   message the server sent, a brought-back or approved run, a same-tab
   stream back on screen from the background) was drawn live, but the
   button stayed mic/send: resumeStream and checkBackgroundStream never
   touched it. Those runs now put the button in Stop (chat.js watchRun), and
   Stop posts /api/chat/stop for them.

2. "When I go to press stop it starts the voice recording." Stop and the mic
   are the same element (.send-btn). Its click handler tried "empty box and
   speech-to-text on: start recording" before it ever looked at whether the
   button was Stop, so with an empty box pressing Stop recorded and the reply
   kept going. The launch animation could also paint the Stop square onto a
   button that was already back in mic mode, and a tap landing just after
   the reply ended hit the mic.

Checked in a real browser when it was built; these keep the wiring in place.
"""
import asyncio
import re
from pathlib import Path

import pytest

from src import agent_runs

_REPO = Path(__file__).resolve().parent.parent
_APP = (_REPO / "static" / "app.js").read_text(encoding="utf-8")
_CHAT = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
_SESSIONS = (_REPO / "static" / "js" / "sessions.js").read_text(encoding="utf-8")


def _fn(src, name):
    """Body of a function, by brace matching."""
    m = re.search(r"(?:async )?function %s\s*\(" % re.escape(name), src)
    assert m, "%s not found" % name
    k = src.index("{", m.end() - 1)
    depth = 0
    for n in range(k, len(src)):
        if src[n] == "{":
            depth += 1
        elif src[n] == "}":
            depth -= 1
            if depth == 0:
                return src[k:n + 1]
    raise AssertionError("unbalanced braces in %s" % name)


def _send_click_handler():
    start = _APP.index("sendBtn.addEventListener('click', (e) => {")
    end = _APP.index("// Enter to send (shift+enter for newline)", start)
    return _APP[start:end]


# ── Stop never starts a recording ─────────────────────────────────────────

def test_stop_is_handled_before_the_mic_branch():
    h = _send_click_handler()
    stop_at = h.index("if (sendBtn.dataset.mode === 'streaming') {")
    assert stop_at < h.index("voiceRecorderModule.startRecording("), \
        "the Stop check must come before the empty-box mic branch"
    assert stop_at < h.index("sendBtn.dataset.mode === 'newchat'")
    stop_block = h[stop_at:h.index("return;", stop_at)]
    assert "submitter: sendBtn" in stop_block, \
        "a Stop click must reach handleChatSubmit as the button, so it stops even with text"
    assert "return;" in h[stop_at:stop_at + 600]


def test_taps_just_after_stop_or_mid_send_do_not_record():
    h = _send_click_handler()
    mic_at = h.index("voiceRecorderModule.startRecording(")
    assert h.index("sendBtn.classList.contains('send-pending')") < mic_at
    assert h.index("sendBtn._stopEndedAt") < mic_at
    idle = _CHAT[_CHAT.index("} else if (state === 'idle') {"):]
    assert "submitBtn._stopEndedAt = Date.now()" in idle[:600]


def test_launch_animation_does_not_paint_stop_onto_an_idle_button():
    stream = _CHAT[_CHAT.index("if (state === 'streaming') {"):_CHAT.index("} else if (state === 'watching') {")]
    swap = stream[stream.index("setTimeout(() => {"):]
    assert swap.index("submitBtn.dataset.mode !== 'streaming'") < swap.index("submitBtn.innerHTML = _stopSvg;")


def test_stop_stays_visible_while_typing_mid_reply():
    # The queue icon replaced Stop whenever the box had text, so Stop was
    # gone for as long as anything was typed.
    assert "_queueIcon" not in _APP
    upd = _fn(_APP, "_updateSendBtnIcon")
    streaming = upd[upd.index("if (sendBtn.dataset.mode === 'streaming') {"):]
    streaming = streaming[:streaming.index("return;")]
    assert "innerHTML" not in streaming


# ── Stop is there for every running reply ─────────────────────────────────

def test_resumed_runs_show_stop_and_give_it_back():
    body = _fn(_CHAT, "resumeStream")
    assert "watchRun(sessionId);" in body
    cleanup = body[body.index("const cleanup = () => {"):]
    assert "unwatchRun(sessionId);" in cleanup[:300]


def test_background_stream_back_on_screen_shows_stop():
    body = _fn(_CHAT, "checkBackgroundStream")
    assert "watchRun(sessionId);" in body
    assert body.count("unwatchRun(sessionId);") >= 2, "both ways the poll ends must give the button back"


def test_sessions_fallback_poll_shows_stop():
    start = _SESSIONS.index("async function _checkServerStreamOnce")
    body = _SESSIONS[start:_SESSIONS.index("export function clearStreamComplete", start)]
    assert "watchRun(sessionId)" in body and "unwatchRun(sessionId)" in body
    assert "clearInterval(pollId); _pollingStream.delete(sessionId);" in body  # only inside _endPoll
    assert body.count("_endPoll();") >= 4


def test_stop_on_a_watched_run_cancels_it_on_the_server():
    submit = _CHAT[_CHAT.index("export async function handleChatSubmit"):]
    watched_at = submit.index("if (!isStreaming && _isWatchingCurrent()) {")
    assert watched_at < submit.index("if (isStreaming && !_clickedTheButton) {")
    assert "_stopWatchedRun(sessionId);" in submit[watched_at:watched_at + 800]
    stop = _fn(_CHAT, "_stopWatchedRun")
    assert "_postStop(sid)" in stop and "unwatchRun(sid)" in stop
    assert "/api/chat/stop/" in _fn(_CHAT, "_postStop")
    assert "_postStop(_sid)" in _fn(_CHAT, "abortCurrentRequest")


def test_watch_api_is_exported_for_sessions_js():
    tail = _CHAT[_CHAT.rindex("resumeStream,"):]
    assert "watchRun," in tail[:200] and "unwatchRun," in tail[:200]


def test_tab_recovery_resets_the_real_button():
    # getElementById('submit') matched nothing, so a recovered stream left a
    # Stop icon on a button that no longer stopped anything.
    assert "getElementById('submit')" not in _CHAT


# ── The server side of Stop for a re-attached page ────────────────────────

@pytest.mark.asyncio
async def test_stop_ends_a_live_subscriber_promptly():
    """A page re-attached through /api/chat/resume is a subscriber. Stop must
    end its stream right away, so the page settles and reloads the turn."""
    sid = "sess-stop-button-subscriber"
    agent_runs._RUNS.pop(sid, None)
    hang = asyncio.Event()

    async def agen():
        yield "data: {\"delta\": \"hi\"}\n\n"
        await hang.wait()            # a tool or the model, still going
        yield "data: never\n\n"

    run = agent_runs.start(sid, agen())
    got = []

    async def read():
        async for ev in agent_runs.subscribe(sid):
            got.append(ev)

    reader = asyncio.create_task(read())
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    assert got, "the subscriber never saw the first event"
    assert agent_runs.stop(sid) is True
    await asyncio.wait_for(reader, timeout=1.0)
    assert run.status == "stopped"
    assert "data: never\n\n" not in got
    agent_runs._RUNS.pop(sid, None)
