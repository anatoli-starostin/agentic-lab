"""VM power for AWS EC2 hosts (#68), against a fake EC2 client: the same rules as Nebius."""
import asyncio
from types import SimpleNamespace

import pytest

from agentic_lab.secretary import power as P

HOSTS = {"sec": {"kind": "cloud", "provider": "aws", "instance": "i-00000000000000001"},
         "gpu": {"kind": "cloud", "provider": "aws", "instance": "i-00000000000000002",
                 "region": "eu-west-1", "idle_stop_min": 30},
         "box": {"kind": "machine"}}


def lab():
    return SimpleNamespace(hosts=HOSTS, agents={"s": {"role": "secretary", "host": "sec"},
                                                "w": {"role": "worker", "host": "gpu"}})


class FakeEC2:
    def __init__(self, state="stopped", fail=None):
        self.state, self.fail, self.calls = state, fail, []

    def _call(self, name, ids):
        self.calls.append((name, ids))
        if self.fail:
            raise self.fail

    def describe_instances(self, InstanceIds):
        self._call("describe", InstanceIds)
        return {"Reservations": [{"Instances": [{"State": {"Name": self.state}}]}]}

    def start_instances(self, InstanceIds):
        self._call("start", InstanceIds)

    def stop_instances(self, InstanceIds):
        self._call("stop", InstanceIds)


@pytest.fixture
def ec2(monkeypatch):
    fake, regions = FakeEC2(), []

    def client(region):
        regions.append(region)
        return fake
    monkeypatch.setattr(P, "_ec2", client)
    fake.regions = regions
    return fake


@pytest.mark.parametrize("aws,lab_state", [("pending", "STARTING"), ("running", "RUNNING"),
                                           ("stopping", "STOPPING"), ("stopped", "STOPPED"),
                                           ("shutting-down", "STOPPING"),
                                           ("terminated", "DELETED")])
def test_status_in_the_labs_words(ec2, aws, lab_state):
    ec2.state = aws
    assert asyncio.run(P.status(lab(), "gpu")) == lab_state
    assert ec2.calls == [("describe", ["i-00000000000000002"])] and ec2.regions == ["eu-west-1"]


def test_start_and_stop(ec2):
    asyncio.run(P.start(lab(), "gpu"))
    asyncio.run(P.stop(lab(), "gpu"))
    assert ec2.calls == [("start", ["i-00000000000000002"]), ("stop", ["i-00000000000000002"])]


def test_the_secretarys_own_host_and_unpowerable_ones_are_refused(ec2):
    for host, why in (("sec", "runs the secretary itself"), ("box", "isn't a cloud VM")):
        with pytest.raises(P.PowerError, match=why):
            asyncio.run(P.stop(lab(), host))
    assert ec2.calls == []


def test_aws_errors_say_what_went_wrong(ec2):
    ec2.fail = RuntimeError("UnauthorizedOperation: not allowed")
    with pytest.raises(P.PowerError, match="AWS stop_instances: UnauthorizedOperation"):
        asyncio.run(P.stop(lab(), "gpu"))


def test_no_boto3_is_a_clear_error(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_boto(name, *a, **k):
        if name.startswith(("boto3", "botocore")):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_boto)
    with pytest.raises(P.PowerError, match="boto3 isn't installed"):
        asyncio.run(P.status(lab(), "gpu"))


def test_an_idle_aws_host_is_stopped_like_any_other():
    snap = {"agents": {"w": {"up": True, "queue": {}}}}
    watch = P.IdleWatch()
    import datetime as dt
    t0 = dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc)
    assert watch.candidates(lab(), snap, now=t0) == []
    assert watch.candidates(lab(), snap, now=t0 + dt.timedelta(minutes=30)) == ["gpu"]


def test_lab_apply_installs_the_sdk_only_for_aws_labs():
    from pathlib import Path

    from agentic_lab import apply as A
    from agentic_lab import registry
    lb = registry.load(Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml")
    sec = next(a["host"] for a in lb.agents.values() if a.get("role") == "secretary")

    def script():
        return A.script(A.host_plan(lb, sec, SimpleNamespace(get=lambda k: "t"),
                                    lambda owner, name: None))
    assert "boto3" not in script()
    lb.hosts["gpu-x"] = {"kind": "cloud", "provider": "aws", "instance": "i-0", "idle_stop_min": 30}
    assert "uv pip install -q -p /opt/agentic-lab/venv 'boto3>=1.34,<2'" in script()
    other = next(a["host"] for a in lb.agents.values() if a.get("role") == "worker")
    assert other != sec and "boto3" not in A.script(A.host_plan(          # the secretary's only
        lb, other, SimpleNamespace(get=lambda k: "t"), lambda owner, name: None))
