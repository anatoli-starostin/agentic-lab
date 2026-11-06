"""A manager's worker check: all at once, and a machine that never answers costs seconds."""
import asyncio
import time
from types import SimpleNamespace

import httpx2

from agentic_lab.manager import workers as W
from agentic_lab.manager.service import Manager


def test_no_answer_is_not_retried(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *a, **kw):
            calls.append(1)

        async def __aenter__(self):
            raise httpx2.ConnectTimeout("timed out")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(W.mcp, "Client", Client)
    ws = W.Workers({"w": "http://w:8770/mcp"}, {"w": "t"})
    try:
        asyncio.run(ws.call("w", "health_check", connect_timeout=0.1))
    except W.WorkerError as e:
        assert e.no_answer
    else:
        raise AssertionError("expected WorkerError")
    assert len(calls) == 1          # no retry: the machine isn't restarting, it's off


def test_other_failures_still_retried(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *a, **kw):
            calls.append(1)

        async def __aenter__(self):
            raise httpx2.ConnectError("refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(W.mcp, "Client", Client)
    monkeypatch.setattr(W, "RETRY_AFTER_S", 0)
    ws = W.Workers({"w": "http://w:8770/mcp"}, {"w": "t"})
    try:
        asyncio.run(ws.call("w", "health_check"))
    except W.WorkerError as e:
        assert not e.no_answer
    assert len(calls) == 2


def test_describe_workers_parallel_and_says_stopped():
    class Fake:
        def known(self):
            return ["a-off", "b-up", "c-off"]

        async def call(self, w, tool, args=None, retries=1, connect_timeout=None):
            assert connect_timeout == W.CHECK_CONNECT_S
            await asyncio.sleep(0.3)
            if w.endswith("off"):
                raise W.WorkerError(f"{w} unreachable", no_answer=True)
            return {"tasks": {"done": 1}, "gpu": None}

    svc = SimpleNamespace(workers=Fake())
    t = time.monotonic()
    out = asyncio.run(Manager.describe_workers(svc))
    assert time.monotonic() - t < 0.6               # at once, not one after another
    assert [o["worker"] for o in out] == ["a-off", "b-up", "c-off"]
    assert out[1]["up"] and not out[0]["up"]
    assert "most likely stopped" in out[0]["error"]
