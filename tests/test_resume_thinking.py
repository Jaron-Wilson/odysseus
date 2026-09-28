"""After a refresh, a local model's thinking stays in its Thinking section.

Reported on 2026-09-28: "when refreshing sometimes the <thinking> gets out and
acts as a normal call". vLLM's reasoning parser sends reasoning as deltas
flagged thinking:true with no tags; the live stream wrapped them in <think>,
the resume path (after a refresh) appended them as reply text. Reproduced in a
browser with a fake reasoning model: without the fix the reasoning showed in
the reply after a refresh; with it, the Thinking section holds it.
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_resume_wraps_reasoning_deltas_like_the_live_stream():
    src = open(os.path.join(HERE, "static", "js", "chat.js"), encoding="utf-8").read()
    i = src.index("export async function resumeStream(sessionId) {")
    body = src[i:src.index("\n  }\n", src.index("const reader = res.body.getReader();", i))]
    assert "let _resThinkOpen = false;" in body
    assert "if (!_resThinkOpen) { _d = '<think>' + _d; _resThinkOpen = true; }" in body
    assert "_d = '</think>' + _d; _resThinkOpen = false;" in body
    assert "liveRoundText[liveRound] = (liveRoundText[liveRound] || '') + _d;" in body
    # a round that ends mid-thought is closed before the next one starts
    step = body[body.index("json.type === 'agent_step'"):]
    assert step.index("if (_resThinkOpen)") < step.index("liveRound = Number(json.round)")
