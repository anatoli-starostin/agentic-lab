"""The worker: task queue, access, the MCP API over HTTP, and the task loop."""
import asyncio
import json
import socket
from pathlib import Path

import httpx2
import mcp
import pytest
import uvicorn
from mcp.client.streamable_http import streamable_http_client

from agentic_lab import registry
from agentic_lab.worker import tasks as T
from agentic_lab.worker.server import Access, AccessError, Caller, build_mcp, http_app, run_tasks
from agentic_lab.worker.service import access_for

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
TOK_A = "a" * 40
TOK_B = "b" * 40


# ── task store ───────────────────────────────────────────────────────────────

def test_fifo_and_lifecycle(tmp_path):
    s = T.TaskStore(tmp_path)
    t1 = s.submit("first", "alice", "mgr-alice")
    t2 = s.submit("second", "alice", "mgr-alice")
    assert s.next_queued().id == t1.id
    s.update(t1.id, state=T.RUNNING)
    assert s.next_queued().id == t2.id
    s.add_progress(t1.id, "halfway")
    done = s.update(t1.id, state=T.DONE, result={"summary": "ok", "artifacts": []})
    assert done.started and done.finished and done.progress[-1]["message"] == "halfway"


def test_bad_transitions_and_ids(tmp_path):
    s = T.TaskStore(tmp_path)
    t = s.submit("x", "alice", "mgr-alice")
    with pytest.raises(T.TaskError, match="can't go from queued to done"):
        s.update(t.id, state=T.DONE)
    with pytest.raises(T.TaskError, match="bad task id"):
        s.get("../../etc/passwd")
    with pytest.raises(T.TaskError, match="needs an instruction"):
        s.submit("  ", "alice", "mgr-alice")


def test_cancel(tmp_path):
    s = T.TaskStore(tmp_path)
    q = s.submit("queued", "alice", "mgr-alice")
    r = s.submit("running", "alice", "mgr-alice")
    s.update(r.id, state=T.RUNNING)
    assert s.request_cancel(q.id).state == T.CANCELLED
    rr = s.request_cancel(r.id)
    assert rr.state == T.RUNNING and rr.cancel_requested


def test_recover_requeues_interrupted_tasks(tmp_path):
    s = T.TaskStore(tmp_path)
    a = s.submit("a", "alice", "mgr-alice")
    b = s.submit("b", "alice", "mgr-alice")
    s.update(a.id, state=T.RUNNING, session_id="sess-1")
    s.update(b.id, state=T.RUNNING)
    s.update(b.id, state=T.AWAITING)
    again = T.TaskStore(tmp_path)          # a new process
    assert {t.id for t in again.recover()} == {a.id, b.id}
    assert again.get(a.id).state == T.QUEUED and again.get(a.id).session_id == "sess-1"


def test_progress_is_capped(tmp_path):
    s = T.TaskStore(tmp_path)
    t = s.submit("x", "alice", "mgr-alice")
    for i in range(T.MAX_PROGRESS + 5):
        s.add_progress(t.id, str(i))
    assert len(s.get(t.id).progress) == T.MAX_PROGRESS


# ── access ───────────────────────────────────────────────────────────────────

def test_access_tokens():
    acc = Access({TOK_A: Caller("mgr-alice", "alice")})
    assert acc.caller({"authorization": f"Bearer {TOK_A}"}).manager == "mgr-alice"
    for h in ({}, {"authorization": TOK_A}, {"authorization": "Bearer nope"}):
        with pytest.raises(AccessError):
            acc.caller(h)


def test_dedicated_worker():
    acc = Access({TOK_A: Caller("mgr-alice", "alice"), TOK_B: Caller("mgr-bob", "bob")},
                 allowed_humans={"alice"})
    assert acc.caller({"authorization": f"Bearer {TOK_A}"}).human == "alice"
    with pytest.raises(AccessError, match="dedicated"):
        acc.caller({"authorization": f"Bearer {TOK_B}"})


def test_access_from_lab_yaml():
    lab = registry.load(EXAMPLE)
    shared = access_for(lab, "worker-a", f"mgr-alice: {TOK_A}\n")
    assert shared.allowed_humans is None
    dedicated = access_for(lab, "worker-b", f"mgr-alice: {TOK_A}\n")
    assert dedicated.allowed_humans == {"alice"}
    with pytest.raises(SystemExit, match="not a manager"):
        access_for(lab, "worker-a", f"worker-b: {TOK_A}\n")
    with pytest.raises(SystemExit, match="32\\+ characters"):
        access_for(lab, "worker-a", "mgr-alice: short\n")


