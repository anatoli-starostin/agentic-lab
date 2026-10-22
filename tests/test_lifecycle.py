"""#58: a task is never lost, duplicated or silent between a manager and a worker."""
import asyncio
from types import SimpleNamespace

from agentic_lab.manager import followup
from agentic_lab.manager import tools as MT
from agentic_lab.manager.state import ManagerState
from agentic_lab.manager.workers import WorkerError
from agentic_lab.worker import tasks as T
from agentic_lab.worker.runner import Decision, SdkRunner
from agentic_lab.worker.server import run_tasks


def manager_tools(ctx, monkeypatch):
    made = []
    real = MT.create_sdk_mcp_server

    def capture(name, tools=None, **kw):
        made.extend(tools or [])
        return real(name, tools=tools, **kw)
    monkeypatch.setattr(MT, "create_sdk_mcp_server", capture)
    MT.build(ctx)
    return {t.name: t.handler for t in made}


def test_a_task_is_followed_even_if_the_broker_is_down(tmp_path, monkeypatch):
    """The worker took it: tracked at once; the broker registration is retried by the
    follow-up, and the agent isn't told to resubmit."""
    state = ManagerState(tmp_path)
    conv = state.conversation("C1", "1.0")
    submitted, registered = [], []

    class Workers:
        async def call(self, worker, tool, args=None, retries=None):
            submitted.append(args)
            return {"id": "t-1", "state": "queued"}

    class Broker:
        down = True

        async def register_task(self, worker, task_id, requester):
            if self.down:
                raise ValueError("not JSON: the broker is restarting")
            registered.append(task_id)
    broker = Broker()
    ctx = SimpleNamespace(workers=Workers(), broker=broker, state=state, conv=conv,
                          requester="alice", private=False)
    h = manager_tools(ctx, monkeypatch)
    out = asyncio.run(h["submit_task"]({"worker": "worker-a", "instruction": "train"}))
    text = out["content"][0]["text"]
    assert "Submitted t-1" in text and "Don't submit it again" in text and not out.get("is_error")
    assert state.tasks["t-1"].registered is False and len(submitted) == 1

    class FollowWorkers:
        async def call(self, worker, tool, args=None):
            return {"state": "running", "progress": [], "progress_total": 0}
    broker.down = False

    async def post(c, text):
        pass
    asyncio.run(followup.check_once(state, FollowWorkers(), post, None, register=lambda t:
                                    broker.register_task(t.worker, t.task_id, t.requester)))
    assert registered == ["t-1"] and state.tasks["t-1"].registered


def test_progress_beyond_the_kept_lines_is_still_posted(tmp_path):
    store = T.TaskStore(tmp_path / "tasks")
    t = store.submit("x", "alice", "mgr")
    for i in range(1, 201):
        store.add_progress(t.id, f"line {i}")
    state = ManagerState(tmp_path / "m")
    conv = state.conversation("C1", "1.0")
    state.track(t.id, "worker-a", conv.key, "alice")
    posts = []

    class Workers:
        async def call(self, worker, tool, args=None):
            return store.get(t.id).view() | {"state": "running"}

    async def post(c, text):
        posts.append(text)
    asyncio.run(followup.check_once(state, Workers(), post, None))
    assert len(posts) == 200 and posts[-1].endswith("line 200")
    for i in range(201, 251):                       # the list stays 200 long from here
        store.add_progress(t.id, f"line {i}")
    assert len(store.get(t.id).progress) == 200 and store.get(t.id).progress_total == 250
    asyncio.run(followup.check_once(state, Workers(), post, None))
    assert len(posts) == 250 and posts[200].endswith("line 201") and posts[-1].endswith("line 250")


def test_the_result_is_delivered_even_if_the_wrapup_fails_once(tmp_path):
    state = ManagerState(tmp_path)
    conv = state.conversation("C1", "1.0")
    state.track("t-1", "worker-a", conv.key, "alice")
    told = []

    class Workers:
        async def call(self, worker, tool, args=None):
            return {"state": "done", "progress": [], "result": {"summary": "ok"}}

    async def post(c, text):
        pass
    attempts = []

    async def finished(c, t, v):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("the manager restarted mid-turn")
        told.append(v["result"]["summary"])
    asyncio.run(followup.check_once(state, Workers(), post, finished))
    assert state.open_tasks() and not told            # not marked done: retried
    again = ManagerState(tmp_path)
    asyncio.run(followup.check_once(again, Workers(), post, finished))
    assert told == []                                 # after a pause, not at once
    again.tasks["t-1"].wrapup_next = 0
    asyncio.run(followup.check_once(again, Workers(), post, finished))
    assert told == ["ok"]


