"""`opencode run` and its event stream with opencode 1.x and 2.x.

2.x has no --dir (the folder is the working directory) and no in-process server: Odysseus points it at the shared
server with --server and its login. Which flags to use is read from the installed CLI's `--version`, so the same code
runs before and after an upgrade. tests/fixtures/opencode_v2_run.jsonl is real 2.4.2 `run --format json` output
(a text reply, then a shell tool call), recorded against a staging server with a fake model.
"""
import json
import os
import stat
from pathlib import Path

import pytest

from src.agent_tools import claude_code_tool as cct
from src import transcript_summaries as ts
from src import devops_stats

FIX = Path(__file__).resolve().parent / "fixtures" / "opencode_v2_run.jsonl"


def _fake_cli(tmp_path, version):
    f = tmp_path / f"opencode-{version}"
    f.write_text(f"#!/bin/sh\necho '{version}'\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return str(f)


@pytest.fixture(autouse=True)
def _clear():
    cct._OC_MAJOR.clear()
    yield
    cct._OC_MAJOR.clear()


def test_1x_run_keeps_its_dir_flag(tmp_path):
    cli = _fake_cli(tmp_path, "1.19.6")
    assert cct.opencode_major(cli) == 1
    assert cct.opencode_run_cmd(cli, "/w", "build", auto=True) == [cli, "run", "--format", "json", "--dir", "/w",
                                                                   "--agent", "build"]
    env = cct.opencode_run_env({}, cli)
    assert "OPENCODE_SERVER_PASSWORD" not in env


def test_2x_run_goes_to_the_shared_server_with_its_login(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "opencode v2.4.2")
    monkeypatch.setenv("ODYSSEUS_OPENCODE_URL", "http://127.0.0.1:4096")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw-x")
    assert cct.opencode_major(cli) == 2
    assert cct.opencode_run_cmd(cli, "/w", "build", auto=True) == [
        cli, "run", "--format", "json", "--server", "http://127.0.0.1:4096", "--agent", "build", "--auto"]
    assert "--auto" not in cct.opencode_run_cmd(cli, "/w", "plan")
    assert "--dir" not in cct.opencode_run_cmd(cli, "/w", "plan")
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD")
    f = tmp_path / "server.env"
    f.write_text("OPENCODE_SERVER_USERNAME=opencode\nOPENCODE_SERVER_PASSWORD=file-pw\n")
    monkeypatch.setenv("ODYSSEUS_OPENCODE_SERVER_ENV", str(f))
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    env = cct.opencode_run_env({}, cli)
    assert env["OPENCODE_SERVER_PASSWORD"] == "file-pw" and env["OPENCODE_SERVER_USERNAME"] == "opencode"


def test_the_version_is_read_again_when_the_binary_changes(tmp_path):
    cli = _fake_cli(tmp_path, "1.19.6")
    assert cct.opencode_major(cli) == 1
    Path(cli).write_text("#!/bin/sh\necho 'opencode v2.4.2'\n")
    os.utime(cli, (1, 2_000_000_000))
    assert cct.opencode_major(cli) == 2


def test_a_missing_or_silent_cli_counts_as_1x(tmp_path):
    assert cct.opencode_major(str(tmp_path / "nope")) == 1
    assert cct.opencode_major(_fake_cli(tmp_path, "garbage")) == 1


def _v2_events():
    return [json.loads(l) for l in FIX.read_text().splitlines() if l.strip()]


def test_the_console_summarizes_2x_text_and_tool_events():
    lines = [cct._summarize_opencode(e) for e in _v2_events()]
    assert "ok" in lines
    assert any(l and l.startswith("● shell(curl -d x https://example.invalid") for l in lines)
    s = cct._Stream("opencode", "")
    for e in _v2_events():
        s.feed(json.dumps(e))
    assert s.session_id.startswith("ses_") and s.turns == 3 and s.last_stop_reason == "stop"


def test_the_transcript_and_devops_read_a_2x_run(tmp_path):
    (tmp_path / "out.jsonl").write_text(FIX.read_text())
    assert ts._detect_engine(_v2_events()) == "opencode"
    summary = ts.parse_run(str(tmp_path))
    kinds = [b["kind"] for b in summary["blocks"]]
    assert "tool_call" in kinds and "text" in kinds and summary["status"] == "done"
    info = devops_stats._parse_run_log(str(tmp_path))
    assert info["engine"] == "opencode" and info["status"] == "done"
