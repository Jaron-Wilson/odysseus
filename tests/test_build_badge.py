"""The latest merged PR the server runs, shown bottom left.

Asked for on 2026-09-27: "show version number or latest pr ... not the one
that's not merged but the latest merged one so that I can tell if the
server's been restarted".
"""
import os
import subprocess

from src import build_info

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_reads_the_latest_merge_commit(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q", "-b", "dev")
    run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "start")
    for n in (57, 58):
        run("checkout", "-q", "-b", f"f{n}")
        run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", f"feature {n}")
        run("checkout", "-q", "dev")
        run("-c", "user.email=a@b", "-c", "user.name=a", "merge", "-q", "--no-ff", f"f{n}",
            "-m", f"Merge pull request #{n} from Jaron-Wilson/f{n}")
    # An open PR's own commits (not merged) must not count.
    run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "Fix #99 later")
    monkeypatch.setattr(build_info, "BASE_DIR", str(repo))
    info = build_info._read()
    assert info["pr"] == 58 and len(info["commit"]) == 8 and info["branch"] == "dev"


def test_endpoint_and_badge_are_wired():
    app = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    assert '@app.get("/api/version")' in app
    html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    assert 'id="build-badge"' in html
    js = open(os.path.join(HERE, "static", "js", "buildBadge.js"), encoding="utf-8").read()
    assert "_json('/api/version')" in js and "· reload" in js