def test_a_task_the_worker_forgot_is_given_up(tmp_path):
    from agentic_lab.manager.workers import WorkerError
    state = ManagerState(tmp_path)
    conv = state.conversation("C1", "1.0")
    state.track("t-9", "worker-a", conv.key, "alice")
    posts = []

    class Workers:
        async def call(self, worker, tool, args=None):
            raise WorkerError("worker-a task_result: no task 't-9'")

    async def post(c, text):
        posts.append(text)
    for _ in range(followup.GIVE_UP_AFTER):
        asyncio.run(followup.check_once(state, Workers(), post, None))
    assert state.open_tasks() == [] and "no longer knows task t-9" in posts[-1]


def runner(tmp_path, approver=None):
    return SdkRunner("w", str(tmp_path), lambda g: {}, approver=approver)


def test_finished_or_cancelled_work_is_not_recorded_as_failed(tmp_path, monkeypatch):
    store = T.TaskStore(tmp_path / "tasks")
    r = runner(tmp_path)

    async def finished_then_crash(task, store_, outcome, session, grants, prompt):
        outcome.update(result={"summary": "trained", "artifacts": []}, success=True)
        raise RuntimeError("CLI exited badly")
    monkeypatch.setattr(r, "_session", finished_then_crash)
    t = store.update(store.submit("a", "alice", "m").id, state=T.RUNNING)
    asyncio.run(r.run(t, store))
    assert store.get(t.id).state == T.DONE and store.get(t.id).result["summary"] == "trained"

    async def interrupted(task, store_, outcome, session, grants, prompt):
        store_.update(task.id, cancel_requested=True)
        raise RuntimeError("interrupted")
    monkeypatch.setattr(r, "_session", interrupted)
    t = store.update(store.submit("b", "alice", "m").id, state=T.RUNNING)
    asyncio.run(r.run(t, store))
    assert store.get(t.id).state == T.CANCELLED


def test_parallel_approvals_and_recovery(tmp_path):
    store = T.TaskStore(tmp_path / "tasks")
    t = store.update(store.submit("x", "alice", "m").id, state=T.RUNNING)
    gates = {"one": asyncio.Event(), "two": asyncio.Event()}

    class Approver:
        async def ask(self, task, tool, inp, violations, stop=None, filed=None):
            filed(f"req-{inp['n']}")
            await gates[inp["n"]].wait()
            return Decision("allow")

        async def active_grants(self):
            return []
    r = runner(tmp_path, Approver())
    session = {"violations": [], "restart_for": None}
    opts = r._options(t, store, {}, session, [])

    async def go():
        a = asyncio.create_task(opts.can_use_tool("WebFetch", {"n": "one"}, None))
        b = asyncio.create_task(opts.can_use_tool("WebFetch", {"n": "two"}, None))
        await asyncio.sleep(0.01)
        assert store.get(t.id).pending_requests == ["req-one", "req-two"]
        gates["one"].set()
        await a
        assert store.get(t.id).state == T.AWAITING      # the other one is still pending
        gates["two"].set()
        await b
    asyncio.run(go())
    assert store.get(t.id).state == T.RUNNING and store.get(t.id).pending_requests == []

    # a crash while a request was pending: recovery withdraws it
    store.update(t.id, state=T.AWAITING, pending_requests=["req-old"])
    withdrawn = []

    class Fake:
        approver = SimpleNamespace(withdraw=lambda rid: _record(withdrawn, rid))

        async def run(self, task, store_):
            store_.update(task.id, state=T.DONE, result={"summary": "", "artifacts": []})

    async def go2():
        stop = asyncio.Event()
        loop = asyncio.create_task(run_tasks(store, Fake(), stop))
        while store.get(t.id).state != T.DONE:
            await asyncio.sleep(0.01)
        stop.set()
        await loop
    asyncio.run(go2())
    assert withdrawn == ["req-old"] and store.get(t.id).pending_requests == []


async def _record(log, rid):
    log.append(rid)



