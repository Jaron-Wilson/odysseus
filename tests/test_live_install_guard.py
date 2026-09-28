"""The agent cannot change the running Odysseus install; features for
Odysseus are built in its copy under ~/odysseus-data/workspaces/odysseus and
delivered as a PR.

Asked on 2026-09-28: "will this allow for modifying the actual odysseus or
no? like if I want another feature".
"""
import asyncio
import json
import os

import pytest

from src import tool_execution as te
from src.agent_tools import ToolBlock

R = te._ODYSSEUS_ROOT
H = os.path.expanduser("~")
T = ("~" + R[len(H):]) if R.startswith(H + "/") else R

BLOCKED = [
    ("bash", f"git -C {R} commit -am 'x'"),
    ("bash", f"git -C {R} push origin dev"),
    ("bash", f"cd {R} && git checkout -b feature"),
    ("bash", f"cd {R}; git pull"),
    ("bash", f"cd {T} && git reset --hard"),
    ("bash", f"echo hi > {R}/app.py"),
    ("bash", f"cat new.py >> {R}/src/x.py"),
    ("bash", f"sed -i 's/a/b/' {R}/app.py"),
    ("bash", f"cp patch.py {R}/src/"),
    ("bash", f"rm -rf {R}/static"),
    ("bash", f"echo x | tee {R}/README.md"),
    ("write_file", json.dumps({"path": f"{R}/src/new.py", "content": "x"})),
    ("edit_file", json.dumps({"file_path": f"{R}/app.py", "old": "a", "new": "b"})),
    ("write_file", f"{R}/src/new.py\nprint(1)"),
]
ALLOWED = [
    ("bash", f"cat {R}/app.py"),
    ("bash", f"grep -rn TODO {R}/src | head"),
    ("bash", f"git -C {R} log --oneline -3"),
    ("bash", f"git -C {R} status"),
    ("bash", f"cd {R} && git diff"),
    ("bash", f"cp {R}/app.py /tmp/app.py"),                       # reading out of it
    ("bash", f"echo x > {H}/odysseus-data/workspaces/odysseus/app.py"),
    ("bash", f"git -C {H}/odysseus-data/workspaces/odysseus commit -am 'feat: x'"),
    ("write_file", json.dumps({"path": f"{H}/odysseus-data/workspaces/odysseus/app.py", "content": "x"})),
    ("bash", "ls ~/odysseus-data/logs"),
]


@pytest.mark.parametrize("tool,cmd", BLOCKED)
def test_changes_to_the_live_install_are_refused(tool, cmd):
    assert te._live_install_change(tool, cmd), cmd
    desc, res = asyncio.run(te.execute_tool_block(ToolBlock(tool, cmd), session_id="c1"))
    assert desc.endswith("refused") and "workspaces" in res["error"]


@pytest.mark.parametrize("tool,cmd", ALLOWED)
def test_reading_it_and_working_in_the_copy_are_fine(tool, cmd):
    assert not te._live_install_change(tool, cmd), cmd


def test_the_agent_is_told_how_to_build_an_odysseus_feature():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    loop = open(os.path.join(here, "src", "agent_loop.py"), encoding="utf-8").read()
    assert "To build a feature for Odysseus ITSELF" in loop and "gh pr create" in loop
    assert "Do not merge it and do not push to dev or main" in loop