# ── the MCP API over real HTTP ───────────────────────────────────────────────

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _with_server(store, access, fn):
    server = build_mcp("worker-a", store, access, guardrails=lambda: {"settings": "x"})
    app, _ = http_app(server, access)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.02)
    try:
        return await fn(f"http://127.0.0.1:{port}/mcp")
    finally:
        srv.should_exit = True
        await task


async def _call(url, token, tool, args):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as hc:
        async with mcp.Client(streamable_http_client(url, http_client=hc)) as c:
            r = await c.call_tool(tool, args)
            if r.is_error:
                return True, r.content[0].text
            # a dict comes back as one JSON text block; a list as one block per item
            items = [json.loads(b.text) for b in r.content]
            return False, (items if tool == "list_tasks" else items[0])


def test_mcp_submit_and_follow(tmp_path):
    store = T.TaskStore(tmp_path)
    access = Access({TOK_A: Caller("mgr-alice", "alice")})

    async def scenario(url):
        err, t = await _call(url, TOK_A, "submit_task", {"instruction": "train", "requester": "alice"})
        assert not err and t["manager"] == "mgr-alice" and t["state"] == "queued"
        err, st = await _call(url, TOK_A, "task_status", {"task_id": t["id"]})
        assert not err and st["state"] == "queued"
        err, lst = await _call(url, TOK_A, "list_tasks", {"state": "queued"})
        assert not err and [x["id"] for x in lst] == [t["id"]]
        err, c = await _call(url, TOK_A, "cancel_task", {"task_id": t["id"]})
        assert not err and c["state"] == "cancelled"
        err, g = await _call(url, TOK_A, "get_guardrails", {})
        assert not err and g == {"settings": "x"}
        err, msg = await _call(url, TOK_A, "task_status", {"task_id": "t-nope"})
        assert err and "no task" in msg

    asyncio.run(_with_server(store, access, scenario))


def test_mcp_rejects_unknown_tokens(tmp_path):
    store = T.TaskStore(tmp_path)
    access = Access({TOK_A: Caller("mgr-alice", "alice")})

    async def scenario(url):
        async with httpx2.AsyncClient() as hc:
            r = await hc.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              headers={"Authorization": "Bearer wrong",
                                       "Accept": "application/json, text/event-stream"})
            assert r.status_code == 401
            r = await hc.post(url, json={})
            assert r.status_code == 401

    asyncio.run(_with_server(store, access, scenario))
    assert store.all() == []


# ── the task loop ────────────────────────────────────────────────────────────

class FakeRunner:
    def __init__(self, fail_on=None):
        self.seen = []
        self.fail_on = fail_on

    async def run(self, task, store):
        self.seen.append(task.instruction)
        if task.instruction == self.fail_on:
            raise RuntimeError("boom")
        store.add_progress(task.id, "working")
        store.update(task.id, state=T.DONE, result={"summary": f"did {task.instruction}",
                                                    "artifacts": []})


def test_run_tasks_fifo_and_survives_runner_errors(tmp_path):
    store = T.TaskStore(tmp_path)
    for name in ("one", "two", "three"):
        store.submit(name, "alice", "mgr-alice")
    runner = FakeRunner(fail_on="two")

    async def go():
        stop = asyncio.Event()
        loop = asyncio.create_task(run_tasks(store, runner, stop))
        while any(t.state not in T.TERMINAL for t in store.all()):
            await asyncio.sleep(0.02)
        stop.set()
        await loop

    asyncio.run(go())
    assert runner.seen == ["one", "two", "three"]
    states = {t.instruction: (t.state, t.error) for t in store.all()}
    assert states["one"] == (T.DONE, None)
    assert states["two"] == (T.FAILED, "RuntimeError: boom")
    assert states["three"][0] == T.DONE


# ── the runner's own permission decisions ────────────────────────────────────

def test_sandboxed_bash_needs_no_human():
    from agentic_lab.worker.runner import allowed_without_asking
    assert allowed_without_asking("Bash", {"command": "export X=1 && make"})
    assert allowed_without_asking("mcp__lab__finish_task", {"summary": "x"})
    # running outside the sandbox, or other tools, go to a human
    assert not allowed_without_asking("Bash", {"command": "make", "dangerouslyDisableSandbox": True})
    for tool in ("Write", "Edit", "WebFetch", "mcp__github__push"):
        assert not allowed_without_asking(tool, {})
