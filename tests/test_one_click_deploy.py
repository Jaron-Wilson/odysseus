"""One-click deploy (routes/deploy_routes.py).

Asked for on 2026-09-28: "yes I would love a one click" to deploy merged PRs
instead of asking for a pull and restart each time.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

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
    monkeypatch.setattr(dr, "_restart", lambda *a, **k: restarts.append(1))
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
    assert "Server did not come back: check the log" in js and "Back up on" in js


# The restart command. Seen live on 2026-10-11: the server ran as
# `python -m uvicorn app:app ...`; the restart ran
# `python .../site-packages/uvicorn/__main__.py ...` instead, which put the
# uvicorn package dir first on sys.path, so `import logging` found
# uvicorn/logging.py and the new server crashed: the deploy looked stuck.

PY = "/srv/venv/bin/python3"
MAIN = "/srv/venv/lib/python3.12/site-packages/uvicorn/__main__.py"
ARGS = ["app:app", "--host", "127.0.0.1", "--port", "7000"]


def test_relaunch_python_dash_m_uvicorn():
    assert dr._relaunch_cmd(argv=[MAIN, *ARGS], cmdline=[PY, "-m", "uvicorn", *ARGS],
                            executable=PY) == [PY, "-m", "uvicorn", *ARGS]
    # Without /proc: rebuilt as -m, never run as a script.
    assert dr._relaunch_cmd(argv=[MAIN, *ARGS], cmdline=[], executable=PY) == [PY, "-m", "uvicorn", *ARGS]
    assert dr._relaunch_cmd(argv=["/srv/venv/lib/python3.12/site-packages/a/b/__main__.py"], cmdline=[],
                            executable=PY) == [PY, "-m", "a.b"]


def test_relaunch_console_script(tmp_path):
    launcher = tmp_path / "bin" / "uvicorn"
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\n")
    py = str(tmp_path / "bin" / "python3")
    # The kernel's argv for a shebang launcher: python3 <launcher> args.
    assert dr._relaunch_cmd(argv=[str(launcher), *ARGS], cmdline=[py, str(launcher), *ARGS],
                            executable=py) == [py, str(launcher), *ARGS]
    # Without /proc: the launcher, found next to the interpreter.
    assert dr._relaunch_cmd(argv=["uvicorn", *ARGS], cmdline=[], executable=py) == [str(launcher), *ARGS]


def test_relaunch_python_app_py():
    assert dr._relaunch_cmd(argv=["app.py"], cmdline=[PY, "app.py"], executable=PY) == [PY, "app.py"]
    assert dr._relaunch_cmd(argv=["app.py"], cmdline=[], executable=PY) == [PY, "app.py"]
    # `python -c` cannot be repeated from /proc: falls back to sys.argv.
    assert dr._relaunch_cmd(argv=["app.py"], cmdline=[PY, "-c", "x"], executable=PY) == [PY, "app.py"]


def test_restart_plan(tmp_path):
    plan = dr._restart_plan(1234, "/tmp/x.log", cwd=str(tmp_path), cmd=[PY, "-m", "uvicorn", *ARGS])
    assert plan["port"] == 7000 and plan["cwd"] == str(tmp_path) and plan["pid"] == os.getpid()
    assert plan["fallback"][1:] == ["-m", "uvicorn", *ARGS]
    assert plan["check"][:2] == [PY, "-c"] and f"sys.path[0] = {str(tmp_path)!r}" in plan["check"][2]
    assert plan["check"][2].endswith("import uvicorn")
    plan = dr._restart_plan(7001, "/tmp/x.log", cwd=str(tmp_path), cmd=[PY, "app.py"])
    assert plan["fallback"][1:] == ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "7001"]
    # The command that crashed on 2026-10-11 is caught by its import check.
    bad = dr._import_check([sys.executable, MAIN], str(tmp_path), sys.executable)
    assert "sys.path[0] = '/srv/venv/lib/python3.12/site-packages/uvicorn'" in bad[2]
    compile(dr.RESTART_HELPER, "helper", "exec")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_helper(tmp_path, cmd=None, check=None):
    """Run the helper against a stand-in `sleep` "server" and an http.server
    on a free port, never the real server."""
    # Not our child, so it does not linger as a zombie once stopped.
    old = int(subprocess.run(["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
                             capture_output=True, text=True).stdout)
    port = _free_port()
    log = tmp_path / "server.log"
    serve = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]
    check = check or [sys.executable, "-c", "pass"]
    plan = {"pid": old, "cwd": str(tmp_path), "log": str(log), "port": port, "wait_s": 15,
            "cmd": cmd or serve, "check": check, "fallback": serve, "fallback_check": check}
    r = subprocess.run([sys.executable, "-I", "-c", dr.RESTART_HELPER, json.dumps(plan)], timeout=90)
    text = log.read_text() if log.exists() else ""
    m = re.search(r"\(pid (\d+)\)", text)
    if m:
        os.kill(int(m.group(1)), signal.SIGTERM)
    time.sleep(0.5)
    try:
        os.kill(old, signal.SIGKILL)
        stopped = False
    except OSError:
        stopped = True
    return r.returncode, text, stopped


def test_helper_restarts_and_waits_for_the_port(tmp_path):
    code, text, stopped = _run_helper(tmp_path)
    assert code == 0 and stopped and "server is back up" in text


def test_helper_falls_back_when_the_server_does_not_come_back(tmp_path):
    code, text, stopped = _run_helper(tmp_path, cmd=[sys.executable, "-c", "raise SystemExit(3)"])
    assert code == 0 and stopped
    assert "SERVER DID NOT COME BACK" in text and "trying the fallback" in text and "server is back up" in text


def test_helper_keeps_the_old_server_if_nothing_would_start(tmp_path):
    code, text, stopped = _run_helper(tmp_path, check=[sys.executable, "-c", "import no_such_module_x"])
    assert code == 1 and not stopped and "NOT RESTARTING" in text
