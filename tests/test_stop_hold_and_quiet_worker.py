"""A task submitted while its machine is being idle-stopped is refused, not lost with the
machine (the worker holds new tasks off from the secretary's last look on); and a manager
following a task whose worker can't be reached says so, once, instead of going on silently."""
import asyncio
from pathlib import Path

from agentic_lab import registry
from agentic_lab.manager import followup
from agentic_lab.manager.state import ManagerState
from agentic_lab.manager.workers import WorkerError
from agentic_lab.secretary import power as P
from agentic_lab.worker import tasks as T
from agentic_lab.worker.server import Access, Caller

from test_worker import TOK_A, TOK_B, _call, _with_server

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"


# ── the worker's hold ────────────────────────────────────────────────────────

def test_a_hold_refuses_new_tasks_until_released_and_is_the_secretarys_only(tmp_path):
    store = T.TaskStore(tmp_path)
    access = Access({TOK_A: Caller("mgr-alice", "alice"),
                     TOK_B: Caller("secretary", "alice", readonly=True)})

    async def scenario(url):
        err, msg = await _call(url, TOK_A, "hold_for_stop", {"seconds": 300})
        assert err and "only the secretary" in msg
        err, t = await _call(url, TOK_A, "submit_task", {"instruction": "a", "requester": "alice"})
        assert not err
        err, h = await _call(url, TOK_B, "hold_for_stop", {"seconds": 300})
        assert not err and h["tasks"] == {"queued": 1}      # submitted before: counted
        err, msg = await _call(url, TOK_A, "submit_task", {"instruction": "b", "requester": "alice"})
        assert err and "being stopped" in msg and "power it on" in msg
        err, _ = await _call(url, TOK_B, "hold_for_stop", {"seconds": 0})     # released
        assert not err
        err, _ = await _call(url, TOK_A, "submit_task", {"instruction": "c", "requester": "alice"})
        assert not err

    asyncio.run(_with_server(store, access, scenario))
    assert [t.instruction for t in store.all()] == ["a", "c"]


# ── the secretary's recheck ──────────────────────────────────────────────────

class Workers:
    def __init__(self, tasks, old=False):
        self.tasks, self.old, self.calls = tasks, old, []

    async def call(self, w, tool, args=None, retries=None):
        self.calls.append((w, tool, (args or {}).get("seconds")))
        if tool == "hold_for_stop" and self.old:
            raise RuntimeError(f"{w}: Unknown tool: hold_for_stop")
        return {"tasks": self.tasks, "runs": [], "gpus": [], "gpu_processes": []}


SNAP = {"agents": {"worker-a": {"up": True, "queue": {}}}}


def test_recheck_holds_an_idle_worker_and_releases_a_busy_one():
    lab = registry.load(EXAMPLE)
    idle = Workers({})
    assert asyncio.run(P.recheck(lab, "gpu-a", idle, SNAP)) is None
    assert idle.calls == [("worker-a", "hold_for_stop", P.STOP_HOLD_S)]    # held: stop it
    busy = Workers({"queued": 1})
    assert asyncio.run(P.recheck(lab, "gpu-a", busy, SNAP)) == "worker-a has 1 queued"
    assert busy.calls[-1] == ("worker-a", "hold_for_stop", 0)               # released
    asyncio.run(P.release(lab, "gpu-a", idle))                              # a failed stop
    assert idle.calls[-1] == ("worker-a", "hold_for_stop", 0)


def test_recheck_reads_a_worker_from_before_holds():
    lab = registry.load(EXAMPLE)
    old = Workers({}, old=True)
    assert asyncio.run(P.recheck(lab, "gpu-a", old, SNAP)) is None
    assert [c[1] for c in old.calls] == ["hold_for_stop", "health_check"]


# ── the manager's follow-up ──────────────────────────────────────────────────

class Flaky:
    def __init__(self):
        self.down = True

    async def call(self, worker, tool, args=None):
        if self.down:
            raise WorkerError(f"{worker} unreachable: ConnectTimeout: ")
        return {"state": "running", "progress": []}


def test_an_unreachable_worker_is_said_once_and_its_return_too(tmp_path, monkeypatch):
    st = ManagerState(tmp_path)
    conv = st.conversation("D1", None)
    st.track("t-1", "gpu-worker", conv.key, "alice")
    posts, now = [], [1000.0]
    monkeypatch.setattr(followup.time, "time", lambda: now[0])

    async def post(c, text):
        posts.append(text)

    async def done(c, t, v):
        pass
    w = Flaky()

    def round_at(t):
        now[0] = t
        asyncio.run(followup.check_once(st, w, post, done, mention=lambda h: f"<@{h}>"))
    round_at(1000)
    round_at(1000 + followup.QUIET_NOTICE_S - 1)
    assert posts == []                                  # a blip isn't worth a message
    round_at(1000 + followup.QUIET_NOTICE_S)
    round_at(1000 + followup.QUIET_NOTICE_S * 5)
    assert len(posts) == 1
    assert "gpu-worker" in posts[0] and "can't be reached since" in posts[0]
    assert "t-1" in posts[0] and "<@alice>" in posts[0] and "power it on" in posts[0]
    st2 = ManagerState(tmp_path)                        # remembered across a restart
    assert st2.tasks["t-1"].unreachable_noted
    w.down = False
    round_at(5000)
    round_at(5008)
    assert len(posts) == 2 and "reachable again" in posts[1]
    assert not st.tasks["t-1"].unreachable_since and not st.tasks["t-1"].done
