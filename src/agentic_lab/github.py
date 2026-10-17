"""GitHub for agents: each acts as a bot account (a GitHub machine user) from lab.yaml.

Every manager and worker with GitHub has a default identity (`github:` on the agent). For
each piece of work (a worker's task, a manager's conversation) the agent gets, in its
environment, the tokens of the bots it may act as for that work: its default, the shared
ones, and the requester's own bot (never another member's). It then uses git and `gh` like
a developer would; github.com is reachable from its sandbox.

What keeps this safe is GitHub itself: the bots' fine-grained tokens cover only the lab's
repos (contents, pull requests, issues), and branch protection on `main` (pull requests,
reviews by human code owners, no bypass for bots) means a bot can push branches, open pull
requests, issues and comments, but its approval never counts and it can't merge.
"""
import os
import re

CONFIG = "~/.config/agentic-lab/github"       # <login>.token, installed by lab apply
DOMAINS = ["github.com", "api.github.com", "codeload.github.com", "uploads.github.com",
           "objects.githubusercontent.com", "raw.githubusercontent.com"]
# git asks this for credentials: the token of the identity in use ($GH_TOKEN)
CREDENTIAL_HELPER = ('!f() { test "$1" = get && echo username=x-access-token && '
                     'echo "password=$GH_TOKEN"; }; f')


def var(login):
    """The environment variable holding a bot's token: GITHUB_TOKEN_<LOGIN>."""
    return "GITHUB_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", login.upper())


def allowed(lab, agent_id, requester):
    """The bots the agent may act as for work asked by `requester`."""
    return [login for login in lab.github_usable(agent_id)
            if lab.github_identity(agent_id, requester, login)[0] == login]


def env_for(lab, agent_id, requester, config=CONFIG):
    """The environment for one piece of work: GH_TOKEN (the default identity's token, which
    git and gh use), GITHUB_TOKEN_<LOGIN> for each other bot it may use, and git set up to
    authenticate with GH_TOKEN. Missing tokens are left out."""
    default = (lab.agents.get(agent_id) or {}).get("github")
    if not default:
        return {}
    env = {}
    for login in allowed(lab, agent_id, requester):
        try:
            with open(os.path.join(os.path.expanduser(config), f"{login}.token")) as f:
                env[var(login)] = f.read().strip()
        except OSError:
            continue
    if var(default) in env:
        env["GH_TOKEN"] = env[var(default)]
    env.update({"GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_0": CREDENTIAL_HELPER})
    return env


def instructions(lab, agent_id, requester):
    """A paragraph for the agent's system prompt."""
    default = (lab.agents.get(agent_id) or {}).get("github")
    if not default:
        return ""
    others = [x for x in allowed(lab, agent_id, requester) if x != default]
    alt = (" For this work you may also act as " + ", ".join(
        f"{x} (token in ${var(x)}, email {lab.github_email(x)})" for x in others) +
        f": only when the work explicitly asks for it, e.g. GH_TOKEN=${var(others[0])} "
        f"git -c user.name={others[0]} -c user.email={lab.github_email(others[0])} ...") \
        if others else ""
    return (f"\nGitHub: you act as the bot account {default} (git and gh are already set "
            f"up: its token is in $GH_TOKEN, commits carry its name and email "
            f"{lab.github_email(default)}).{alt} Say which identity you act as. Push feature "
            "branches and open pull requests; issues and comments are fine. Never push to "
            "main, never approve or merge a pull request: humans review and merge. Never "
            "print or share a token.\n")
