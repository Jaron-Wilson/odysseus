"""The assistant: a LOCAL model asks short questions about the print and picks the options.

The model never touches a mesh. It gets the analysis summary and the current options, asks what it needs
to know (keep sockets open or fill, size, cutting and gluing, what failed before, one color or AMS) and
answers with JSON: what to say, the options it chose, and whether to run. The options are checked
(pipeline.normalize_options) and every change is logged (jobs.log_decision); the geometry is all code.

Which model: the one set in the helper's settings, else the best chat model on a local endpoint
(this machine, the LAN or the tailnet), in this order: qwen3.8, other qwen3, gpt-oss, gemma, llama,
mistral, anything else. Never a cloud endpoint. totoro (the shared lab GPU server) is skipped on
Mondays before noon, when the lab meets in its room.
"""
import datetime
import json
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.print_helper import jobs
from src.print_helper.pipeline import DEFAULT_OPTIONS, normalize_options

logger = logging.getLogger(__name__)

MODEL_PREFERENCE = (r"qwen3\.8", r"qwen3", r"gpt-oss", r"gemma", r"llama", r"mistral", r"phi", r"")
BUSY = ({"match": "totoro", "weekday": 0, "before_hour": 12},)   # shared lab server: not Monday mornings
NON_CHAT = ("embed", "rerank", "whisper", "tts", "clip", "diffusion", "flux", "vision-only")

SYSTEM = """You help the user make a 3D model easier to print on their Bambu Lab P1S (with AMS, textured PEI plate, PLA).
They prefer NO supports: tall thin tree supports and support-filament interfaces gave them spaghetti and huge purge.

You do not change the model yourself. Deterministic tools do: orient each piece for the least overhang,
cut pieces with 2.2 mm filament-pin holes (glue and 1.75 mm filament pins), fill enclosed hollows, add 45 degree
roofs under ceilings so hollows stay open, add posts under outside step-outs, and slice every piece in Bambu
Studio on their PC to check it. You only choose their options.

The options (JSON):
- scale_percent: number, the whole model's scale (all parts the same, so they still fit together)
- hollows: "roof" (keep hollows open, 45 degree roofs under ceilings), "fill" (fill enclosed hollows), "leave"
- keep_holes: true keeps sockets, stud holes and pin holes open; false lets them fill
- allow_cut: true lets the tools cut parts (with pin holes) to avoid overhangs or to fit the 256 mm bed
- posts: true adds posts to the plate under outside ledges (changes the look)
- parts: names of the parts to work on; [] means all
- colors: "single" (one filament) or "ams"
- slice_check: true slices every piece on the PC
- notes: what failed before and how, in their words

Ask short, useful questions, one or two at a time, about what you do not know yet:
keep holes and sockets open, or fill them; the target size or scale (at most the largest that fits the bed);
whether cutting and gluing is OK; which parts failed before and how; one color or AMS.
Use the analysis to suggest an answer ("the legs have 68 cm2 of overhang under a ceiling; keep the hollow and roof it?").
Do not ask about something they already answered. When you know enough, sum up the plan in one or two sentences
and ask them to confirm. Set "run": true only after they confirm (or say go).

Reply with ONE JSON object and nothing else:
{"say": "what you tell them (plain text, short)", "options": {only the options to change}, "run": false}"""


# ── Picking the model ───────────────────────────────────────────────────────

def _is_local(url: str) -> bool:
    from src.pricing import is_local_host
    try:
        return is_local_host(urlparse(url or "").hostname)
    except Exception:
        return False


def is_busy(name: str, url: str, now: Optional[datetime.datetime] = None) -> bool:
    now = now or datetime.datetime.now()
    text = f"{name} {url}".lower()
    return any(b["match"] in text and now.weekday() == b["weekday"] and now.hour < b["before_hour"] for b in BUSY)


def _rank(model: str) -> int:
    low = model.lower()
    for i, pat in enumerate(MODEL_PREFERENCE):
        if re.search(pat, low):
            return i
    return len(MODEL_PREFERENCE)


def candidates(owner: Optional[str] = None, now: Optional[datetime.datetime] = None) -> Tuple[List[Dict], List[str]]:
    """Local (url, model, headers) choices, best first, and notes on what was skipped."""
    from core.database import ModelEndpoint, SessionLocal
    from src.endpoint_resolver import (_endpoint_enabled_models, build_chat_url, build_headers,
                                       resolve_endpoint_runtime)
    cfg = jobs.load_config()
    notes: List[str] = []
    out: List[Dict] = []
    db = SessionLocal()
    try:
        rows = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()  # noqa: E712
        for ep in rows:
            if (getattr(ep, "model_type", None) or "llm") != "llm":
                continue
            if cfg.get("endpoint_id") and ep.id != cfg["endpoint_id"]:
                continue
            if not _is_local(ep.base_url):
                if cfg.get("endpoint_id"):
                    notes.append(f"{ep.name} is not a local endpoint, so it is not used")
                continue
            if is_busy(ep.name or "", ep.base_url or "", now):
                notes.append(f"{ep.name} is skipped on Monday mornings (shared lab server)")
                continue
            try:
                base, key = resolve_endpoint_runtime(ep, owner=owner)
            except Exception as e:
                notes.append(f"{ep.name}: {e}")
                continue
            models = [m for m in _endpoint_enabled_models(ep) if not any(x in str(m).lower() for x in NON_CHAT)]
            if cfg.get("model"):
                models = [m for m in models if m == cfg["model"]] or ([cfg["model"]] if cfg.get("endpoint_id") else [])
            for m in models:
                out.append({"url": build_chat_url(base), "model": m, "headers": build_headers(key, base),
                            "endpoint": ep.name or ep.id, "endpoint_id": ep.id})
    finally:
        db.close()
    out.sort(key=lambda c: (_rank(c["model"]), c["endpoint"], c["model"]))
    return out, notes


