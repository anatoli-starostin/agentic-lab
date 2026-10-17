"""GitHub for agents: a manager or worker may have one GitHub identity, a bot account (a
GitHub machine user) named in lab.yaml (`github: <bot>` on the agent, the bot under
`github.accounts`).

Its token is in the agent's environment for all its work: git and `gh` act as that bot
(GH_TOKEN, a credential helper, the bot's commit name and email), and github.com is
reachable from its sandbox.

What keeps this safe is GitHub itself: the bot's fine-grained token covers only the lab's
private repos (contents, pull requests, issues), and branch protection on `main` (pull
requests, reviews by human code owners, no bypass for bots) means a bot can push branches,
open pull requests, issues and comments, but its approval never counts and it can't merge.
"""
import os

CONFIG = "~/.config/agentic-lab/github.token"     # installed by lab apply (service only)
DOMAINS = ["github.com", "api.github.com", "codeload.github.com", "uploads.github.com",
           "objects.githubusercontent.com", "raw.githubusercontent.com"]
# git asks this for credentials: the bot's token
CREDENTIAL_HELPER = ('!f() { test "$1" = get && echo username=x-access-token && '
                     'echo "password=$GH_TOKEN"; }; f')


def env_for(lab, agent_id, path=CONFIG):
    """The agent's environment for GitHub: its bot's token (if it's there yet) and git set
    up to use it. Empty for an agent without a GitHub identity."""
    if not (lab.agents.get(agent_id) or {}).get("github"):
        return {}
    try:
        with open(os.path.expanduser(path)) as f:
            token = f.read().strip()
    except OSError:
        return {}
    return {"GH_TOKEN": token, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_0": CREDENTIAL_HELPER}


def instructions(lab, agent_id):
    """A paragraph for the agent's system prompt."""
    bot = (lab.agents.get(agent_id) or {}).get("github")
    if not bot:
        return ""
    return (f"\nGitHub: you act as the bot account {bot}. git and gh are set up for it (its "
            f"token is in $GH_TOKEN; commits carry its name and email "
            f"{lab.github_email(bot)}). Push feature branches and open pull requests; issues "
            "and comments are fine. Never push to main, never approve or merge a pull "
            "request: humans review and merge. Never print or share the token.\n")
