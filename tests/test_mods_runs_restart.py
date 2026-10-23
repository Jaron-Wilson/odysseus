"""Agent runs survive a restart of the core (src/mods/runs.py persist/recover):
a run that was live is marked interrupted and its mod is told once."""
import json
import os
import time

from src.mods import events, runs
from tests.mods_helpers import add_mod, mods_dir  # noqa: F401


def _run(mod_id, status, **kw):
    r = runs.ModRun(mod_id, "alice")
    r.status, r.session_id, r.scope, r.metadata = status, "sess-" + status, "c/1/a/2", {"issue": "JAR-1"}
    r.tools = [{"tool": "x"}] * 3
    for k, v in kw.items():
        setattr(r, k, v)
    runs._RUNS[r.id] = r
    return r


def test_live_runs_are_interrupted_and_the_mod_is_told(mods_dir, monkeypatch):
    add_mod(mod_id="company", permissions=["agent.run"])
    runs._RUNS.clear()
    live = _run("company", "running")
    queued = _run("company", "queued")
    done = _run("company", "succeeded", finished=time.time())
    runs.persist()
    assert os.path.exists(runs._store_path())
    runs._RUNS.clear()                                   # the process ended
    sent = []
    monkeypatch.setattr(events, "send_to", lambda mod, typ, data: sent.append((mod, typ, data)))
    told = runs.recover()
    assert {r.id for r in told} == {live.id, queued.id}
    for rid in (live.id, queued.id):
        r = runs._RUNS[rid]
        assert r.status == "interrupted" and r.error == "Odysseus restarted" and r.finished
        v = r.view(full=False)
        assert v["restarted"] is True and v["metadata"] == {"issue": "JAR-1"} and v["session_id"] == r.session_id
    assert runs._RUNS[done.id].status == "succeeded" and "restarted" not in runs._RUNS[done.id].view()
    assert runs._RUNS[live.id].view()["tool_calls"] == 3
    assert len(sent) == 2 and all(m == "company" and t == "agent.run.finished" and d["restarted"] for m, t, d in sent)
    # Only once: a second start does not repeat the notice.
    runs._RUNS.clear()
    sent.clear()
    assert runs.recover() == [] and sent == []
    assert runs._RUNS[live.id].status == "interrupted"


def test_a_graceful_restart_notice_that_never_arrived_is_sent_at_start(mods_dir, monkeypatch):
    add_mod(mod_id="company", permissions=["agent.run"])
    runs._RUNS.clear()
    r = _run("company", "interrupted", error="Odysseus restarted", finished=time.time(), restarted=True)
    runs.persist()
    runs._RUNS.clear()
    sent = []
    monkeypatch.setattr(events, "send_to", lambda mod, typ, data: sent.append(data))
    assert [x.id for x in runs.recover()] == [r.id] and sent[0]["status"] == "interrupted"


def test_a_missing_or_broken_file_is_no_runs(mods_dir):
    runs._RUNS.clear()
    path = runs._store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    assert runs.recover() == []
    with open(path, "w") as f:
        f.write("{not json")
    assert runs.recover() == []
    with open(path, "w") as f:
        json.dump({"runs": ["junk", {"id": 1}]}, f)
    assert runs.recover() == []
