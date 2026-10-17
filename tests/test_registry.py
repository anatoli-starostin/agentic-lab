"""lab.yaml loading and `lab check`."""
import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
import yaml

from agentic_lab import registry
from agentic_lab.policy import grants as G
from agentic_lab.cli import main

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal"
TODAY = dt.date(2026, 10, 17)


@pytest.fixture
def lab_dir(tmp_path):
    d = tmp_path / "lab"
    shutil.copytree(EXAMPLE, d)
    return d


def edit(lab_dir, fn):
    p = lab_dir / "lab.yaml"
    data = yaml.safe_load(p.read_text())
    fn(data)
    p.write_text(yaml.safe_dump(data))
    return registry.check(registry.load(p), TODAY)


def errors(issues):
    return [str(i) for i in issues if i.level == "error"]


def test_example_is_clean():
    issues = registry.check(registry.load(EXAMPLE / "lab.yaml"), TODAY)
    assert errors(issues) == []
    assert [i.where for i in issues if i.level == "warning"] == []


def test_example_agent_settings():
    lab = registry.load(EXAMPLE / "lab.yaml")
    w = lab.agent_settings("worker-a")
    assert w["sandbox"]["network"]["allowedDomains"] == [
        "*.example.ts.net", "pypi.org", "files.pythonhosted.org"]
    m = lab.agent_settings("mgr-alice")
    assert {"WebFetch", "WebSearch"} <= set(m["permissions"]["allow"])
    assert m["sandbox"]["network"]["allowedDomains"] == ["*.example.ts.net"]
    b = lab.agent_settings("worker-b")             # no grants: the bare model
    assert b["sandbox"]["network"]["allowedDomains"] == ["*.example.ts.net"]


def test_timed_grant_through_registry():
    lab = registry.load(EXAMPLE / "lab.yaml")
    now = dt.datetime(2026, 10, 17, 12)
    g = G.timed("domain", "arxiv.org", dt.timedelta(hours=1), "alice", now)
    s = lab.agent_settings("worker-b", [g], now=now)
    assert "arxiv.org" in s["sandbox"]["network"]["allowedDomains"]


def test_every_agent_needs_a_human(lab_dir):
    errs = errors(edit(lab_dir, lambda d: d["agents"]["worker-a"].update(human="bob")))
    assert any("human 'bob' is not in humans" in e for e in errs)
    errs = errors(edit(lab_dir, lambda d: d["agents"]["worker-a"].pop("human")))
    assert any("missing required key 'human'" in e for e in errs)


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["agents"]["worker-a"].update(role="boss"), "role must be one of"),
    (lambda d: d["agents"]["worker-a"].update(host="nowhere"), "host 'nowhere' is not in hosts"),
    (lambda d: d["agents"]["worker-a"].update(acces="shared"), "unknown key 'acces'"),
    (lambda d: d["agents"]["worker-a"].update(access="public"), "access must be one of"),
    (lambda d: d["agents"]["mgr-alice"].update(access="shared"), "applies to workers only"),
    (lambda d: d["agents"]["worker-a"].update(slack_app="A1"), "applies to managers only"),
    (lambda d: d["agents"]["worker-a"].update(max_parallel_gpu_tasks=True), "expected int"),
    (lambda d: d["agents"].pop("secretary"), "exactly one secretary"),
    (lambda d: d["agents"].update(sec2={"role": "secretary", "human": "alice", "host": "nuc-01"}),
     "exactly one secretary"),
    (lambda d: d["humans"]["alice"].pop("channels"), "no permission channel"),
    (lambda d: d["humans"]["alice"]["channels"][0].update(type="pager"), "type must be one of"),
    (lambda d: d["humans"]["alice"]["channels"][0].update(on_timeout="approve"),
     "a timeout never approves"),
    (lambda d: d["humans"]["alice"]["channels"][0].update(timeout_s=0), "must be positive"),
    (lambda d: d["hosts"]["gpu-a"].pop("provider"), "needs a provider"),
    (lambda d: d["hosts"]["gpu-a"].update(power="sometimes"), "power must be one of"),
    (lambda d: d["hosts"]["mgr-vm-01"].update(restart_by="mgr-alice"), "must name the secretary"),
    (lambda d: d["credentials"][0].update(host="nowhere"), "is not in hosts"),
    (lambda d: d["credentials"][0].update(agent="ghost"), "agent 'ghost' is not in agents"),
    (lambda d: d["credentials"][0].pop("expires"), "missing required key 'expires'"),
    (lambda d: d["agents"]["worker-a"].update(workdir="work"), "absolute or ~/"),
    (lambda d: d["agents"]["worker-a"]["grants"].append({"domain": "*"}), "not a domain"),
    (lambda d: d["agents"]["worker-a"]["grants"].append(
        {"domain": "arxiv.org", "expires": dt.datetime(2026, 10, 18)}), "don't expire"),
    (lambda d: d.update(trust_tailnet="yes"), "expected bool"),
    (lambda d: d.update(extra=1), "unknown key 'extra'"),
])
def test_check_errors(lab_dir, mutate, expect):
    errs = errors(edit(lab_dir, mutate))
    assert any(expect in e for e in errs), errs


def test_no_tailnet_warns(lab_dir):
    warns = [i.message for i in edit(lab_dir, lambda d: d.pop("tailnet")) if i.level == "warning"]
    assert any("no tailnet set" in w for w in warns)


def test_credential_expiry(lab_dir):
    issues = edit(lab_dir, lambda d: d["credentials"][2].update(expires=dt.date(2026, 10, 25)))
    assert any(i.level == "warning" and "expires in 8 days" in i.message for i in issues)
    issues = edit(lab_dir, lambda d: d["credentials"][2].update(expires=dt.date(2026, 10, 1)))
    assert any(i.level == "error" and "expired 16 days ago" in i.message for i in issues)


def test_warnings(lab_dir):
    issues = edit(lab_dir, lambda d: (d["agents"]["mgr-alice"].pop("slack_app"),
                                      d["hosts"]["nuc-01"].update(kind="cloud", provider="nebius")))
    warns = [i.message for i in issues if i.level == "warning"]
    assert any("no slack_app" in w for w in warns)
    assert any("on-prem" in w for w in warns)


def test_dedicated_worker_without_manager_warns(lab_dir):
    def mutate(d):
        d["humans"]["bob"] = {}
        d["agents"]["worker-b"]["human"] = "bob"
    warns = [i.message for i in edit(lab_dir, mutate) if i.level == "warning"]
    assert any("dedicated to 'bob'" in w for w in warns)


def test_cli_check(capsys):
    assert main(["check", str(EXAMPLE / "lab.yaml")]) == 0
    out = capsys.readouterr().out
    assert "1 human, 2 workers, 1 manager, 1 secretary, 4 hosts" in out
    assert "0 errors, 0 warnings" in out


def test_cli_check_fails_on_error(lab_dir, capsys):
    edit(lab_dir, lambda d: d["agents"].pop("secretary"))
    assert main(["check", str(lab_dir / "lab.yaml")]) == 1


def test_cli_settings(capsys):
    assert main(["--lab", str(EXAMPLE / "lab.yaml"), "settings", "worker-a"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sandbox"]["enabled"] is True
