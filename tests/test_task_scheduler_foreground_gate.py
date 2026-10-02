"""Scheduled/background tasks must pause while the user is active and resume
once Odysseus goes idle again, so an interactive session always gets GPU/model
capacity first (see src/interactive_gate.py).

Two behaviors are covered:
  1. A task queued while the user is active waits ("Queued, waiting for
     Odysseus to be idle...") instead of starting immediately.
  2. A task that is already running gets cancelled the moment the user shows
     back up, and is rescheduled ~15 minutes out rather than treated as an
     error or a user-initiated stop.
A manually forced run (bypass_model_slot=True, i.e. the user's own "Run now")
is exempt from both: the user asked for it directly.
"""
import asyncio

import pytest
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


def _setup_db(tmp_path, monkeypatch):
    import core.database as cd

    base = declarative_base()

    class ScheduledTask(base):
        __tablename__ = "scheduled_tasks"

        id = Column(String, primary_key=True)
        owner = Column(String)
        name = Column(String)
        task_type = Column(String, default="action")
        action = Column(String)
        status = Column(String, default="active")
        trigger_type = Column(String, default="schedule")
        next_run = Column(DateTime)
        last_run = Column(DateTime)
        then_task_id = Column(String)
        output_target = Column(String)
        notifications_enabled = Column(String, default=True)
        run_count = Column(Integer, default=0)
        schedule = Column(String)
        scheduled_time = Column(String)
        scheduled_day = Column(String)
        scheduled_date = Column(String)
        cron_expression = Column(String)

    class TaskRun(base):
        __tablename__ = "task_runs"

        id = Column(String, primary_key=True)
        task_id = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        status = Column(String)
        result = Column(Text)
        error = Column(Text)
        model = Column(String)

    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(cd, "SessionLocal", session_local)
    monkeypatch.setattr(cd, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(cd, "TaskRun", TaskRun)
    return session_local, ScheduledTask, TaskRun


def _new_scheduler():
    from src.task_scheduler import TaskScheduler
    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._executing = set()
    scheduler._executing_lock = asyncio.Lock()
    scheduler._run_semaphore = asyncio.Semaphore(4)
    scheduler._task_handles = {}
    scheduler._concurrency_cap = 4
    scheduler._task_defer_counts = {}
    return scheduler


def test_running_task_is_cancelled_when_user_becomes_active(tmp_path, monkeypatch):
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)

    db = session_local()
    db.add(ScheduledTask(
        id="bg-task", owner="alice", name="BG Task", task_type="action",
        action="some_housekeeping", status="active", trigger_type="schedule",
    ))
    db.commit()
    db.close()

    from src.task_scheduler import TaskScheduler
    import src.interactive_gate as ig

    async def _noop_wait(label=""):
        return False
    monkeypatch.setattr(ig, "wait_for_interactive_quiet", _noop_wait)

    became_active = {"flag": False}
    monkeypatch.setattr(ig, "has_foreground_activity", lambda now=None: became_active["flag"])

    async def fake_execute_action(self, task, run_id=None):
        await asyncio.sleep(10)
        return "done", True
    monkeypatch.setattr(TaskScheduler, "_execute_action", fake_execute_action)

    async def _noop_deliver(self, task, result, db, model=None):
        return None
    monkeypatch.setattr(TaskScheduler, "_deliver_task_result", _noop_deliver)

    async def drive():
        scheduler = _new_scheduler()
        task = asyncio.create_task(scheduler._execute_task("bg-task"))
        # Let it reach "running" and spin up the foreground monitor.
        await asyncio.sleep(0.1)
        became_active["flag"] = True
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())

    db = session_local()
    try:
        run = db.query(TaskRun).filter(TaskRun.task_id == "bg-task").first()
        assert run.status == "aborted"
        assert run.error == "Paused because Odysseus became active"
        task_row = db.query(ScheduledTask).filter(ScheduledTask.id == "bg-task").first()
        assert task_row.next_run is not None, "task must be rescheduled, not dropped"
    finally:
        db.close()


def test_forced_run_now_is_not_cancelled_by_foreground_activity(tmp_path, monkeypatch):
    """bypass_model_slot=True (the user's own 'Run now') must not pause/cancel
    on its own account: the user asked for this run directly."""
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)

    db = session_local()
    db.add(ScheduledTask(
        id="forced-task", owner="alice", name="Forced Task", task_type="action",
        action="some_housekeeping", status="active", trigger_type="schedule",
    ))
    db.commit()
    db.close()

    from src.task_scheduler import TaskScheduler
    import src.interactive_gate as ig

    # If the gate were (incorrectly) consulted for a forced run, this would
    # make wait_for_interactive_quiet/has_foreground_activity block or cancel
    # it; fail loudly so the test can't pass by accident.
    async def _blow_up_wait(label=""):
        raise AssertionError("forced run must not wait on the foreground gate")
    monkeypatch.setattr(ig, "wait_for_interactive_quiet", _blow_up_wait)
    monkeypatch.setattr(ig, "has_foreground_activity", lambda now=None: True)

    async def fake_execute_action(self, task, run_id=None):
        await asyncio.sleep(0.05)
        return "done", True
    monkeypatch.setattr(TaskScheduler, "_execute_action", fake_execute_action)

    async def _noop_deliver(self, task, result, db, model=None):
        return None
    monkeypatch.setattr(TaskScheduler, "_deliver_task_result", _noop_deliver)

    async def drive():
        scheduler = _new_scheduler()
        await scheduler._execute_task("forced-task", bypass_model_slot=True, release_executing=False)

    asyncio.run(drive())

    db = session_local()
    try:
        run = db.query(TaskRun).filter(TaskRun.task_id == "forced-task").first()
        assert run.status == "success"
    finally:
        db.close()


def test_queued_task_waits_for_the_quiet_window(tmp_path, monkeypatch):
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)

    db = session_local()
    db.add(ScheduledTask(
        id="queued-task", owner="alice", name="Queued Task", task_type="action",
        action="some_housekeeping", status="active", trigger_type="schedule",
    ))
    db.commit()
    db.close()

    from src.task_scheduler import TaskScheduler
    import src.interactive_gate as ig

    release = asyncio.Event()

    async def _wait_until_released(label=""):
        await release.wait()
        return True
    monkeypatch.setattr(ig, "wait_for_interactive_quiet", _wait_until_released)
    monkeypatch.setattr(ig, "has_foreground_activity", lambda now=None: False)

    async def fake_execute_action(self, task, run_id=None):
        return "done", True
    monkeypatch.setattr(TaskScheduler, "_execute_action", fake_execute_action)

    async def _noop_deliver(self, task, result, db, model=None):
        return None
    monkeypatch.setattr(TaskScheduler, "_deliver_task_result", _noop_deliver)

    async def drive():
        scheduler = _new_scheduler()
        task = asyncio.create_task(scheduler._execute_task("queued-task"))
        await asyncio.sleep(0.1)
        assert not task.done(), "task must not run while the gate is waiting"
        db2 = session_local()
        try:
            run = db2.query(TaskRun).filter(TaskRun.task_id == "queued-task").first()
            assert "waiting for Odysseus to be idle" in (run.result or "")
        finally:
            db2.close()
        release.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())

    db = session_local()
    try:
        run = db.query(TaskRun).filter(TaskRun.task_id == "queued-task").first()
        assert run.status == "success"
    finally:
        db.close()
