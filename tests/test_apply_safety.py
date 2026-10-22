"""lab apply runs as root: it ships only committed code, owned by root, acts inside
agents' homes only as their owners, validates what lab.yaml puts into commands, and records
what a host runs only once it runs it."""
import subprocess
from pathlib import Path

import pytest
import yaml

from agentic_lab import apply as A
from agentic_lab import registry

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
REPO = Path(__file__).parent.parent


class Tok:
    def get(self, k):
        return "t"


def plan(host="gpu-a"):
    lab = registry.load(EXAMPLE)
    return A.host_plan(lab, host, Tok(), lambda owner, name: "secret")


def test_files_in_agent_homes_are_written_as_their_owner():
    s = A.script(plan())
    lines = [ln for ln in s.splitlines() if "/home/lab-worker-a/" in ln]
    assert lines and all(ln.startswith("runuser -u lab-worker-a -- ") or "setfacl -m" in ln
                         for ln in lines if "files/" in ln or "install" in ln)
    assert "install -m 600 -o lab-worker-a" not in s and "install -d -m 700 -o" not in s
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0


def test_the_owner_write_replaces_the_file(tmp_path):
    """The command apply runs for each file: a new file, then an atomic rename into place."""
    s = A.script(plan())
    line = next(ln for ln in s.splitlines() if ln.startswith("runuser -u lab-worker-a -- sh -c"))
    import shlex
    argv = shlex.split(line.split(" -- ", 1)[1].rsplit(" < ", 1)[0])   # sh -c SCRIPT _ DIR PATH
    assert argv[:2] == ["sh", "-c"] and argv[3] == "_"
    target = tmp_path / "home" / "cfg" / "secret"
    other = tmp_path / "elsewhere"
    other.write_text("unchanged")
    target.parent.mkdir(parents=True)
    target.symlink_to(other)
    r = subprocess.run(["sh", "-c", argv[2], "_", str(target.parent), str(target)],
                       input="new content", text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert not target.is_symlink() and target.read_text() == "new content"
    assert other.read_text() == "unchanged"


def test_the_framework_is_root_owned_and_only_committed_files_ship(tmp_path):
    s = A.script(plan())
    assert "tar --no-same-owner --no-same-permissions -xzf" in s
    assert "chown -R root:root /opt/agentic-lab/src.new && chmod -R go-w" in s
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,  # noqa
                                    env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                         "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    git("init", "-q")
    (repo / "kept.py").write_text("x = 1\n")
    git("add", "kept.py")
    git("commit", "-qm", "c")
    (repo / "stray.py").write_text("y = 2\n")             # never committed
    import io
    import tarfile
    names = tarfile.open(fileobj=io.BytesIO(A.source_archive(str(repo))), mode="r:gz").getnames()
    assert names == ["kept.py"]


def test_the_record_comes_last():
    s = A.script(plan())
    assert s.rstrip().splitlines()[-1] == \
        'install -m 644 "$B/applied.yaml" /etc/agentic-lab/applied.yaml'
    assert sum('"$B/applied.yaml"' in ln for ln in s.splitlines()) == 1


def test_names_used_in_commands_are_validated(tmp_path):
    d = yaml.safe_load(EXAMPLE.read_text())
    d["hosts"]["gpu-a"]["ssh_user"] = "-oFoo=bar"
    d["hosts"]["gpu-b"]["tailnet_name"] = "gpu-b.example.ts.net;true"
    d["hosts"]["Bad Host"] = {"kind": "onprem"}
    d["tailnet"] = "example ts net"
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    errs = " ".join(str(i) for i in registry.check(registry.load(p)) if i.level == "error")
    assert "ssh_user '-oFoo=bar' isn't a Linux user name" in errs
    assert "tailnet_name" in errs and "isn't a DNS name" in errs
    assert "hosts.Bad Host" in errs and "tailnet" in errs


def test_apply_refuses_a_lab_yaml_that_fails_lab_check(tmp_path):
    from agentic_lab.cli import deploy
    d = yaml.safe_load(EXAMPLE.read_text())
    d["hosts"]["gpu-a"]["ssh_user"] = "-oFoo=bar"
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(SystemExit, match="doesn't pass lab check"):
        deploy(str(p), {}, str(REPO), "admin", hosts=["gpu-a"])


def test_ssh_targets_follow_a_separator(monkeypatch):
    p = plan()
    p.update(lab_path=str(EXAMPLE), source=str(REPO), applied={})
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(A, "bundle", lambda *a, **k: None)
    monkeypatch.setattr(A.subprocess, "run", run)
    monkeypatch.setattr(A.os, "geteuid", lambda: 1000)
    A.run_bundle(p, local=False, admin="admin")
    for argv in calls:
        i = argv.index("--")
        assert all(not x.startswith(("admin@", "gpu")) for x in argv[:i])
