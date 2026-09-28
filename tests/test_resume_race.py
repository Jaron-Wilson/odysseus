"""A refresh must not add a second reply section to a running chat.

Reported on 2026-09-28: "during a refresh it spazzes out and it adds another
<model> section under the chat, so then it keeps getting pushed down while
saying working ... but then at the end it does nothing", and "it will say
waiting to get first token, then I refresh and it says generating response".
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fn(src, start):
    i = src.index(start)
    return src[i:src.index("\n  }\n", i)]


def test_resume_claims_the_chat_before_it_waits_on_the_network():
    src = open(os.path.join(HERE, "static", "js", "chat.js"), encoding="utf-8").read()
    body = _fn(src, "export async function resumeStream(sessionId) {")
    assert body.index("_resumingStreams.add(sessionId);") < body.index("await fetch(`${API_BASE}/api/chat/resume/")
    # every early return gives the claim back
    assert body.count("_resumingStreams.delete(sessionId);") >= 3


def test_the_spinner_waits_for_the_first_token():
    src = open(os.path.join(HERE, "static", "js", "chat.js"), encoding="utf-8").read()
    body = _fn(src, "export async function resumeStream(sessionId) {")
    assert "spinnerModule.create('Waiting for the first token\\u2026'" in body
    assert "if (json.delta || json.type === 'tool_start' || json.type === 'agent_step') _markLive();" in src


def test_one_stream_check_and_one_placeholder_per_chat():
    src = open(os.path.join(HERE, "static", "js", "sessions.js"), encoding="utf-8").read()
    assert "if (_checkingStream.has(sessionId) || _pollingStream.has(sessionId)) return;" in src
    body = src[src.index("async function _checkServerStreamOnce(sessionId) {"):src.index("export function clearStreamComplete")]
    assert body.count("clearInterval(pollId); _pollingStream.delete(sessionId);") == 4
    assert "_pollingStream.add(sessionId);" in body
