"""#95: the catch-up makes the secretary's config clone know the lab's commit before it
decides, and says so when it can't (real git repositories, no stubbed history)."""
import asyncio
import datetime as dt
import subprocess
from pathlib import Path

from agentic_lab import registry, selfadmin as SA
from agentic_lab.secretary import catchup as CU
from agentic_lab.secretary.web import stale_text

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)
FW = "f" * 40
SEC = "nuc-01"


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, text):
    (repo / "lab.yaml").write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", text)
    return git(repo, "rev-parse", "HEAD")


def origin(tmp):
    """The config repo, with commit A on main."""
    o = tmp / "origin"
    o.mkdir()
    git(o, "init", "-q", "-b", "main")
    git(o, "config", "user.email", "t@t")
    git(o, "config", "user.name", "t")
    return o, commit(o, "a\n")


def state(lab_commit, host_commit, unreviewed=False):
    """The secretary's host runs `lab_commit`; worker-b's host (gpu-b) `host_commit`."""
    ap = {"config_commit": host_commit, "framework_commit": FW}
    if unreviewed:
        ap["unreviewed"] = True
    return {"applied": {"config_commit": lab_commit, "framework_commit": FW},
            "agents": {"worker-b": {"up": True, "host": "gpu-b", "queue": {}, "applied": ap}}}


class Deploys:
    def __init__(self):
        self.started, self.said = [], []

    def start(self):
        self.started.append(1)
        return "log"

    def result(self, log):
        return None

    async def announce(self, text):
        self.said.append(text)


def copy(clone, url):
    """The secretary's clone as with a GitHub identity: the service clones it itself."""
    return SA.ConfigCopy(clone=str(clone), github={"env": {}, "url": str(url),
                                                   "token_file": "unused"})


def step(cu, st, clone, d, refresh, now=NOW):
    asyncio.run(CU.step(cu, registry.load(EXAMPLE), st, SEC, d.start, d.result, d.announce,
                        repo=str(clone), now=now, refresh=refresh))


def test_no_clone_yet_is_cloned_then_the_host_caught_up(tmp_path):
    o, a = origin(tmp_path)
    b = commit(o, "b\n")
    clone, d = tmp_path / "config-repo", Deploys()
    cu = CU.CatchUp(str(tmp_path / "cu.json"))
    step(cu, state(b, a), clone, d, copy(clone, o).ensure_commit)
    assert (clone / ".git").is_dir() and d.started == [1] and d.said == []
    assert CU.CatchUp(str(tmp_path / "cu.json")).checked[b]["ok"]     # kept across restarts


def test_a_clone_without_the_labs_commit_is_fetched_then_the_host_caught_up(tmp_path):
    o, a = origin(tmp_path)
    clone = tmp_path / "config-repo"
    git(tmp_path, "clone", "-q", str(o), str(clone))
    b = commit(o, "b\n")                    # merged and deployed from a console since
    assert CU.is_ancestor(str(clone), a, b) is None
    d = Deploys()
    step(CU.CatchUp(str(tmp_path / "cu.json")), state(b, a), clone, d,
         copy(clone, o).ensure_commit)
    assert d.started == [1]


def test_when_git_cant_tell_nothing_is_deployed_and_the_admins_hear_once(tmp_path):
    o, a = origin(tmp_path)
    clone = tmp_path / "config-repo"
    git(tmp_path, "clone", "-q", str(o), str(clone))
    b = commit(o, "b\n")
    git(clone, "remote", "set-url", "origin", "file:///nowhere/config.git")   # fetch fails
    cu, d, st = CU.CatchUp(str(tmp_path / "cu.json")), Deploys(), state(b, a)
    refresh = SA.ConfigCopy(clone=str(clone)).ensure_commit
    step(cu, st, clone, d, refresh)
    step(cu, st, clone, d, refresh, now=NOW + dt.timedelta(minutes=CU.CHECK_GAP_MIN + 1))
    assert d.started == [] and len(d.said) == 1
    assert d.said[0].startswith(":warning: gpu-b missed a deploy, but I can't check main's")
    assert "nowhere" not in d.said[0] and "<repo>" in d.said[0]        # no URLs
    info = CU.stale_info(st, SEC, str(clone), blocked=cu.blocked)["gpu-b"]
    assert info["blocked"] == cu.blocked[b] and info["config_behind"] is None
    assert "catch-up blocked: " in stale_text(info)


def test_a_count_git_couldnt_give_is_asked_again(tmp_path):
    o, a = origin(tmp_path)
    clone = tmp_path / "config-repo"
    git(tmp_path, "clone", "-q", str(o), str(clone))
    b = commit(o, "b\n")
    st = state(b, a)
    assert CU.stale_info(st, SEC, str(clone))["gpu-b"]["config_behind"] is None
    git(clone, "fetch", "-q", "origin", "main")
    assert CU.stale_info(st, SEC, str(clone))["gpu-b"]["config_behind"] == 1


def test_newer_or_unreviewed_hosts_are_still_left_alone(tmp_path):
    o, a = origin(tmp_path)
    b = commit(o, "b\n")
    clone = tmp_path / "config-repo"
    refresh = copy(clone, o).ensure_commit
    for st in (state(a, b),                         # the host runs a NEWER commit than the lab
               state(b, a, unreviewed=True)):       # the host runs something unreviewed
        d = Deploys()
        step(CU.CatchUp(str(tmp_path / "cu.json")), st, clone, d, refresh)
        assert d.started == [] and d.said == []
