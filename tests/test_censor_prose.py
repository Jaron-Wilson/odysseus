"""The sensitive-info blur must not blur ordinary words.

Seen live: words like "escape" and "gate" were blurred. The label/value pass
took a plain space as the separator and had no word boundaries, so prose like
"the approval token gate" read as a label and its value, and every "gate" in
the message was then blurred.
"""
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const fn = src.slice(src.indexOf('function _looksSecret('), src.indexOf('// Labels that indicate the NEXT value'));
const reSrc = src.match(/const labelValueRe = (\/.+\/gi);/)[1];
const _looksSecret = eval('(' + fn.replace('function _looksSecret', 'function') + ')');
const re = eval(reSrc);
const out = {};
for (const t of JSON.parse(process.argv[3])) {
  re.lastIndex = 0; const hits = []; let m;
  while ((m = re.exec(t)) !== null) if (_looksSecret(m[1])) hits.push(m[1]);
  out[t] = hits;
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_prose_is_not_blurred_but_credentials_are(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(HARNESS)
    cases = [
        "The plan-and-approve token gate is identical either way.",
        "Press Escape to interrupt; the secret escape hatch is Stop.",
        "Use an API token scoped overlay for the desktop app.",
        "password: hunter2",
        "api_key=sk_live_51HxYzAbCdEf",
        "Token:\n  ody_cSae8f3kLmN0pQ",
    ]
    out = json.loads(subprocess.check_output(
        ["node", str(h), os.path.join(HERE, "static", "js", "censor.js"), json.dumps(cases)]).decode())
    assert out[cases[0]] == [] and out[cases[1]] == [] and out[cases[2]] == []
    assert out[cases[3]] == ["hunter2"]
    assert out[cases[4]] == ["sk_live_51HxYzAbCdEf"]
    assert out[cases[5]] == ["ody_cSae8f3kLmN0pQ"]


def test_overlay_token_is_fenced_and_page_presence_reported():
    app = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    assert 'set(matched_scopes) == {"overlay"} and not path.startswith("/api/overlay/")' in app
    assert 'setdefault("browser_seen", {})[request.client.host] = time.time()' in app
    routes = open(os.path.join(HERE, "routes", "overlay_routes.py"), encoding="utf-8").read()
    assert '"page_seen_ago"' in routes
    ov = open(os.path.join(HERE, "tools", "music_overlay", "music_overlay.py"), encoding="utf-8").read()
    assert 'self.settings.get("close_with_odysseus", True)' in ov and "Close with Odysseus" in ov
