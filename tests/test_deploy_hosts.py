"""#61: lab apply across several hosts: unreachable hosts are skipped fast, one deploy at a
time, agents removed from lab.yaml are stopped, and no two agents share a port."""
import subprocess
from pathlib import Path

import yaml

from agentic_lab import apply as A
from agentic_lab import registry
from agentic_lab.cli import deploy_lock, main

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"


class Tok:
    def get(self, k):
        return "t"


def test_an_unreachable_host_is_skipped_fast(tmp_path, monkeypatch):
    lab = registry.load(EXAMPLE)
    plan = A.host_plan(lab, "gpu-a", Tok(), lambda owner, name: "secret")
    plan.update(lab_path=str(EXAMPLE), source=str(Path(__file__).parent.parent),
                applied={})
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 255)       # ssh: no route / timed out
    monkeypatch.setattr(A, "bundle", lambda *a, **k: None)
    monkeypatch.setattr(A.subprocess, "run", run)
    monkeypatch.setattr(A.os, "geteuid", lambda: 1000)
    assert A.run_bundle(plan, local=False, admin="admin") == A.UNREACHABLE
    assert len(calls) == 1 and "ConnectTimeout=10" in calls[0] and "BatchMode=yes" in calls[0]


def test_one_deploy_at_a_time(tmp_path):
    p = str(tmp_path / "deploy.lock")
    first = deploy_lock(p)
    assert first and deploy_lock(p) is False
    import os
    os.close(first)
    again = deploy_lock(p)
    assert again
    os.close(again)


def test_agents_removed_from_lab_yaml_are_stopped():
    lab = registry.load(EXAMPLE)
    plan = A.host_plan(lab, "gpu-a", Tok(), lambda owner, name: "secret")
    s = A.script(plan)
    assert 'keep=" lab-worker@worker-a "' in s and 'keep_runs=" lab-run-worker-a@.service "' in s
    assert 'systemctl disable --now "$u"' in s and 'rm -f "/etc/agentic-lab/$role-$id.env"' in s
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0
    # a host with no worker any more: no runs sudo rule left behind
    plan = A.host_plan(lab, "mgr-vm-01", Tok(), lambda owner, name: "secret")
    assert "rm -f /etc/sudoers.d/agentic-lab-runs" in A.script(plan)


def test_the_cleanup_keeps_listed_agents_and_stops_the_rest(tmp_path):
    """The generated loop, run against a fake systemd tree."""
    lab = registry.load(EXAMPLE)
    plan = A.host_plan(lab, "gpu-a", Tok(), lambda owner, name: "secret")
    root = tmp_path / "root"
    wants = root / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    (root / "etc/agentic-lab").mkdir()
    for u in ("lab-worker@worker-a", "lab-worker@old-worker"):
        (wants / f"{u}.service").write_text("")
        (root / f"etc/agentic-lab/worker-{u.split('@')[1]}.env").write_text("")
    for r in ("worker-a", "old-worker"):
        (root / f"etc/systemd/system/lab-run-{r}@.service").write_text("")
    frag = "\n".join(A.removed_agents_script(plan)).replace("/etc/", f"{root}/etc/")
    log = tmp_path / "systemctl.log"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    (fake / "systemctl").chmod(0o755)
    r = subprocess.run(["bash", "-c", 'say() { echo "== $*"; }\n' + frag], text=True,
                       capture_output=True, env={"PATH": f"{fake}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert log.read_text().strip() == "disable --now lab-worker@old-worker"
    assert not (root / "etc/agentic-lab/worker-old-worker.env").exists()
    assert (root / "etc/agentic-lab/worker-worker-a.env").exists()
    assert not (root / "etc/systemd/system/lab-run-old-worker@.service").exists()
    assert (root / "etc/systemd/system/lab-run-worker-a@.service").exists()


def test_no_two_agents_share_a_port(tmp_path, monkeypatch, capsys):
    d = yaml.safe_load(EXAMPLE.read_text())
    d["agents"]["worker-b"]["host"] = "gpu-a"           # two workers, both on 8770
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    errs = [str(i) for i in registry.check(registry.load(p)) if i.level == "error"]
    assert any("port 8770 on host gpu-a is taken by worker-a" in e for e in errs)
    d["agents"]["worker-b"]["port"] = 8780
    p.write_text(yaml.safe_dump(d))
    assert [i for i in registry.check(registry.load(p)) if i.level == "error"] == []
    # lab init gives each worker on one host its own port
    monkeypatch.setattr("agentic_lab.init.tailscale_self", lambda: ("t.ts.net", "s.t.ts.net"))
    monkeypatch.setattr("agentic_lab.init.tailscale_login", lambda: "a@example.com")
    out = tmp_path / "init.yaml"
    assert main(["init", str(out), "--name", "acme", "--admin", "alice", "--admin-name",
                 "Alice", "--slack-user", "U1", "--worker", "w1@gpu", "--worker",
                 "w2@gpu"]) == 0
    lab = registry.load(out)
    assert lab.address("w1")[1] != lab.address("w2")[1]
