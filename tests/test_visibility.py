"""Transparent about the lab, private about the work (#62)."""
import asyncio
import datetime as dt
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx2
import uvicorn
import yaml

from agentic_lab import registry
from agentic_lab.manager import tools as MT
from agentic_lab.secretary import visibility as V
from agentic_lab.secretary import web
from agentic_lab.secretary.service import Secretary

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
NOW = dt.datetime.now(dt.timezone.utc).isoformat()


def lab(**top):
    lb = registry.load(EXAMPLE)
    lb.humans["alice"]["admin"] = False
    lb.humans["bob"] = {"name": "Bob"}
    lb.humans["root"] = {"name": "Root", "admin": True}
    lb.data.update(top)
    return lb


def task(tid, who, text):
    return {"id": tid, "state": "running", "requester": who, "manager": "mgr-alice",
            "created": NOW, "finished": None, "instruction": text,
            "result": {"summary": f"{text} done"}}


STATE = {
    "lab": "dev-lab", "at": NOW, "applied": {"config_commit": "c" * 40},
    "agents": {"worker-a": {
        "id": "worker-a", "name": "Alice's worker", "role": "worker", "human": "alice",
        "host": "gpu-a", "up": True, "queue": {"running": 2},
        "keep_awake": [{"user": "bob", "note": "debugging"}],
        "runs": [{"id": "r-1", "name": "bob-secret-sweep", "task": "t-b", "started": NOW},
                 {"id": "r-2", "name": "alice-train", "task": "t-a", "started": NOW}],
        "recent": [task("t-a", "alice", "ALICE-INSTRUCTION"),
                   task("t-b", "bob", "BOB-INSTRUCTION")]}},
    "accounts": {"alice": {"name": "Alice", "agents": ["worker-a"], "limits": {},
                           "tokens_today": {}}},
    "approvals": {"pending": [
        {"id": "q-1", "agent": "worker-a", "human": "alice", "detail": "ALICE-DETAIL",
         "expires": None},
        {"id": "q-2", "agent": "worker-a", "human": "bob", "detail": "BOB-DETAIL",
         "expires": None}], "grants": {}},
    "hosts": {"gpu-a": {"state": "RUNNING"}},
}


def text(x):
    return json.dumps(x)


def test_admins_see_everything():
    assert V.shape(STATE, lab(), "root") == STATE


def test_a_member_sees_the_lab_but_not_others_work():
    s = text(V.shape(STATE, lab(), "alice"))
    assert "ALICE-INSTRUCTION" in s and "ALICE-DETAIL" in s and "alice-train" in s
    assert "BOB-INSTRUCTION" not in s and "BOB-DETAIL" not in s and "BOB-INSTRUCTION done" \
        not in s and "bob-secret-sweep" not in s
    seen = V.shape(STATE, lab(), "alice")
    bob_task = seen["agents"]["worker-a"]["recent"][1]
    assert bob_task["state"] == "running" and bob_task["requester"] == "bob"   # state, owner
    assert seen["agents"]["worker-a"]["keep_awake"][0]["user"] == "bob"        # why it's busy
    assert seen["accounts"] == STATE["accounts"]                               # usage per seat
    assert "BOB-INSTRUCTION" in text(STATE)                                    # not modified


def test_a_team_lab_shares_work_among_members():
    assert V.shape(STATE, lab(visibility="team"), "alice") == STATE


def test_anyone_else_gets_machines_states_and_counts_only():
    s = V.shape(STATE, lab(), None)
    dump = text(s)
    for secret in ("INSTRUCTION", "DETAIL", "alice", "Alice", "bob", "debugging", "sweep"):
        assert secret not in dump, secret
    w = s["agents"]["worker-1"]
    assert w["queue"] == {"running": 2} and len(w["recent"]) == 2 and w["host"] == "gpu-a"
    assert len(s["approvals"]["pending"]) == 2 and s["hosts"]["gpu-a"]["state"] == "RUNNING"
    assert "<!doctype html>" in web.render(s)


def test_the_web_ui_shapes_by_viewer(free_port):
    def get(who, path):
        async def viewer(ip):
            return who
        server = uvicorn.Server(uvicorn.Config(web.app(
            lambda: STATE, None, viewer, shape=lambda st, m: V.shape(st, lab(), m)),
            host="127.0.0.1", port=free_port, log_level="error"))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        while not server.started:
            time.sleep(0.02)
        try:
            return httpx2.get(f"http://127.0.0.1:{free_port}{path}").text
        finally:
            server.should_exit = True
            th.join()
    assert "BOB-INSTRUCTION" not in get(None, "/state.json")
    assert "BOB-INSTRUCTION" not in get("alice", "/")
    assert "ALICE-INSTRUCTION" in get("alice", "/state.json")
    assert "BOB-INSTRUCTION" in get("root", "/state.json")


class Workers:
    async def call(self, worker, tool, args=None):
        if tool == "list_tasks":
            return [task("t-a", "alice", "ALICE-INSTRUCTION"), task("t-b", "bob", "BOB-INSTRUCTION")]
        return task(args["task_id"], "bob" if args["task_id"] == "t-b" else "alice",
                    "BOB-INSTRUCTION" if args["task_id"] == "t-b" else "ALICE-INSTRUCTION")


def test_the_secretary_tools_follow_the_same_rules():
    sec = SimpleNamespace(lab=lab(), workers=Workers())
    run = asyncio.run
    mine = run(Secretary.worker_tasks(sec, "worker-a", "alice"))
    assert "ALICE-INSTRUCTION" in text(mine) and "BOB-INSTRUCTION" not in text(mine)
    assert [t["requester"] for t in mine] == ["alice", "bob"]
    assert "error" in run(Secretary.task_result(sec, "worker-a", "t-b", "alice"))
    assert run(Secretary.task_result(sec, "worker-a", "t-a", "alice"))["instruction"]
    assert "BOB-INSTRUCTION" in text(run(Secretary.worker_tasks(sec, "worker-a", "root")))


def test_shared_workers_hand_out_only_the_members_task_folders():
    from agentic_lab.manager.state import TrackedTask
    tasks = {"t-a": TrackedTask("t-a", "worker-a", "D1", "alice"),
             "t-b": TrackedTask("t-b", "worker-a", "D2", "bob")}

    def ctx(who, lb=None):
        return SimpleNamespace(lab=lb or lab(), requester=who, private=True,
                               conv=SimpleNamespace(key="D1"),
                               state=SimpleNamespace(tasks=tasks))
    assert MT._file_refused(ctx("alice"), "worker-a", "tasks/t-a/plot.png") is None
    for path in ("tasks/t-b/plot.png", "results/plot.png", "/home/x/work/tasks/t-a/p",
                 "tasks/t-a/../t-b/p", "tasks/t-a"):
        assert MT._file_refused(ctx("alice"), "worker-a", path), path
    assert MT._file_refused(ctx("alice"), "worker-b", "anything.png") is None   # dedicated
    assert MT._file_refused(ctx("root"), "worker-a", "results/plot.png") is None
    assert MT._file_refused(ctx("alice", lab(visibility="team")), "worker-a",
                            "results/plot.png") is None


def test_lab_check_knows_the_visibility_values(tmp_path):
    d = yaml.safe_load(EXAMPLE.read_text())
    d["visibility"] = "everyone"
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    errs = [str(i) for i in registry.check(registry.load(p)) if i.level == "error"]
    assert any("visibility" in e for e in errs)