def model_info(owner: Optional[str] = None) -> Dict:
    try:
        cands, notes = candidates(owner)
    except Exception as e:
        return {"model": None, "notes": [f"could not read the model endpoints: {e}"]}
    if not cands:
        return {"model": None, "notes": notes}
    c = cands[0]
    return {"model": c["model"], "endpoint": c["endpoint"], "fallbacks": len(cands) - 1, "notes": notes}


# ── The conversation ────────────────────────────────────────────────────────

def parse_reply(text: str) -> Dict:
    """The model's JSON reply; tolerant of think blocks, code fences and chatter around it."""
    from src.text_helpers import strip_think
    t = strip_think(text or "")
    t = re.sub(r"```(?:json)?", "", t)
    best = None
    for m in re.finditer(r"\{", t):
        depth = 0
        for j in range(m.start(), len(t)):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[m.start():j + 1])
                        if isinstance(obj, dict) and ("say" in obj or "options" in obj):
                            best = obj
                    except ValueError:
                        pass
                    break
        if best is not None:
            break
    if best is None:
        return {"say": t.strip()[:2000] or "(no answer)", "options": {}, "run": False}
    say = best.get("say")
    return {"say": (say if isinstance(say, str) else "")[:4000],
            "options": best.get("options") if isinstance(best.get("options"), dict) else {},
            "run": best.get("run") is True}


def fallback_questions(analysis: Optional[Dict]) -> str:
    """The same questions without a model, for the form."""
    a = analysis or {}
    top = f" (it fits the bed up to {a['max_scale_percent']}% without cutting)" if a.get("max_scale_percent") else ""
    return ("No local model is reachable, so here are the questions; answer them in the options below:\n"
            "1. Keep the sockets and holes open, or let them fill?\n"
            f"2. What size? Scale in percent{top}.\n"
            "3. Is it OK to cut parts and glue them with filament pins?\n"
            "4. Which parts failed before, and how? (Pick them under Parts, and say how in Notes.)\n"
            "5. One color, or AMS colors?")


def build_messages(job: Dict, options: Dict, history: List[Dict]) -> List[Dict]:
    a = job.get("analysis") or {}
    ctx = (f"Analysis of {job.get('file')}:\n{a.get('summary') or '(not analyzed yet)'}\n\n"
           f"Current options: {json.dumps(options)}")
    last = job.get("result")
    if last:
        ctx += "\n\nLast run: " + "; ".join((last.get("log") or [])[:20])
        sl = last.get("slice")
        if sl and not sl.get("ok"):
            ctx += f"\nSlice check skipped: {sl.get('skipped')}"
        for p in last.get("pieces") or []:
            s = p.get("slice") or {}
            if s:
                ctx += (f"\n- {p['name']}: sliced {s.get('time')}, {s.get('weight_g')} g, support lines "
                        f"{s.get('support_lines')}, needs support: {s.get('needs_support')}, warnings: {s.get('warnings')}")
    msgs = [{"role": "system", "content": SYSTEM + "\n\n" + ctx}]
    for h in history[-20:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"]})
    if len(msgs) == 1 or msgs[-1]["role"] != "user":
        msgs.append({"role": "user", "content": "(The user opened the chat. Start with your first question.)"})
    return msgs


async def chat(job_id: str, message: str, owner: Optional[str] = None, call=None) -> Dict:
    """One turn. Returns {say, options, run, model}. `call` replaces the LLM call in tests."""
    import time
    job = jobs.get_job(job_id)
    names = [p["name"] for p in ((job.get("analysis") or {}).get("parts") or [])]
    options = normalize_options(job.get("options") or DEFAULT_OPTIONS, names or None)
    history = list(job.get("chat") or [])
    message = (message or "").strip()[:4000]
    if message:
        history.append({"role": "user", "content": message, "at": time.time()})
    if call is None:
        cands, notes = candidates(owner)
    else:
        cands, notes = [{"model": "test", "endpoint": "test"}], []
    reply, used, errors = None, None, []
    for c in cands[:3]:
        try:
            if call is not None:
                text = await call(build_messages(job, options, history))
            else:
                from src.llm_core import llm_call_async
                text = await llm_call_async(c["url"], c["model"], build_messages(job, options, history),
                                            temperature=0.3, max_tokens=1200, headers=c["headers"],
                                            timeout=180, max_retries=1, owner=owner)
            reply, used = parse_reply(text), c
            break
        except Exception as e:
            errors.append(f"{c['endpoint']}/{c['model']}: {str(e)[:200]}")
            logger.warning("print helper chat: %s %s failed: %s", c.get("endpoint"), c.get("model"), e)
    if reply is None:
        why = "; ".join(errors or notes) or "no local model"
        reply = {"say": fallback_questions(job.get("analysis")) + (f"\n\n({why})" if errors else ""),
                 "options": {}, "run": False}
    changed = {}
    if reply["options"]:
        new = normalize_options({**options, **reply["options"]}, names or None)
        changed = {k: v for k, v in new.items() if new[k] != options.get(k)}
        if changed:
            jobs.write_json(jobs.job_dir(job_id) + "/options.json", new)
            jobs.log_decision(job_id, "assistant", "options", changed,
                              note=f"{used['endpoint']}/{used['model']}" if used else "")
        options = new
    history.append({"role": "assistant", "content": reply["say"], "at": time.time(),
                    "model": f"{used['endpoint']}/{used['model']}" if used else None, "changed": changed or None})
    jobs.save_chat(job_id, history)
    return {"say": reply["say"], "options": options, "changed": changed, "run": bool(reply["run"] and used),
            "model": f"{used['endpoint']}/{used['model']}" if used else None}
