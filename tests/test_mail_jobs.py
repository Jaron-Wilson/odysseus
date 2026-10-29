"""Job buttons on Inbound mail (src/mail_jobs.py): CleverNode submission emails
are fixed with a click, not a chat, and jobs a restart cut off are continued.
Asked for 2026-10-29."""
import json
import os
import sys
import textwrap
import time

import pytest

from src import mail_jobs, mail_listener

KEY = "001790000000000-11111111-2222-3333-4444-555555555555.eml"
B1 = "ee33c980-89a0-43f3-b782-c884e513cdd9"
B2 = "f3555a05-0000-4000-8000-000000000001"

# A stand-in for tools/fix_submission.py: keeps its jobs in jobs.json, logs every call.
RUNNER = textwrap.dedent("""
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    db = os.path.join(here, "jobs.json")
    jobs = json.load(open(db)) if os.path.exists(db) else []
    open(os.path.join(here, "calls.jsonl"), "a").write(json.dumps(sys.argv[1:]) + "\\n")
    cmd, args = sys.argv[1], [a for a in sys.argv[2:] if a != "--json"]
    def save(): json.dump(jobs, open(db, "w"))
    if cmd == "start":
        jobs.append({"job": "a1b2c3d4", "state": "working", "batches": args, "started": 1})
        save(); print(json.dumps({"job": "a1b2c3d4", "state": "working"}))
    elif cmd == "status":
        ids = args[args.index("--batch") + 1:]
        print(json.dumps([j for j in jobs if set(j["batches"]) & set(ids)]))
    elif cmd in ("resume", "drop", "publish"):
        j = next(j for j in jobs if j["job"] == args[0])
        j["state"] = {"resume": "working", "drop": "dropped", "publish": "published"}[cmd]
        save(); print(json.dumps({"job": args[0], "state": j["state"]}) if cmd == "resume" else "ok: " + cmd)
""")


def eml(frm, body):
    return (f"From: CleverNode <{frm}>\r\nTo: submissions@clevernode.org\r\nSubject: CleverNode: 2 new pages\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode()


BODY = (f"Run this one command, and do not do the work yourself:\n"
        f"  python3 /home/x/Canvas-Gemini-Extension/tools/fix_submission.py start {B1} {B2}\n"
        f"Then run the `wait` command it prints.")


@pytest.fixture
def env(tmp_path, monkeypatch):
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    mail_jobs._publishing.clear()
    tools = tmp_path / "tools"
    tools.mkdir()
    runner = tools / "fix_submission.py"
    runner.write_text(RUNNER)

    def setup(frm="alerts@clevernode.org", body=BODY, with_runner=True, allow=("alerts@clevernode.org",)):
        rule = {"address": "submissions@clevernode.org", "action": "notify", "instructions": "",
                "from_allow": list(allow)}
        if with_runner:
            rule["runner"] = str(runner)
        mail_listener.save_config({"enabled": True, "rules": [rule]})
        with open(os.path.join(mail_listener._inbox_dir(), KEY), "wb") as f:
            f.write(eml(frm, body))
        mail_listener._write_json(mail_listener._log_path(), [
            {"key": KEY, "received": time.time(), "from": f"CleverNode <{frm}>", "to": ["submissions@clevernode.org"],
             "subject": "CleverNode: 2 new pages", "rule": "submissions@clevernode.org", "action": "in Inbound mail"}])
        return tools

    return setup


def calls(tools):
    return [json.loads(line) for line in (tools / "calls.jsonl").read_text().splitlines()]


def test_fix_it_starts_the_runner_with_the_ids_from_the_email(env):
    tools = env()
    assert mail_listener.read_message(KEY)["has_runner"] is True
    assert mail_jobs.jobs(KEY) == {"runner": True, "batches": [B1, B2], "jobs": [], "hidden_failed": 0}
    assert mail_jobs.start(KEY)["job"] == "a1b2c3d4"
    assert ["start", B1, B2, "--json"] in calls(tools)
    assert [j["job"] for j in mail_jobs.jobs(KEY)["jobs"]] == ["a1b2c3d4"]


def test_resume_drop_and_publish_only_touch_this_emails_jobs(env):
    tools = env()
    mail_jobs.start(KEY)
    jobs = json.loads((tools / "jobs.json").read_text())
    jobs[0]["state"] = "interrupted"
    jobs.append({"job": "ffff0000", "state": "done", "batches": ["someone-elses"], "started": 2})
    (tools / "jobs.json").write_text(json.dumps(jobs))
    assert mail_jobs.resume(KEY, "a1b2c3d4")["state"] == "working"
    with pytest.raises(ValueError, match="not one of this email's"):
        mail_jobs.drop(KEY, "ffff0000")
    with pytest.raises(ValueError, match="bad job id"):
        mail_jobs.resume(KEY, "../../etc")
    with pytest.raises(ValueError, match="not ready to publish"):
        mail_jobs.publish(KEY, "a1b2c3d4")       # it is working, not done
    jobs = json.loads((tools / "jobs.json").read_text())
    jobs[0]["state"] = "done"
    (tools / "jobs.json").write_text(json.dumps(jobs))
    mail_jobs.publish(KEY, "a1b2c3d4")
    for _ in range(100):
        if mail_jobs._publishing["a1b2c3d4"]["state"] != "running":
            break
        time.sleep(0.05)
    assert mail_jobs._publishing["a1b2c3d4"]["state"] == "done"
    assert "ok: publish" in mail_jobs._publishing["a1b2c3d4"]["output"]
    assert not any(c[:2] == ["drop", "ffff0000"] for c in calls(tools))


def test_many_identical_failures_show_as_one(env):
    tools = env()
    rows = [{"job": f"{i:08x}", "state": "failed", "batches": [B1], "started": i} for i in range(148)]
    rows.append({"job": "aaaabbbb", "state": "interrupted", "batches": [B1], "started": 500})
    (tools / "jobs.json").write_text(json.dumps(rows))
    d = mail_jobs.jobs(KEY)
    assert [j["state"] for j in d["jobs"]] == ["interrupted", "failed"] and d["hidden_failed"] == 147


def test_no_buttons_without_a_runner_an_allowed_sender_or_ids(env):
    env(with_runner=False)
    assert mail_jobs.jobs(KEY)["runner"] is False
    assert mail_listener.read_message(KEY)["has_runner"] is False
    env(frm="stranger@example.com")
    assert "not an allowed sender" in mail_jobs.jobs(KEY)["reason"]
    with pytest.raises(ValueError):
        mail_jobs.start(KEY)
    env(body="start ; rm -rf / and start not-a-uuid")
    assert "no batch ids" in mail_jobs.jobs(KEY)["reason"]


def test_the_rule_keeps_a_runner_that_exists(env, tmp_path):
    tools = env()
    rules = mail_listener.clean_rules([{"address": "submissions@clevernode.org", "action": "notify",
                                        "runner": str(tools / "fix_submission.py")}])
    assert rules[0]["runner"] == str(tools / "fix_submission.py")
    for bad in ("fix_submission.py", str(tmp_path / "missing.py"), sys.executable):
        with pytest.raises(ValueError, match="job runner"):
            mail_listener.clean_rules([{"address": "a@b.org", "action": "notify", "runner": bad}])
