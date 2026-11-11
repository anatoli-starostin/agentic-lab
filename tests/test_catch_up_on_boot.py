"""A host that missed a deploy while it was off is caught up as the secretary powers it on,
before it takes work: a manager usually powers a GPU host on for a task and off right after
it, so it's busy all the time it's up and the usual catch-up never reaches it."""
import asyncio
import datetime as dt
from pathlib import Path

import pytest

from agentic_lab import registry
from agentic_lab.secretary import catchup as CU
from agentic_lab.secretary import power as P
from agentic_lab.worker import tasks as T
from agentic_lab.worker.server import Access, Caller

from test_catch_up import NEW, NOW, OLD, Deploys, history, lab, state  # noqa: F401
from test_dedicated_power_off import power, setup
from test_worker import TOK_A, TOK_B, _call, _with_server


def seen_then_off(tmp_path, applied=OLD):
    """gpu-a was last seen running `applied`, and is off now."""
    cu = CU.CatchUp(str(tmp_path / "cu.json"))
    asyncio.run(CU.step(cu, lab(auto_catch_up=False), state(applied), "sec-host",
                        None, None, None, now=NOW))
    return cu, state(applied, up=False)


def test_what_a_host_ran_is_remembered_while_it_is_off(tmp_path):
    cu, off = seen_then_off(tmp_path)
    assert CU.missed(cu, off, "sec-host", "gpu-a", None)
    assert CU.CatchUp(str(tmp_path / "cu.json")).seen["gpu-a"]["config_commit"] == OLD
    cu, off = seen_then_off(tmp_path / "2", applied=NEW)
    assert not CU.missed(cu, off, "sec-host", "gpu-a", None)          # current
    assert CU.missed(cu, off, "sec-host", "gpu-a", None, keys={"gpu-a": "other"})
    assert not CU.missed(cu, off, "sec-host", "gpu-never-seen", None)


class Boot:
    """Workers that answer after `down` polls, with `tasks`; a deploy that may be busy."""

    def __init__(self, tmp, down=2, tasks=None, deploys_busy=0):
        self.polls, self.down, self.tasks = 0, down, tasks or {}
        self.held, self.released, self.slept = [], 0, 0
        self.deploys, self.busy_left = Deploys(tmp), deploys_busy

    async def hold(self, aid, seconds):
        self.polls += 1
        if self.polls <= self.down:
            raise RuntimeError("connect timeout")
        self.held.append((aid, seconds))
        return {"tasks": self.tasks}

    async def release(self):
        self.released += 1

    def start(self):
        if self.busy_left:
            self.busy_left -= 1
            raise RuntimeError("a deploy is already running")
        return self.deploys.start()

    async def sleep(self, s):
        self.slept += s

    def clock(self):
        return self.slept

    def run(self, cu, lb=None):
        return asyncio.run(CU.on_boot(cu, lb or lab(), "gpu-a", self.hold, self.release,
                                      self.start, commit=NEW, now=NOW, sleep=self.sleep,
                                      clock=self.clock))


def test_its_workers_are_held_from_their_first_answer_and_the_deploy_starts(tmp_path):
    cu, _ = seen_then_off(tmp_path)
    b = Boot(tmp_path)
    assert b.run(cu) == "started"
    assert b.held == [("worker-a", CU.UPDATE_HOLD_S)] and b.released == 0
    assert b.deploys.started == 1 and cu.awaiting == {"gpu-a": NEW}
    assert cu.last_start                        # step() doesn't start a second one


def test_a_task_that_got_there_first_leaves_it_to_the_usual_catch_up(tmp_path):
    cu, _ = seen_then_off(tmp_path)
    b = Boot(tmp_path, tasks={"running": 1})
    assert b.run(cu) == "busy"
    assert b.released == 1 and b.deploys.started == 0


def test_a_host_that_never_answers_is_given_up(tmp_path):
    cu, _ = seen_then_off(tmp_path)
    b = Boot(tmp_path, down=10**6)
    assert b.run(cu) == "not up" and b.deploys.started == 0
    assert b.slept >= CU.BOOT_WAIT_S


def test_another_deploy_is_waited_for(tmp_path):
    cu, _ = seen_then_off(tmp_path)
    b = Boot(tmp_path, deploys_busy=2)
    assert b.run(cu) == "started" and b.deploys.started == 1 and b.released == 0


def test_nothing_when_catch_ups_are_off_or_nothing_takes_tasks_there(tmp_path):
    cu, _ = seen_then_off(tmp_path)
    b = Boot(tmp_path)
    assert b.run(cu, lab(auto_catch_up=False)) == "off" and b.polls == 0
    lb = lab()
    lb.agents["worker-a"]["host"] = "gpu-b"
    assert b.run(cu, lb) == "no workers" and b.polls == 0 and b.deploys.started == 0


def test_the_power_on_reply_says_so(monkeypatch):
    _, sec, done = setup(monkeypatch)
    sec.on_power_on = lambda host: "It missed a deploy." if host == "gpu-a" else None
    assert power(sec, "gpu-a", "on", "alice").endswith("a minute or two. It missed a deploy.")
    assert power(sec, "gpu-b", "on", "alice").endswith("a minute or two")
    assert done == [("on", "gpu-a"), ("on", "gpu-b")]


def test_a_worker_held_for_an_update_says_why(tmp_path):
    store = T.TaskStore(tmp_path)
    access = Access({TOK_A: Caller("mgr-alice", "alice"),
                     TOK_B: Caller("secretary", "alice", readonly=True)})

    async def scenario(url):
        err, _ = await _call(url, TOK_B, "hold_for_stop", {"seconds": 300, "reason": "update"})
        assert not err
        err, msg = await _call(url, TOK_A, "submit_task", {"instruction": "a", "requester": "alice"})
        assert err and "being updated" in msg and "again in 3 minutes" in msg
        err, _ = await _call(url, TOK_B, "hold_for_stop", {"seconds": 300})    # a stop again
        err, msg = await _call(url, TOK_A, "submit_task", {"instruction": "b", "requester": "alice"})
        assert err and "being stopped" in msg

    asyncio.run(_with_server(store, access, scenario))


def test_an_older_worker_is_held_as_for_a_stop():
    calls = []

    class Old:
        async def call(self, aid, tool, args, retries=0):
            calls.append((tool, args))
            if "reason" in args:
                raise RuntimeError("unexpected keyword argument 'reason'")
            return {"tasks": {}}
    assert asyncio.run(P.hold_for_update(Old(), "worker-a", 600)) == {"tasks": {}}
    assert calls[-1] == ("hold_for_stop", {"seconds": 600})
