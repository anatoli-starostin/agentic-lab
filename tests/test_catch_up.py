"""A host that missed a deploy is brought up to what the lab runs when it's back (#61)."""
import asyncio
import datetime as dt
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import apply as A
from agentic_lab import configrepo as CR
from agentic_lab import registry
from agentic_lab.secretary import catchup as CU
from agentic_lab.secretary.web import render

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
NEW, OLD, FW = "c" * 40, "a" * 40, "f" * 40


def lab(**top):
    lb = registry.load(EXAMPLE)
    lb.data.update(top)
    return lb


def state(applied=OLD, queue=None, up=True):
    """The secretary's host runs NEW; worker-a's host (gpu-a) reports `applied`."""
    return {"applied": {"config_commit": NEW, "framework_commit": FW},
            "agents": {"worker-a": {"host": "gpu-a", "up": up, "queue": queue or {},
                                    "applied": {"config_commit": applied,
                                                "framework_commit": FW}}}}


class Deploys:
    def __init__(self, fail=False, busy=False):
        self.started, self.fail, self.busy, self.said = 0, fail, busy, []

    def start(self):
        if self.busy:
            raise RuntimeError("couldn't start the deployment: a deploy is already running")
        self.started += 1
        return f"/log/{self.started}"

    def result(self, log):
        return (not self.fail, "")

    async def announce(self, text):
        self.said.append(text)


def run(cu, lb, st, d, now=NOW):
    asyncio.run(CU.step(cu, lb, st, "sec-host", d.start, d.result, d.announce, now=now))


def test_a_stale_host_is_caught_up_and_announced(tmp_path):
    cu, d, lb = CU.CatchUp(str(tmp_path / "c.json")), Deploys(), lab()
    assert CU.behind(state(), "sec-host") == {"gpu-a": {"config_commit": OLD,
                                                        "framework_commit": FW}}
    run(cu, lb, state(), d)
    assert d.started == 1 and cu.running["hosts"] == ["gpu-a"]
    run(cu, lb, state(applied=NEW), d)                  # its worker now reports NEW
    assert d.said == [f":white_check_mark: gpu-a is up (updated to {NEW[:7]})."]
    assert d.started == 1 and CU.CatchUp(str(tmp_path / "c.json")).attempts == {}


def test_busy_hosts_wait_and_nothing_runs_when_switched_off(tmp_path):
    d = Deploys()
    run(CU.CatchUp(str(tmp_path / "a.json")), lab(), state(queue={"running": 1}), d)
    run(CU.CatchUp(str(tmp_path / "b.json")), lab(auto_catch_up=False), state(), d)
    assert d.started == 0


def test_an_unreviewed_lab_is_never_spread(tmp_path):
    st = state()
    st["applied"]["unreviewed"] = True
    d = Deploys()
    run(CU.CatchUp(str(tmp_path / "c.json")), lab(), st, d)
    assert d.started == 0 and CU.behind(st, "sec-host") == {}


def test_attempts_are_limited_then_admins_are_told(tmp_path):
    cu, d, lb = CU.CatchUp(str(tmp_path / "c.json")), Deploys(fail=True), lab()
    t = NOW
    for _ in range(6):
        run(cu, lb, state(), d, now=t)
        t += dt.timedelta(minutes=30)
    assert d.started == CU.MAX_ATTEMPTS
    assert len([s for s in d.said if "stays stale" in s]) == 1
    run(cu, lb, state(), d, now=NOW + dt.timedelta(minutes=1))   # no retry in the pause
    assert d.started == CU.MAX_ATTEMPTS


def test_pauses_between_attempts(tmp_path):
    cu, d, lb = CU.CatchUp(str(tmp_path / "c.json")), Deploys(fail=True), lab()
    run(cu, lb, state(), d)
    run(cu, lb, state(), d, now=NOW + dt.timedelta(minutes=1))
    assert d.started == 1
    run(cu, lb, state(), d, now=NOW + dt.timedelta(minutes=CU.PAUSE_MIN[0] + 1))
    assert d.started == 2


