"""~/.lab-keep-awake: someone working on a machine directly keeps it from being stopped."""
import os
import subprocess
from pathlib import Path

from agentic_lab.secretary import power as P
from agentic_lab.worker.server import keep_awake

SCRIPT = Path(__file__).parent.parent / "deploy" / "worker" / "lab-keep-awake.sh"


def test_scanner_lists_who_keeps_the_machine_awake(tmp_path):
    homes = {u: tmp_path / "home" / u for u in ("alice", "bob", "svc")}
    for h in homes.values():
        h.mkdir(parents=True)
    (homes["alice"] / ".lab-keep-awake").write_text("CPU sweep until Friday\nmore\n")
    (homes["svc"] / ".lab-keep-awake").write_text("")      # a system user: ignored
    passwd = tmp_path / "passwd"
    passwd.write_text(f"alice:x:1001:1001::{homes['alice']}:/bin/bash\n"
                      f"bob:x:1002:1002::{homes['bob']}:/bin/bash\n"
                      f"svc:x:998:998::{homes['svc']}:/usr/sbin/nologin\n")
    out = tmp_path / "lib" / "keep-awake"
    subprocess.run(["bash", str(SCRIPT)], check=True, env={
        **os.environ, "LAB_PASSWD": str(passwd), "LAB_KEEP_AWAKE_OUT": str(out)})
    assert keep_awake(str(out)) == [{"user": "alice", "note": "CPU sweep until Friday"}]
    assert oct(out.stat().st_mode)[-3:] == "644"
    assert keep_awake(str(tmp_path / "missing")) == []


def test_keep_awake_makes_the_machine_busy():
    seen = {"up": True, "queue": {}, "gpus": [{"name": "H100", "util": 0}],
            "gpu_processes": [], "gpu_readings": 5,
            "keep_awake": [{"user": "alice", "note": "CPU sweep"}]}
    assert P.worker_busy("w", seen) == "kept awake by alice (CPU sweep) (~/.lab-keep-awake)"
    seen["keep_awake"] = []
    assert P.worker_busy("w", seen) is None


def test_scanner_never_follows_links(tmp_path):
    """A link to a root-only file must not leak it into the world-readable list."""
    home = tmp_path / "home" / "mallory"
    home.mkdir(parents=True)
    secret = tmp_path / "shadow"
    secret.write_text("root:$6$hash:...\n")
    (home / ".lab-keep-awake").symlink_to(secret)
    passwd = tmp_path / "passwd"
    passwd.write_text(f"mallory:x:1003:1003::{home}:/bin/bash\n")
    out = tmp_path / "lib" / "keep-awake"
    subprocess.run(["bash", str(SCRIPT)], check=True, env={
        **os.environ, "LAB_PASSWD": str(passwd), "LAB_KEEP_AWAKE_OUT": str(out)})
    assert "hash" not in out.read_text() and keep_awake(str(out)) == []
