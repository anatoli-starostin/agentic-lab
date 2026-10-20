"""#43: the host's humans may read agents' working folders with plain Linux tools, when the
host lists them as `readers`; secrets and state stay closed."""
import subprocess
from pathlib import Path

import yaml

from agentic_lab import registry
from agentic_lab.apply import host_plan, readers_script, script

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
DEPLOY = Path(__file__).parent.parent / "deploy"


def lab_with(tmp_path, readers):
    d = yaml.safe_load(EXAMPLE.read_text())
    if readers is not None:
        d["hosts"]["gpu-a"]["readers"] = readers
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    return registry.load(p)


class Tok:
    def get(self, k):
        return "t"


def test_readers_open_the_working_folder_and_nothing_else(tmp_path):
    lab = lab_with(tmp_path, ["alice", "bob"])
    assert [i for i in registry.check(lab) if i.level == "error"] == []
    plan = host_plan(lab, "gpu-a", Tok(), lambda owner, name: "secret")
    assert plan["readers"] == ["alice", "bob"]
    assert plan["readable"] == {"lab-worker-a": "/home/lab-worker-a/work"}
    s = "\n".join(readers_script(plan["readers"], plan["readable"]))
    assert 'gpasswd -M "${members#,}" lab-readers' in s
    assert "setfacl -m g:lab-readers:x /home/lab-worker-a" in s            # traverse only
    assert "setfacl -R -d -m g:lab-readers:rX /home/lab-worker-a/work" in s  # later files too
    assert "! -path /home/lab-worker-a/work -exec chmod go-rwx" in s       # the rest: private
    assert "w" not in s.split("g:lab-readers:")[1].split()[0]               # never write
    assert subprocess.run(["bash", "-n"], input=script(plan), text=True).returncode == 0
    deep = "\n".join(readers_script(["alice"], {"lab-m": "/home/lab-m/projects/w"}))
    assert "not shared" in deep and "setfacl -m" not in deep and "setfacl -R" not in deep
    assert subprocess.run(["bash", "-n"], input=deep, text=True).returncode == 0
    # the agent's tmp: stable, in its working folder, only with readers
    assert lab.agent_tmpdir("worker-a").endswith("/work/tmp")
    assert lab.agent_settings("worker-a")["env"]["TMPDIR"].endswith("/work/tmp")
    assert lab.agent_tmpdir("worker-b") is None
    assert "TMPDIR" not in lab.agent_settings("worker-b")["env"]


def test_without_readers_nothing_opens(tmp_path):
    lab = lab_with(tmp_path, None)
    plan = host_plan(lab, "gpu-a", Tok(), lambda owner, name: "secret")
    s = "\n".join(readers_script(plan["readers"], plan["readable"]))
    assert "setfacl" not in s and "gpasswd -M ''" in s                     # group emptied
    assert lab.agent_tmpdir("worker-a") is None


def test_readers_must_be_user_names(tmp_path):
    lab = lab_with(tmp_path, ["alice", "Bad User", 3])
    errs = [str(i) for i in registry.check(lab) if i.level == "error"]
    assert len([e for e in errs if "isn't a Linux user name" in e]) == 2


def test_agents_files_are_private_by_default():
    for unit in (DEPLOY / "worker" / "lab-worker@.service",
                 DEPLOY / "manager" / "lab-manager@.service"):
        assert "UMask=0077" in unit.read_text()
    from agentic_lab.worker.runs import unit_template
    assert "UMask=0077" in unit_template("worker-a", "lab-worker-a")
