"""lab plan / apply: what each host gets, without touching a host."""
from pathlib import Path

import yaml

from agentic_lab import apply as A
from agentic_lab import registry

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
SINGLE = Path(__file__).parent.parent / "examples" / "single-vm" / "lab.yaml"


def fake_intake(have):
    return lambda owner, name: have.get((owner, name))


def test_tokens_are_generated_once_and_private(tmp_path):
    t = A.Tokens(tmp_path)
    a = t.get("mgr:mgr-alice:worker-a")
    assert len(a) == 64 and t.get("mgr:mgr-alice:worker-a") == a
    t.save()
    assert (tmp_path / "tokens.yaml").stat().st_mode & 0o777 == 0o600
    assert A.Tokens(tmp_path).get("mgr:mgr-alice:worker-a") == a


def test_single_vm_plan(tmp_path):
    lab = registry.load(SINGLE)
    tokens = A.Tokens(tmp_path)
    p = A.host_plan(lab, "dev-vm", tokens, fake_intake({
        ("alice", "claude-token"): "sk-ant-x\n",
        ("service:mgr-alice", "slack-app"): "SLACK_BOT_TOKEN=a\n",
        ("service:broker", "slack-app"): "SLACK_BOT_TOKEN=b\n"}))
    assert p["missing"] == [] and p["secretary_host"]
    assert {u for _, u in p["units"]} == {"lab-worker@worker-a", "lab-manager@mgr-alice",
                                          "lab-broker", "lab-intake.path", "lab-secretary"}
    # the secretary monitors the worker with a read-only token the worker knows
    mon = yaml.safe_load(p["files"]["/home/lab-secretary/.config/agentic-lab/monitor.yaml"][2])
    wtok = yaml.safe_load(p["files"]["/home/lab-worker-a/.config/agentic-lab/manager-tokens.yaml"][2])
    assert mon["worker-a"] == wtok["secretary"]
    f = p["files"]
    # the manager and the worker hold the same generated token, and so does the broker
    mgr_side = yaml.safe_load(f["/home/lab-mgr-alice/.config/agentic-lab/workers.yaml"][2])
    wrk_side = yaml.safe_load(f["/home/lab-worker-a/.config/agentic-lab/manager-tokens.yaml"][2])
    assert mgr_side["worker-a"] == wrk_side["mgr-alice"]
    brk = yaml.safe_load(f["/home/lab-broker/.config/agentic-lab/agent-tokens.yaml"][2])
    wb = yaml.safe_load(f["/home/lab-worker-a/.config/agentic-lab/broker.yaml"][2])
    assert brk["worker-a"] == wb["token"] and wb["url"].endswith(":8772")
    assert all(mode == "600" for _, mode, _ in f.values())


def test_missing_secrets_are_listed(tmp_path):
    lab = registry.load(SINGLE)
    p = A.host_plan(lab, "dev-vm", A.Tokens(tmp_path), fake_intake({}))
    assert any("alice's Claude token" in m for m in p["missing"])
    assert any("Lab Approvals" in m for m in p["missing"])


def test_script_and_bundle(tmp_path):
    lab = registry.load(SINGLE)
    p = A.host_plan(lab, "dev-vm", A.Tokens(tmp_path / "t"), fake_intake({
        ("alice", "claude-token"): "sk-ant-x\n",
        ("service:mgr-alice", "slack-app"): "x\n", ("service:broker", "slack-app"): "y\n"}))
    sh = A.script(p)
    assert "create-agent-user.sh lab-worker-a" in sh and "systemctl restart lab-broker" in sh
    assert "sk-ant" not in sh                                 # secrets travel as files only
    A.bundle(p, SINGLE, Path(__file__).parent.parent, tmp_path / "b")
    assert (tmp_path / "b" / "apply.sh").exists() and (tmp_path / "b" / "source.tgz").exists()
    assert (tmp_path / "b" / "files" / "0").stat().st_mode & 0o777 == 0o600


def test_find_source():
    assert (Path(A.find_source()) / "pyproject.toml").exists()


def test_services_wait_for_their_secrets(tmp_path):
    lab = registry.load(SINGLE)
    p = A.host_plan(lab, "dev-vm", A.Tokens(tmp_path), fake_intake({
        ("alice", "claude-token"): "sk-ant-x\n"}))           # no Slack apps yet
    assert set(p["held"]) == {"lab-manager@mgr-alice", "lab-broker"}
    sh = A.script(p)
    assert "systemctl disable --now lab-broker" in sh and "systemctl restart lab-intake" in sh
    assert "systemctl restart lab-worker@worker-a" in sh


def test_fresh_install_keeps_the_intake_running(tmp_path):
    lab = registry.load(SINGLE)
    p = A.host_plan(lab, "dev-vm", A.Tokens(tmp_path), fake_intake({}))   # nothing yet
    assert "lab-intake" not in p["held"] and "lab-secretary" not in p["held"]
    assert set(p["held"]) == {"lab-manager@mgr-alice", "lab-broker"}
