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


def key_file(tmp_path, monkeypatch):
    import json
    d = tmp_path / ".config" / "agentic-lab"
    d.mkdir(parents=True)
    (d / "aws-key.json").write_text(json.dumps({"access_key_id": "AKIAEXAMPLEEXAMPLE00",
                                                "secret_access_key": "x" * 40}))
    monkeypatch.setenv("HOME", str(tmp_path))


def test_no_boto3_is_a_clear_error(monkeypatch, tmp_path):
    import builtins
    key_file(tmp_path, monkeypatch)
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


def test_a_powered_aws_host_needs_its_region(tmp_path):
    import yaml
    from pathlib import Path

    from agentic_lab import registry
    d = yaml.safe_load((Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml")
                       .read_text())
    d["hosts"]["gpu-x"] = {"kind": "cloud", "provider": "aws", "instance": "i-0",
                           "idle_stop_min": 30}
    d["hosts"]["mgr-x"] = {"kind": "cloud", "provider": "aws", "idle_stop_min": 0}  # unpowered
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    issues = [str(i) for i in registry.check(registry.load(p))]
    assert any(i.startswith("ERROR") and "gpu-x" in i and "region" in i for i in issues)
    assert not any("mgr-x" in i for i in issues)
    d["hosts"]["gpu-x"]["region"] = "eu-west-1"
    p.write_text(yaml.safe_dump(d))
    assert not any("gpu-x" in str(i) and "region" in str(i)
                   for i in registry.check(registry.load(p)))


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
    assert "uv pip install -q -p /opt/agentic-lab/venv '/opt/agentic-lab/src[aws]'" in script()
    other = next(a["host"] for a in lb.agents.values() if a.get("role") == "worker")
    assert other != sec and "boto3" not in A.script(A.host_plan(          # the secretary's only
        lb, other, SimpleNamespace(get=lambda k: "t"), lambda owner, name: None))


class FakeBoto3:
    def __init__(self):
        self.clients = []

    def client(self, service, **kw):
        self.clients.append((service, kw))
        return FakeEC2(state="running")


@pytest.fixture
def boto(monkeypatch):
    import sys
    import types
    fake = FakeBoto3()
    monkeypatch.setitem(sys.modules, "boto3", fake)
    config = types.ModuleType("botocore.config")
    config.Config = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.config", config)
    return fake


def test_the_secretarys_own_key_and_nothing_else(boto, tmp_path, monkeypatch):
    key_file(tmp_path, monkeypatch)
    assert asyncio.run(P.status(lab(), "gpu")) == "RUNNING"
    service, kw = boto.clients[0]
    assert service == "ec2" and kw["region_name"] == "eu-west-1"
    assert kw["aws_access_key_id"] == "AKIAEXAMPLEEXAMPLE00"
    assert kw["aws_secret_access_key"] == "x" * 40


def test_no_key_no_call_never_the_default_chain(boto, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))                     # no key file
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFROMTHEENVIRONMENT")
    with pytest.raises(P.PowerError, match="no AWS key"):
        asyncio.run(P.status(lab(), "gpu"))
    assert boto.clients == []                                    # boto3 never asked


def test_the_key_comes_through_the_intake():
    import json

    from agentic_lab.secrets import intake as I
    v = json.loads(I.check_value("aws-key", {"access_key_id": "AKIAEXAMPLEEXAMPLE00",
                                             "secret_access_key": "x" * 40}))
    assert v == {"access_key_id": "AKIAEXAMPLEEXAMPLE00", "secret_access_key": "x" * 40}
    with pytest.raises(I.IntakeError):
        I.check_value("aws-key", {"access_key_id": "nope", "secret_access_key": "x" * 40})


def test_lab_apply_installs_it_for_the_secretary_only():
    from pathlib import Path

    from agentic_lab import apply as A
    from agentic_lab import registry
    lb = registry.load(Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml")
    sec = next(a for a, x in lb.agents.items() if x.get("role") == "secretary")
    user = lb.unix_user(sec)
    plan = A.host_plan(lb, lb.agents[sec]["host"], SimpleNamespace(get=lambda k: "t"),
                       lambda owner, name: '{"access_key_id": "AKIA0"}'
                       if (owner, name) == (f"service:{sec}", "aws-key") else None)
    path = f"/home/{user}/.config/agentic-lab/aws-key.json"
    assert plan["files"][path][:2] == (user, "600")


def test_doctor_checks_the_key_against_each_aws_host(tmp_path):
    import json
    import os
    import pwd

    from agentic_lab import doctor as D
    me = pwd.getpwuid(os.getuid())
    pw = SimpleNamespace(pw_dir=str(tmp_path), pw_uid=me.pw_uid, pw_name=me.pw_name)
    assert D.aws_checks(lab(), pw)[0].status == D.FAIL                  # no key yet
    d = tmp_path / ".config" / "agentic-lab"
    d.mkdir(parents=True)
    (d / "aws-key.json").write_text(json.dumps({"access_key_id": "AKIA0",
                                                "secret_access_key": "x"}))
    os.chmod(d / "aws-key.json", 0o600)
    asked = []

    def ec2(region, key):
        asked.append((region, key))
        return FakeEC2()
    res = D.aws_checks(lab(), pw, ec2=ec2)
    assert [r.status for r in res] == [D.OK, D.OK, D.OK]     # the key, then each AWS host
    assert asked == [(None, str(d / "aws-key.json")), ("eu-west-1", str(d / "aws-key.json"))]


def test_endpoints_from_the_environment_cant_redirect_the_calls(boto, tmp_path, monkeypatch):
    key_file(tmp_path, monkeypatch)
    asyncio.run(P.status(lab(), "gpu"))
    assert boto.clients[0][1]["config"]["ignore_configured_endpoint_urls"] is True


def test_lab_check_warns_without_an_aws_key_rotation_entry(tmp_path):
    import yaml
    from pathlib import Path

    from agentic_lab import registry
    d = yaml.safe_load((Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml")
                       .read_text())
    d["hosts"]["gpu-x"] = {"kind": "cloud", "provider": "aws", "instance": "i-0",
                           "region": "eu-west-1", "idle_stop_min": 30}
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    issues = [str(i) for i in registry.check(registry.load(p))]
    warn = [i for i in issues if "aws-key" in i]
    assert len(warn) == 1 and warn[0].startswith("WARN") and "gpu-x" in warn[0]
    d.setdefault("credentials", []).append({"name": "aws-key", "host": "gpu-x",
                                            "expires": "2099-01-01"})
    p.write_text(yaml.safe_dump(d))
    assert not any("aws-key" in str(i) for i in registry.check(registry.load(p)))
