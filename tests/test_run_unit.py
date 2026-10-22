"""A long run writes only where it must: its working folder and its own status."""
import json
import os
import shutil
import stat
import subprocess

import pytest

from agentic_lab.worker import runs as R


def test_underneath_the_home_stays_read_only():
    """The unit's own layer, in case the job's mounts were ever wrong."""
    u = R.unit_template("worker-a", "lab-worker-a", home="/home/lab-worker-a",
                        workdir="/home/lab-worker-a/work",
                        writable=["/home/lab-worker-a/datasets"]).splitlines()
    assert "ReadOnlyPaths=/home/lab-worker-a" in u
    assert ("ReadWritePaths=-/home/lab-worker-a/work -/home/lab-worker-a/datasets "
            "-/home/lab-worker-a/.local/state/agentic-lab/runs/%i") in u
    assert "InaccessiblePaths=-/home/lab-worker-a/.config/agentic-lab" in u
    assert not any("run-home" in ln for ln in u)
    start = next(ln for ln in u if ln.startswith("ExecStart="))
    assert start.startswith("ExecStart=/opt/agentic-lab/venv/bin/lab ") and "bwrap" not in start


def policy(tmp_path, writes=(), gpu=True):
    from agentic_lab.policy import sandbox
    return sandbox.filesystem(str(tmp_path / "work"), writes, gpu)


