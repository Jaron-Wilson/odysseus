"""trigger_research is not offered for short status questions (src/research_gate.py)."""
import pytest

from src import research_gate as rg


@pytest.mark.parametrize("text", [
    "hows my 3d print doing?",
    "how's the render going",
    "is my print done yet?",
    "status of the backup?",
    "any update on the download",
    "what time is it",
    "check the printer",
])
def test_short_or_status_messages_cannot_start_research(text):
    assert not rg.allowed(text)


@pytest.mark.parametrize("text", [
    "research the best PETG settings for a P1S",
    "do a deep dive on solid state batteries",
    "look into why my prints keep warping",
    "investigate local LLM tool calling",
    "write me a report on home battery storage options",
])
def test_explicit_research_wording_allows_it(text):
    assert rg.allowed(text)


def test_a_long_request_keeps_it_but_a_long_status_question_does_not():
    long_ask = ("compare the main options for storing solar energy at home with costs lifetimes safety "
                "and what the incentives are in Ohio this year")
    assert rg.allowed(long_ask)
    assert not rg.allowed("how is the print doing now that it has been running for a few hours since I left the house")


def test_continuation_inherits_research_from_context():
    assert rg.allowed("yes do it", context="Want me to research PETG settings for you?")
    assert not rg.allowed("yes do it", context="Want me to check the printer?")
