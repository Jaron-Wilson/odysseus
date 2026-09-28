"""One-click deploy (routes/deploy_routes.py).

Asked for on 2026-09-28: "yes I would love a one click" to deploy merged PRs
instead of asking for a pull and restart each time.
"""
import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.deploy_routes as dr


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True, text=True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    origin, live, dev = tmp_path / "origin.git", tmp_path / "live", tmp_path / "dev"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(origin)], check=True)
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    (dev / "app.py").write_text("x = 1\n")
    _git(dev, "add", "app.py")
    _git(dev, "commit", "-q", "-m", "start")
    _git(dev, "push", "-q", "origin", "HEAD:dev")
    _git(tmp_path, "clone", "-q", "-b", "dev", str(origin), str(live))
    monkeypatch.setattr(dr, "BASE_DIR", str(live))
    monkeypatch.setattr(dr, "_state", {"fetched": 0.0})
    restarts = []
    monkeypatch.setattr(dr, "_restart", lambda: restarts.append(1))
    from src import build_info
    head = subprocess.run(["git", "-C", str(live), "rev-parse", "--short=8", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(build_info, "INFO", {"pr": 44, "commit": head, "branch": "dev", "started": 1})
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(dr.setup_deploy_routes())
    return TestClient(app), live, dev, restarts


def _merge_pr(dev, n, title, content="x = 2\n"):
    _git(dev, "checkout", "-q", "-b", f"f{n}")
    (dev / "app.py").write_text(content)
    _git(dev, "commit", "-q", "-am", f"feature {n}")
    _git(dev, "checkout", "-q", "dev")
    _git(dev, "merge", "-q", "--no-ff", f"f{n}", "-m", f"Merge pull request #{n} from x/f{n}", "-m", title)
    _git(dev, "push", "-q", "origin", "dev")


def test_status_lists_waiting_merges_then_deploy_pulls_and_restarts(repos):
    c, live, dev, restarts = repos
    assert c.get("/api/admin/deploy/status").json()["behind"] == 0
    _merge_pr(dev, 45, "Server nicknames")
    st = c.get("/api/admin/deploy/status?refresh=1").json()
    assert st["behind"] == 2 and st["can_deploy"] and st["new_prs"][0] == {
        "pr": 45, "commit": st["new_prs"][0]["commit"], "title": "Server nicknames"}
    r = c.post("/api/admin/deploy").json()
    assert r["ok"] and r["to_pr"] == 45 and restarts == [1]
    assert (live / "app.py").read_text() == "x = 2\n"                   # fast-forwarded
    assert c.post("/api/admin/deploy").json()["ok"]                      # pulled, not yet restarted
    assert restarts == [1, 1]


def test_refuses_local_changes_other_branches_and_broken_code(repos):
    c, live, dev, restarts = repos
    _merge_pr(dev, 46, "Broken", content="def x(:\n")
    r = c.post("/api/admin/deploy")
    assert r.status_code == 422 and "does not compile" in r.json()["detail"]
    assert (live / "app.py").read_text() == "x = 1\n" and not restarts    # nothing pulled
    (live / "app.py").write_text("local edit\n")
    r = c.post("/api/admin/deploy")
    assert r.status_code == 409 and "local changes" in r.json()["detail"]
    _git(live, "checkout", "-q", "--", "app.py")
    _git(live, "checkout", "-q", "-b", "other")
    r = c.post("/api/admin/deploy")
    assert r.status_code == 409 and "not dev" in r.json()["detail"]


def test_the_badge_is_wired():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(here, "static", "js", "buildBadge.js"), encoding="utf-8").read()
    assert "fetch('/api/admin/deploy', { method: 'POST' })" in js or "_json('/api/admin/deploy', { method: 'POST' })" in js
    assert "\u2192 #${to} \u00b7 Deploy" in js
    assert "setup_deploy_routes()" in open(os.path.join(here, "app.py")).read()
