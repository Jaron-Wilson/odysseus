"""Picking a model on a chat that was deleted elsewhere starts a new chat.

Reported 2026-09-30: the agent deleted the open chat ("delete this chat in 1
minute"), the page kept showing it, and every model pick after that answered
"Failed to set model" (PATCH /api/session/<id> -> 404). The picker now treats
a 404 as "this chat is gone" and starts a new chat with the picked model.
Like test_model_name_tooltip.py, this checks the source: the module needs the
whole app's globals to run.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PICKER = (ROOT / "static/js/modelPicker.js").read_text(encoding="utf-8")
SESSIONS = (ROOT / "static/js/sessions.js").read_text(encoding="utf-8")


def test_a_404_on_the_model_patch_starts_a_new_chat():
    patch = PICKER.index("{ method: 'PATCH', body: fd });\n        if (res.status === 404)")
    branch = PICKER[patch:PICKER.index("if (!res.ok)", patch)]
    assert "_deps.createDirectChat(m.url, m.mid, m.endpointId)" in branch
    assert "_deps.loadSessions" in branch
    assert "return;" in branch


def test_the_picker_is_given_load_sessions():
    init = SESSIONS[SESSIONS.index("initModelPicker({"):]
    assert "loadSessions," in init[:init.index("});")]
