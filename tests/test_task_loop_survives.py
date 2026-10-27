"""The worker's task loop survives whatever happens after a task. A running task that was
cancelled used to end it (`runs` was undefined there), which also stopped the waking of
waiting tasks: the worker still answered, but no task ran or woke again until a restart."""
import asyncio
import logging
from types import SimpleNamespace

from agentic_lab.worker import tasks as T
from agentic_lab.worker.server import run_tasks
from agentic_lab.worker.service import loop_ended


class CancelledRunner:
    """Like the real runner: a task it's told to stop ends as cancelled."""

    def __init__(self):
        self.seen = []

    async def run(self, task, store):
        self.seen.append(task.instruction)
        if task.instruction == "long":
            while not store.get(task.id).cancel_requested:
                await asyncio.sleep(0.01)
            store.update(task.id, state=T.CANCELLED)
            return
        store.update(task.id, state=T.DONE, result={"summary": "ok", "artifacts": []})


class Runs:
    def __init__(self, fail=False):
        self.stopped, self.fail = [], fail

    def active(self, task_id):
        if self.fail:
            raise RuntimeError("systemctl hung up")
        return [{"id": f"r-of-{task_id}"}]

    def stop(self, run_id):
        self.stopped.append(run_id)


def drive(store, runner, runs, cancel_first=True):
    async def go():
        stop = asyncio.Event()
        loop = asyncio.create_task(run_tasks(store, runner, stop, runs))
        long = next(t for t in store.all() if t.instruction == "long")
        while store.get(long.id).state != T.RUNNING:
            await asyncio.sleep(0.01)
        if cancel_first:
            store.request_cancel(long.id)
        for _ in range(500):
            if all(t.state in T.TERMINAL for t in store.all()):
                break
            await asyncio.sleep(0.01)
        stop.set()
        await loop
        return long.id
    return asyncio.run(go())


def test_cancelling_a_running_task_stops_its_runs_and_the_next_task_still_runs(tmp_path):
    store = T.TaskStore(tmp_path)
    store.submit("long", "alice", "mgr-alice")
    store.submit("after", "alice", "mgr-alice")
    runner, runs = CancelledRunner(), Runs()
    long_id = drive(store, runner, runs)
    assert runner.seen == ["long", "after"]
    assert store.get(long_id).state == T.CANCELLED
    assert runs.stopped == [f"r-of-{long_id}"]
    assert {t.instruction: t.state for t in store.all()}["after"] == T.DONE


def test_a_failure_after_a_task_is_logged_and_the_loop_goes_on(tmp_path, caplog):
    store = T.TaskStore(tmp_path)
    store.submit("long", "alice", "mgr-alice")
    store.submit("after", "alice", "mgr-alice")
    runner = CancelledRunner()
    with caplog.at_level(logging.ERROR, "agentic_lab.worker"):
        drive(store, runner, Runs(fail=True))
    assert runner.seen == ["long", "after"]
    assert "the task loop failed" in caplog.text


def test_waiting_tasks_are_still_woken_after_a_cancelled_task(tmp_path):
    """The housekeeping loop (waking waiting tasks) outlives a cancelled running task."""
    store = T.TaskStore(tmp_path)
    store.submit("long", "alice", "mgr-alice")
    parked = store.submit("parked", "alice", "mgr-alice")
    store.update(parked.id, state=T.RUNNING)
    store.update(parked.id, state=T.WAITING, waiting={"runs": ["r-gone"]})
    runs = Runs()
    runs.view = lambda rid: {"state": "exited"}
    runner = CancelledRunner()
    drive(store, runner, runs)
    assert "parked" in runner.seen          # woken (its run ended), then run to the end
    assert store.get(parked.id).state == T.DONE


def test_a_task_loop_that_ends_while_serving_restarts_the_worker(caplog):
    async def boom():
        raise NameError("runs")

    async def go():
        t = asyncio.create_task(boom())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return t
    task = asyncio.run(go())
    server, stop = SimpleNamespace(should_exit=False), asyncio.Event()
    with caplog.at_level(logging.ERROR, "agentic_lab.worker"):
        loop_ended(task, stop, server)
    assert server.should_exit is True
    assert "NameError: runs" in caplog.text


def test_a_task_loop_that_ends_on_shutdown_is_fine():
    server, stop = SimpleNamespace(should_exit=False), asyncio.Event()
    stop.set()
    loop_ended(SimpleNamespace(cancelled=lambda: True), stop, server)
    assert server.should_exit is False
