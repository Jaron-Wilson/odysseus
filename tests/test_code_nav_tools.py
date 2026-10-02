"""Tests for the code-navigation tools (grep, glob, ls) + read_file line range."""
import os
import shutil
import asyncio
import tempfile
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test_code_nav.db")

from src.tool_execution import _direct_fallback


def _run(tool, content):
    return asyncio.run(_direct_fallback(tool, content))


@pytest.fixture
def repo():
    # Built under /tmp, which is on the default tool-path allowlist.
    root = tempfile.mkdtemp(dir="/tmp", prefix="codenav_")
    try:
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("import os\n# needle here\nprint('x')\n")
        os.mkdir(os.path.join(root, "sub"))
        with open(os.path.join(root, "sub", "b.txt"), "w") as f:
            f.write("nothing\nNEEDLE upper\n")
        os.mkdir(os.path.join(root, "node_modules"))
        with open(os.path.join(root, "node_modules", "dep.py"), "w") as f:
            f.write("needle in dep\n")
        g = os.path.join(root, ".git")
        os.mkdir(g)
        with open(os.path.join(g, "config"), "w") as f:
            f.write("needle in git\n")
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── grep ──────────────────────────────────────────────────────────────────

def test_grep_finds_match(repo):
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]


def test_grep_skips_junk_dirs(repo):
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert "node_modules" not in r["output"]
    assert ".git/config" not in r["output"]


def test_grep_ignore_case(repo):
    r = _run("grep", f'{{"pattern": "needle", "ignore_case": true, "path": "{repo}"}}')
    assert "b.txt:2:" in r["output"]


def test_grep_glob_filter(repo):
    r = _run("grep", f'{{"pattern": "needle", "ignore_case": true, "glob": "*.py", "path": "{repo}"}}')
    assert "a.py" in r["output"]
    assert "b.txt" not in r["output"]


