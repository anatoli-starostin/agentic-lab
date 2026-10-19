"""GitHub for agents: a manager or worker may have one GitHub identity, named in lab.yaml
(`github: <name>` on the agent, the identity under `github.accounts`). It is either
  - a bot account (a GitHub machine user) with a fine-grained token, in the agent's
    environment for all its work (GH_TOKEN); or
  - a GitHub App (`app_id`), appearing as `<slug>[bot]`: no account to create. Its tokens
    last an hour, so the agent's service mints them from the app's private key (which the
    agent can't read) and keeps a fresh one where git and gh read it (github_app.py).
Either way git and `gh` act as that identity (a credential helper, the commit name and
email), and github.com is reachable from the agent's sandbox.

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


def env_for(lab, agent_id, path=CONFIG, token_dir=None):
    """The agent's environment for GitHub: its token (if it's there yet) and git and gh set
    up to use it. Empty for an agent without a GitHub identity."""
    name = (lab.agents.get(agent_id) or {}).get("github")
    if not name:
        return {}
    if lab.github_is_app(name):                     # a fresh token kept in a file
        from .github_app import TOKEN_DIR
        d = os.path.expanduser(token_dir or TOKEN_DIR)
        return {"GH_CONFIG_DIR": os.path.join(d, "gh"), "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_0": ('!f() { test "$1" = get && echo username=x-access-token'
                                       f' && echo "password=$(cat {d}/token)"; }}; f')}
    try:
        with open(os.path.expanduser(path)) as f:
            token = f.read().strip()
    except OSError:
        return {}
    return {"GH_TOKEN": token, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_0": CREDENTIAL_HELPER}


def start(lab, agent_id, stop):
    """For an agent whose identity is a GitHub App: the task that keeps its token fresh
    (run beside the agent's service), else None."""
    name = (lab.agents.get(agent_id) or {}).get("github")
    if not name or not lab.github_is_app(name):
        return None
    import asyncio

    from .github_app import keep_token
    acc = lab.github_accounts[name]
    return asyncio.create_task(keep_token(acc["app_id"], lab.github_login(name), stop,
                                          installation_id=acc.get("installation_id")))


def instructions(lab, agent_id):
    """A paragraph for the agent's system prompt."""
    name = (lab.agents.get(agent_id) or {}).get("github")
    if not name:
        return ""
    bot = lab.github_login(name)
    kind = "the GitHub App" if lab.github_is_app(name) else "the bot account"
    return (f"\nGitHub: you act as {kind} {bot}. git and gh are set up for it (commits carry "
            f"its name and email {lab.github_email(name)}). Push feature branches and open pull requests; issues "
            "and comments are fine. Never push to main, never approve or merge a pull "
            "request: humans review and merge. Never print or share the token.\n")
