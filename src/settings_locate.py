"""The Utility model's half of "take me to ..." in Settings.

static/js/settingsNav.js matches a request like "bring me to the ai voice
settings" against the Settings page itself first. When that match isn't sure,
it sends the request and the list of places it found (id and path, such as
"set-vcStt" and "AI Defaults > Voice call > Hears with") here, and the
Utility model picks one. The model only ever answers with a number from the
list, and the number is checked, so a made-up place can't come back.
"""

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 400
MAX_QUERY = 300

_SYSTEM = (
    "You help a user find one place in an app: a place in its Settings, or one of its "
    "pages. You get their request and a numbered list of places: Settings places are "
    "tab > card > control, and whole pages are \"Page: <name>\". Reply with only the number "
    "of the place that best fits the request, or 0 if none fits. No words, just the number."
)


def clean_candidates(raw) -> List[Dict[str, str]]:
    """Keep well-formed {id, path} pairs, deduplicated and capped."""
    out, seen = [], set()
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()[:160]
        path = re.sub(r"\s+", " ", str(c.get("path") or "")).strip()[:200]
        if not cid or not path or cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "path": path})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def parse_pick(text: str, count: int) -> Optional[int]:
    """The 1-based pick in the model's reply, or None for none / nonsense."""
    from src.text_helpers import strip_think
    t = strip_think(text or "", prose=True, prompt_echo=True).strip()
    m = re.search(r"\d+", t)
    if not m:
        return None
    n = int(m.group(0))
    return n if 1 <= n <= count else None


def _endpoint(owner: Optional[str]):
    from src.endpoint_resolver import resolve_endpoint
    return resolve_endpoint("utility", owner=owner or None)


async def pick(query: str, candidates: List[Dict[str, str]], owner: Optional[str] = None) -> Dict:
    """{"id": <a candidate id> | None, "model": bool}. `model` is False when no
    Utility (or Default Chat) model is set up, so the page can say so."""
    query = re.sub(r"\s+", " ", query or "").strip()[:MAX_QUERY]
    if not query or not candidates:
        return {"id": None, "model": True}
    url, model, headers = _endpoint(owner)
    if not url or not model:
        return {"id": None, "model": False}
    listing = "\n".join(f"{i}. {c['path']}" for i, c in enumerate(candidates, 1))
    from src.llm_core import llm_call_async
    try:
        raw = await llm_call_async(
            url=url, model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Request: {query}\n\nPlaces:\n{listing}\n\nNumber:"},
            ],
            temperature=0, max_tokens=400, headers=headers, timeout=25, max_retries=1,
        )
    except Exception as e:
        logger.info("settings locate: utility model failed: %s", e)
        return {"id": None, "model": True, "error": "The Utility model did not answer."}
    n = parse_pick(raw, len(candidates))
    return {"id": candidates[n - 1]["id"] if n else None, "model": True}
