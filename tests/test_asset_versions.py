"""Content-hashed asset URLs and the import map (core/asset_versions.py)."""
import json
import re

from core import asset_versions as av


def _tree(tmp_path):
    (tmp_path / "js").mkdir()
    (tmp_path / "css").mkdir()
    (tmp_path / "app.js").write_text("a")
    (tmp_path / "js" / "x.js").write_text("x")
    (tmp_path / "css" / "y.css").write_text("y")
    av._state.update(at=0.0, map={})
    return str(tmp_path)


def test_stamp_html_versions_references_and_adds_import_map(tmp_path):
    d = _tree(tmp_path)
    html = ('<html><head><script type="module" src="/static/js/x.js?v=old"></script>'
            '<link rel="stylesheet" href="/static/css/y.css"></head></html>')
    out = av.stamp_html(html, d, "N")
    vmap = av.versions(d)
    assert f'/static/js/x.js?v={vmap["/static/js/x.js"]}"' in out
    assert f'/static/css/y.css?v={vmap["/static/css/y.css"]}"' in out
    m = re.search(r'<script type="importmap" nonce="N">(.*?)</script>', out)
    imports = json.loads(m.group(1))["imports"]
    assert imports["/static/app.js"].startswith("/static/app.js?v=")
    assert "/static/css/y.css" not in imports


def test_cache_header_immutable_only_for_current_hash(tmp_path):
    d = _tree(tmp_path)
    h = av.versions(d)["/static/js/x.js"]
    assert "immutable" in av.cache_header_for("js/x.js", f"v={h}", d)
    assert av.cache_header_for("js/x.js", "v=deadbeef", d) == "no-cache"
    assert av.cache_header_for("js/x.js", "", d) == "no-cache"
