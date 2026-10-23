"""odysseus-mod.json validation (src/mods/manifest.py)."""
import json
import os

import pytest

from src.mods import manifest as mf
from tests.mods_helpers import HELLO


def _base(**kw):
    m = {"id": "print-hub", "name": "Print Hub", "version": "0.1.0",
         "runtime": {"type": "process", "command": ["python", "-m", "printhub"]}}
    m.update(kw)
    return m


def test_admin_only_is_a_boolean():
    assert mf.validate(_base())["admin_only"] is False
    assert mf.validate(_base(admin_only=True))["admin_only"] is True
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(_base(admin_only="yes"))
    assert "admin_only" in str(e.value)


def test_hello_mod_manifest_is_valid():
    m = mf.load(HELLO)
    assert m["id"] == "hello-mod"
    assert m["runtime"]["start"] == "lazy" and m["runtime"]["health"] == "/health"
    assert m["ui"]["tabs"][0]["path"] == "/" and m["tools"]["endpoint"] == "/mod/tools"
    assert "hello.waved" in m["events"]["emits"]


def test_the_draft_spec_example_validates():
    """The print-hub example from the spec (docker runtime) is accepted."""
    raw = _base(runtime={"type": "docker", "compose": "docker-compose.yml", "health": "/health"},
                permissions=["notify", "events.emit", "events.subscribe", "devices.read", "devices.control",
                             "network.lan", "storage", "llm.call"],
                ui={"tabs": [{"id": "printer", "title": "Printer", "icon": "printer", "path": "/"}],
                    "home_cards": [{"id": "status", "path": "/cards/status"}], "settings_path": "/settings"},
                tools={"endpoint": "/mod/tools", "keywords": ["print", "printer", "3d", "bambu"]},
                events={"emits": ["print.finished", "print.failed", "detector.failure"], "subscribes": []},
                author="Jaron Wilson", license="AGPL-3.0", min_core="0.1")
    m = mf.validate(raw)
    assert m["runtime"]["type"] == "docker" and m["runtime"]["start"] == "lazy"
    assert m["ui"]["home_cards"][0]["height"] == 180


def test_defaults():
    m = mf.validate(_base())
    assert m["permissions"] == [] and m["tools"] is None
    assert m["events"]["webhook"] == "/mod/events" and m["ui"]["settings_path"] is None


@pytest.mark.parametrize("bad, words", [
    ({"id": "Print_Hub"}, "`id`"),
    ({"id": "v1"}, "reserved"),
    ({"id": "a--b"}, "`id`"),
    ({"version": "latest"}, "version"),
    ({"permissions": ["root"]}, "unknown permission"),
    ({"runtime": {"type": "process"}}, "runtime.command"),
    ({"runtime": {"type": "vm", "command": ["x"]}}, "runtime.type"),
    ({"runtime": {"type": "docker", "compose": "../../etc/passwd"}}, "runtime.compose"),
    ({"runtime": {"type": "process", "command": ["x"], "env": {"ODYSSEUS_MOD_TOKEN": "x"}}}, "runtime.env"),
    ({"runtime": {"type": "process", "command": ["x"], "health": "http://evil/"}}, "runtime.health"),
    ({"ui": {"tabs": [{"id": "t", "path": "/../../api"}]}}, "ui.tabs[0].path"),
    ({"events": {"emits": ["print.done"]}}, "events.emit permission"),
    ({"min_core": "9.0"}, "needs mod API"),
    ({"homepage": "javascript:alert(1)"}, "homepage"),
])
def test_invalid_manifests_are_refused(bad, words):
    with pytest.raises(mf.ManifestError) as e:
        mf.validate(_base(**bad))
    assert any(words in err for err in e.value.errors), e.value.errors


def test_every_problem_is_reported_at_once():
    with pytest.raises(mf.ManifestError) as e:
        mf.validate({"id": "X", "runtime": {"type": "process"}, "permissions": ["nope"]})
    assert len(e.value.errors) >= 4


def test_missing_and_broken_files(tmp_path):
    with pytest.raises(mf.ManifestError, match="no odysseus-mod.json"):
        mf.load(str(tmp_path))
    (tmp_path / "odysseus-mod.json").write_text("{nope")
    with pytest.raises(mf.ManifestError, match="not valid JSON"):
        mf.load(str(tmp_path))


def test_permission_descriptions_cover_every_permission():
    assert {d["id"] for d in mf.describe_permissions(list(mf.PERMISSIONS))} == set(mf.PERMISSIONS)
    assert all(v and "\u2014" not in v for v in mf.PERMISSIONS.values())


def test_template_manifest_is_valid():
    tpl = os.path.join(os.path.dirname(HELLO), "..", "templates", "mod-template")
    assert mf.load(tpl)["id"] == "my-mod"
    with open(os.path.join(tpl, "odysseus-mod.json")) as f:
        assert json.load(f)["runtime"]["command"][0] == "python"


def test_scaffold_makes_a_working_mod(tmp_path):
    import subprocess
    import sys
    from src.mods import scaffold
    out = scaffold.create(str(tmp_path / "garage"), "garage-door", "Garage Door", "Opens the garage.")
    folder = out["folder"]
    m = mf.load(folder)
    assert m["id"] == "garage-door" and m["name"] == "Garage Door"
    assert m["runtime"]["command"] == ["python", "-m", "garage_door"]
    assert os.path.isfile(os.path.join(folder, "garage_door", "app.py"))
    assert os.path.isfile(os.path.join(folder, "tests", "test_garage_door.py"))
    assert os.path.isfile(os.path.join(folder, ".claude", "skills", "new-mod", "SKILL.md"))
    for root, _d, files in os.walk(folder):
        for fn in files:
            if fn.endswith((".py", ".json")) and "odysseus_mod" not in root:
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    text = f.read()
                assert "my-mod" not in text and "my_mod" not in text, fn
    with pytest.raises(ValueError):
        scaffold.create(folder, "garage-door")
    with pytest.raises(ValueError):
        scaffold.create(str(tmp_path / "bad"), "Bad_Id")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                       cwd=folder, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
