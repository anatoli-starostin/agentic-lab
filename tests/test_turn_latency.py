"""A conversation turn's latency (#69): the lab's tools load up front, and where the time
goes is logged per turn."""
import asyncio
from types import SimpleNamespace

from agentic_lab.eventlog import TurnClock
from agentic_lab.policy import sandbox


def test_the_lab_tools_load_up_front():
    assert sandbox.settings("/w")["env"]["ENABLE_TOOL_SEARCH"] == "false"


def test_a_turn_clock_says_where_the_time_went():
    t = iter([0.0, 5.8, 7.1, 9.0])
    clock = TurnClock(now=lambda: next(t))
    clock.started()
    clock.replied()
    clock.replied()                     # only the first reply counts
    text, meta = clock.done()
    assert text == "start-up 5.8 s, first reply 7.1 s, total 9.0 s"
    assert meta == {"startup_s": 5.8, "first_reply_s": 7.1, "total_s": 9.0}


def test_a_turn_without_a_reply_still_has_a_total():
    t = iter([0.0, 2.5])
    text, meta = TurnClock(now=lambda: next(t)).done()
    assert text == "total 2.5 s" and meta["startup_s"] is None


class _Client:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        pass

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock
        yield SystemMessage(subtype="init", data={"session_id": "s1"})
        yield AssistantMessage(content=[TextBlock(text="hi")], model="m")


def test_a_manager_turn_logs_its_timing():
    from agentic_lab.manager import brain as B
    logged = []
    events = SimpleNamespace(add=lambda kind, text="", **m: logged.append((kind, text, m)))
    br = B.Brain("Flash", "lab", "/tmp", lambda grants: {}, turn_budget_s=30, events=events)
    br.client_factory = _Client
    conv = SimpleNamespace(key="c", session_id=None)
    ctx = SimpleNamespace(conv=conv, requester="alice", state=SimpleNamespace(save=lambda: None))
    assert asyncio.run(br.turn(ctx, "hello")) == "hi"
    timing = [e for e in logged if e[0] == "timing"]
    assert len(timing) == 1
    kind, text, meta = timing[0]
    assert text.startswith("start-up ") and "first reply" in text and "total" in text
    assert meta["conv"] == "c" and meta["member"] == "alice"
    assert meta["total_s"] >= meta["first_reply_s"] >= meta["startup_s"] >= 0
