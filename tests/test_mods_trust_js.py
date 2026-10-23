"""The trusted publishers UI (static/js/modsTrust.js): the badge, the review
notice, the History list, and the markup and wiring it needs."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


def _run(js: str):
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True,
                         cwd=str(_REPO), timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_badge_notice_and_history():
    res = _run("""
    globalThis.document = { getElementById: () => null };
    const T = await import('./static/js/modsTrust.js');
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    T.initTrust({ api: async () => ({}), esc, say: () => {} });
    const auto = { trust: { trusted: true, auto_approve: true, publisher: 'Jaron-Wilson', always_ask: [] } };
    const must = { trust: { trusted: true, auto_approve: false, publisher: 'Jaron-Wilson', always_ask: ['ui.style'] } };
    console.log(JSON.stringify({
      yours: T.trustBadgeParts({ trusted: true, yours: true, publisher: 'Jaron-Wilson' }).label,
      other: T.trustBadgeParts({ trusted: true, yours: false, publisher: 'acme' }).label,
      none: T.trustBadgeParts({ trusted: false }),
      noTrust: T.trustBadgeParts(undefined),
      auto: T.noticeHtml(auto, { waiting: true }),
      autoReview: T.noticeHtml(auto, { waiting: false }),
      click: T.noticeHtml(must),
      plain: T.noticeHtml({ trust: { trusted: false } }),
      row: T.alwaysAskRow({ id: 'm', trust: { trusted: true, publisher: 'x', always_ask: true } }),
      noRow: T.alwaysAskRow({ id: 'm', trust: { trusted: false } }),
      hist: T.historyHtml([{ event: 'auto_installed', ts: 1, text: 'Installed 1.0.0: granted notify; trusted publisher Jaron-Wilson <b>' }]),
      empty: T.historyHtml([]),
    }));
    """)
    assert res["yours"] == "Yours" and res["other"] == "Trusted: acme"
    assert res["none"] is None and res["noTrust"] is None
    assert "Auto-approved because it's from Jaron-Wilson, a trusted publisher" in res["auto"]
    assert "installs in a moment" in res["auto"] and "review it anyway" in res["autoReview"]
    assert 'data-trust-notice="click"' in res["click"] and "<code>ui.style</code>" in res["click"]
    assert res["plain"] == ""
    assert "data-mod-trust" in res["row"] and " checked" in res["row"] and res["noRow"] == ""
    assert "data-history-event" in res["hist"] and "&lt;b&gt;" in res["hist"] and "<b>" not in res["hist"]
    assert "Nothing yet" in res["empty"]


def test_markup_and_wiring_present():
    html = (_REPO / "static" / "index.html").read_text()
    assert 'id="mods-trust"' in html
    settings = (_REPO / "static" / "js" / "modsSettings.js").read_text()
    for needle in ("Review anyway", "mods-approve-review", "auto: !!auto", "data-mod-trust", "Trust.loadTrust"):
        assert needle in settings or needle.replace("data-mod-trust", "modTrust") in settings
    assert "Always ask me" in (_REPO / "static" / "js" / "modsTrust.js").read_text()
    assert "History" in (_REPO / "static" / "js" / "modsInstalled.js").read_text()
    assert "trusted_publisher" in (_REPO / "static" / "js" / "modsBrowse.js").read_text()
    # American English, no em dashes or arrows in UI text or code.
    for name in ("modsTrust.js", "modsSettings.js", "modsInstalled.js", "modsBrowse.js", "modsUpdates.js"):
        text = (_REPO / "static" / "js" / name).read_text()
        assert "—" not in text and "→" not in text, name
    assert not re.search(r"\bhonour|colour|behaviour|cancell", (_REPO / "static" / "js" / "modsTrust.js").read_text())
