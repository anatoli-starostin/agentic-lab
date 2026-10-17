"""GitHub for agents: bot identities from lab.yaml; each piece of work gets the tokens of the
bots it may act as (its default, shared ones, the requester's own), never another member's."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from agentic_lab import registry
from agentic_lab import github as GH
from agentic_lab.apply import host_plan
from agentic_lab.secrets import intake as I

SINGLE = Path(__file__).parent.parent / "examples" / "single-vm" / "lab.yaml"


def lab_with_github(tmp_path, mutate=None):
    d = yaml.safe_load(SINGLE.read_text())
    alice = next(iter(d["humans"]))
    d["humans"]["bob"] = {"name": "Bob"}
    d["github"] = {"accounts": {"claudebot": {},
                                f"{alice}-claudebot": {"member": alice},
                                "bob-claudebot": {"member": "bob"}}}
    for aid, a in d["agents"].items():
        if a["role"] in ("manager", "worker"):
            a["github"] = "claudebot"
    if mutate:
        mutate(d, alice)
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    return registry.load(p), alice


def errors(lab):
    return [str(i) for i in registry.check(lab) if i.level == "error"]


def test_schema_and_identity_rules(tmp_path):
    lab, alice = lab_with_github(tmp_path)
    assert errors(lab) == []
    worker = next(a for a, x in lab.agents.items() if x["role"] == "worker")
    assert lab.github_identity(worker, alice) == ("claudebot", None)
    assert lab.github_identity(worker, alice, f"{alice}-claudebot")[0] == f"{alice}-claudebot"
    acc, why = lab.github_identity(worker, alice, "bob-claudebot")
    assert acc is None and "bob's own bot" in why
    assert lab.github_identity(worker, alice, "nobody")[0] is None
    env = lab.agent_settings(worker)["env"]
    assert env["GIT_AUTHOR_NAME"] == "claudebot"
    assert env["GIT_COMMITTER_EMAIL"] == "claudebot@users.noreply.github.com"

    def bad(d, alice):
        d["github"]["accounts"]["ghost-bot"] = {"member": "ghost"}
        next(a for a in d["agents"].values() if a["role"] == "worker")["github"] = "bob-claudebot"
        next(a for a in d["agents"].values() if a["role"] == "secretary")["github"] = "claudebot"
    lab2, _ = lab_with_github(tmp_path, bad)
    errs = " ".join(errors(lab2))
    assert "member 'ghost'" in errs and "bob's own bot" in errs and "managers and workers only" in errs


def test_tokens_go_to_the_services_that_may_use_them(tmp_path):
    lab, alice = lab_with_github(tmp_path)
    worker = next(a for a, x in lab.agents.items() if x["role"] == "worker")
    host = lab.agents[worker]["host"]
    tokens = {"service:github-claudebot": "ghp_" + "x" * 36}

    class Tok:
        def get(self, k):
            return "t"
    def intake(owner, name):
        return tokens.get(owner) if name == "github-token" else (
            None if name == "nebius-sa" else "secret")
    plan = host_plan(lab, host, Tok(), intake)
    user = lab.unix_user(worker)
    files = plan["files"]
    assert files[f"/home/{user}/.config/agentic-lab/github/claudebot.token"][2].startswith("ghp_")
    cfg = yaml.safe_load(files[f"/home/{user}/.config/agentic-lab/github.yaml"][2])
    assert cfg["default"] == "claudebot" and list(cfg["accounts"]) == ["claudebot"]
    # a missing bot token is reported but never holds the host back
    assert any(f"{alice}-claudebot" in m for m in plan["later"])
    assert not any("GitHub" in m for m in plan["missing"])
    # a shared worker may run bob's tasks: his bot is installed too (each TASK still gets
    # only its requester's); a dedicated one never gets another member's bot
    shared = lab.agents[worker].get("access", "dedicated") == "shared"
    assert any("bob-claudebot" in m for m in plan["later"]) == shared


def test_intake_takes_github_tokens():
    assert I.check_value("github-token", "github_pat_" + "a" * 40).startswith("github_pat_")
    with pytest.raises(I.IntakeError, match="GitHub token"):
        I.check_value("github-token", "xoxb-" + "a" * 40)


# ── what the agent gets for a piece of work ──────────────────────────────────

def test_env_gives_only_the_bots_this_work_may_use(tmp_path):
    lab, alice = lab_with_github(tmp_path)
    worker = next(a for a, x in lab.agents.items() if x["role"] == "worker")
    cfg = tmp_path / "tokens"
    cfg.mkdir()
    for login in ("claudebot", f"{alice}-claudebot", "bob-claudebot"):
        (cfg / f"{login}.token").write_text(f"ghp_{login}\n")
    env = GH.env_for(lab, worker, alice, config=str(cfg))
    assert env["GH_TOKEN"] == "ghp_claudebot"                     # the default identity
    assert env[GH.var(f"{alice}-claudebot")] == f"ghp_{alice}-claudebot"   # requester's own
    assert GH.var("bob-claudebot") not in env                     # never another member's
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    bob_env = GH.env_for(lab, worker, "bob", config=str(cfg))
    assert GH.var(f"{alice}-claudebot") not in bob_env
    prompt = GH.instructions(lab, worker, alice)
    assert "claudebot" in prompt and "never approve or merge" in prompt.lower()
    assert "ghp_" not in prompt                                   # tokens never in the prompt
    net = lab.agent_settings(worker)["sandbox"]["network"]["allowedDomains"]
    assert "github.com" in net and "api.github.com" in net
    sec = next(a for a, x in lab.agents.items() if x["role"] == "secretary")
    assert "github.com" not in lab.agent_settings(sec)["sandbox"]["network"]["allowedDomains"]


def test_the_credential_helper_hands_git_the_token(tmp_path):
    """git, configured only through the environment, gets GH_TOKEN for github.com."""
    env = {**os.environ, "GH_TOKEN": "ghp_abc", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
           "GIT_CONFIG_VALUE_0": GH.CREDENTIAL_HELPER, "GIT_CONFIG_GLOBAL": "/dev/null"}
    r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, env=env, cwd=tmp_path, timeout=20)
    assert "username=x-access-token" in r.stdout and "password=ghp_abc" in r.stdout
