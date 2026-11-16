#!/usr/bin/env python3
"""Time the model side of a phone call's turns: what Odysseus sends for each spoken turn, and how long the
call model takes to its first answer token.

Builds each turn of a short call with the agent loop in this process (the call note on the system message, a
growing history), catches the request it would send instead of sending it, then sends those requests to the
model server one after the other and times them. Nothing touches a running Odysseus: a throwaway data dir,
an in-memory database, and the tool index is only read (retrieval), never written.

    python scripts/call_turn_bench.py --url http://192.168.100.101:8114/v1 --model qwen3.8-27b \\
        [--repo PATH] [--thinking on|off|auto] [--turns 4] [--json out.json]

--repo runs another checkout's agent loop (origin/dev, for the "before" numbers). --thinking forces the
reasoning pass on or off in the replay; auto sends what that checkout's agent loop asked for. A random call id
starts the system prompt, so every run starts cold, like a new call after the server's cache moved on.

For each turn: prompt tokens, the server's prefix-cache hits for it (vLLM /metrics, when it has them), time to
the first token of any kind, and to the first answer token (spoken text or a tool call, not reasoning).
"""

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import uuid

PROMPTS = [
    "Hey, can you hear me okay?",
    "What is a good quick dinner I can make tonight?",
    "Can you check the weather for tomorrow?",
    "Give me one quick tip for sleeping better.",
    "Thanks. What time is it now?",
    "Remind me what we talked about at the start of this call.",
]
REPLIES = [
    "Yes, loud and clear. What can I do for you?",
    "A veggie stir fry with rice is quick: about twenty minutes, and it uses what you have.",
    "Tomorrow looks mild, around 12 degrees with some clouds in the afternoon.",
    "Keep the same wake up time every day, weekends too.",
    "It is a little after seven in the evening.",
    "We started with a dinner idea, then the weather and a sleep tip.",
]


def _args():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://192.168.100.101:8114/v1")
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--thinking", choices=["on", "off", "auto"], default="auto")
    ap.add_argument("--turns", type=int, default=4)
    ap.add_argument("--json", default="")
    return ap.parse_args()


def _setup(repo: str):
    os.environ["ODYSSEUS_DATA_DIR"] = tempfile.mkdtemp(prefix="call-bench-")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("ODYSSEUS_OLLAMA_PROBE", "0")
    sys.path.insert(0, repo)
    os.chdir(repo)


class ReadOnlyIndex:
    """The real tool index for retrieval; its writes do nothing (the index belongs to the running server)."""

    def __init__(self, idx):
        self._idx = idx
        self.healthy = True

    def index_mcp_tools(self, *a, **k):
        pass

    def index_extra_tools(self, *a, **k):
        pass

    def get_tools_for_query(self, q, k=8):
        return self._idx.get_tools_for_query(q, k)


def capture(url: str, model: str, turns: int):
    """The requests the agent loop sends for each turn of one call: [(messages, tools, kw, prep)]."""
    import src.agent_loop as al
    import src.tool_index as ti
    from routes.chat_routes import VOICE_CALL_NOTE
    try:
        idx = ReadOnlyIndex(ti.ToolIndex())
    except Exception as e:
        print(f"(no tool index here, keyword selection only: {e})", file=sys.stderr)
        idx = None
    ti.get_tool_index = lambda: idx
    al.get_setting = lambda key, default=None: default

    system = {"role": "system", "content": f"Call {uuid.uuid4().hex[:8]}.\n\n{VOICE_CALL_NOTE}\n\nOn a phone call"}
    sid = "bench-" + uuid.uuid4().hex[:8]
    out, history = [], []
    for n in range(turns):
        got = {}

        async def fake_stream(candidates, messages, **kw):
            if "messages" not in got:
                got.update(messages=messages, tools=kw.get("tools") or [], kw=kw)
            yield 'data: {"delta": "ok"}\n\n'
            yield "data: [DONE]\n\n"
        al.stream_llm_with_fallback = fake_stream
        msgs = [dict(system)] + history + [{"role": "user", "content": PROMPTS[n % len(PROMPTS)]}]

        async def go():
            prep = {}
            async for chunk in al.stream_agent_loop(url, model, msgs, max_rounds=1, owner=None, session_id=sid):
                if '"agent_prep"' in chunk:
                    prep = json.loads(chunk[6:])["data"]
            return prep
        t = time.time()
        prep = asyncio.run(go())
        prep["total_s"] = round(time.time() - t, 3)
        kw = {k: v for k, v in got["kw"].items() if k in ("thinking", "reasoning_effort")}
        out.append((got["messages"], got["tools"], kw, prep))
        history += [{"role": "user", "content": PROMPTS[n % len(PROMPTS)]},
                    {"role": "assistant", "content": REPLIES[n % len(REPLIES)]}]
    return out