def test_a_run_is_held_to_the_sandbox_policy(tmp_path, monkeypatch):
    """One source of truth: what the job may write and may not read is exactly what the
    agent's short commands get from Claude Code's sandbox."""
    from agentic_lab.policy import sandbox
    home = tmp_path / "home"
    for d in (".config/agentic-lab", ".local/state/agentic-lab", "work"):
        (home / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    fs = sandbox.settings(str(home / "work"), gpu=True)["sandbox"]["filesystem"]
    assert fs == sandbox.filesystem(str(home / "work"), gpu=True)
    w = R.job_wrapper(fs, str(home / "work"))
    assert w[:10] == ["/usr/bin/bwrap", "--unshare-pid", "--ro-bind", "/", "/",
                      "--dev-bind", "/dev", "/dev", "--proc", "/proc"]
    writable = [w[i + 2] for i in range(len(w)) if w[i] == "--bind-try"]
    covered = [w[i + 1] for i in range(len(w)) if w[i] == "--tmpfs"]
    shown = [w[i + 1] for i in range(len(w)) if w[i] == "--ro-bind-try"]
    assert writable == fs["allowWrite"]
    assert set(covered) == {d for d in fs["denyRead"] if os.path.isdir(d)
                            and not os.path.islink(d)}
    assert str(home / ".config/agentic-lab") in covered
    assert str(home / ".local/state/agentic-lab") in covered
    assert shown == fs["allowRead"]
    assert w[w.index("--chdir") + 1] == str(home / "work") and w[-1] == "--"
    assert "--die-with-parent" not in w
    # covers come after the writable binds, the GPU socket after its cover
    assert max(i for i, a in enumerate(w) if a == "--bind-try") < \
        min(i for i, a in enumerate(w) if a == "--tmpfs")


def test_a_working_folder_on_another_disk_is_bound_from_where_it_is(tmp_path):
    disk = tmp_path / "nvme"
    disk.mkdir()
    work = tmp_path / "home" / "work"
    work.parent.mkdir()
    work.symlink_to(disk)
    fs = {"allowWrite": [str(work)], "denyRead": ["/nonexistent"], "allowRead": []}
    w = R.job_wrapper(fs, str(work))
    i = w.index("--bind-try")
    assert w[i + 1:i + 3] == [str(disk), str(work)]


def test_the_policy_comes_from_the_session(tmp_path):
    rs = runs(tmp_path)
    fs = policy(tmp_path, writes=["/data/shared"])
    r = rs.start("t-1", "x", "true", requester="alice", policy=fs)
    assert rs.get(r["id"])["policy"] == fs and r["requester"] == "alice"


def test_a_session_hands_its_runs_its_own_sandbox_policy(tmp_path, monkeypatch):
    from agentic_lab.policy import sandbox
    from agentic_lab.worker import tasks as T
    from agentic_lab.worker.runner import SdkRunner
    work = str(tmp_path / "work")
    r = SdkRunner("w", work, lambda grants: sandbox.settings(work, gpu=True))
    seen = {}
    monkeypatch.setattr(r, "_lab_server", lambda task, store, outcome, fs=None:
                        seen.update(fs=fs) or {})
    store = T.TaskStore(tmp_path / "tasks")
    t = store.submit("x", "alice", "m")
    r._options(t, store, {}, {"violations": [], "restart_for": None}, [])
    assert seen["fs"] == sandbox.settings(work, gpu=True)["sandbox"]["filesystem"]


def test_no_policy_no_job(tmp_path):
    rs = runs(tmp_path)
    marker = tmp_path / "ran"
    r = rs.start("t-1", "x", f"touch {marker}", requester="alice")
    assert R.run_exec(rs, r["id"], wrapper=lambda *a: []) == 1
    assert not marker.exists()
    v = rs.view(r["id"])
    assert v["state"] == "failed" and "policy" in v["error"]


def test_no_bwrap_no_job(tmp_path, monkeypatch):
    rs = runs(tmp_path)
    marker = tmp_path / "ran"
    r = rs.start("t-1", "x", f"touch {marker}", requester="alice", policy=policy(tmp_path))
    monkeypatch.setattr(R, "BWRAP", str(tmp_path / "no-bwrap"))
    assert R.run_exec(rs, r["id"]) == 1
    assert not marker.exists()
    v = rs.view(r["id"])
    assert v["state"] == "failed" and "never starts without it" in v["error"]


def test_a_stopped_run_gets_its_grace_period_and_nothing_outlives_it():
    u = R.unit_template("worker-a", "lab-worker-a").splitlines()
    assert "--die-with-parent" not in " ".join(u)     # TERM reaches the job: it can save
    assert "KillMode=control-group" in u               # then everything in the unit goes
    assert "TimeoutStopSec=60" in u                    # within this


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
    r = rs.start("t-1", "x", "true", requester="alice")
    folder = tmp_path / "state" / r["id"]
    assert folder.is_dir() and rs._path(r["id"], ".status") == str(folder / "status.json")


def test_a_status_says_how_the_run_went_and_nothing_else(tmp_path):
    rs = runs(tmp_path)
    r = rs.start("t-1", "x", "true", requester="alice")
    (tmp_path / "state" / r["id"] / "status.json").write_text(json.dumps(
        {"state": "exited", "exit_code": 0, "task": "t-other", "command": "other"}))
    v = rs.view(r["id"])
    assert v["state"] == "exited" and v["task"] == "t-1" and v["command"] == "true"


def test_runs_from_before_still_read(tmp_path):
    rs = runs(tmp_path)
    r = rs.start("t-1", "x", "true", requester="alice")
    (tmp_path / "state" / f"{r['id']}.status.json").write_text(
        json.dumps({"state": "exited", "exit_code": 3}))
    assert rs.view(r["id"])["exit_code"] == 3


def test_the_runs_page_lists_the_cache_redirects():
    from pathlib import Path
    from agentic_lab.policy.sandbox import cache_env
    page = (Path(__file__).parent.parent / "docs" / "long-runs.md").read_text()
    for name in cache_env("/w"):
        assert f"`{name}`" in page, name
    assert not hasattr(R, "home_env")


def test_after_a_stop_run_exec_waits_until_its_cgroup_is_empty(tmp_path):
    """systemd TERMs the rest of a unit again when its main process exits during a stop,
    so run-exec must outlive the job: it waits until it's alone in its cgroup."""
    procs = tmp_path / "cgroup.procs"
    states = ["100 200 300", "100 300", "100"]          # the job's tree ends
    procs.write_text(states.pop(0))
    slept = []

    def sleep(s):
        slept.append(s)
        procs.write_text(states.pop(0))
    assert R.wait_alone(str(procs), me=100, sleep=sleep) is True
    assert slept == [R.STOP_POLL_S, R.STOP_POLL_S]      # it sleeps between looks


def test_when_it_cant_tell_it_doesnt_wait(tmp_path):
    slept = []
    assert R.wait_alone(None, sleep=slept.append) is False
    assert R.wait_alone(str(tmp_path / "missing"), sleep=slept.append) is False
    (tmp_path / "junk").write_text("not pids")
    assert R.wait_alone(str(tmp_path / "junk"), sleep=slept.append) is False
    (tmp_path / "busy").write_text("1 2")
    assert R.wait_alone(str(tmp_path / "busy"), me=1, limit=2, poll=1,
                        sleep=slept.append) is False                  # bounded
    assert slept == [1, 1, 1]


def _run_exec_in_child(tmp_path, run_id, procs):
    import sys
    code = ("import sys; from agentic_lab.worker import runs as R; "
            f"s = R.RunStore({str(tmp_path / 'state')!r}, {str(tmp_path / 'work')!r}, 'w', "
            "systemctl=lambda *a: True); "
            f"R.run_exec(s, sys.argv[1], wrapper=lambda *a: [], procs_file={procs!r})")
    return subprocess.Popen([sys.executable, "-c", code, run_id])


def test_a_stopped_run_is_recorded_when_the_job_has_really_ended(tmp_path):
    import datetime as dt
    import signal
    import time
    rs = runs(tmp_path)
    ready = tmp_path / "ready"
    r = rs.start("t-1", "x", f"touch {ready}; sleep 30", requester="alice",
                 policy={"allowWrite": ["/w"], "denyRead": ["/s"]})
    procs = tmp_path / "cgroup.procs"
    proc = _run_exec_in_child(tmp_path, r["id"], str(procs))
    procs.write_text(f"{proc.pid} 424242")          # the job (inside bwrap) still there
    deadline = time.time() + 10
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    os.killpg(rs._status(r["id"])["pid"], signal.SIGTERM)   # the wrapper's side ends...
    proc.send_signal(signal.SIGTERM)                         # ...and run-exec is told
    time.sleep(1.5)
    st = rs._status(r["id"])
    assert proc.poll() is None                      # still there: the job hasn't ended
    assert st["state"] == "stopped" and "ended" not in st
    job_end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    procs.write_text(f"{proc.pid}")                 # now only run-exec is left
    assert proc.wait(10) == 0
    st = rs._status(r["id"])
    assert st["state"] == "stopped" and dt.datetime.fromisoformat(st["ended"]) >= job_end
    assert st["exit_code"] is None                  # the job's own code isn't visible


def test_a_normal_run_is_recorded_as_before(tmp_path):
    rs = runs(tmp_path)
    r = rs.start("t-1", "x", "exit 3", requester="alice",
                 policy={"allowWrite": ["/w"], "denyRead": ["/s"]})
    proc = _run_exec_in_child(tmp_path, r["id"], str(tmp_path / "never-read"))
    assert proc.wait(10) == 0
    st = rs._status(r["id"])
    assert st["state"] == "exited" and st["exit_code"] == 3 and st["ended"]
