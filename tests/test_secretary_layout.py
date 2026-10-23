"""#99: the secretary never acts on a remembered layout. Every turn starts with the lab as
deployed (which agent runs on which machine, whose it is), and its power tool takes an
agent and looks its machine up in lab.yaml."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import registry
from agentic_lab.secretary import brain as B
from agentic_lab.secretary import power as P
from agentic_lab.secretary.service import Secretary
from agentic_lab.slackio import Incoming

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"


def lab():
    return registry.load(EXAMPLE)


def test_layout_lists_every_machine_with_its_agents_and_owner():
    text = P.layout(lab())
    assert text.startswith("[lab, as deployed now:")
    assert "- mgr-vm-01 (nebius, Alice's): Flash (manager)" in text
    assert "- gpu-a (nebius, shared): worker-a (worker)" in text
    assert "- gpu-b (nebius, Alice's): worker-b (worker)" in text
    assert "- nuc-01 (onprem, the secretary's): Iris (secretary)" in text


def test_layout_follows_lab_yaml_when_an_agent_moves():
    lb = lab()
    lb.agents["mgr-alice"]["host"] = "gpu-b"        # the manager moved
    text = P.layout(lb)
    assert "- gpu-b (nebius, Alice's): Flash (manager), worker-b (worker)" in text
    assert "- mgr-vm-01 (nebius, shared): no agents" in text


def test_host_of_finds_an_agent_by_id_or_name_in_any_case():
    lb = lab()
    assert P.host_of(lb, "mgr-alice") == ("mgr-vm-01", "Flash")
    assert P.host_of(lb, "flash") == ("mgr-vm-01", "Flash")
    assert P.host_of(lb, " FLASH ") == ("mgr-vm-01", "Flash")
    with pytest.raises(P.PowerError, match="no agent 'Bolt'.*Flash"):
        P.host_of(lb, "Bolt")


def tools(ctx, monkeypatch):
    made = []
    real = B.create_sdk_mcp_server

    def capture(name, tools=None, **kw):
        made.extend(tools or [])
        return real(name, tools=tools, **kw)
    monkeypatch.setattr(B, "create_sdk_mcp_server", capture)
    B.build_tools(ctx)
    return {t.name: t.handler for t in made}


def fake_ctx(lb, done):
    async def power(host, action):
        done.append((host, action))
        return f"{host}: powering {action}"
    return SimpleNamespace(power=power, host_of=lambda agent: P.host_of(lb, agent))


def said(result):
    return result["content"][0]["text"]


def test_power_with_an_agent_powers_its_machine_from_lab_yaml(monkeypatch):
    lb, done = lab(), []
    power = tools(fake_ctx(lb, done), monkeypatch)["power"]
    out = asyncio.run(power({"agent": "Flash", "action": "on"}))
    assert done == [("mgr-vm-01", "on")]
    assert said(out) == "Flash runs on mgr-vm-01. mgr-vm-01: powering on"


def test_power_with_an_unknown_agent_powers_nothing(monkeypatch):
    lb, done = lab(), []
    power = tools(fake_ctx(lb, done), monkeypatch)["power"]
    out = asyncio.run(power({"agent": "Bolt", "action": "on"}))
    assert done == []
    assert "no agent 'Bolt'" in said(out)


def test_power_with_a_host_is_unchanged_and_needs_one_of_them(monkeypatch):
    lb, done = lab(), []
    power = tools(fake_ctx(lb, done), monkeypatch)["power"]
    asyncio.run(power({"host": "gpu-b", "action": "off"}))
    assert done == [("gpu-b", "off")]
    assert "say which" in said(asyncio.run(power({"action": "on"})))
    assert "action must be" in said(asyncio.run(power({"agent": "Flash", "action": "up"})))
    assert done == [("gpu-b", "off")]


def test_every_turn_starts_with_the_deployed_layout():
    lb, prompts = lab(), []

    async def turn(ctx, conv, prompt):
        prompts.append(prompt)
        return "ok"
    sec = Secretary.__new__(Secretary)
    sec.lab, sec.slack, sec.via, sec._locks, sec.workers = lb, None, "in Slack", {}, None
    sec.brain = SimpleNamespace(turn=turn)
    sec.state = SimpleNamespace(
        conversation=lambda ch, ts: SimpleNamespace(key=ch, channel=ch, thread_ts=ts),
        save=lambda: None)
    msg = Incoming("D1", None, "1.0", None, "alice", "power on my manager", True, True)
    asyncio.run(sec.on_message(msg))
    lb.agents["mgr-alice"]["host"] = "gpu-b"        # moved, then redeployed
    asyncio.run(sec.on_message(msg))
    assert prompts[0].startswith("[lab, as deployed now:")
    assert "mgr-vm-01 (nebius, Alice's): Flash (manager)" in prompts[0]
    assert "gpu-b (nebius, Alice's): Flash (manager), worker-b (worker)" in prompts[1]
    assert prompts[1].rstrip().endswith("power on my manager")