def _metrics(base: str) -> dict:
    import httpx
    try:
        text = httpx.get(base.rstrip("/").removesuffix("/v1") + "/metrics", timeout=5).text
    except Exception:
        return {}
    got = {}
    for name in ("prefix_cache_queries_total", "prefix_cache_hits_total"):
        m = re.search(r"^vllm:" + name + r"\{[^}]*\} ([0-9.e+]+)$", text, re.M)
        if m:
            got[name] = float(m.group(1))
    return got


def replay(url: str, model: str, reqs, thinking: str):
    import httpx
    rows = []
    for n, (messages, tools, kw, prep) in enumerate(reqs, 1):
        payload = {"model": model, "messages": messages, "stream": True, "temperature": 0.3, "max_tokens": 1500,
                   "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = tools
        off = (thinking == "off") or (thinking == "auto" and kw.get("thinking") is False)
        if off:
            # What src/llm_core.no_thinking_payload sends a self-hosted server (here too for an older checkout).
            payload["reasoning_effort"] = "none"
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        before = _metrics(url)
        t0 = time.time()
        first = answer = None
        usage = {}
        with httpx.stream("POST", url.rstrip("/") + "/chat/completions", json=payload, timeout=300) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                d = json.loads(line[6:])
                if d.get("usage"):
                    usage = d["usage"]
                for ch in d.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if first is None and (delta.get("content") or delta.get("reasoning_content")
                                          or delta.get("reasoning") or delta.get("tool_calls")):
                        first = time.time() - t0
                    if answer is None and (delta.get("content", "").strip() or delta.get("tool_calls")):
                        answer = time.time() - t0
                if answer is not None:
                    break       # the first spoken word (or tool call) is what the caller waits for
        after = _metrics(url)
        q = after.get("prefix_cache_queries_total", 0) - before.get("prefix_cache_queries_total", 0)
        h = after.get("prefix_cache_hits_total", 0) - before.get("prefix_cache_hits_total", 0)
        rows.append({
            "turn": n, "tools": len(tools), "tool_names": [t["function"]["name"] for t in tools],
            "system_chars": len(messages[0]["content"]) if messages and messages[0]["role"] == "system" else 0,
            "reasoning": "off" if off else "on",
            "prompt_tokens_queried": int(q), "prefix_cache_hit_tokens": int(h),
            "first_token_s": round(first, 3) if first is not None else None,
            "first_answer_s": round(answer, 3) if answer is not None else None,
            "setup_s": prep.get("tool_selection"), "prep": prep, "usage": usage,
        })
        print(f"turn {n}: {len(tools)} tools, prompt {int(q)} tok, cache hit {int(h)} tok, "
              f"reasoning {'off' if off else 'on'}, first token {rows[-1]['first_token_s']} s, "
              f"first answer {rows[-1]['first_answer_s']} s, Odysseus setup {prep}", flush=True)
    return rows


def main():
    a = _args()
    _setup(os.path.abspath(a.repo))
    reqs = capture(a.url, a.model, a.turns)
    names = [[t["function"]["name"] for t in tools] for _, tools, _, _ in reqs]
    systems = [m[0]["content"] for m, _, _, _ in reqs]
    print(f"tool lists identical across turns: {all(n == names[0] for n in names)}; "
          f"system prompt identical: {all(s == systems[0] for s in systems)}", flush=True)
    rows = replay(a.url, a.model, reqs, a.thinking)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"repo": a.repo, "thinking": a.thinking, "rows": rows}, f, indent=1)


if __name__ == "__main__":
    main()
