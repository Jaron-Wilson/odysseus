"""When the agent may start a Deep Research job (trigger_research).

trigger_research is always offered (src/tool_index.py ALWAYS_AVAILABLE), so
nothing stopped a model from starting one for any message. On 2026-10-21
"hows my 3d print doing?" started Deep Research on the ChatGPT plan. A job
like that is slow and costs plan usage, so the agent loop now takes the tool
away unless the user asked for research:

  - explicit research wording ("research", "deep dive", "look into",
    "investigate", "write a report on"...) always allows it, also when the
    words are in the recent context of a continuation ("yes, do it");
  - otherwise a status question ("how's X doing", "is it done", "status of")
    or any short message (under MIN_WORDS words) never gets it;
  - a longer request without research wording keeps it.
"""
from __future__ import annotations

import re

MIN_WORDS = 12

_RESEARCH_RE = re.compile(
    r"\b(?:research(?:es|ed|ing)?|deep[- ]?dive|deep research|investigate|investigation|look into|"
    r"literature review|in[- ]depth (?:report|analysis|look|overview)|comprehensive (?:report|overview|analysis)|"
    r"(?:write|make|put together|give me) (?:me )?(?:a |an )?(?:full |detailed )?report|report on|"
    r"find out everything|survey of)\b",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    r"\b(?:how'?s|how is|how are|hows|how did|how'?re|status|progress|doing|going|done yet|finished yet|"
    r"is (?:it|my|the) .{0,30}(?:done|finished|ready|working|running|printing)|what'?s (?:up|happening|the status)|"
    r"any (?:update|news)|still (?:running|printing|going))\b",
    re.IGNORECASE,
)


def asks_for_research(text: str) -> bool:
    return bool(_RESEARCH_RE.search(text or ""))


def allowed(last_user: str, context: str = "") -> bool:
    """May trigger_research be offered for this message? `context` is the
    recent conversation used for retrieval on an explicit continuation."""
    text = (last_user or "").strip()
    if asks_for_research(text) or (context and asks_for_research(context)):
        return True
    if _STATUS_RE.search(text):
        return False
    return len(re.findall(r"\w+", text)) >= MIN_WORDS
