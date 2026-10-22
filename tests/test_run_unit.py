"""A long run writes only where it must: its working folder and its own status."""
import json
import shutil
import subprocess

import pytest

from agentic_lab.worker import runs as R


def test_the_home_is_read_only_but_the_work_isnt():
    u = R.unit_template("worker-a", "lab-worker-a", home="/home/lab-worker-a",
                        workdir="/home/lab-worker-a/work").splitlines()
    assert "ReadOnlyPaths=/home/lab-worker-a" in u
    assert ("ReadWritePaths=-/home/lab-worker-a/work "
            "-/home/lab-worker-a/.local/state/agentic-lab/runs/%i") in u
    assert "InaccessiblePaths=-/home/lab-worker-a/.config/agentic-lab" in u


def test_a_run_has_its_own_process_view():
    u = R.unit_template("worker-a", "lab-worker-a").splitlines()
    start = next(ln for ln in u if ln.startswith("ExecStart="))
    assert start.startswith("ExecStart=/usr/bin/bwrap --die-with-parent --unshare-pid "
                            "--dev-bind / / --proc /proc -- /opt/agentic-lab/venv/bin/lab ")


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd here")
def test_the_unit_parses(tmp_path):
    text = "\n".join(ln for ln in R.unit_template("w", "nobody").splitlines()
                     if not ln.startswith(("User=", "Group=")))
    (tmp_path / "lab-run-w@.service").write_text(text)
    r = subprocess.run(["systemd-analyze", "verify", str(tmp_path / "lab-run-w@x.service")],
                       capture_output=True, text=True)
    assert "Unknown key" not in r.stderr, r.stderr


def runs(tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    return R.RunStore(str(tmp_path / "state"), str(tmp_path / "work"), "worker-a",
                      systemctl=lambda *a: False)


def test_each_run_has_a_status_folder_of_its_own(tmp_path):
    rs = runs(tmp_path)
    r = rs.start("t-1", "x", "true")
    folder = tmp_path / "state" / r["id"]
    assert folder.is_dir() and rs._path(r["id"], ".status") == str(folder / "status.json")


def test_a_status_says_how_the_run_went_and_nothing_else(tmp_path):
    rs = runs(tmp_path)
    r = rs.start("t-1", "x", "true")
    (tmp_path / "state" / r["id"] / "status.json").write_text(json.dumps(
        {"state": "exited", "exit_code": 0, "task": "t-other", "command": "other"}))
    v = rs.view(r["id"])
    assert v["state"] == "exited" and v["task"] == "t-1" and v["command"] == "true"


def test_runs_from_before_still_read(tmp_path):
    rs = runs(tmp_path)
    r = rs.start("t-1", "x", "true")
    (tmp_path / "state" / f"{r['id']}.status.json").write_text(
        json.dumps({"state": "exited", "exit_code": 3}))
    assert rs.view(r["id"])["exit_code"] == 3


def test_tools_keep_their_files_in_the_working_folder():
    env = R.home_env("/w")
    assert env["XDG_CONFIG_HOME"] == "/w/.config" and env["XDG_DATA_HOME"] == "/w/.local/share"
