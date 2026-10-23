"""Who may power a machine off (#91): an idle machine dedicated to a member, that member or
an admin; a shared one, any member; a busy one, admins only (force_off), as before."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import registry
from agentic_lab.secretary import power as P
from agentic_lab.secretary.service import Secretary

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
IDLE = {"up": True, "queue": {}}


def setup(monkeypatch, busy=False):
    """alice (admin), bob and carol. gpu-b: worker-b, dedicated to bob; gpu-a: worker-a,
    shared. Power actions are recorded instead of made."""
    lab = registry.load(EXAMPLE)
    lab.humans["alice"]["admin"] = True
    lab.humans["bob"] = {"name": "Bob", "slack_user": "U-BOB"}
    lab.humans["carol"] = {"name": "Carol", "slack_user": "U-CAROL"}
    lab.agents["worker-b"]["human"] = "bob"
    lab.agents["worker-b"]["access"] = "dedicated"
    lab.agents["worker-a"]["access"] = "shared"
    done = []

    async def record(lab_, host, what="off"):
        done.append((what, host))
    monkeypatch.setattr(P, "stop", record)
    monkeypatch.setattr(P, "start", lambda lab_, host: record(lab_, host, "on"))
    sec = Secretary.__new__(Secretary)
    sec.lab, sec.slack, sec.workers, sec.events = lab, None, None, None
    sec.via = "in Slack"
    q = {"running": 1} if busy else {}
    sec.latest = {"state": {"agents": {"worker-a": {**IDLE, "queue": q},
                                       "worker-b": {**IDLE, "queue": q}}}}

    async def quiet(text):
        return True
    sec.announce = quiet
    return lab, sec, done


def off(sec, host, who, action="off"):
    return asyncio.run(sec.power(host, action, "asked", asked_by=who))


def test_whose_machine_it_is():
    lab = registry.load(EXAMPLE)
    lab.agents["worker-b"]["access"] = "dedicated"
    assert P.owner(lab, "gpu-b") == lab.agents["worker-b"]["human"]
    lab.agents["worker-a"]["access"] = "shared"
    assert P.owner(lab, "gpu-a") is None                          # a shared agent
    lab.hosts["empty"] = {"kind": "cloud", "provider": "nebius", "instance": "x",
                          "idle_stop_min": 30}
    assert P.owner(lab, "empty") is None                          # no agents
    sec_host = next(x["host"] for x in lab.agents.values() if x["role"] == "secretary")
    assert P.owner(lab, sec_host) is None                         # the secretary's


def test_two_members_or_a_shared_agent_make_it_shared():
    lab = registry.load(EXAMPLE)
    lab.humans["bob"] = {"name": "Bob"}
    lab.agents["worker-b"]["access"] = "dedicated"
    lab.agents["worker-x"] = {**lab.agents["worker-b"], "human": "bob"}   # same host, bob
    assert P.owner(lab, "gpu-b") is None
    lab.agents["worker-x"].update(human=lab.agents["worker-b"]["human"], access="shared")
    assert P.owner(lab, "gpu-b") is None                          # dedicated + shared
    lab.agents["worker-x"]["access"] = "dedicated"
    assert P.owner(lab, "gpu-b") == lab.agents["worker-b"]["human"]  # both the same member's


def test_only_the_owner_or_an_admin_stops_an_idle_dedicated_machine(monkeypatch):
    lab, sec, done = setup(monkeypatch)
    out = off(sec, "gpu-b", "carol")
    assert out.startswith("refused: gpu-b is dedicated to Bob: only Bob or an admin")
    assert "idle for 30 min" in out and done == []                # not a permanent block
    assert "powering off" in off(sec, "gpu-b", "bob")             # the owner
    assert "powering off" in off(sec, "gpu-b", "alice")           # an admin
    assert done == [("off", "gpu-b"), ("off", "gpu-b")]


def test_a_shared_idle_machine_any_member(monkeypatch):
    lab, sec, done = setup(monkeypatch)
    assert "powering off" in off(sec, "gpu-a", "carol")
    assert done == [("off", "gpu-a")]


def test_busy_still_needs_an_admin_and_force_off(monkeypatch):
    lab, sec, done = setup(monkeypatch, busy=True)
    assert "busy" in off(sec, "gpu-b", "bob")                     # even the owner
    assert "busy" in off(sec, "gpu-a", "carol")
    assert "force_off" in off(sec, "gpu-b", "alice")              # the admin, to confirm
    assert "powering off" in off(sec, "gpu-b", "alice", "force_off")
    assert done == [("off", "gpu-b")]


def test_power_on_and_the_idle_stop_are_unchanged(monkeypatch):
    lab, sec, done = setup(monkeypatch)
    assert "powering on" in asyncio.run(sec.power("gpu-b", "on", "asked", asked_by="carol"))
    assert "powering off" in asyncio.run(sec.power("gpu-b", "off", "idle for 30 min"))
    assert done == [("on", "gpu-b"), ("off", "gpu-b")]


@pytest.mark.parametrize("stop_min,said", [(30, "idle for 30 min"), (0, "no idle stop")])
def test_the_refusal_says_when_it_stops_by_itself(monkeypatch, stop_min, said):
    lab, sec, done = setup(monkeypatch)
    lab.hosts["gpu-b"]["idle_stop_min"] = stop_min
    assert said in off(sec, "gpu-b", "carol")
