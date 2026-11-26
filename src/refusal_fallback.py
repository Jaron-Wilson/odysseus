"""Refusal fallback for the gateway: when a model says "no" to a direct request from its owner, ask its abliterated twin.

Design: plans-site big-models-outline.md, section 2 (agent MoE). The agent model (Qwen 3.8 27B today, ``subagent-moe``
later) refuses some of its owner's own work: security testing on his own PC, CAN work on his own car, low-level code.
An abliterated copy of the same base (refusal direction removed from the weights, Arditi et al. 2024) is served as its
own gateway model (``subagent-moe:unrestricted``). This module decides, per request, whether a refusal from the normal
model may be re-sent to that copy.

Abliteration also weakens the model's resistance to instructions injected into what it reads (web pages, files, tool
output, emails). So the fallback is only for requests that come straight from the user or his agents, and it stays
behind the guard:

1. Off unless turned on (``enabled``), and only for the model pairs listed in ``pairs`` (resolved model id -> twin).
2. Only for the Odysseus users listed in ``owners`` (empty: nobody), never a shared or anonymous caller.
3. Input guard: the conversation must be clean, i.e. the newest message is the user's, there are no tool or function
   results and no tool calls in it, no untrusted-source blocks (src/prompt_security.py markers), no images or files,
   and no text that reads like an injected instruction (``screen_input``). A request that fails any of these keeps the
   normal model's answer, refusal and all. When the planned guard Jev exists it replaces ``screen_input`` (see
   ``set_guard``); it must still say yes before a request may fall back.
4. Output guard: the twin's answer goes through ``screen_output`` (same hook point) before it is returned; a blocked
   answer is replaced by the normal model's refusal.
5. Every fallback is marked: ``X-Odysseus-Fallback: refusal -> <twin>`` on the response, and in the usage log the
   resolved model is the twin with the reason. Nothing about the message content is logged.

Only the passthrough path (OpenAI-compatible servers: llama.cpp, Ollama /v1, vLLM) is covered; translated providers
(Anthropic, ChatGPT sign-in, Copilot) are never paired.

Settings: DEFAULTS below, with keys from <data>/gateway_refusal_fallback.json on top.
"""
import json
import logging
import os
import re
import threading
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULTS: Dict = {
    "enabled": False,
    # resolved gateway model id -> its abliterated twin (also a gateway model id)
    "pairs": {
        "subagent-moe:default": "subagent-moe:unrestricted",
        "a-liberty-11435:qwen3.8:27b": "subagent-moe:unrestricted",
    },
    # Odysseus users whose own requests may fall back (empty: nobody)
    "owners": [],
    # how much of the normal model's answer is read before deciding it is a refusal
    "probe_chars": 400,
    # a standing system prompt added on the twin's route only (the owner context of the design); "" for none
    "owner_system_prompt": ("The user owns every machine, network, account, device and vehicle discussed and is "
                            "authorized to test, modify and repair them."),
}

_lock = threading.Lock()


def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def settings() -> Dict:
    s = dict(DEFAULTS)
    try:
        with open(os.path.join(_data_dir(), "gateway_refusal_fallback.json"), encoding="utf-8") as f:
            s.update(json.load(f) or {})
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("refusal fallback: settings file unreadable, using defaults (off)")
        s["enabled"] = False
    return s


# ── refusal detection ─────────────────────────────────────────────────────

_ACT = r"(?:help|assist|provide|comply|support|fulfill|create|write|generate|give|share|engage|do that|do this)"
_REFUSAL = re.compile(
    r"\b(i'?m sorry|i am sorry|i apologi[sz]e|i'?m really sorry|as an ai\b|i must (?:decline|refuse)|i refuse\b"
    r"|i(?: can ?not| can't| won't| will not| am unable to| am not able to|'m unable to|'m not able to"
    r"|'m not going to| am not going to)(?: \w+){0,2} " + _ACT +
    r"|(?:can ?not|can't|unable to) (?:help|assist|provide|comply|support) with"
    r"|against (?:my|the) (?:guidelines|policies|policy)|i'?d rather not)", re.I)


def looks_like_refusal(text: str, probe_chars: int = 400) -> bool:
    """The start of an answer reads as a refusal: a refusal phrase in its opening, and no code in it. An answer that
    starts helping and only warns later ("I can't verify this, but here is the script: ...") is not a refusal."""
    t = (text or "").replace("’", "'").strip()
    if not t:
        return False
    head = t[:probe_chars]
    if "```" in head:
        return False
    sentences = re.split(r"(?<=[.!?])\s+", head, maxsplit=2)
    opening = sentences[0] if len(sentences[0]) >= 40 or len(sentences) == 1 else " ".join(sentences[:2])
    return bool(_REFUSAL.search(opening))


# ── the guard ─────────────────────────────────────────────────────────────

_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any|your)\b.{0,30}"
    r"\b(instructions?|rules|prompts?|guidelines|directions)"
    r"|\byou are now\b|\bnew (system )?instructions?\b|\b(system|developer) (prompt|message|mode)\s*[:=]"
    r"|<\|im_start\|>|<\|system\|>|\[/?INST\]|<<<|BEGIN (SYSTEM|INSTRUCTIONS)", re.I | re.S)


