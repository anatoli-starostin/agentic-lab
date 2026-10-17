"""GitHub for agents: one bot account per agent, set in lab.yaml; its token in the agent's
environment, git and gh set up to use it."""
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


def lab_with_github(tmp_path, mutate=None):
    d = yaml.safe_load(SINGLE.read_text())
    d["github"] = {"accounts": {"flash-bot": {}, "worker-bot": {"email": "w@example.org"}}}
    for a in d["agents"].values():
        if a["role"] == "manager":
            a["github"] = "flash-bot"
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


def test_schema(tmp_path):
    lab = lab_with_github(tmp_path)
    assert errors(lab) == []

    def bad(d):
        next(a for a in d["agents"].values() if a["role"] == "worker")["github"] = "nobody-bot"
        next(a for a in d["agents"].values() if a["role"] == "secretary")["github"] = "flash-bot"
        d["github"]["accounts"]["flash-bot"] = {"member": "alice"}       # no such key now
        d["github"]["accounts"]["bad login!"] = {}
    errs = " ".join(errors(lab_with_github(tmp_path, bad)))
    assert "'nobody-bot' is not in github.accounts" in errs
    assert "managers and workers only" in errs and "unknown key 'member'" in errs
    assert "GitHub login" in errs


def test_settings_env_and_prompt(tmp_path):
    lab = lab_with_github(tmp_path)
    worker = first(lab, "worker")
    s = lab.agent_settings(worker)
    assert s["env"]["GIT_AUTHOR_NAME"] == "worker-bot"
    assert s["env"]["GIT_COMMITTER_EMAIL"] == "w@example.org"
    assert "github.com" in s["sandbox"]["network"]["allowedDomains"]
    sec = first(lab, "secretary")
    assert "github.com" not in lab.agent_settings(sec)["sandbox"]["network"]["allowedDomains"]

    tok = tmp_path / "github.token"
    tok.write_text("ghp_abc\n")
    env = GH.env_for(lab, worker, path=str(tok))
    assert env["GH_TOKEN"] == "ghp_abc"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert GH.env_for(lab, worker, path=str(tmp_path / "missing")) == {}   # not there yet
    assert GH.env_for(lab, sec, path=str(tok)) == {}                       # no identity
    prompt = GH.instructions(lab, worker)
    assert "worker-bot" in prompt and "never approve or merge" in prompt.lower()
    assert "ghp_" not in prompt


def test_apply_installs_only_the_agents_own_bot(tmp_path):
    lab = lab_with_github(tmp_path)
    worker = first(lab, "worker")
    tokens = {"service:github-worker-bot": "ghp_" + "w" * 36}

    class Tok:
        def get(self, k):
            return "t"

    def intake(owner, name):
        return tokens.get(owner) if name == "github-token" else (
            None if name == "nebius-sa" else "secret")
    plan = host_plan(lab, lab.agents[worker]["host"], Tok(), intake)
    files = {p: c for p, (_, _, c) in plan["files"].items()}
    assert files[f"/home/{lab.unix_user(worker)}/.config/agentic-lab/github.token"].startswith(
        "ghp_w")
    mgr = first(lab, "manager")
    if lab.agents[mgr]["host"] == lab.agents[worker]["host"]:
        # the manager's bot has no token yet: reported, and never holds the host back
        assert f"/home/{lab.unix_user(mgr)}/.config/agentic-lab/github.token" not in files
        assert any("flash-bot" in m for m in plan["later"])
    assert not any("GitHub" in m for m in plan["missing"])


def test_intake_takes_github_tokens():
    assert I.check_value("github-token", "github_pat_" + "a" * 40).startswith("github_pat_")
    with pytest.raises(I.IntakeError, match="GitHub token"):
        I.check_value("github-token", "xoxb-" + "a" * 40)


def test_the_credential_helper_hands_git_the_token(tmp_path):
    """git, configured only through the environment, gets GH_TOKEN for github.com."""
    env = {**os.environ, "GH_TOKEN": "ghp_abc", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
           "GIT_CONFIG_VALUE_0": GH.CREDENTIAL_HELPER, "GIT_CONFIG_GLOBAL": "/dev/null"}
    r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, env=env, cwd=tmp_path, timeout=20)
    assert "username=x-access-token" in r.stdout and "password=ghp_abc" in r.stdout
