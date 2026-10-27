"""Turn Odysseus's internal model stream (src/llm_core.py ``stream_llm``) into OpenAI chat-completions.

``stream_llm`` speaks to every provider Odysseus supports and yields small SSE events: ``data: {"delta": "text"}``
(with ``"thinking": true`` for reasoning), ``data: {"type": "tool_calls", "calls": [...]}``, ``data: {"type": "usage",
...}``, ``event: error`` and ``data: [DONE]``. The gateway uses it for models that do not take a plain OpenAI request
(Anthropic, ChatGPT sign-in, Copilot, native Ollama, add-ons) and this class rewrites those events as OpenAI chunks, or
as one completion when the caller did not ask for a stream. No request content is stored or logged here.
"""
import json
import time
import uuid
from typing import Dict, List, Optional


def _sse(obj) -> str:
    return "data: " + json.dumps(obj) + "\n\n"


def _events(chunk: str):
    """(event name, data string) pairs in one piece of the internal stream."""
    for block in chunk.split("\n\n"):
        event, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            yield event, "\n".join(data)


class Translator:
    def __init__(self, model: str, include_usage: bool = False):
        self.model = model
        self.include_usage = include_usage
        self.id = "chatcmpl-" + uuid.uuid4().hex[:24]
        self.created = int(time.time())
        self.started = False        # any output chunk has been produced
        self.done = False
        self.error: Optional[Dict] = None      # {"message", "status"} when the model's side failed
        self.text: List[str] = []
        self.reasoning: List[str] = []
        self.calls: List[Dict] = []
        self.usage: Dict = {}

    def _chunk(self, delta: Dict, finish: Optional[str] = None) -> str:
        return _sse({"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    def _start(self) -> List[str]:
        if self.started:
            return []
        self.started = True
        return [self._chunk({"role": "assistant", "content": ""})]

    def feed(self, chunk: str) -> List[str]:
        """OpenAI SSE strings for one piece of the internal stream (maybe none)."""
        out: List[str] = []
        for event, data in _events(chunk):
            if self.done:
                break
            if event == "error":
                try:
                    j = json.loads(data)
                except Exception:
                    j = {"error": data}
                msg = j.get("text") or j.get("error") or "the model returned an error"
                status = j.get("status") if isinstance(j.get("status"), int) else 502
                self.error = {"message": str(msg)[:500], "status": status if 400 <= status < 600 else 502}
                if self.started:
                    out.append(_sse({"error": {"message": self.error["message"], "type": "api_error"}}))
                    out += self.finish()
                continue
            if data.strip() == "[DONE]":
                out += self.finish()
                continue
            try:
                j = json.loads(data)
            except Exception:
                continue
            if not isinstance(j, dict):
                continue
            kind = j.get("type")
            if "delta" in j and kind is None:
                text = str(j.get("delta") or "")
                if not text:
                    continue
                out += self._start()
                if j.get("thinking"):
                    self.reasoning.append(text)
                    out.append(self._chunk({"reasoning_content": text}))
                else:
                    self.text.append(text)
                    out.append(self._chunk({"content": text}))
            elif kind == "tool_calls":
                for c in j.get("calls") or []:
                    if not isinstance(c, dict) or not c.get("name"):
                        continue
                    args = c.get("arguments")
                    args = args if isinstance(args, str) else json.dumps(args or {})
                    call = {"id": c.get("id") or f"call_{len(self.calls)}", "name": c["name"], "arguments": args}
                    out += self._start()
                    out.append(self._chunk({"tool_calls": [{"index": len(self.calls), "id": call["id"], "type": "function",
                                                            "function": {"name": call["name"], "arguments": call["arguments"]}}]}))
                    self.calls.append(call)
            elif kind == "usage" and isinstance(j.get("data"), dict):
                self.usage = j["data"]       # cumulative: the last one wins
        return out

    def _usage_obj(self) -> Dict:
        i = int(self.usage.get("input_tokens") or 0)
        o = int(self.usage.get("output_tokens") or 0)
        return {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o}

    def finish(self) -> List[str]:
        """The closing chunks. Safe to call more than once; also call it if the stream ends without [DONE]."""
        if self.done:
            return []
        self.done = True
        out = self._start()
        out.append(self._chunk({}, "tool_calls" if self.calls else "stop"))
        if self.include_usage and self.usage:
            out.append(_sse({"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                             "choices": [], "usage": self._usage_obj()}))
        out.append("data: [DONE]\n\n")
        return out

    def completion(self) -> Dict:
        """The whole answer as one chat.completion, for a caller that did not ask for a stream."""
        msg: Dict = {"role": "assistant", "content": "".join(self.text)}
        if self.reasoning:
            msg["reasoning_content"] = "".join(self.reasoning)
        if self.calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": c["arguments"]}} for c in self.calls]
            if not msg["content"]:
                msg["content"] = None
        body = {"id": self.id, "object": "chat.completion", "created": self.created, "model": self.model,
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if self.calls else "stop"}]}
        if self.usage:
            body["usage"] = self._usage_obj()
        return body