def test_grep_no_match(repo):
    r = _run("grep", f'{{"pattern": "zzzznotfound", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "No matches" in r["output"]


def test_grep_requires_pattern(repo):
    r = _run("grep", "{}")
    assert r["exit_code"] == 1
    assert "pattern is required" in r["error"]


def test_grep_path_outside_roots_rejected(repo):
    r = _run("grep", '{"pattern": "x", "path": "/etc"}')
    assert r["exit_code"] == 1
    assert "outside the allowed roots" in r["error"]


def test_grep_python_fallback_when_no_rg(repo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]
    assert "node_modules" not in r["output"]
    assert ".git/config" not in r["output"]


# ── glob ──────────────────────────────────────────────────────────────────

def test_glob_py(repo):
    r = _run("glob", f'{{"pattern": "*.py", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]


def test_glob_recursive_skips_junk(repo):
    r = _run("glob", f'{{"pattern": "**/*.py", "path": "{repo}"}}')
    assert "a.py" in r["output"]
    assert "node_modules" not in r["output"]


def test_glob_requires_pattern(repo):
    r = _run("glob", "{}")
    assert r["exit_code"] == 1


# ── ls ────────────────────────────────────────────────────────────────────

def test_ls_lists_entries(repo):
    r = _run("ls", f'{{"path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]
    assert "sub/" in r["output"]
    assert ".git" not in r["output"]  # hidden skipped


def test_ls_path_outside_rejected(repo):
    r = _run("ls", '{"path": "/etc"}')
    assert r["exit_code"] == 1
    assert "outside the allowed roots" in r["error"]


# ── read_file line range ───────────────────────────────────────────────────

def test_read_file_offset_limit(repo):
    p = os.path.join(repo, "lines.txt")
    with open(p, "w") as f:
        f.write("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    r = _run("read_file", f'{{"path": "{p}", "offset": 3, "limit": 2}}')
    assert r["exit_code"] == 0
    assert r["output"] == "line3\nline4\n"


def test_read_file_plain_path_backcompat(repo):
    r = _run("read_file", os.path.join(repo, "a.py"))
    assert r["exit_code"] == 0
    assert "needle" in r["output"]


# ── sensitive-file deny-list applies during recursive search ──────────────
#
# read_file/write_file/edit_file already refuse to touch a deny-listed path
# (.ssh, .gnupg, id_rsa, authorized_keys, known_hosts, .env, shell rc files).
# grep and glob must honor the SAME deny-list while recursively walking a
# directory, not just on a root path handed to _resolve_search_root — an
# otherwise-legitimate search root (e.g. a workspace or an opted-in extra
# root) can still contain a sensitive subpath, and a prompt-injected model
# could use grep/glob as a read oracle for it.

@pytest.fixture
def repo_with_secrets(repo):
    with open(os.path.join(repo, ".env"), "w") as f:
        f.write("needle AWS_SECRET=xxxxx\n")
    with open(os.path.join(repo, "id_rsa"), "w") as f:
        f.write("needle PRIVATE KEY\n")
    with open(os.path.join(repo, "known_hosts"), "w") as f:
        f.write("needle host-key\n")
    os.mkdir(os.path.join(repo, ".ssh"))
    with open(os.path.join(repo, ".ssh", "authorized_keys"), "w") as f:
        f.write("needle ssh-rsa AAAA\n")
    return repo


def test_grep_skips_sensitive_files_rg(repo_with_secrets):
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo_with_secrets}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]
    for leak in (".env", "id_rsa", "known_hosts", "authorized_keys"):
        assert leak not in r["output"], f"grep leaked sensitive file: {leak}"


def test_grep_skips_sensitive_files_python_fallback(repo_with_secrets, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo_with_secrets}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]
    for leak in (".env", "id_rsa", "known_hosts", "authorized_keys"):
        assert leak not in r["output"], f"grep leaked sensitive file: {leak}"


def test_grep_skips_case_variant_sensitive_files(repo_with_secrets, monkeypatch):
    # _is_sensitive_path must fold case: a case-variant filename (ID_RSA,
    # Known_Hosts) is the same secret on a case-insensitive filesystem and
    # must be excluded the same way the lowercase form is.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with open(os.path.join(repo_with_secrets, "ID_RSA"), "w") as f:
        f.write("needle PRIVATE KEY UPPER\n")
    with open(os.path.join(repo_with_secrets, "Known_Hosts"), "w") as f:
        f.write("needle host-key mixed-case\n")
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo_with_secrets}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]
    assert "ID_RSA" not in r["output"]
    assert "Known_Hosts" not in r["output"]


@pytest.mark.skipif(shutil.which("rg") is None, reason="targets the ripgrep fast-path")
def test_grep_skips_case_variant_sensitive_files_rg(repo_with_secrets):
    with open(os.path.join(repo_with_secrets, "ID_RSA"), "w") as f:
        f.write("needle PRIVATE KEY UPPER\n")
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo_with_secrets}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]
    assert "ID_RSA" not in r["output"]


def test_glob_skips_sensitive_files(repo_with_secrets):
    r = _run("glob", f'{{"pattern": "**/*", "path": "{repo_with_secrets}"}}')
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]
    for leak in (".env", "id_rsa", "known_hosts", "authorized_keys"):
        assert leak not in r["output"], f"glob leaked sensitive file: {leak}"


def test_glob_skips_sensitive_files_case_insensitive(repo_with_secrets):
    with open(os.path.join(repo_with_secrets, "ID_RSA"), "w") as f:
        f.write("PRIVATE KEY UPPER\n")
    r = _run("glob", f'{{"pattern": "**/*", "path": "{repo_with_secrets}"}}')
    assert r["exit_code"] == 0
    assert "ID_RSA" not in r["output"]


def test_glob_direct_sensitive_pattern_returns_no_match(repo_with_secrets):
    for pat in ("id_rsa", "**/authorized_keys"):
        r = _run("glob", f'{{"pattern": "{pat}", "path": "{repo_with_secrets}"}}')
        assert r["exit_code"] == 0
        assert "No files" in r["output"], f"glob matched a sensitive pattern: {pat}"
