"""The Plans editor's text handling (static/js/plansText.js), run in node.

What matters for a natural Markdown editor:
  * the task lines and headings it finds are the ones markdown.js renders
    (fenced code skipped), so ticking box N changes line N;
  * Enter continues a list (next number, empty box for tasks) and ends it on
    an empty item; Tab / Shift+Tab indent list items and leave other lines
    alone; Ctrl+B / Ctrl+I wrap and unwrap.

Skips when `node` is not installed.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_MOD = (_REPO / "static" / "js" / "plansText.js").as_uri()
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def _call(*calls):
    js = f"""
import * as T from '{_MOD}';
const calls = {json.dumps(calls)};
console.log(JSON.stringify(calls.map(([fn, ...args]) => T[fn](...args))));
"""
    proc = subprocess.run(["node", "--input-type=module"], input=js, capture_output=True, text=True,
                          encoding="utf-8", cwd=str(_REPO), timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    return out if len(calls) > 1 else out[0]


def _apply(text, edit):
    assert edit is not None
    new = text[:edit["start"]] + edit["insert"] + text[edit["end"]:]
    return new, edit["selStart"], edit["selEnd"]


PLAN = """# Trip

- [ ] book flights
- [x] passport
  - [X] photos

```
- [ ] not a task
# not a heading
```

## Packing
* [ ] socks
1. [ ] numbered items are not tasks
"""


def test_scan_finds_tasks_and_headings_outside_code():
    s = _call(["scan", PLAN])
    assert [(t["line"], t["done"], t["text"]) for t in s["tasks"]] == [
        (2, False, "book flights"), (3, True, "passport"), (4, True, "photos"), (12, False, "socks")]
    assert [(h["level"], h["text"], h["line"]) for h in s["headings"]] == [(1, "Trip", 0), (2, "Packing", 11)]
    assert _call(["progress", PLAN]) == {"done": 2, "total": 4}


def test_toggling_a_task_flips_only_its_box():
    e0, e1 = _call(["toggleTask", PLAN, 0], ["toggleTask", PLAN, 2])
    after, _, _ = _apply(PLAN, e0)
    assert after.splitlines()[2] == "- [x] book flights"
    assert after.replace("- [x] book flights", "- [ ] book flights") == PLAN
    after, _, _ = _apply(PLAN, e1)
    assert after.splitlines()[4] == "  - [ ] photos"
    assert _call(["toggleTask", PLAN, 9]) is None


def test_enter_continues_lists():
    cases = [
        ("- milk", "- milk\n- "),
        ("  * nested", "  * nested\n  * "),
        ("- [x] done thing", "- [x] done thing\n- [ ] "),
        ("9. nine", "9. nine\n10. "),
        ("3) three", "3) three\n4) "),
    ]
    edits = _call(*[["continueList", t, len(t)] for t, _ in cases])
    for (text, want), e in zip(cases, edits):
        new, s, _ = _apply(text, e)
        assert new == want and s == len(want)


def test_enter_on_an_empty_item_ends_the_list():
    text = "- one\n- "
    new, s, _ = _apply(text, _call(["continueList", text, len(text)]))
    assert new == "- one\n" and s == len("- one\n")
    text = "- one\n- [ ] "
    new, _, _ = _apply(text, _call(["continueList", text, len(text)]))
    assert new == "- one\n"


def test_enter_elsewhere_is_left_alone():
    assert _call(["continueList", "plain words", 5]) is None
    assert _call(["continueList", "- item", 1]) is None          # caret inside the marker
    assert _call(["continueList", "-not a list", 11]) is None


def test_enter_mid_item_splits_it():
    text = "- buy milk"
    new, s, _ = _apply(text, _call(["continueList", text, 6]))
    assert new == "- buy \n- milk" and new[s:] == "milk"


def test_tab_indents_list_items_and_shift_tab_outdents():
    text = "- a\n- b\n- c"
    new, s, e = _apply(text, _call(["indentList", text, 4, 11, False]))  # "- b" and "- c" selected
    assert new == "- a\n  - b\n  - c"
    back, _, _ = _apply(new, _call(["indentList", new, 6, 6, True]))
    assert back == "- a\n- b\n  - c"
    # A caret on a plain line: Tab is not taken (it moves focus).
    assert _call(["indentList", "just text", 2, 2, False]) is None
    # Nothing left to outdent.
    assert _call(["indentList", "- a", 1, 1, True]) is None


def test_bold_and_italic_wrap_and_unwrap():
    text = "make this bold"
    new, s, e = _apply(text, _call(["wrap", text, 10, 14, "**"]))
    assert new == "make this **bold**" and new[s:e] == "bold"
    again, s2, e2 = _apply(new, _call(["wrap", new, s, e, "**"]))
    assert again == text and again[s2:e2] == "bold"
    new, s, e = _apply("x", _call(["wrap", "x", 1, 1, "*"]))
    assert new == "x**" and s == e == 2
    # Italic inside bold doesn't strip the bold.
    new, _, _ = _apply("**word**", _call(["wrap", "**word**", 2, 6, "*"]))
    assert new == "***word***"


def test_line_prefixes_swap_and_toggle():
    new, _, _ = _apply("- thing", _call(["prefixLines", "- thing", 0, 0, "- [ ] "]))
    assert new == "- [ ] thing"
    new, _, _ = _apply("a\nb", _call(["prefixLines", "a\nb", 0, 3, "- "]))
    assert new == "- a\n- b"
    new, _, _ = _apply("## Title", _call(["prefixLines", "## Title", 3, 3, "## "]))
    assert new == "Title"
