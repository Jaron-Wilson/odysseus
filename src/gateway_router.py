"""The gateway's ``auto`` model: pick a model for each conversation, and read images for models that cannot.

Asked for on 2026-10-29: opencode (or any client) asks the Odysseus gateway for ``auto`` and Odysseus decides where
the request goes, so a quick question does not wait in line for the big model, and an image sent to a text-only model
is still understood. In order:

1. Rules that always hold, checked on every request:
   - a conversation longer than the chosen model can read goes to the long-context model;
   - an endpoint that is offline, or busy with as many gateway requests as it can take at once (arrietty runs one
     at a time), is skipped for the next model on the fallback list.
2. Chat, easy or hard, decided on a new message from the user (not on tool results), by comparing an embedding of
   the message with labeled example prompts (``embeddinggemma`` on the desktop). First easy or hard: when the two are
   nearly tied, a small model breaks the tie. Then, for an easy one, chat or easy: a plain question that needs no
   tools or files ("what does git stash pop do?") goes to the tiny chat model with the tool list removed, but only
   on a conversation's first step, with no images and no file named, and only when it is clearly a question; a near
   tie stays easy. The answer is kept for the conversation, so one session keeps one model; it can only move up
   (chat, easy, hard), never back.
3. Images: when the model chosen cannot read images (Ollama reports it), each image is replaced with a description
   from the vision model, written for the user's question. Descriptions are cached by image, since clients such as
   opencode send the whole conversation again on every step.

Settings: DEFAULTS below, with any keys in <data>/gateway_router.json on top. Every decision is logged to
<data>/logs/gateway_router.jsonl with the route and the reason, never the content of a message.
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import socket
import threading
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

AUTO_IDS = ("auto", "odysseus:auto")

DEFAULTS: Dict = {
    "enabled": True,
    "chat": "desktop-jaron:gemma3:1b",          # plain questions; it has no tools, so they are removed
    "fast": "desktop-jaron:qwen3:8b",
    "strong": "a-liberty-11435:qwen3.8:27b",
    "long": "a-liberty-11435:qwen3.8:27b",
    # tried in order when the model picked is offline or busy
    "fallback": ["desktop-jaron:qwen3:8b"],
    "describer": "desktop-jaron:qwen2.5vl:7b",
    "embed_via": "desktop-jaron:qwen3:8b",       # a model on the endpoint that also serves the embedding model
    "embed_model": "embeddinggemma",
    "embed_prefix": "task: classification | query: ",     # the task prompt embeddinggemma was trained with
    # measured 2026-10-29 on 12 prompts: embeddinggemma 12/12 with a gap of 0.158 between the closest easy and hard,
    # nomic-embed-text 12/12 with 0.056; for near ties qwen3:8b got 11/12 and gemma3:1b 10/12
    "tiebreak": "desktop-jaron:qwen3:8b",
    "tie_margin": 0.02,
    "chat_margin": 0.03,     # chat must beat easy by this much; measured: questions +0.07 to +0.20, actions below +0.02
    # how many gateway requests an endpoint takes at once (by endpoint slug); not listed = no limit
    "concurrency": {"a-liberty-11435": 1},
    # trained context, in tokens, by model name (the part after the endpoint slug)
    "context": {"qwen3.8:27b": 262144, "qwen3.8-27b": 262144, "gemma4:26b": 262144, "gpt-oss:20b": 131072,
                "qwen3:8b": 40960, "gemma3:1b": 32768, "qwen2.5vl:7b": 128000, "llama3.2-vision": 131072, "deepseek-r1:8b": 131072,
                "llama3.1": 131072},
    "examples": {
        "chat": [
            "what does git rebase do?",
            "explain what a mutex is",
            "what's the difference between TCP and UDP?",
            "how do I exit vim?",
            "what's the command to list docker containers?",
            "what is a closure in JavaScript?",
            "what does HTTP 502 mean?",
            "how do list comprehensions work in python?",
            "what's a good name for a function that parses dates?",
            "is python pass by reference?",
            "what does chmod 755 mean?",
            "when should I use a set instead of a list?",
        ],
        "easy": [
            "what does this function return?",
            "rename this variable to something clearer",
            "fix the typo in the README",
            "add a comment explaining this line",
            "convert this list to a set",
            "what time zone is the server in?",
            "summarize this file in two sentences",
            "is this json valid?",
            "show me the git log for the last 5 commits",
            "what port does this service listen on?",
            "change the default timeout in config.py to 30",
            "run the tests and tell me what fails",
        ],
        "hard": [
            "find out why the server keeps crashing and fix it",
            "refactor the payment module to support subscriptions, with tests",
            "build a new settings page with a form, an API route and tests",
            "debug this race condition between the poller and the web handler",
            "design a caching layer for the API and implement it",
            "migrate the database schema and write the migration with a rollback",
            "the tests fail intermittently on CI, investigate and fix the root cause",
            "port this Python service to TypeScript",
            "review this pull request for correctness and security problems",
            "implement the lab assignment from the instructor's spec, matching the exe output exactly",
            "write a parser for this file format and handle every edge case",
            "plan and build a multi-step feature across the backend and frontend",
        ],
    },
}

_lock = threading.Lock()
_sticky: Dict[str, Dict] = {}            # conversation key -> {"route", "seen", "at"}
_inflight: Dict[str, int] = {}           # endpoint slug -> gateway requests in progress
_vision: Dict[str, bool] = {}            # model id -> reads images
_online: Dict[str, Tuple[float, bool]] = {}
_example_vecs: Dict[str, List[List[float]]] = {}
STICKY_MAX = 2000
STICKY_TTL_S = 12 * 3600
ONLINE_TTL_S = 20
DESCRIPTION_CACHE_MAX = 400


# ── settings, log, caches ─────────────────────────────────────────────────

def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def _config_path() -> str:
    return os.path.join(_data_dir(), "gateway_router.json")


def config() -> Dict:
    path = _config_path()
    try:
        with open(path) as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        saved = {}
    except Exception as e:
        logger.warning("gateway router: %s unreadable (%s); using the defaults", path, e)
        saved = {}
    return {**DEFAULTS, **saved}


def enabled() -> bool:
    return bool(config().get("enabled"))


def _log(entry: Dict) -> None:
    try:
        d = os.path.join(_data_dir(), "logs")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "gateway_router.jsonl")
        if os.path.exists(p) and os.path.getsize(p) > 2_000_000:
            os.replace(p, p + ".1")
        with open(p, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    logger.info("gateway auto -> %s (%s)", entry.get("model"), entry.get("reason"))


def _desc_path() -> str:
    return os.path.join(_data_dir(), "gateway_router_images.json")


def _desc_cache() -> Dict[str, str]:
    try:
        with open(_desc_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _desc_save(cache: Dict[str, str]) -> None:
    if len(cache) > DESCRIPTION_CACHE_MAX:
        cache = dict(list(cache.items())[-DESCRIPTION_CACHE_MAX:])
    try:
        with open(_desc_path(), "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


# ── in-flight requests per endpoint (all gateway traffic, not only auto) ──

def slug_of(model_id: str) -> str:
    return (model_id or "").split(":", 1)[0]


def begin(model_id: str) -> None:
    with _lock:
        s = slug_of(model_id)
        _inflight[s] = _inflight.get(s, 0) + 1


def end(model_id: str) -> None:
    with _lock:
        s = slug_of(model_id)
        _inflight[s] = max(0, _inflight.get(s, 0) - 1)


def _busy(cfg: Dict, model_id: str) -> bool:
    limit = (cfg.get("concurrency") or {}).get(slug_of(model_id))
    return bool(limit) and _inflight.get(slug_of(model_id), 0) >= int(limit)


def _reachable(url: str) -> bool:
    """Can a TCP connection be made to the model's server (cached for a few seconds)?"""
    now = time.time()
    hit = _online.get(url)
    if hit and now - hit[0] < ONLINE_TTL_S:
        return hit[1]
    u = urlparse(url)
    ok = True
    if u.hostname:
        try:
            with socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=2):
                pass
        except OSError:
            ok = False
    _online[url] = (now, ok)
    return ok


