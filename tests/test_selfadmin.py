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
                 sec.ctx("bob").read_config(), sec.ctx("bob").slack_verify("broker"),
                 sec.ctx("bob").request_slack_app("mgr-alice")):
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


# ── the secretary with a GitHub identity (github: on the secretary) ─────────

def test_with_a_github_identity_it_commits_as_it_and_opens_the_pr(copy, tmp_path, monkeypatch):
    c, origin, git = copy
    token = tmp_path / "token"
    token.write_text("ghs_abc\n")
    c.github = {"env": {}, "url": str(origin), "token_file": str(token)}
    c.author = ("flash-bot", "flash-bot@users.noreply.github.com")
    c.agent = "Iris (secretary)"
    monkeypatch.setattr(SA, "github_repo", lambda url: "acme/lab-config")
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"html_url": "https://github.com/acme/lab-config/pull/7"}'

    def urlopen(req, timeout=None):
        sent.update(url=req.full_url, auth=req.get_header("Authorization"),
                    body=__import__("json").loads(req.data))
        return Resp()
    monkeypatch.setattr(SA.urllib.request, "urlopen", urlopen)
    main = c.read_main()
    r = c.propose("Rename Alice", main["lab.yaml"].replace("name: Alice", "name: Alice L."))
    assert r["pr"] == "https://github.com/acme/lab-config/pull/7"
    assert sent["url"] == "https://api.github.com/repos/acme/lab-config/pulls"
    assert sent["auth"] == "Bearer ghs_abc"
    assert sent["body"]["head"] == r["branch"] and sent["body"]["base"] == "main"
    log = git(origin, "log", "-1", "--format=%an <%ae>%n%B", r["branch"])
    assert log.startswith("flash-bot <flash-bot@users.noreply.github.com>")
    assert "Rename Alice" in log and "Lab-Agent: Iris (secretary)" in log


def test_with_a_github_identity_the_service_clones_over_https(copy, tmp_path):
    c, origin, git = copy
    fresh = SA.ConfigCopy(clone=str(tmp_path / "state" / "config-repo"),
                          github={"env": {}, "url": str(origin), "token_file": "/nonexistent"})
    assert "humans:" in fresh.read_main()["lab.yaml"]           # cloned on first use
    assert SA.https_url("git@github.com:acme/lab-config.git") == \
        "https://github.com/acme/lab-config.git"
    assert SA.https_url("ssh://git@github.com/acme/lab-config") == \
        "https://github.com/acme/lab-config.git"


def test_a_pr_that_cant_be_opened_falls_back_to_the_link(copy, tmp_path, monkeypatch):
    c, origin, git = copy
    c.github = {"env": {}, "url": str(origin), "token_file": str(tmp_path / "missing")}
    monkeypatch.setattr(SA, "github_repo", lambda url: "acme/lab-config")
    main = c.read_main()
    r = c.propose("Rename", main["lab.yaml"].replace("name: Alice", "name: A"))
    assert r["pr"] is None and r["link"].endswith(f"compare/main...{r['branch']}?expand=1")


def test_deploy_key_mode_unchanged_but_commits_say_who(copy):
    c, origin, git = copy
    c.agent = "Iris (secretary)"
    main = c.read_main()
    r = c.propose("Rename", main["lab.yaml"].replace("name: Alice", "name: A"))
    assert r["pr"] is None                                       # the admin opens it
    assert "Lab-Agent: Iris (secretary)" in git(origin, "log", "-1", "--format=%B", r["branch"])
    lines = "\n".join(self_admin_script({"config_owner": "lab-secretary",
                                         "config_url": "git@github.com:acme/lab-config.git"}))
    assert "ssh-keygen" in lines and "config-repo-key" in lines


def test_apply_skips_the_deploy_key_with_a_github_identity():
    lines = "\n".join(self_admin_script({"config_owner": "lab-secretary", "github": "flash-bot",
                                         "config_url": "git@github.com:acme/lab-config.git"}))
    assert "NOPASSWD: /usr/local/bin/lab apply-main" in lines
    assert "ssh-keygen" not in lines and "deploy key" not in lines.replace("no deploy key", "")
    assert "GitHub identity flash-bot" in lines
    assert subprocess.run(["bash", "-n"], input=lines, text=True).returncode == 0


def test_secretary_service_uses_its_identity(tmp_path, monkeypatch):
    import yaml
    from agentic_lab import github as GH
    from agentic_lab.secretary.service import Secretary
    d = yaml.safe_load(SINGLE.read_text())
    d["github"] = {"accounts": {"flash-bot": {"email": "flash@acme.org"}}}
    sec = next(a for a, x in d["agents"].items() if x["role"] == "secretary")
    d["agents"][sec]["github"] = "flash-bot"
    d["agents"][sec]["workdir"] = str(tmp_path / "work")
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    lab = registry.load(p)
    assert [i for i in registry.check(lab) if i.level == "error"] == []
    # its identity is its service's: the agent's sandbox doesn't get GitHub
    assert "github.com" not in lab.agent_settings(sec)["sandbox"]["network"]["allowedDomains"]
    monkeypatch.setattr(GH, "SERVICE_ROOT", str(tmp_path / "state" / "github"))
    monkeypatch.setattr(SA, "applied", lambda path: {"config_url":
                                                     "git@github.com:acme/lab-config.git"})
    s = Secretary.__new__(Secretary)
    s.lab = lab
    c = s.config_copy()
    assert c.author == ("flash-bot", "flash@acme.org")
    assert c.github["url"] == "git@github.com:acme/lab-config.git"
    assert c.github["env"]["LAB_GITHUB_DIR"] == str(tmp_path / "state" / "github")
    assert c.github["token_file"] == str(tmp_path / "state" / "github" / "flash-bot" / "token")
    assert c.agent.endswith("(secretary)")
    del lab.agents[sec]["github"]                              # without: the deploy key
    c = s.config_copy()
    assert c.github is None and c.author[0].endswith("(lab secretary)")