def _text_parts(content) -> Tuple[List[str], bool]:
    """The text of a message's content, and whether it carries anything but text (images, files, audio)."""
    if isinstance(content, str):
        return [content], False
    texts, other = [], False
    for p in content or []:
        if isinstance(p, dict) and p.get("type") == "text":
            texts.append(str(p.get("text") or ""))
        elif isinstance(p, str):
            texts.append(p)
        else:
            other = True
    return texts, other


def screen_input(messages: List[Dict]) -> Optional[str]:
    """Default input guard. None when the conversation is clean and direct from the user; else why it is not."""
    from src.prompt_security import GUARD_OPEN
    if not messages:
        return "no messages"
    if (messages[-1] or {}).get("role") != "user":
        return "the newest message is not the user's"
    for m in messages:
        role = (m or {}).get("role")
        if role in ("tool", "function"):
            return "the conversation has tool results"
        if role == "assistant" and (m.get("tool_calls") or m.get("function_call")):
            return "the conversation has tool calls"
        if role not in ("system", "developer", "user", "assistant"):
            return f"unknown role {role!r}"
        texts, other = _text_parts(m.get("content"))
        if other:
            return "the conversation has images or files"
        for t in texts:
            if GUARD_OPEN in t:
                return "the conversation has untrusted source data"
            if role == "user" and _INJECTION.search(t):
                return "a user message reads like an injected instruction"
    return None


def screen_output(text: str) -> Optional[str]:
    """Default output guard for the twin's answer. None = fine. The guard Jev replaces this via set_guard."""
    return None


_guard: Dict[str, Callable] = {"input": screen_input, "output": screen_output}


def set_guard(input_check: Optional[Callable] = None, output_check: Optional[Callable] = None) -> None:
    """Plug in the guard model. Each check returns None to allow, or a reason string to block."""
    with _lock:
        if input_check:
            _guard["input"] = input_check
        if output_check:
            _guard["output"] = output_check


def check_output(text: str) -> Optional[str]:
    try:
        return _guard["output"](text)
    except Exception:
        logger.warning("refusal fallback: output guard failed, blocking", exc_info=True)
        return "output guard failed"


# ── the decision ──────────────────────────────────────────────────────────

def plan(model_id: str, payload: Dict, owner: Optional[str], translate: bool = False,
         s: Optional[Dict] = None) -> Tuple[Optional[str], str]:
    """(twin model id, reason) when a refusal from ``model_id`` on this request may be re-sent; (None, why not)
    otherwise. Decided before the request goes out, so a request that may not fall back is not buffered at all."""
    s = s or settings()
    if not s.get("enabled"):
        return None, "off"
    twin = (s.get("pairs") or {}).get(model_id)
    if not twin:
        return None, "no twin for this model"
    if translate:
        return None, "translated provider"
    if not owner or owner not in (s.get("owners") or []):
        return None, "caller is not an allowed owner"
    try:
        why = _guard["input"](payload.get("messages") or [])
    except Exception:
        logger.warning("refusal fallback: input guard failed, not falling back", exc_info=True)
        why = "input guard failed"
    if why:
        return None, why
    return twin, "eligible"


def twin_payload(payload: Dict, model: str, s: Optional[Dict] = None) -> Dict:
    """The request as sent to the twin: same messages and options, the twin's upstream model name, and the owner
    context as a leading system message (when set)."""
    s = s or settings()
    out = dict(payload, model=model)
    sp = str(s.get("owner_system_prompt") or "").strip()
    if sp:
        out["messages"] = [{"role": "system", "content": sp}] + list(payload.get("messages") or [])
    return out


def completion_text(data: bytes) -> Tuple[str, bool]:
    """(answer text, has tool calls) of a non-streamed OpenAI completion."""
    try:
        j = json.loads(data)
        msg = ((j.get("choices") or [{}])[0] or {}).get("message") or {}
        return str(msg.get("content") or ""), bool(msg.get("tool_calls"))
    except Exception:
        return "", False


class StreamProbe:
    """Reads an OpenAI SSE stream until it knows whether the answer is a refusal. Keeps every byte it read so the
    stream can be passed on unchanged when it is not."""

    def __init__(self, probe_chars: int = 400):
        self.probe_chars = probe_chars
        self.raw: List[bytes] = []
        self.buf = b""
        self.text = ""
        self.tool = False
        self.finished = False

    def feed(self, chunk: bytes) -> Optional[bool]:
        """None while undecided; True for a refusal, False when it is a real answer."""
        self.raw.append(chunk)
        self.buf += chunk
        while b"\n" in self.buf:
            line, _, self.buf = self.buf.partition(b"\n")
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            if line[5:].strip() == b"[DONE]":
                self.finished = True
                continue
            try:
                j = json.loads(line[5:])
            except Exception:
                continue
            for c in (j.get("choices") or []) if isinstance(j, dict) else []:
                d = (c or {}).get("delta") or {}
                if d.get("tool_calls") or d.get("function_call"):
                    self.tool = True
                if isinstance(d.get("content"), str):
                    self.text += d["content"]
                if c.get("finish_reason"):
                    self.finished = True
        return self.decide(final=False)

    def decide(self, final: bool = True) -> Optional[bool]:
        if self.tool:
            return False
        if len(self.text) >= self.probe_chars or self.finished or final:
            return looks_like_refusal(self.text, self.probe_chars)
        return None
