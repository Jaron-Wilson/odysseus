"""The Installed view of Settings > Mods (static/js/modsInstalled.js): its pure
helpers run under node, the markup it needs is in index.html, and the read-only
fields it relies on come from registry.public()."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


def _run(js: str):
    out = subprocess.run(
        ["node", "--input-type=module", "-e", js],
        capture_output=True, text=True, cwd=str(_REPO), timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_status_and_source_helpers():
    res = _run("""
    import { statusOf, sourceShort, sourceFull } from './static/js/modsInstalled.js';
    console.log(JSON.stringify({
      off: statusOf({ enabled: false, state: { status: 'running' } }),
      run: statusOf({ enabled: true, state: { status: 'running' } }),
      failed: statusOf({ enabled: true, state: { status: 'failed' } }),
      backoff: statusOf({ enabled: true, state: { status: 'backoff' } }),
      setup: statusOf({ enabled: true, setup_error: 'x', state: { status: 'stopped' } }),
      idle: statusOf({ enabled: true, state: {} }),
      gh: sourceShort({ url: 'https://github.com/Jaron-Wilson/odysseus-adsb.git' }),
      sub: sourceShort({ url: 'https://github.com/a/b', subdir: 'mod' }),
      dir: sourceShort({ path: '/home/me/my-mod' }),
      full: sourceFull({ url: 'https://github.com/a/b', ref: 'v1', subdir: 's' }),
    }));
    """)
    assert res["off"] == "disabled" and res["run"] == "running" and res["idle"] == "stopped"
    assert res["failed"] == "error" and res["backoff"] == "error" and res["setup"] == "error"
    assert res["gh"] == "Jaron-Wilson/odysseus-adsb" and res["sub"] == "a/b/mod" and res["dir"] == "my-mod"
    assert res["full"] == "https://github.com/a/b @ v1 (s)"


def test_installed_markup_present():
    html = (_REPO / "static" / "index.html").read_text()
    for needle in ("mods-inst-q", "mods-inst-sort", "mods-inst-chips", "mods-inst-details",
                   "mods-installed-extra", "modsInstalled.css"):
        assert needle in html
    js = (_REPO / "static" / "js" / "modsInstalled.js").read_text()
    for chip in ("update", "running", "stopped", "errors", "disabled", "admin", "docker"):
        assert re.search(r"\['%s'," % chip, js)
    # American English, no em dashes or arrows in UI text.
    for path in (_REPO / "static" / "js" / "modsInstalled.js", _REPO / "static" / "js" / "modsUpdates.js"):
        text = path.read_text()
        assert "—" not in text and "→" not in text and "&rarr;" not in text


def test_public_exposes_icon_tags_imports():
    from src.mods import registry
    entry = {"id": "x", "name": "X", "version": "1", "enabled": True,
             "source": {"type": "git", "url": "https://github.com/Some-Owner/some-mod"},
             "manifest": {"tags": ["a"], "imports": ["email"], "icon_url": ""}}
    pub = registry.public(entry)
    assert pub["tags"] == ["a"] and pub["imports"] == ["email"]
    assert pub["icon_url"].startswith("/api/mods/catalog/image?u=") and "some-owner" in pub["icon_url"].lower()
    entry["source"] = {"type": "path", "path": "/tmp/x"}
    assert registry.public(entry)["icon_url"] == ""
