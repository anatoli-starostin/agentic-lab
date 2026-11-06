"""The live view: what an agent is doing right now (live.py, the log page's 'Right now')."""
import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace

from starlette.testclient import TestClient

from agentic_lab import live as L
from agentic_lab.manager.status import app as status_app
from agentic_lab.secretary import web as W
from agentic_lab.worker import tasks as T
from agentic_lab.worker.server import Access, Caller

from test_worker import TOK_A, TOK_B, _call, _with_server


def _msg(*blocks, usage=None):
    return SimpleNamespace(content=list(blocks), usage=usage)


def test_live_follows_a_turn():
    lv = L.Live()
    lv.start("c1", member="alice")
    lv.assistant("c1", _msg(SimpleNamespace(text="Looking."),
                            SimpleNamespace(id="u1", name="Bash", input={"command": "ls"}),
                            usage={"output_tokens": 40, "input_tokens": 5,
                                   "cache_read_input_tokens": 1000}),
                 lambda n, i: f"{n}: {i['command']}")
    [t] = lv.snapshot()
    assert t["member"] == "alice" and t["text"] == "Looking." and t["tool_calls"] == 1
    assert [x["tool"] for x in t["tools"]] == ["Bash: ls"]
    assert t["output_tokens"] == 40 and t["context_tokens"] == 1005
    lv.tool_done("c1", "u1")
    assert lv.snapshot()[0]["tools"] == []
    lv.end("c1")
    assert lv.snapshot() == []
    lv.assistant("gone", _msg(), lambda n, i: n)        # a turn that ended: ignored


def test_mask_hides_credentials():
    m = L.mask("curl -H token=abc123 --api-key s3cr3t ghp_ABCDEFGHIJ0123 x "
               + "A" * 48 + " train.py --lr 3e-4")
    assert "abc123" not in m and "s3cr3t" not in m and "ghp_" not in m and "A" * 48 not in m
    assert "train.py --lr 3e-4" in m


def test_processes_lists_ours(monkeypatch):
    monkeypatch.setattr(L, "MAX_PROCESSES", 10000)     # a busy test machine: all of them
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        for _ in range(50):         # until it has started (exec'd) under its own name
            procs = L.processes()
            if any(x["pid"] == p.pid and "time.sleep" in x["command"] for x in procs):
                break
            time.sleep(0.1)
        else:
            raise AssertionError(procs)
        assert all(x["pid"] != os.getpid() for x in procs)
    finally:
        p.kill()


def test_manager_status_gives_now_only_when_asked(tmp_path):
    usage = SimpleNamespace(load=lambda: {"limits": {}})
    events = SimpleNamespace(read=lambda **k: [])
    c = TestClient(status_app("tok", usage, events, now=lambda: {"turns": [1]}))
    h = {"Authorization": "Bearer tok"}
    assert "now" not in c.get("/v1/status", headers=h).json()
    assert c.get("/v1/status?now=1", headers=h).json()["now"] == {"turns": [1]}


def test_worker_now_is_for_the_secretary_only(tmp_path):
    store = T.TaskStore(tmp_path)
    access = Access({TOK_A: Caller("mgr-alice", "alice"),
                     TOK_B: Caller("secretary", "alice", readonly=True)})

    async def scenario(url):
        err, msg = await _call(url, TOK_A, "now", {})
        assert err and "secretary only" in msg
        err, r = await _call(url, TOK_B, "now", {})
        assert not err and r["turns"] == [] and isinstance(r["processes"], list)

    asyncio.run(_with_server(store, access, scenario))


def test_render_now():
    now = {"turns": [{"member": "alice", "started_at": "2026-01-01T00:00:00+00:00",
                      "tool_calls": 3, "output_tokens": 1200, "context_tokens": 45000,
                      "text": "Searching <all> branches",
                      "tools": [{"tool": "Bash: git grep x", "since": "2026-01-01T00:00:00+00:00"}]}],
           "runs": [{"id": "r1", "name": "train", "state": "running", "task": "t-1",
                     "log_tail": "step 4200 loss 0.31"}],
           "processes": [{"pid": 7, "seconds": 125, "cpu": 98.0, "mem_mb": 300,
                          "command": "git grep x"}]}
    h = W.render_now(now)
    assert "Bash: git grep x" in h and "step 4200" in h and "1k tokens" in h
    assert "&lt;all&gt;" in h and "2 min 5 s" in h
    assert "No Claude session" in W.render_now({"turns": []})
    assert "couldn" in W.render_now(None, "couldn't ask it: down")
    assert W.render_now(None) == ""


def test_log_page_live_mode():
    async def get_events(agent, hours, member):
        assert hours == W.LIVE_HOURS
        return []

    async def get_now(agent, member):
        return {"turns": []}

    async def viewer(ip):
        return "alice"

    c = TestClient(W.app(lambda: {"agents": {}}, get_events, viewer, access=lambda m, a: "all",
                         get_now=get_now))
    r = c.get("/logs/mgr-a?live=1")
    assert r.status_code == 200 and "http-equiv='refresh'" in r.text and "Right now" in r.text
    assert "http-equiv" not in c.get("/logs/mgr-a?hours=1").text