def test_another_deploy_running_is_waited_for(tmp_path):
    cu, d = CU.CatchUp(str(tmp_path / "c.json")), Deploys(busy=True)
    run(cu, lab(), state(), d)
    assert cu.attempts == {} and cu.running is None


def test_the_dashboard_shows_stale_hosts():
    st = state()
    st.update(lab="L", at=NOW.isoformat(), accounts={}, approvals={})
    st["agents"]["worker-a"].update(name="worker-a", role="worker")
    st["hosts"] = {"gpu-a": {"state": "RUNNING",
                             "stale": CU.stale_info(state(), "sec-host")["gpu-a"]}}
    assert "stale: config differs" in render(st)


def test_only_a_commit_of_main_is_deployed(tmp_path):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                              text=True, env=env).stdout.strip()
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "lab.yaml").write_text("lab: one\n")
    git(work, "add", "lab.yaml")
    git(work, "commit", "-qm", "one")
    first = git(work, "rev-parse", "HEAD")
    (work / "lab.yaml").write_text("lab: two\n")
    git(work, "commit", "-qam", "two")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "side")
    (work / "lab.yaml").write_text("lab: side\n")
    git(work, "commit", "-qam", "side")
    side = git(work, "rev-parse", "HEAD")
    path, info = CR.fetch_main(str(work / "lab.yaml"), first)
    assert info["config_commit"] == first and Path(path).read_text() == "lab: one\n"
    with pytest.raises(CR.ConfigError, match="isn't a commit of main"):
        CR.fetch_main(str(work / "lab.yaml"), side)


def test_a_catch_up_deploys_only_hosts_that_run_something_else(monkeypatch):
    from agentic_lab import cli, doctor, lock
    plans = [{"host": h, "name": h, "missing": [], "units": [1]} for h in ("gpu-a", "gpu-b")]
    shipped = []
    monkeypatch.setattr(A, "plan_all", lambda *a, **k: (
        SimpleNamespace(), SimpleNamespace(save=lambda: None), plans))
    monkeypatch.setattr(cli.registry, "check", lambda lb: [])
    monkeypatch.setattr(lock, "current", lambda src: (None, FW))
    monkeypatch.setattr(A, "secretary_host", lambda lb: ("sec-host", "secretary"))
    monkeypatch.setattr(doctor, "this_host", lambda lb: "sec-host")
    monkeypatch.setattr(A, "describe", lambda p: p["host"])
    monkeypatch.setattr(A, "has_work", lambda p: True)
    monkeypatch.setattr(A, "applied_on", lambda p, local, admin: (
        {"config_commit": NEW, "framework_commit": FW} if p["host"] == "gpu-a" else
        {"config_commit": OLD, "framework_commit": FW}))
    monkeypatch.setattr(A, "run_bundle", lambda p, local, admin: shipped.append(p["host"]) or 0)
    monkeypatch.setattr(cli, "deploy_lock", lambda: None)
    assert cli.deploy("lab.yaml", {"config_commit": NEW}, ".", "admin", only_behind=True) == 0
    assert shipped == ["gpu-b"]


def test_the_secretary_may_run_the_catch_up():
    s = A.self_admin_script({"config_owner": "lab-secretary", "config_url": "u",
                             "lab_rel": "lab.yaml", "framework_source": "/src",
                             "framework_owner": "admin"})
    line = next(ln for ln in s if ln.startswith("printf"))
    out = subprocess.run(["bash", "-c", line.split(" > ")[0]], capture_output=True,
                         text=True).stdout
    assert out.splitlines() == [
        "lab-secretary ALL=(root) NOPASSWD: /usr/local/bin/lab apply-main",
        "lab-secretary ALL=(root) NOPASSWD: /usr/local/bin/lab apply-main --catch-up"]
