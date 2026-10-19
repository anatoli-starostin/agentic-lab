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
        next(a for a in d["agents"].values() if a["role"] == "secretary")["github"] = "flash-bot"
    errs = " ".join(errors(lab_with_github(tmp_path, bad)))
    assert "exactly one identity `default`" in errs and "owner/repo" in errs
    assert "'nobody-bot' is not in github.accounts" in errs
    assert "managers and workers only" in errs


def test_settings_and_prompt(tmp_path):
    lab = lab_with_github(tmp_path)
    mgr, sec = first(lab, "manager"), first(lab, "secretary")
    s = lab.agent_settings(mgr)
    assert "github.com" in s["sandbox"]["network"]["allowedDomains"]
    assert "GIT_AUTHOR_NAME" not in s["env"]          # per repository: from git's config
    assert "github.com" not in lab.agent_settings(sec)["sandbox"]["network"]["allowedDomains"]
    prompt = GH.instructions(lab, mgr, root="/r")
    assert "bot account flash-bot" in prompt and "anatoli-starostin/spiky" in prompt
    assert "spikyclaudebot" in prompt and "GH_TOKEN=$(cat /r/<identity>/token)" in prompt
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
    env = GH.prepare(lab, mgr, root=str(root), secrets=str(secrets), helper=str(HELPER))
    assert env["GH_CONFIG_DIR"] == str(root / "flash-bot" / "gh")
    assert "oauth_token: ghp_flash" in (root / "flash-bot" / "gh" / "hosts.yml").read_text()
    full = {**os.environ, **env, "HOME": str(tmp_path)}

    def git(cwd, *a, stdin=None):
        return subprocess.run(["git", *a], cwd=cwd, env=full, input=stdin, text=True,
                              capture_output=True, timeout=20).stdout
    for name, url in (("spiky", "https://github.com/anatoli-starostin/spiky.git"),
                      ("other", "https://github.com/deepspike/models")):
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
    assert password("deepspike/models.git") == "ghp_flash"


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
