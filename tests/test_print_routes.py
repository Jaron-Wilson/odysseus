"""The 3D print helper's API (routes/print_routes.py): upload, background analysis and fix in the niced worker
process, the chat with a (faked) local model, decisions, files, and admin-only access."""
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("manifold3d")
pytest.importorskip("matplotlib")

from routes import print_routes  # noqa: E402
from src.print_helper import assistant, jobs  # noqa: E402
from tests.test_print_helper import write_box_stl  # noqa: E402


class _Auth:
    is_configured = True

    def __init__(self, admins):
        self.admins = admins

    def is_admin(self, user):
        return user in self.admins


def _app(user=None):
    app = FastAPI()
    app.state.auth_manager = _Auth({"jaron"})

    @app.middleware("http")
    async def _who(request, call_next):
        request.state.current_user = user
        return await call_next(request)

    app.include_router(print_routes.setup_print_routes())
    return app


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    # the worker process reads ODYSSEUS_DATA_DIR; this process reads src.constants.DATA_DIR
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    jobs._queue.clear()
    jobs._state.update(runner=None, proc=None, job=None)
    return tmp_path


@pytest.fixture
def stl(tmp_path):
    p = tmp_path / "hollow box.stl"
    write_box_stl(str(p))
    return p


def _wait(c, job_id, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/print/jobs/{job_id}").json()
        # done when the worker says so and the queue has reaped it (no subprocess left for the loop to close)
        if j["status"]["state"] not in ("new", "queued", "running") and jobs._state.get("job") is None:
            return j
        time.sleep(0.3)
    raise AssertionError(f"job still {j['status']}")


def test_only_an_admin_gets_in(data_dir, stl):
    for user in (None, "guest"):
        c = TestClient(_app(user))
        assert c.get("/api/print/jobs").status_code == 403
        assert c.get("/api/print/status").status_code == 403
        with open(stl, "rb") as f:
            assert c.post("/api/print/jobs", files={"file": ("a.stl", f)}).status_code == 403
        assert c.put("/api/print/config", json={"pc_host": "evil"}).status_code == 403
    assert not (data_dir / "print-helper" / "jobs").exists()


def test_upload_analyze_chat_fix_and_download(data_dir, stl, monkeypatch):
    with TestClient(_app("jaron")) as c:
        assert c.post("/api/print/jobs", files={"file": ("model.obj", b"x" * 100)}).status_code == 400
        with open(stl, "rb") as f:
            r = c.post("/api/print/jobs", files={"file": ("../hollow box.stl", f)})
        assert r.status_code == 200, r.text
        job = r.json()
        assert job["file"] == "hollow_box.stl"
        job = _wait(c, job["id"])
        assert job["status"]["state"] == "done", job["status"]
        (part,) = job["analysis"]["parts"]
        assert part["sockets"] == 2 and part["hollow_cm3"] > 14
        png = c.get(f"/api/print/jobs/{job['id']}/files/{part['render']}")
        assert png.status_code == 200 and png.content[:4] == b"\x89PNG"

        # the local model asks, then picks options and runs; the geometry is done by the tools
        replies = iter([
            '{"say": "Keep the stud socket and the side hole open?", "options": {}, "run": false}',
            '<think>ok</think>{"say": "Keeping them open and roofing the hollow. Running it.", '
            '"options": {"keep_holes": true, "hollows": "roof", "scale_percent": 50, "slice_check": false}, "run": true}',
        ])
        seen = []

        async def fake_llm(url, model, messages, **kw):
            seen.append(messages)
            return next(replies)
        monkeypatch.setattr(assistant, "candidates", lambda owner=None, now=None: (
            [{"url": "http://127.0.0.1:9/v1/chat/completions", "model": "qwen3.8:27b", "headers": {},
              "endpoint": "test", "endpoint_id": "t"}], []))
        import src.llm_core
        monkeypatch.setattr(src.llm_core, "llm_call_async", fake_llm)
        r1 = c.post(f"/api/print/jobs/{job['id']}/chat", json={"message": ""}).json()
        assert r1["say"].startswith("Keep the stud socket") and not r1["run"]
        assert "hollow inside" in seen[0][0]["content"]          # the analysis is the model's context
        r2 = c.post(f"/api/print/jobs/{job['id']}/chat", json={"message": "yes keep them, half size, no slicing"}).json()
        assert r2["run"] and r2["options"]["scale_percent"] == 50 and r2["status"]["state"] == "queued"
        job = _wait(c, job["id"])
        assert job["status"]["state"] == "done", job["status"]
        (piece,) = job["result"]["pieces"]
        assert piece["after"]["unsupported_cm2"] < 0.3
        assert job["result"]["slice"] is None
        stl_out = c.get(f"/api/print/jobs/{job['id']}/files/{piece['file']}")
        assert stl_out.status_code == 200 and "attachment" in stl_out.headers["content-disposition"]
        whats = [(d["source"], d["what"]) for d in job["decisions"]]
        assert ("assistant", "options") in whats and ("assistant", "run") in whats and ("user", "upload") in whats
        assert [m["role"] for m in job["chat"]] == ["assistant", "user", "assistant"]

        # by hand: options are checked, logged, and a run uses them
        r = c.put(f"/api/print/jobs/{job['id']}/options", json={"options": {"hollows": "fill", "scale_percent": 9999}})
        assert r.json()["options"]["scale_percent"] == 1000 and r.json()["changed"]["hollows"] == "fill"
        assert c.get(f"/api/print/jobs/{job['id']}/files/..%2Fmeta.json").status_code == 404
        assert c.get(f"/api/print/jobs/{job['id']}/files/meta.json").status_code == 404
        assert c.get("/api/print/jobs/0123456789abcdef").status_code == 404
        assert c.get("/api/print/jobs/../../etc").status_code == 404
        assert c.delete(f"/api/print/jobs/{job['id']}").json() == {"ok": True}
        assert c.get("/api/print/jobs").json() == {"jobs": []}


def test_without_a_local_model_the_questions_come_as_a_form(data_dir, stl, monkeypatch):
    monkeypatch.setattr(assistant, "candidates", lambda owner=None, now=None: ([], ["no local endpoints"]))
    with TestClient(_app("jaron")) as c:
        with open(stl, "rb") as f:
            job = c.post("/api/print/jobs", files={"file": ("box.stl", f)}).json()
        _wait(c, job["id"])
        out = c.post(f"/api/print/jobs/{job['id']}/chat", json={"message": ""}).json()
        assert "Keep the sockets and holes open" in out["say"] and out["model"] is None and not out["run"]


def test_settings_are_checked(data_dir):
    c = TestClient(_app("jaron"))
    assert c.put("/api/print/config", json={"pc_host": "bad host; rm -rf"}).status_code == 400
    assert c.put("/api/print/config", json={"ssh_user": "a b"}).status_code == 400
    r = c.put("/api/print/config", json={"pc_host": "desktop-jaron", "profiles_dir": "C:\\Users\\me\\profiles"})
    assert r.status_code == 200 and r.json()["pc_host"] == "desktop-jaron"
    assert json.loads((data_dir / "print-helper" / "config.json").read_text())["profiles_dir"] == "C:\\Users\\me\\profiles"


def test_the_worker_runs_niced(data_dir):
    argv = jobs.worker_argv("/x", "fix")
    assert argv[1:4] == ["-m", "src.print_helper.worker", "/x"] and argv[-1] == str(jobs.NICE)
