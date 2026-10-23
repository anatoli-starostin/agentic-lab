"""Who may power a machine on or off (#91): one dedicated to a member (every agent on it a
worker or manager with access: dedicated, all that member's), that member or an admin,
busy or not; any other machine, an admin only. A busy one goes off only with force_off."""
import asyncio
from pathlib import Path

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


def power(sec, host, action, who):
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


def test_the_owner_powers_their_machine_on_and_off(monkeypatch):
    lab, sec, done = setup(monkeypatch)
    lab.agents["mgr-bob"] = {"role": "manager", "human": "bob", "host": "gpu-b"}
    assert P.owner(lab, "gpu-b") == "bob"                         # two agents, still bob's
    assert power(sec, "gpu-b", "on", "bob") == "gpu-b: powering on; it takes a minute or two"
    assert power(sec, "gpu-b", "off", "bob").startswith("gpu-b: powering off")
    assert done == [("on", "gpu-b"), ("off", "gpu-b")]


def test_the_owner_stops_their_busy_machine_once_told_what_is_busy(monkeypatch):
    lab, sec, done = setup(monkeypatch, busy=True)
    assert power(sec, "gpu-b", "off", "bob") == (
        "gpu-b is busy (worker-b has 1 running): powering it off interrupts that and loses "
        "its work. Tell them so, and once they confirm, use force_off.")
    assert done == []
    assert power(sec, "gpu-b", "force_off", "bob").startswith("gpu-b: powering off")
    assert done == [("off", "gpu-b")]


def test_another_member_is_refused_both_ways(monkeypatch):
    lab, sec, done = setup(monkeypatch, busy=True)
    assert power(sec, "gpu-b", "on", "carol") == (
        "refused: gpu-b is dedicated to Bob: only Bob or an admin (Alice) may power it on.")
    assert power(sec, "gpu-b", "off", "carol") == (
        "refused: gpu-b is dedicated to Bob: only Bob or an admin (Alice) may power it off. "
        "I stop it myself once it has been idle for 30 min.")
    assert power(sec, "gpu-b", "force_off", "carol").startswith("refused: gpu-b is dedicated")
    assert done == []


@pytest.mark.parametrize("stop_min,said", [(30, "idle for 30 min"), (0, "no idle stop")])
def test_the_off_refusal_says_when_it_stops_by_itself(monkeypatch, stop_min, said):
    lab, sec, done = setup(monkeypatch)
    lab.hosts["gpu-b"]["idle_stop_min"] = stop_min
    assert said in power(sec, "gpu-b", "off", "carol")
    assert "idle" not in power(sec, "gpu-b", "on", "carol")       # not when powering on


def test_an_admin_powers_any_machine(monkeypatch):
    lab, sec, done = setup(monkeypatch, busy=True)
    for host in ("gpu-b", "gpu-a"):
        assert "powering on" in power(sec, host, "on", "alice")
        assert "use force_off" in power(sec, host, "off", "alice")    # busy: confirm first
        assert "powering off" in power(sec, host, "force_off", "alice")
    assert done == [("on", "gpu-b"), ("off", "gpu-b"), ("on", "gpu-a"), ("off", "gpu-a")]


def test_a_machine_dedicated_to_no_one_is_for_admins_only(monkeypatch):
    lab, sec, done = setup(monkeypatch)
    sec_host = next(x["host"] for x in lab.agents.values() if x["role"] == "secretary")
    lab.agents["worker-c"] = {"role": "worker", "human": "carol", "host": "mgr-vm-01"}
    lab.hosts["empty"] = {"kind": "cloud", "provider": "nebius", "instance": "x",
                          "idle_stop_min": 30}
    why = {"gpu-a": "worker-a is shared",
           "mgr-vm-01": "it runs agents of more than one member (Alice, Carol)",
           sec_host: "the secretary runs there",
           "empty": "no agents run there"}
    for host, cause in why.items():
        for action, verb in (("on", "on"), ("off", "off"), ("force_off", "off")):
            for who in ("bob", "carol"):                         # members, but not admins
                assert power(sec, host, action, who) == (
                    f"refused: {host} isn't dedicated to one member ({cause}), so only an "
                    f"admin (Alice) may power it {verb}.")
    assert done == []


def test_every_cause_is_named():
    lab = registry.load(EXAMPLE)
    lab.humans["bob"] = {"name": "Bob"}
    sec_host = next(x["host"] for x in lab.agents.values() if x["role"] == "secretary")
    lab.agents["worker-z"] = {"role": "worker", "human": "bob", "host": sec_host,
                              "access": "shared"}
    assert P.why_not_dedicated(lab, sec_host) == (
        "the secretary runs there; worker-z is shared; "
        "it runs agents of more than one member (Alice, Bob)")


def test_the_secretarys_own_actions_are_unaffected(monkeypatch):
    lab, sec, done = setup(monkeypatch, busy=True)
    for host in ("gpu-b", "gpu-a"):                             # dedicated, and not
        assert "powering on" in asyncio.run(sec.power(host, "on", "asked by the console"))
        assert "powering off" in asyncio.run(sec.power(host, "off", "idle for 30 min"))
    assert done == [("on", "gpu-b"), ("off", "gpu-b"), ("on", "gpu-a"), ("off", "gpu-a")]
