"""Which .env the server loads (src/env_file.py). python-dotenv's default
searched parent folders, so a checkout under ~/odysseus/.claude/worktrees/
read the main checkout's .env (the live data dir, auth and keys)."""
import os
import subprocess
import sys

import pytest

from src import env_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def nested(tmp_path):
    """A main checkout with a .env and a worktree inside it without one."""
    main = tmp_path / "odysseus"
    wt = main / ".claude" / "worktrees" / "test"
    wt.mkdir(parents=True)
    (main / ".env").write_text("ODYSSEUS_TEST_FROM=main\n")
    return main, wt


def test_a_parent_folders_env_is_never_used(nested):
    main, wt = nested
    assert env_file.env_file_path({}, app_root=str(wt)) is None


def test_the_app_roots_own_env_is_used(nested):
    main, wt = nested
    assert env_file.env_file_path({}, app_root=str(main)) == str(main / ".env")
    (wt / ".env").write_text("ODYSSEUS_TEST_FROM=worktree\n")
    assert env_file.env_file_path({}, app_root=str(wt)) == str(wt / ".env")


def test_odysseus_env_file(nested, tmp_path):
    main, wt = nested
    other = tmp_path / "test.env"
    other.write_text("X=1\n")
    assert env_file.env_file_path({"ODYSSEUS_ENV_FILE": str(other)}, app_root=str(main)) == str(other)
    for off in ("none", "NONE", "off"):
        assert env_file.env_file_path({"ODYSSEUS_ENV_FILE": off}, app_root=str(main)) is None
    with pytest.raises(SystemExit, match="does not exist"):
        env_file.env_file_path({"ODYSSEUS_ENV_FILE": str(tmp_path / "missing.env")}, app_root=str(main))


def _load_in_child(app_root, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    env.update(extra_env or {})
    env["PYTHONPATH"] = ROOT
    code = ("import os; from src.env_file import load_env_file; "
            f"load_env_file({str(app_root)!r}); print(os.environ.get('ODYSSEUS_TEST_FROM', '-'))")
    return subprocess.run([sys.executable, "-c", code], cwd=str(app_root), env=env, capture_output=True,
                          text=True, timeout=60, check=True).stdout.strip()


def test_loading_from_a_worktree_does_not_read_the_main_checkouts_env(nested):
    main, wt = nested
    assert _load_in_child(wt) == "-"
    assert _load_in_child(main) == "main"                     # the live deployment still finds its .env
    assert _load_in_child(main, {"ODYSSEUS_TEST_FROM": "shell"}) == "shell"   # the environment wins


def test_bom_is_tolerated(tmp_path):
    (tmp_path / ".env").write_bytes("﻿ODYSSEUS_TEST_FROM=bom\n".encode("utf-8"))
    assert _load_in_child(tmp_path) == "bom"


def test_app_uses_it_before_anything_else():
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "load_dotenv(" not in src
    first_app_import = min(src.index(s) for s in ("from core.", "from src.app_helpers", "from fastapi"))
    assert src.index("load_env_file()") < first_app_import


def test_chromadb_can_be_turned_off(monkeypatch):
    from src import chroma_client as cc
    monkeypatch.setattr(cc, "_client", None)
    monkeypatch.setenv("CHROMADB_DISABLED", "1")
    monkeypatch.setattr(cc, "_port_open", lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(RuntimeError, match="turned off"):
        cc.get_chroma_client()
