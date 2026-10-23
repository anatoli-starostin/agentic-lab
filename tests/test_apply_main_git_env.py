"""#97: apply-main runs git as the secretary's user through sudo, which resets the
environment; in GitHub-identity mode the service's git setup goes along as `env K=V`
arguments. These pin the command apply-main runs, not the credential exchange with GitHub
itself (that needs a live GitHub-identity lab)."""
import subprocess
from pathlib import Path

from agentic_lab import configrepo as CR
from agentic_lab import github as GH
from agentic_lab import registry

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
SETUP = "/home/lab-sec/.local/state/agentic-lab/github"


def commands(monkeypatch, euid=0):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(CR.subprocess, "run", run)
    monkeypatch.setattr(CR.os, "geteuid", lambda: euid)
    monkeypatch.setenv("LAB_GIT_USER", "lab-sec")
    return seen


def test_the_command_carries_the_services_github_setup_through_sudo(monkeypatch):
    seen = commands(monkeypatch)
    monkeypatch.setattr(CR, "GIT_ENV", {**GH.git_env(SETUP), "GIT_TERMINAL_PROMPT": "0"})
    CR.git("/clone", "fetch", "--quiet", "origin", "main")
    assert seen == [["sudo", "-u", "lab-sec", "-H", "env",
                     f"GIT_CONFIG_GLOBAL={SETUP}/gitconfig", f"LAB_GITHUB_DIR={SETUP}",
                     "GIT_TERMINAL_PROMPT=0",
                     "git", "-C", "/clone", "fetch", "--quiet", "origin", "main"]]


def test_without_a_github_setup_the_command_is_unchanged(monkeypatch):
    """Deploy-key labs (their clone's own core.sshCommand) and a console `lab apply`."""
    seen = commands(monkeypatch)
    monkeypatch.setattr(CR, "GIT_ENV", {})
    CR.git("/clone", "fetch", "--quiet", "origin", "main")
    assert seen == [["sudo", "-u", "lab-sec", "-H",
                     "git", "-C", "/clone", "fetch", "--quiet", "origin", "main"]]
    seen.clear()
    monkeypatch.setattr(CR.os, "geteuid", lambda: 1000)        # not root: no sudo, no env
    monkeypatch.setattr(CR, "GIT_ENV", {"GIT_TERMINAL_PROMPT": "0"})
    CR.git("/clone", "status")
    assert seen == [["git", "-C", "/clone", "status"]]


def test_an_agents_environment_has_the_same_keys_as_before(tmp_path):
    lab = registry.load(EXAMPLE)
    agent = next(a for a in lab.agents if lab.github_identities(a)[0])
    env = GH.environment(lab, agent, root=str(tmp_path))
    assert set(env) == {"GIT_CONFIG_GLOBAL", "GH_CONFIG_DIR", "LAB_GITHUB_DIR", "PATH"}
    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / "gitconfig")
    assert env["LAB_GITHUB_DIR"] == str(tmp_path)
    assert GH.git_env(str(tmp_path)).items() <= env.items()