# ── reading the request ───────────────────────────────────────────────────

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _images_in(messages: List[Dict]) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            n += sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
    return n


def estimate_tokens(payload: Dict) -> int:
    """Rough: about 3.5 characters a token, over the messages and the tool definitions."""
    chars = len(json.dumps(payload.get("messages") or [])) + len(json.dumps(payload.get("tools") or []))
    return int(chars / 3.5)


def conversation_key(messages: List[Dict]) -> str:
    """The same conversation, step after step: its first system prompt and first user message."""
    sys_ = next((_text_of(m.get("content")) for m in messages if m.get("role") == "system"), "")
    user = next((_text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
    return hashlib.sha256((sys_[:2000] + "\0" + user[:4000]).encode()).hexdigest()


def _last_user(messages: List[Dict]) -> Optional[str]:
    """The user's newest message, if the request ends with it (not with a tool result)."""
    if messages and messages[-1].get("role") == "user":
        return _text_of(messages[-1].get("content"))
    return None


RANK = ["chat", "easy", "hard"]       # a conversation's route only moves right


def _chat_ok(messages: List[Dict]) -> bool:
    """The chat model has no tools and no eyes: only for conversations that have used neither."""
    return not _images_in(messages) and not any(
        m.get("role") in ("tool", "function") or m.get("tool_calls") or m.get("function_call") for m in messages)


def context_of(cfg: Dict, model_id: str) -> Optional[int]:
    name = model_id.split(":", 1)[1] if ":" in model_id else model_id
    table = cfg.get("context") or {}
    return next((v for k, v in table.items() if name == k or name.startswith(k + ":")), None)


# ── the outside world: injectable for tests ───────────────────────────────

Complete = Callable[[str, List[Dict], Optional[str], int], Awaitable[str]]


async def _embed(cfg: Dict, owner: Optional[str], texts: List[str]) -> List[List[float]]:
    """Embeddings from the endpoint that serves cfg["embed_via"], Ollama native or OpenAI-style."""
    import httpx
    from src import openai_gateway as gw
    url, headers, _model, _tr = await asyncio.to_thread(gw.resolve, cfg["embed_via"], owner)
    texts = [str(cfg.get("embed_prefix") or "") + t for t in texts]
    async with httpx.AsyncClient(timeout=60) as c:
        if url.endswith("/chat/completions"):
            r = await c.post(url[: -len("/chat/completions")] + "/embeddings", headers=headers,
                             json={"model": cfg["embed_model"], "input": texts})
            r.raise_for_status()
            return [d["embedding"] for d in r.json()["data"]]
        base = url.split("/api/")[0] if "/api/" in url else url.rstrip("/")
        r = await c.post(base + "/api/embed", headers=headers, json={"model": cfg["embed_model"], "input": texts})
        r.raise_for_status()
        return r.json()["embeddings"]


async def _supports_images(model_id: str, owner: Optional[str]) -> bool:
    """Ollama says whether a model reads images (/api/show "capabilities"); anything else is assumed to."""
    if model_id in _vision:
        return _vision[model_id]
    import httpx
    from src import openai_gateway as gw
    ok = True
    try:
        url, headers, model, _tr = await asyncio.to_thread(gw.resolve, model_id, owner)
        u = urlparse(url)
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{u.scheme}://{u.netloc}/api/show", headers=headers, json={"model": model})
        if r.status_code == 200 and isinstance(r.json().get("capabilities"), list):
            ok = "vision" in r.json()["capabilities"]
    except Exception:
        pass
    _vision[model_id] = ok
    return ok


async def _url_of(model_id: str, owner: Optional[str]) -> str:
    from src import openai_gateway as gw
    return (await asyncio.to_thread(gw.resolve, model_id, owner))[0]


# ── easy or hard ──────────────────────────────────────────────────────────

def _cos(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _top3(q: List[float], vecs: List[List[float]]) -> float:
    """The mean of the three closest examples."""
    best = sorted((_cos(q, v) for v in vecs), reverse=True)[:3]
    return sum(best) / len(best) if best else -1.0


# a file name or a path in the message means it is about the workspace, not a plain question
_NAMES_A_FILE = re.compile(r"(?:^|[\s`'\"(])(?:[\w.-]*/[\w./-]+|[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?=$|[\s`'\")?,:;!])")


async def classify(cfg: Dict, owner: Optional[str], text: str, complete: Complete,
                   embed=None) -> Tuple[str, str, Optional[float]]:
    """("chat" | "easy" | "hard", how it was decided, the hard-minus-easy margin)."""
    embed = embed or (lambda texts: _embed(cfg, owner, texts))
    ex = cfg.get("examples") or DEFAULTS["examples"]
    chat_ex = list(ex.get("chat") or [])
    try:
        key = json.dumps(ex, sort_keys=True) + cfg.get("embed_model", "") + str(cfg.get("embed_prefix"))
        if key not in _example_vecs:
            vecs = await embed(chat_ex + ex["easy"] + ex["hard"])
            _example_vecs.clear()
            _example_vecs[key] = vecs
        vecs = _example_vecs[key]
        q = (await embed([text[:2000]]))[0]
    except Exception as e:
        return "hard", f"embeddings unavailable ({type(e).__name__}), so the strong model", None
    nc, ne = len(chat_ex), len(ex["easy"])
    chat_v, easy_v, hard_v = vecs[:nc], vecs[nc:nc + ne], vecs[nc + ne:]
    margin = _top3(q, hard_v) - _top3(q, chat_v + easy_v)
    if abs(margin) >= float(cfg.get("tie_margin", 0.01)):
        if margin > 0:
            return "hard", "embeddings", round(margin, 4)
        how = "embeddings"
        if chat_ex and cfg.get("chat"):
            chat_by = _top3(q, chat_v) - _top3(q, easy_v)
            if chat_by >= float(cfg.get("chat_margin", 0.03)) and not _NAMES_A_FILE.search(text):
                return "chat", f"embeddings, a plain question by {chat_by:.3f}", round(margin, 4)
            how = f"embeddings, not a plain question ({chat_by:+.3f})"
        return "easy", how, round(margin, 4)
    try:
        said = await complete(cfg["tiebreak"], [{"role": "user", "content": (
            "/no_think Is this request EASY (a quick answer, a small edit, a lookup) or HARD (multi-step coding, "
            "debugging, design or long reasoning)? Reply with one word: EASY or HARD.\n\nRequest:\n" + text[:2000])}],
            owner, 8)
    except Exception as e:
        return "hard", f"near tie and the tiebreaker failed ({type(e).__name__})", round(margin, 4)
    said = re.sub(r"<think>.*?</think>", "", said or "", flags=re.S).strip().lower()
    return ("easy" if said.startswith("easy") else "hard"), "near tie, tiebreaker", round(margin, 4)


# ── images for a model that cannot read them ──────────────────────────────

async def describe_images(cfg: Dict, owner: Optional[str], messages: List[Dict], complete: Complete) -> Tuple[List[Dict], int, int]:
    """(messages with each image replaced by a text description, images, newly described)."""
    cache = _desc_cache()
    out, n, fresh = [], 0, 0
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list) or not any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            out.append(m)
            continue
        question = _text_of(c)[:1500]
        parts = []
        for p in c:
            if not (isinstance(p, dict) and p.get("type") == "image_url"):
                parts.append(p)
                continue
            n += 1
            url = str((p.get("image_url") or {}).get("url") or "")
            key = hashlib.sha256(url.encode()).hexdigest()[:32] + ":" + hashlib.sha256(question.encode()).hexdigest()[:8]
            text = cache.get(key)
            if text is None:
                try:
                    text = await complete(cfg["describer"], [{"role": "user", "content": [
                        {"type": "text", "text": (
                            "Describe this image for someone who cannot see it, so they can answer the question below. "
                            "Copy any text, code, numbers and error messages in it exactly. Be complete but concise."
                            + (f"\n\nThe question: {question}" if question else ""))},
                        {"type": "image_url", "image_url": {"url": url}}]}], owner, 900)
                    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip() or "(no description)"
                    cache[key] = text
                    fresh += 1
                except Exception as e:
                    text = f"(the image could not be described: {type(e).__name__})"
            parts.append({"type": "text", "text": f"[image {n}, described by {cfg['describer']}: {text}]"})
        out.append({**m, "content": parts})
    if fresh:
        _desc_save(cache)
    return out, n, fresh


# ── the decision ──────────────────────────────────────────────────────────

async def route(payload: Dict, owner: Optional[str], complete: Complete, embed=None,
                reachable=None, supports_images=None) -> Tuple[Dict, Dict]:
    """(the payload to send, with "model" set to a real model, decision). Never raises for a routing problem: the
    strong model is the answer when in doubt."""
    cfg = config()
    reachable = reachable or (lambda mid: _url_of(mid, owner))
    supports_images = supports_images or (lambda mid: _supports_images(mid, owner))
    messages = payload.get("messages") or []
    key = conversation_key(messages)
    tokens = estimate_tokens(payload)
    now = time.time()
    with _lock:
        for k in [k for k, v in _sticky.items() if now - v["at"] > STICKY_TTL_S]:
            _sticky.pop(k, None)
        st = _sticky.get(key)

    last = _last_user(messages)
    label, how, margin = (st or {}).get("route"), "kept for this conversation", None
    new_message = last is not None and (not st or hashlib.sha256(last.encode()).hexdigest() != st.get("seen"))
    if new_message and label != "hard":
        got, how, margin = await classify(cfg, owner, last, complete, embed=embed)
        if got == "chat" and not _chat_ok(messages):
            got, how = "easy", how + ", but the conversation has used tools or has images"
        label = max(label or got, got, key=RANK.index)
    label = label or "hard"
    with _lock:
        if len(_sticky) >= STICKY_MAX:
            _sticky.pop(next(iter(_sticky)))
        _sticky[key] = {"route": label, "at": now,
                        "seen": hashlib.sha256((last or "").encode()).hexdigest() if new_message else (st or {}).get("seen")}

    model = {"hard": cfg["strong"], "chat": cfg.get("chat")}.get(label) or cfg["fast"]
    reason = [f"{label} ({how})"]
    if model == cfg.get("chat"):
        tokens = estimate_tokens({"messages": messages})      # its tools are removed, so they do not count
    ctx = context_of(cfg, model)
    if ctx and tokens > ctx * 0.9:
        if model == cfg.get("chat"):
            model, tokens = cfg["fast"], estimate_tokens(payload)
            reason.append(f"too long for {cfg['chat']}, so the fast model")
            ctx = context_of(cfg, model)
        if ctx and tokens > ctx * 0.9:
            model = cfg["long"]
            reason.append(f"about {tokens} tokens is too long for {label}'s model, so the long-context one")
    # offline or busy: the next one on the fallback list that is neither, and long enough
    for cand in [model] + [m for m in cfg.get("fallback") or [] if m != model]:
        c_ctx = context_of(cfg, cand)
        if c_ctx and tokens > c_ctx * 0.9 and cand != model:
            continue
        if _busy(cfg, cand):
            reason.append(f"{slug_of(cand)} is busy")
            continue
        try:
            if not await asyncio.to_thread(_reachable, await reachable(cand)):
                reason.append(f"{slug_of(cand)} is offline")
                continue
        except Exception:
            reason.append(f"{cand} is not available")
            continue
        model = cand
        break
    out = dict(payload, model=model)
    if model == cfg.get("chat"):
        for k in ("tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"):
            out.pop(k, None)
    images = _images_in(messages)
    described = 0
    if images and not await supports_images(model):
        out["messages"], images, described = await describe_images(cfg, owner, messages, complete)
        reason.append(f"{images} image(s) described by {cfg['describer']}")
    decision = {"at": round(now, 3), "conversation": key[:12], "model": model, "route": label, "margin": margin,
                "tokens": tokens, "images": images, "described": described, "reason": "; ".join(reason)}
    _log(decision)
    return out, decision
