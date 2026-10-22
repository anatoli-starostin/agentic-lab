"""GitHub for agents: a manager or worker may have GitHub identities, named in lab.yaml under
`github.accounts`, each either
  - a full bot account (a GitHub user with its own email) and its fine-grained token; or
  - a GitHub App (`app_id`), appearing as `<slug>[bot]`: nothing to create but the app. Its
    tokens last an hour, so the agent's service mints them from the app's private key
    (which the agent can't read) and keeps a fresh one (github_app.py).

An agent has one identity (`github: flash-bot`), or several chosen by repository:

    github:
      acme-flash: default                     # everything else
      spikyclaudebot: [anatoli-starostin/spiky]    # these repositories (owner/* too)

The agent's service writes, where the agent can read but not write (ROOT):
  <identity>/token                               each identity's current token
  <identity>/gitconfig                           its commit name and email
  default, repos.tsv                             which identity for which repository
  gitconfig                                      the agent's git config (GIT_CONFIG_GLOBAL):
                                                 the default identity, per-repo includes,
                                                 and the credential helper below
So git picks the identity by the repository (lab-git-credential for the token, an
includeIf for the commit author). gh is a shim first on the agent's PATH (deploy/worker/
gh-shim): it reads the token at call time (an App's changes hourly), the identity mapped
to `-R owner/repo` or the default one, unless the agent set GH_TOKEN itself. gh's own
config (it writes there on many commands) is in the agent's working folder
(<workdir>/.config/gh), never next to the token (#52).

What keeps this safe is GitHub itself: tokens limited to the repositories each identity is
for, and branch protection on `main` (pull requests, reviews by human code owners, no
bypass for bots or apps): identities push branches and open pull requests, issues and
comments, but can't approve or merge.
"""
import os

SECRETS = "~/.config/agentic-lab/github"     # <name>.token / <name>.pem (service only)
ROOT = "~/.local/share/agentic-lab/github"   # readable by the agent, not writable
# the secretary's identity is its service's alone (config repo PRs): kept in its state
# folder, which its agent's sandbox can't read
SERVICE_ROOT = "~/.local/state/agentic-lab/github"
HELPER = "/opt/agentic-lab/src/deploy/worker/lab-git-credential"
SHIM = "/opt/agentic-lab/src/deploy/worker/gh-shim"      # its `gh`, first on the PATH
DOMAINS = ["github.com", "api.github.com", "codeload.github.com", "uploads.github.com",
           "objects.githubusercontent.com", "raw.githubusercontent.com"]


def _write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_token(token, login, identity_dir):
    """An identity's token, where the credential helper and the gh shim read it."""
    _write(os.path.join(identity_dir, "token"), token + "\n")
    try:                                # gh's old copy of it (before #52)
        os.remove(os.path.join(identity_dir, "gh", "hosts.yml"))
    except OSError:
        pass


def prepare(lab, agent_id, root=ROOT, secrets=SECRETS, helper=HELPER, shim=SHIM):
    """Write the agent's git setup (and bot accounts' tokens). Returns its environment:
    empty for an agent without GitHub."""
    default, mapped = lab.github_identities(agent_id)
    if not default:
        return {}
    root, secrets = os.path.expanduser(root), os.path.expanduser(secrets)
    for name in lab.github_names(agent_id):
        d = os.path.join(root, name)
        _write(os.path.join(d, "gitconfig"), f"[user]\n\tname = {lab.github_login(name)}\n"
                                             f"\temail = {lab.github_email(name)}\n")
        if not lab.github_is_app(name):              # an app's token comes from keep_token
            try:
                with open(os.path.join(secrets, f"{name}.token")) as f:
                    write_token(f.read().strip(), lab.github_login(name), d)
            except OSError:
                pass                                 # not handed over yet
    _write(os.path.join(root, "default"), default + "\n")
    _write(os.path.join(root, "repos.tsv"), "".join(f"{p}\t{n}\n" for p, n in mapped))
    includes = "".join(
        f'[includeIf "hasconfig:remote.*.url:https://github.com/{p}{suffix}"]\n'
        f"\tpath = {os.path.join(root, n, 'gitconfig')}\n"
        for p, n in mapped for suffix in ("", ".git"))
    _write(os.path.join(root, "gitconfig"),
           f"[include]\n\tpath = {os.path.join(root, default, 'gitconfig')}\n"
           f'[credential "https://github.com"]\n\thelper = {helper}\n\tuseHttpPath = true\n'
           + includes)
    # gh writes its config (migrations, state): a folder of its own the sandbox lets it
    # write, with no secrets in it
    gh_config = os.path.join(os.path.expanduser(
        (lab.agents.get(agent_id) or {}).get("workdir") or "~/work"), ".config", "gh")
    try:
        os.makedirs(gh_config, mode=0o700, exist_ok=True)
    except OSError:                     # gh makes it itself; never lose git's setup over it
        pass
    return {"GIT_CONFIG_GLOBAL": os.path.join(root, "gitconfig"),
            "GH_CONFIG_DIR": gh_config,
            "LAB_GITHUB_DIR": root,
            "PATH": shim + ":" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}


def env_for(lab, agent_id, root=ROOT, secrets=SECRETS):
    """The agent's environment for GitHub (writing its git setup first)."""
    try:
        return prepare(lab, agent_id, root=root, secrets=secrets)
    except OSError:
        return {}


def start(lab, agent_id, stop, root=ROOT, secrets=SECRETS):
    """The tasks that keep the agent's GitHub App identities' tokens fresh (beside its
    service)."""
    import asyncio

    from .github_app import keep_token
    tasks = []
    for name in lab.github_names(agent_id):
        if lab.github_is_app(name):
            acc = lab.github_accounts[name]
            tasks.append(asyncio.create_task(keep_token(
                acc["app_id"], lab.github_login(name), stop,
                installation_id=acc.get("installation_id"),
                key_path=os.path.join(os.path.expanduser(secrets), f"{name}.pem"),
                token_dir=os.path.join(os.path.expanduser(root), name))))
    return tasks


def instructions(lab, agent_id, root=ROOT):
    """A paragraph for the agent's system prompt."""
    default, mapped = lab.github_identities(agent_id)
    if not default:
        return ""

    def who(name):
        kind = "the GitHub App" if lab.github_is_app(name) else "the bot account"
        return f"{kind} {lab.github_login(name)}"
    text = (f"\nGitHub: you act as {who(default)} (git and gh are set up for it; commits "
            f"carry its name and email).")
    if mapped:
        root = os.path.expanduser(root)
        rows = "; ".join(f"{p}: {who(n)}" for p, n in mapped)
        text += (f" For some repositories you act as another identity, picked by the "
                 f"repository: {rows}. git does this by itself (use https remotes); so does "
                 f"gh when you name the repository: gh ... -R owner/repo (without -R it acts "
                 f"as {lab.github_login(default)}). {root}/repos.tsv lists these mappings.")
    return text + (" Push feature branches and open pull requests; issues and comments are "
                   "fine. Never push to main, never approve or merge a pull request: humans "
                   "review and merge. Never print or share a token.\n")
