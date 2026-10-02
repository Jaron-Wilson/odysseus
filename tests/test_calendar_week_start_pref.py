"""Week-start-day (Sun/Mon) setting (static/js/calendar/utils.js).

Pins the pure date-math helpers backing the "Week starts on" pref: the
default stays Monday (matching the calendar's pre-setting layout) until
`setWeekStartsOn` is called, `_dowOffset` returns days-since-week-start for
both settings, and `weekdayLabels` reorders the header row to match.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(source: str):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_default_week_start_is_monday():
    values = _node_eval(
        """
        import { getWeekStartsOn, weekdayLabels } from './static/js/calendar/utils.js';
        console.log(JSON.stringify({
          start: getWeekStartsOn(),
          labels: weekdayLabels(),
        }));
        """
    )
    assert values["start"] == 1
    assert values["labels"] == ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def test_set_week_starts_on_sunday_reorders_labels_and_offset():
    values = _node_eval(
        """
        import { setWeekStartsOn, getWeekStartsOn, weekdayLabels, _dowOffset } from './static/js/calendar/utils.js';
        setWeekStartsOn('sun');
        // 2026-06-10 is a Wednesday.
        const wed = new Date(2026, 5, 10);
        console.log(JSON.stringify({
          start: getWeekStartsOn(),
          labels: weekdayLabels(),
          offset: _dowOffset(wed),
        }));
        """
    )
    assert values["start"] == 0
    assert values["labels"] == ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    assert values["offset"] == 3  # Wed is 3 days after Sun


def test_set_week_starts_on_monday_offset_matches_legacy_formula():
    values = _node_eval(
        """
        import { setWeekStartsOn, _dowOffset } from './static/js/calendar/utils.js';
        setWeekStartsOn('mon');
        // 2026-06-10 is a Wednesday; legacy formula was (getDay()+6)%7 == 2.
        const wed = new Date(2026, 5, 10);
        // 2026-06-07 is a Sunday; legacy formula gave 6 (last day of a Mon-start week).
        const sun = new Date(2026, 5, 7);
        console.log(JSON.stringify({
          wed: _dowOffset(wed),
          sun: _dowOffset(sun),
        }));
        """
    )
    assert values["wed"] == 2
    assert values["sun"] == 6


def test_unrecognized_pref_value_falls_back_to_monday():
    values = _node_eval(
        """
        import { setWeekStartsOn, getWeekStartsOn } from './static/js/calendar/utils.js';
        setWeekStartsOn('bogus');
        console.log(JSON.stringify({ start: getWeekStartsOn() }));
        """
    )
    assert values["start"] == 1