def test_notes_only_on_requests_this_conversation_may_see(tmp_path, monkeypatch):
    """#62: a shared manager can't annotate another member's pending request."""
    state = ManagerState(tmp_path)
    conv = state.conversation("C1", "1.0")
    state.track("t-1", "worker-a", conv.key, "alice")
    notes = []

    class Broker:
        async def pending(self):
            return [{"id": "r-mine", "task_id": "t-1", "human": "alice"},
                    {"id": "r-bob", "task_id": "t-bob", "human": "bob"}]

        async def add_context(self, rid, text):
            notes.append(rid)
    ctx = SimpleNamespace(workers=None, broker=Broker(), state=state, conv=conv,
                          requester="alice", private=False)
    h = manager_tools(ctx, monkeypatch)
    assert not asyncio.run(h["add_approval_note"]({"request_id": "r-mine", "note": "x"})).get(
        "is_error")
    assert asyncio.run(h["add_approval_note"]({"request_id": "r-bob", "note": "x"}))["is_error"]
    assert notes == ["r-mine"]


def test_progress_continues_across_an_upgrade(tmp_path):
    """A task saved before progress_total existed, followed by a manager that counted
    list positions: new lines still reach the conversation."""
    import json as _json
    from agentic_lab.worker import tasks as T2
    store = T2.TaskStore(tmp_path / "tasks")
    t = store.submit("x", "alice", "m")
    raw = _json.loads(open(store._path(t.id)).read())
    raw["progress"] = [{"at": "x", "message": f"line {i}"} for i in range(40)]
    raw.pop("progress_total", None)
    open(store._path(t.id), "w").write(_json.dumps(raw))
    assert store.get(t.id).progress_total == 40
    store.add_progress(t.id, "line 40")
    state = ManagerState(tmp_path / "m")
    conv = state.conversation("C1", "1.0")
    state.track(t.id, "worker-a", conv.key, "alice")
    state.tasks[t.id].progress_seen = 40                 # the old manager's list index
    posted = []

    class Workers:
        async def call(self, worker, tool, args=None):
            return store.get(t.id).view()

    async def post(c, text):
        posted.append(text)
    asyncio.run(followup.check_once(state, Workers(), post, None))
    assert posted and posted[-1].endswith("line 40")


def test_a_wrapup_that_keeps_failing_is_given_up(tmp_path):
    state = ManagerState(tmp_path)
    conv = state.conversation("C1", "1.0")
    state.track("t-1", "worker-a", conv.key, "alice")
    turns = []

    class Workers:
        async def call(self, worker, tool, args=None):
            return {"state": "done", "progress": [], "result": {"summary": "ok"}}

    async def finished(c, t, v):
        turns.append(1)
        return "the wrap-up"

    async def post(c, text):
        raise RuntimeError("not_in_channel")
    for _ in range(followup.WRAPUP_TRIES):
        state.tasks["t-1"].wrapup_next = 0
        asyncio.run(followup.check_once(state, Workers(), post, finished))
    assert turns == [1]                     # written once; only the post was retried
    assert not state.open_tasks()           # given up


def test_registration_already_done_counts(tmp_path):
    state = ManagerState(tmp_path)
    conv = state.conversation("C1", "1.0")
    state.track("t-1", "worker-a", conv.key, "alice", registered=False)

    class Workers:
        async def call(self, worker, tool, args=None):
            return {"state": "running", "progress": []}

    async def register(t):
        raise WorkerError("broker: task t-1 is already registered")

    async def post(c, text):
        pass
    asyncio.run(followup.check_once(state, Workers(), post, None, register=register))
    assert state.tasks["t-1"].registered


def test_old_requests_are_withdrawn_until_the_broker_answers(tmp_path):
    from agentic_lab.worker.server import withdraw_stale
    store = T.TaskStore(tmp_path / "tasks")
    t = store.update(store.submit("x", "alice", "m").id, stale_requests=["q-1", "q-2"])
    answers = {"q-1": True, "q-2": False}

    class Approver:
        async def withdraw(self, rid):
            return answers[rid]
    asyncio.run(withdraw_stale(store, Approver()))
    assert store.get(t.id).stale_requests == ["q-2"]
    answers["q-2"] = True
    asyncio.run(withdraw_stale(store, Approver()))
    assert store.get(t.id).stale_requests == []
