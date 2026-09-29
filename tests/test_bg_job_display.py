"""The Background panel's job rows read as the task, and a finished run says so.

Seen 2026-09-29, a finished plan run in the panel:
  "Carry out the approved plan. --- Status for the user --- Keep the user
   posted while you work ... done · in chat · 2m 56s
   ● working · Creating ci-previews worktree off origin/main"
The title was our own progress instructions to the agent, and a done run
still showed its last "working" line.
"""
from src import claude_code_jobs as jobs
from src.agent_tools.claude_code_tool import STATUS_INSTRUCTIONS


def test_the_title_is_the_task_without_the_status_instructions():
    assert jobs.display_prompt("Carry out the approved plan." + STATUS_INSTRUCTIONS) == "Carry out the approved plan."
    assert jobs.display_prompt("Fix the login bug") == "Fix the login bug"
    assert jobs.display_prompt("") == ""


def test_a_finished_run_drops_its_last_working_line():
    working = {"state": "working", "detail": "Creating ci-previews worktree off origin/main"}
    assert jobs.shown_status("running", working) == working
    assert jobs.shown_status("done", working) is None
    assert jobs.shown_status("failed", {"state": "needs_input", "detail": "Which branch?"}) is None
    done = {"state": "done", "detail": "Previews live on 3 branches"}
    assert jobs.shown_status("done", done) == done
    assert jobs.shown_status("done", None) is None


def test_the_panel_gets_both():
    src = open(jobs.__file__, encoding="utf-8").read()
    assert '"prompt": display_prompt(self.prompt)' in src
    assert '"agent_status": shown_status(self.status, self.agent_status)' in src
