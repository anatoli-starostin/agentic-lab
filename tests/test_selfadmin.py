"""Administering the lab from Slack: proposals are checked branches (never main), deploying
is an admin's click, and only main is ever deployed."""
import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import registry
from agentic_lab import selfadmin as SA
from agentic_lab.apply import self_admin_script

SINGLE = Path(__file__).parent.parent / "examples" / "single-vm" / "lab.yaml"


@pytest.fixture
def copy(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout
    origin, admin, mine = tmp_path / "origin.git", tmp_path / "admin", tmp_path / "secretary"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(admin))
    shutil.copy(SINGLE, admin / "lab.yaml")
    (admin / "framework.lock").write_text("framework: {repo: x, commit: abc}\n")
    git(admin, "add", ".")
    git(admin, "commit", "-qm", "initial")
    git(admin, "push", "-q", "origin", "HEAD:main")
    git(tmp_path, "clone", "-q", str(origin), str(mine))
    return SA.ConfigCopy(clone=str(mine)), origin, git


def test_propose_pushes_a_checked_branch_never_main(copy):
    c, origin, git = copy
    main = c.read_main()
    assert "humans:" in main["lab.yaml"] and main["framework.lock"]
    new = main["lab.yaml"].replace("name: Alice", "name: Alice Liddell")
    r = c.propose("Rename Alice", new, framework_commit="def456")
    assert r["branch"].startswith("lab/") and "rename-alice" in r["branch"]
    assert "+    name: Alice Liddell" in r["diff"] and "def456" in r["diff"]
    branches = git(origin, "branch", "--list")
    assert r["branch"] in branches
    assert git(origin, "rev-parse", "main").strip() == main["commit"]   # main untouched
    with pytest.raises(SA.AdminError, match="lab check failed|doesn't load"):
        c.propose("break it", "humans: 3\n")
    with pytest.raises(SA.AdminError, match="nothing to change"):
        c.propose("no-op", main["lab.yaml"])
    head, todo = c.not_applied(main["commit"])
    assert head == main["commit"] and todo == []
    assert c.not_applied(None)[1] == ["(nothing recorded as applied yet)"]


def test_apply_result(tmp_path):
    log = tmp_path / "apply.log"
    log.write_text("config: abc\n")
    assert SA.apply_result(str(log)) is None
    log.write_text("config: abc\n...\nAPPLY-RESULT: ok\n")
    ok, tail = SA.apply_result(str(log))
    assert ok and "config: abc" in tail
    log.write_text("lab apply-main: boom\nAPPLY-RESULT: failed\n")
    assert SA.apply_result(str(log))[0] is False


def test_sudo_rule_is_one_fixed_command():
    lines = "\n".join(self_admin_script({"config_owner": "lab-secretary",
                                         "config_url": "git@github.com:acme/lab-config.git"}))
    assert "lab-secretary ALL=(root) NOPASSWD: /usr/local/bin/lab apply-main'" in lines
    assert "visudo -cqf" in lines and "WITH write access" in lines
    r = subprocess.run(["bash", "-n"], input=lines, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr                  # the generated shell parses


def test_admin_only_and_the_click_decides(monkeypatch):
    from agentic_lab.secretary.service import Secretary
    lab = registry.load(SINGLE)
    alice = next(iter(lab.humans))
    lab.humans[alice]["admin"] = True
    lab.humans["bob"] = {"name": "Bob", "slack_user": "U-BOB"}
    sec = Secretary.__new__(Secretary)
    sec.lab, sec.events, sec.workers, sec.latest = lab, None, None, {"state": {}}
    sec.state = SimpleNamespace(save=None)
    posted, updated, started = [], [], []

    async def post(channel, thread, text):
        posted.append(text)

    async def chat_update(**kw):
        updated.append(kw["text"])
    sec.slack = SimpleNamespace(post=post, web=SimpleNamespace(chat_update=chat_update))
    run = asyncio.run
    for call in (sec.ctx("bob").propose_change("x", "y"), sec.ctx("bob").offer_apply(),
                 sec.ctx("bob").read_config(), sec.ctx("bob").slack_verify("broker")):
        assert run(call).startswith("refused: only an admin")
    # a non-admin's click on the Deploy button does nothing but say so
    monkeypatch.setattr(SA, "start_apply", lambda: started.append(1) or "/tmp/x.log")
    where = {"channel": "D1", "ts": "1.0", "thread_ts": None}
    run(sec.on_action("U-BOB", "bob", "lab_apply", "abc", where))
    assert started == [] and "only an admin" in posted[-1]
    # an admin's click on a stale button (main moved) doesn't deploy either
    monkeypatch.setattr(Secretary, "config_copy", lambda self: SimpleNamespace(
        read_main=lambda: {"commit": "newer"}))
    run(sec.on_action("U-A", alice, "lab_apply", "abc", where))
    assert started == [] and "main moved" in updated[-1]
