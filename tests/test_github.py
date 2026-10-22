"""GitHub for agents: identities in lab.yaml (bot accounts or GitHub Apps), one per agent or
several chosen by repository; git and gh set up by the agent's service."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from agentic_lab import github as GH
from agentic_lab import registry
from agentic_lab.apply import host_plan
from agentic_lab.secrets import intake as I

SINGLE = Path(__file__).parent.parent / "examples" / "single-vm" / "lab.yaml"
HELPER = Path(__file__).parent.parent / "deploy" / "worker" / "lab-git-credential"


def lab_with_github(tmp_path, mutate=None):
    d = yaml.safe_load(SINGLE.read_text())
    d["github"] = {"accounts": {"flash-bot": {}, "worker-bot": {"email": "w@example.org"},
                                "spikyclaudebot": {"email": "spiky@example.org"}}}
    for a in d["agents"].values():
        if a["role"] == "manager":
            a["github"] = {"flash-bot": "default", "spikyclaudebot": ["anatoli-starostin/spiky"]}
        elif a["role"] == "worker":
            a["github"] = "worker-bot"
    if mutate:
        mutate(d)
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    return registry.load(p)


def errors(lab):
    return [str(i) for i in registry.check(lab) if i.level == "error"]


def first(lab, role):
    return next(a for a, x in lab.agents.items() if x["role"] == role)


class Tok:
    def get(self, k):
        return "t"


def test_schema(tmp_path):
    lab = lab_with_github(tmp_path)
    assert errors(lab) == []
    mgr = first(lab, "manager")
    assert lab.github_identities(mgr) == ("flash-bot", [("anatoli-starostin/spiky",
                                                         "spikyclaudebot")])
    assert lab.github_names(mgr) == ["flash-bot", "spikyclaudebot"]
    assert lab.github_identities(first(lab, "worker")) == ("worker-bot", [])

    def bad(d):
        m = next(a for a in d["agents"].values() if a["role"] == "manager")
        m["github"] = {"flash-bot": ["not a repo!"], "worker-bot": ["x/y"]}   # no default
        next(a for a in d["agents"].values() if a["role"] == "worker")["github"] = "nobody-bot"
        next(a for a in d["agents"].values() if a["role"] == "secretary")["github"] = {
            "flash-bot": "default"}                     # the secretary: one identity
    errs = " ".join(errors(lab_with_github(tmp_path, bad)))
    assert "exactly one identity `default`" in errs and "owner/repo" in errs
    assert "'nobody-bot' is not in github.accounts" in errs
    assert "the secretary has one identity" in errs


def test_settings_and_prompt(tmp_path):
    lab = lab_with_github(tmp_path)
    mgr, sec = first(lab, "manager"), first(lab, "secretary")
    s = lab.agent_settings(mgr)
    assert "github.com" in s["sandbox"]["network"]["allowedDomains"]
    assert "GIT_AUTHOR_NAME" not in s["env"]          # per repository: from git's config
    assert "github.com" not in lab.agent_settings(sec)["sandbox"]["network"]["allowedDomains"]
    prompt = GH.instructions(lab, mgr, root="/r")
    assert "bot account flash-bot" in prompt and "anatoli-starostin/spiky" in prompt
    assert "spikyclaudebot" in prompt and "-R owner/repo" in prompt and "hosts.yml" not in prompt
    assert "never approve or merge" in prompt.lower()


def test_git_picks_the_identity_by_repository(tmp_path):
    """The real git: in a Spiky clone it commits and authenticates as spikyclaudebot,
    elsewhere as the default identity."""
    lab = lab_with_github(tmp_path)
    mgr = first(lab, "manager")
    secrets, root = tmp_path / "secrets", tmp_path / "share"
    secrets.mkdir()
    (secrets / "flash-bot.token").write_text("ghp_flash\n")
    (secrets / "spikyclaudebot.token").write_text("ghp_spiky\n")
    lab.agents[mgr]["workdir"] = str(tmp_path / "work")
    env = GH.prepare(lab, mgr, root=str(root), secrets=str(secrets), helper=str(HELPER))
    # gh's config: writable, in the working folder, no token in it (#52)
    assert env["GH_CONFIG_DIR"] == str(tmp_path / "work" / ".config" / "gh")
    assert (tmp_path / "work" / ".config" / "gh").is_dir()
    assert not (root / "flash-bot" / "gh").exists()
    assert env["PATH"].startswith(GH.SHIM + ":")
    assert (root / "flash-bot" / "token").read_text().strip() == "ghp_flash"
    full = {**os.environ, **env, "HOME": str(tmp_path)}

    def git(cwd, *a, stdin=None):
        return subprocess.run(["git", *a], cwd=cwd, env=full, input=stdin, text=True,
                              capture_output=True, timeout=20).stdout
    for name, url in (("spiky", "https://github.com/anatoli-starostin/spiky.git"),
                      ("other", "https://github.com/acme/models")):
        (tmp_path / name).mkdir()
        git(tmp_path / name, "init", "-q")
        git(tmp_path / name, "remote", "add", "origin", url)
    assert git(tmp_path / "spiky", "var", "GIT_AUTHOR_IDENT").startswith(
        "spikyclaudebot <spiky@example.org>")
    assert git(tmp_path / "other", "var", "GIT_AUTHOR_IDENT").startswith(
        "flash-bot <flash-bot@users.noreply.github.com>")

    def password(path):
        out = git(tmp_path / "other", "credential", "fill",
                  stdin=f"protocol=https\nhost=github.com\npath={path}\n\n")
        return next(line[9:] for line in out.splitlines() if line.startswith("password="))
    assert password("anatoli-starostin/spiky.git") == "ghp_spiky"
    assert password("acme/models.git") == "ghp_flash"


def test_apply_installs_every_identity_of_the_agent(tmp_path):
    lab = lab_with_github(tmp_path)
    mgr, worker = first(lab, "manager"), first(lab, "worker")
    tokens = {"service:github-flash-bot": "ghp_" + "f" * 36,
              "service:github-worker-bot": "ghp_" + "w" * 36}

    def intake(owner, name):
        if name in ("github-token", "github-app"):
            return tokens.get(owner)
        return None if name == "nebius-sa" else "secret"
    plan = host_plan(lab, lab.agents[mgr]["host"], Tok(), intake)
    files = {p: c for p, (_, _, c) in plan["files"].items()}
    home = f"/home/{lab.unix_user(mgr)}/.config/agentic-lab/github"
    assert files[f"{home}/flash-bot.token"].startswith("ghp_f")
    assert f"{home}/spikyclaudebot.token" not in files        # not handed over yet...
    assert any("spikyclaudebot" in m for m in plan["later"])  # ...reported, never blocking
    assert not any("GitHub" in m for m in plan["missing"])
    wfiles = f"/home/{lab.unix_user(worker)}/.config/agentic-lab/github"
    assert files[f"{wfiles}/worker-bot.token"].startswith("ghp_w")
    assert f"{wfiles}/spikyclaudebot.token" not in files      # another agent's identity


def test_intake_takes_github_tokens():
    assert I.check_value("github-token", "github_pat_" + "a" * 40).startswith("github_pat_")
    with pytest.raises(I.IntakeError, match="GitHub token"):
        I.check_value("github-token", "xoxb-" + "a" * 40)


SHIM = Path(__file__).parent.parent / "deploy" / "worker" / "gh-shim" / "gh"


def test_gh_shim_reads_the_token_at_call_time(tmp_path):
    """#52: the gh shim gives the real gh the current token (an App's changes hourly), the
    identity mapped to -R owner/repo, or keeps a GH_TOKEN the agent set itself."""
    root = tmp_path / "share"
    for name, tok in (("flash-bot", "ghs_one"), ("spikyclaudebot", "ghp_spiky")):
        (root / name).mkdir(parents=True)
        (root / name / "token").write_text(tok + "\n")
    (root / "default").write_text("flash-bot\n")
    (root / "repos.tsv").write_text("anatoli-starostin/spiky\tspikyclaudebot\n")
    real = tmp_path / "real-gh"
    real.write_text('#!/bin/sh\necho "token=$GH_TOKEN args=$*"\n')
    real.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LAB_GITHUB_DIR": str(root),
           "LAB_REAL_GH": str(real)}

    def gh(*args, cwd=tmp_path, **extra):
        r = subprocess.run([str(SHIM), *args], capture_output=True, text=True, timeout=10,
                           env={**env, **extra}, cwd=cwd)
        return r.returncode, r.stdout.strip()
    assert gh("pr", "list") == (0, "token=ghs_one args=pr list")
    (root / "flash-bot" / "token").write_text("ghs_two\n")              # the App refreshed it
    assert gh("pr", "list")[1].startswith("token=ghs_two")
    assert gh("pr", "list", "-R", "Anatoli-Starostin/Spiky")[1].startswith("token=ghp_spiky")
    assert gh("--repo=anatoli-starostin/spiky", "issue", "list")[1].startswith("token=ghp_spiky")
    assert gh("repo", "view", "-R", "acme/models")[1].startswith("token=ghs_two")
    assert gh("pr", "list", GH_TOKEN="mine")[1].startswith("token=mine")   # the agent's own
    # no -R, inside a clone of a mapped repository: its origin decides (#53)
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "https://github.com/Anatoli-Starostin/spiky.git"], cwd=clone, check=True)
    assert gh("pr", "list", cwd=clone)[1].startswith("token=ghp_spiky")
    subprocess.run(["git", "remote", "set-url", "origin", "git@github.com:acme/models.git"],
                   cwd=clone, check=True)
    assert gh("pr", "list", cwd=clone)[1].startswith("token=ghs_two")   # unmapped: default
    env["LAB_REAL_GH"] = str(tmp_path / "missing")
    assert gh("pr", "list")[0] == 127


# ── which repositories each identity can reach (#53) ─────────────────────────

class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return __import__("json").dumps(self.body).encode()


def test_list_repos_for_an_app_and_a_bot():
    seen = []

    def opener(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization")))
        if "/installation/repositories" in req.full_url:
            page = int(__import__("re").search(r"[?&]page=(\d+)", req.full_url).group(1))
            n = 100 if page == 1 else 3                 # two pages
            return _Resp({"repositories": [{"full_name": f"acme/r{page}-{i}"} for i in range(n)]})
        return _Resp([{"full_name": "Acme/Models"}, {"full_name": "acme/lab-config"}])
    app = GH.list_repos("ghs_x", True, opener=opener)
    assert len(app) == 103 and "acme/r2-2" in app and seen[0][1] == "Bearer ghs_x"
    assert GH.list_repos("ghp_y", False, opener=opener) == ["acme/lab-config", "Acme/Models"]
    assert "/user/repos" in seen[-1][0]


def test_repos_reach_the_prompt_and_failures_keep_the_last_list(tmp_path):
    lab = lab_with_github(tmp_path)
    mgr = first(lab, "manager")
    root = tmp_path / "share"
    for n in ("flash-bot", "spikyclaudebot"):
        (root / n).mkdir(parents=True)
    # before any list: the prompt still makes sense and points to the file
    p = GH.instructions(lab, mgr, root=str(root))
    assert "isn't there yet" in p and f"{root}/flash-bot/repos.txt" in p
    assert GH.refresh_repos("t", False, str(root / "flash-bot"),
                            lister=lambda t, a: ["acme/models", "acme/lab-config"])
    assert GH.refresh_repos("t", False, str(root / "spikyclaudebot"),
                            lister=lambda t, a: ["anatoli-starostin/spiky"])
    p = GH.instructions(lab, mgr, root=str(root))
    assert "can reach: acme/models, acme/lab-config" in p
    assert "anatoli-starostin/spiky" in p
    assert "git clone https://github.com/<owner>/<repo>.git" in p
    assert "empty means one identity for everything, not \"no access\"" in p
    assert "never guess an owner" in p and "-R owner/repo" in p

    def broken(token, app):
        raise OSError("no network")
    assert not GH.refresh_repos("t", False, str(root / "flash-bot"), lister=broken)
    assert GH.read_repos(str(root / "flash-bot"))[0] == ["acme/models", "acme/lab-config"]
    GH.write_repos(str(root / "flash-bot"), [])
    assert "can reach no repository yet" in GH.instructions(lab, mgr, root=str(root))


def test_bot_repos_listed_at_start(tmp_path):
    import asyncio
    d = tmp_path / "flash-bot"
    d.mkdir()
    (d / "token").write_text("ghp_flash\n")
    stop = asyncio.Event()

    def lister(token, app):
        stop.set()                                  # one round
        assert (token, app) == ("ghp_flash", False)
        return ["acme/models"]
    asyncio.run(GH.keep_bot_repos("flash-bot", stop, str(d), lister=lister))
    assert GH.read_repos(str(d))[0] == ["acme/models"]


def test_the_prompt_is_read_at_every_session():
    """prompt_extra may be a callable (the GitHub paragraph, with fresh repository lists)."""
    from agentic_lab.worker.runner import SdkRunner
    calls = []
    r = SdkRunner("w", "/tmp", lambda g: {}, prompt_extra=lambda: calls.append(1) or "EXTRA")
    from agentic_lab.worker import tasks as T
    task = T.Task(id="t-1", instruction="x", requester="alice", manager="m")
    store = type("S", (), {"get": lambda self, i: task})()
    o = r._options(task, store, {}, {"violations": [], "restart_for": None}, [])
    assert o.system_prompt["append"].endswith("EXTRA") and calls == [1]
